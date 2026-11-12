"""Request bodies that refuse what they do not understand.

Pydantic ignores fields it has never heard of. On a response that is the right
default: a client sending a newer field against an older server keeps working.
On a *write* it is data loss with a 201 on top.

Found on a real install by sending `userId` instead of `user_id`, which is the
first thing anyone arriving from JavaScript types:

    POST /v1/memories  {"content": "Anna lives in Turin.", "userId": "anna"}
    201 Created        {"user_id": null, ...}

The memory is stored against nobody. Every read scoped to `anna` comes back
empty, the developer's agent appears to have no memory at all, and nothing
anywhere said a word. `end_user_id` does the same, and so does a field invented
on the spot.

So every body a caller sends inherits from `Body` and gets a 422 naming the
field. The cost is stated rather than hidden: a client sending a field this
version does not know is now refused instead of tolerated, which for a
self-hosted product means the SDK must not run ahead of the install. That is
the right way round. A refusal is a message; a silent drop is a mystery.
"""
import functools
import itertools
import math
import types
from datetime import date
from typing import Annotated, Any, Optional, Union, get_args, get_origin

from fastapi import HTTPException, status
from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator
from pydantic_core import PydanticCustomError

from ..services.ids import namespace_id_problem
from ..services.limits import first_in, metadata_filter_problem, moment_problem, nul_problem


def _can_hold_a_moment(annotation: Any) -> bool:
    """Whether a value of this type can be or hold a date (a date, a
    datetime, a model that may have one, a list or a union of them). A
    `dict` or `Any` cannot: the parser gives them only what JSON spells."""
    if get_origin(annotation) is None and isinstance(annotation, type):
        return issubclass(annotation, (date, BaseModel))
    return any(_can_hold_a_moment(a) for a in get_args(annotation))


@functools.lru_cache(maxsize=None)
def _fields_with_moments(model: type) -> tuple:
    """The fields of `model` whose type can hold a date."""
    return tuple(name for name, f in model.model_fields.items() if _can_hold_a_moment(f.annotation))


def _first_moment_out_of_range(field: str, value: Any) -> Optional[str]:
    """The first date or datetime of a body that cannot be stored and read
    back (limits.moment_problem), wherever it is, or None: walked as the NUL
    rule walks it (limits.first_in), with no recursion, into the fields that
    can hold one only. It walked every value parsed, the contents of a
    `dict` field too, which never holds a date: 2.6 million values of one
    batch item's metadata held the event loop some 4.6 s (the review of
    138b8cb)."""
    return first_in(field, value, lambda v, where: moment_problem(where(), v)
                    if isinstance(v, date) else None, fields=_fields_with_moments)


class Checked(BaseModel):
    """A request body read by the rule of every door (services/limits.py)
    once it has parsed: a NUL byte anywhere in it, or a date it holds that
    cannot be stored and read back, is a 422 that names the field, never the
    driver's 500 (audit of 20 Oct 2026: the fact names, the declared names and
    the /admin labels bounded only their length).

    The refusal is the routes' own (`detail` the sentence, as every door's
    check answers it), raised from here as an HTTPException, which Pydantic
    lets through and FastAPI answers like any refusal. Every body of every
    route is one of these (tests/test_every_input_by_one_rule.py walks
    them). A body that refuses unknown fields is a `Body`; the few that
    tolerate them on purpose (the Cloud's sign-up and pages) are Checked
    only. A model nested in a body is a `Part`: the body walks it, so the
    sentence keeps its place (`memories[3].run_id`).

    The strings are read as they were sent, before any check of a field's
    own: a value no column can store (a NUL byte, a lone UTF-16 surrogate:
    limits.nul_problem) is the rule's 422 in any field, whatever its type
    and whatever else the body lacks. Read after Pydantic, a lone surrogate
    in a field Pydantic keeps as it came (a NamespaceId, a plain str, a
    list of them, a dict) reached the driver (500) or the json column of a
    batch job, and one in a constrained field answered Pydantic's refusal
    instead of the rule's (the audit before 1.0.0). Only what the body declares is read (`declared`):
    an undeclared field is refused by its name or ignored, and its value
    reaches nothing. The moments are read once parsed."""

    @model_validator(mode="before")
    @classmethod
    def _strings_by_the_rule_of_every_door(cls, data):
        problem = nul_problem("", declared(cls, data))
        if problem:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
        return data

    @model_validator(mode="after")
    def _moments_by_the_rule_of_every_door(self):
        problem = _first_moment_out_of_range("", self)
        if problem:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
        return self


def _model_in(annotation: Any) -> Optional[type]:
    """The model a field's value is read as, when it is one: the field's
    type, or the type of its items (a list of Parts), optional or not."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in (Union, types.UnionType, list, tuple, set, frozenset):
        return next((m for m in map(_model_in, get_args(annotation)) if m is not None), None)
    return None


@functools.lru_cache(maxsize=None)
def _read_as(model: type) -> dict:
    """{each name `model` takes, its fields' and their aliases': the model
    the value is read as, or None}."""
    out = {}
    for name, f in model.model_fields.items():
        out[name] = _model_in(f.annotation)
        if f.alias:
            out[f.alias] = out[name]
    return out


def declared(model: type, data: Any) -> Any:
    """What of `data` the rule of every door reads before `model` parses it
    (services/limits.nul_problem): the value of each field the model
    declares, by its name or its alias, read as its own model's when it is
    one (a Part, or a list of them), and every name sent, since a refusal of
    an unknown field names it. The value of a field the model does not
    declare is read by nothing: the model refuses the field by its name
    (a Body, a Part) or ignores it (a body that tolerates unknown fields, a
    form of the MCP SDK). The rule walked the body whole, and one unknown
    field of 2.6 million values held the event loop some 12 s before the
    body's refusal (the review of the audit before 1.0.0), the read the
    MCP's arguments lost in the audit of 27 Oct 2026
    (mcp/server._arguments_checked)."""
    if not isinstance(data, dict):
        return data
    fields = _read_as(model)
    out = {}
    for name, value in data.items():
        if name not in fields:
            value = None
        elif fields[name] is not None and isinstance(value, list):
            value = [declared(fields[name], v) if type(v) is dict else v for v in value]
        elif fields[name] is not None and isinstance(value, dict):
            value = declared(fields[name], value)
        out[name] = value
    return out


@functools.lru_cache(maxsize=None)
def _names(model: type) -> frozenset:
    """The names a body's model takes: its fields and their aliases."""
    return frozenset(_read_as(model))


def _first_unknown_only(model: type, data: Any) -> Any:
    """`data` with its unknown fields after the first set aside: a refused
    body is one `extra_forbidden` error per model, naming the first one
    (and "did you mean", main._suggest_the_right_field), never one per
    unknown key. Pydantic keeps an error for each, and a write key's batch
    under every count of _BodyCap spelled 1.38 million of them, each some
    1.4 KB of heap, then copied and rendered whole by the 422 (audit of 27
    Oct 2026)."""
    if not isinstance(data, dict):
        return data
    names = _names(model)
    unknown = list(itertools.islice((k for k in data if k not in names), 2))
    if len(unknown) < 2:
        return data
    return {k: v for k, v in data.items() if k in names or k == unknown[0]}


class Body(Checked):
    """A request body. Unknown fields are an error, not a shrug: one error,
    naming the first (`_first_unknown_only`)."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _one_unknown_field(cls, data):
        return _first_unknown_only(cls, data)


class Part(BaseModel):
    """A piece of a request body (an item of a batch): unknown fields are an
    error, as in a Body, and the body that holds it reads its strings."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _one_unknown_field(cls, data):
        return _first_unknown_only(cls, data)


def _namespace_id(value: str) -> str:
    # PydanticCustomError, not ValueError: its error carries no exception
    # object in `ctx`, so the 422 handler in main.py can serialise it.
    problem = namespace_id_problem(value)
    if problem:
        raise PydanticCustomError("invalid_namespace_id", problem)
    return value


# A `user_id` or `agent_id` in a write body: the rule is
# services.ids.NAMESPACE_ID_RULE, the same one the MCP write applies.
NamespaceId = Annotated[str, AfterValidator(_namespace_id)]


def _metadata_filter(value: Optional[dict]) -> Optional[dict]:
    # A search's metadata filter compares scalars: strings, finite numbers,
    # booleans and null (memory_engine.metadata_matches). An object or an
    # array matched nothing, and a 200 with no results said "no such memory"
    # instead of "this filter cannot match" (the hosted product's code audit
    # of 6 Oct 2026): refused, naming the key. So is NaN or an infinity, which
    # Python's JSON parser accepts and no stored JSON can hold.
    for key, item in (value or {}).items():
        if isinstance(item, (dict, list)) or (isinstance(item, float) and not math.isfinite(item)):
            raise PydanticCustomError(
                "metadata_filter",
                "the value of '{key}' is not a string, a number, a boolean or null: "
                "a metadata filter compares those",
                {"key": str(key)},
            )
    # How many keys and how many bytes (services/limits.metadata_filter_problem,
    # audit of 18 Oct 2026): any number was taken, and each is a condition
    # the database plans.
    problem = metadata_filter_problem(value)
    if problem:
        raise PydanticCustomError("metadata_filter", problem)
    return value


# `metadata` of a search body.
MetadataFilter = Annotated[Optional[dict], AfterValidator(_metadata_filter)]
