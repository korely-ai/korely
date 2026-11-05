"""What the engine writes in its logs about the end users' content: never the
text, only its shape (memory_core 3.0.0; code audit of 6 Oct).

A log line leaves the store. It goes to files and collectors kept by other
rules than the memories, read by people who never read a memory, and no
forget reaches it: "dropped 'Maria' 'has' 'type 2 diabetes'" is a copy of an
end user's health data that `DELETE /v1/memories` does not erase. The OWASP
Logging Cheat Sheet ("Data to exclude"): "The following should usually not be
recorded directly in the logs, but instead should be removed, masked,
sanitized, hashed, or encrypted: [...] Sensitive personal data and some forms
of personally identifiable information (PII) e.g. health, government
identifiers, vulnerable people", and for the rest "Consider using personal
data de-identification techniques such as deletion, scrambling or
pseudonymization of direct and indirect identifiers where the individual's
identity is not required". Debugging the engine needs where and what kind,
not the words: the class of an error and its codes, the shape of a model's
reply, lengths, ids, counts.

Two kinds of text reach a log without anyone writing them there:
  * a model's reply. The extractor's and the judge's carry the memory's words
    by construction (subjects, objects, the evidence copied from the text);
    the vocabulary's may echo the example object it was shown;
  * the message of a database error. SQLAlchemy formats the statement's
    parameters into it ("[parameters: ...]", the row being written) unless
    the engine was created with `hide_parameters=True`, and PostgreSQL's
    DETAIL of a unique violation repeats the key's values ("Key (...)=(...)
    already exists").
So an exception is described by `error` (classes, SQLSTATE, the names of the
constraint, table and column, an HTTP status, the length of the message) and
its stack by `stack` (file, line and function of each frame, without the
messages and without the lines of code), never with `exc_info=True`, whose
formatter prints the message. The names of the relations stay in the logs:
they are the vocabulary's words, not an end user's values
(`store.examples_after_the_erasure`).
"""
from __future__ import annotations

import re
import traceback
from typing import Any, Iterable, List, Optional

# How many links of a cause chain are described (an error, the error it was
# raised from, the driver's error under SQLAlchemy's...).
DEPTH = 4

_UNREAD = object()

# Names a database gives its own objects (constraints, tables, columns): never
# a value. Anything else in those fields is left out.
_SCHEMA_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}")
_SQLSTATE = re.compile(r"[0-9A-Z]{5}")
_SQLITE_NAME = re.compile(r"SQLITE_[A-Z_]{2,60}")
_HTTP_STATUS = re.compile(r"[A-Z][A-Z_]{2,40}")

# A value quoted inside a reason: `repr` of a string, either quote.
_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def length(s: Any) -> str:
    """A text by its length: "<41 characters>" ("<none>" for None)."""
    if s is None:
        return "<none>"
    return f"<{len(str(s))} characters>"


def type_name(v: Any) -> str:
    """A value by its type and size: "list of 3", "string of 12 characters"."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return f"string of {len(v)} characters"
    if isinstance(v, (list, tuple)):
        return f"list of {len(v)}"
    if isinstance(v, dict):
        return f"object of {len(v)} keys"
    return type(v).__name__


def answer(raw: Any, parsed: Any = _UNREAD, key: Optional[str] = None) -> str:
    """A model's reply by its shape, never its words: its length, whether it
    ends with a closing brace or bracket (a reply cut at the token ceiling
    does not) and, when the caller read it (`parsed`: the value read, None
    when it was not JSON), the type of the value and of `key` in it."""
    s = "" if raw is None else str(raw)
    parts = [f"{len(s)} characters"]
    last = s.rstrip()[-1:]
    if last:
        parts.append("ends with a closing brace" if last in ("}", "]") else "no closing brace at the end")
    if parsed is not _UNREAD:
        if parsed is None:
            parts.append("not JSON")
        else:
            parts.append(f"JSON {type_name(parsed)}")
            if key is not None and isinstance(parsed, dict):
                parts.append(f"{key!r}: {type_name(parsed[key])}" if key in parsed else f"no {key!r}")
    return "a reply of " + ", ".join(parts)


def fields(item: Any, allowed: Iterable[str]) -> str:
    """An object by the names of its fields that a schema allows (`allowed`),
    the others only counted: a model can write anything as a key."""
    if not isinstance(item, dict):
        return type_name(item)
    known = set(allowed)
    seen = sorted(k for k in item if isinstance(k, str) and k in known)
    others = sum(1 for k in item if not (isinstance(k, str) and k in known))
    text = "fields " + (", ".join(seen) if seen else "none of the schema's")
    return text + (f" (+{others} not in the schema)" if others else "")


def without_values(reason: Any) -> str:
    """A reason with the values it quotes replaced by <value>: the reasons of
    `relative_time` name the field and repeat the model's value with `!r`,
    and the model's value may be the memory's words."""
    return _QUOTED.sub("<value>", str(reason))


def _chain(e: BaseException) -> List[BaseException]:
    """`e`, then what it came from: the driver's error under SQLAlchemy's
    (`orig`), else the cause, else the context, at most DEPTH."""
    seen: List[BaseException] = []
    x: Optional[BaseException] = e
    while x is not None and len(seen) < DEPTH and not any(x is v for v in seen):
        seen.append(x)
        after = getattr(x, "orig", None)
        if not isinstance(after, BaseException) or any(after is v for v in seen):
            after = x.__cause__ if x.__cause__ is not None else (
                None if x.__suppress_context__ else x.__context__)
        x = after
    return seen


def _class_name(x: BaseException) -> str:
    t = type(x)
    module = getattr(t, "__module__", "") or ""
    return t.__qualname__ if module == "builtins" else f"{module}.{t.__qualname__}"


def _codes(x: BaseException) -> List[str]:
    """What identifies an error without its message: SQLSTATE (psycopg2
    `pgcode`, psycopg 3 `sqlstate`), the names in PostgreSQL's diagnostics,
    SQLite's error name, an HTTP status (`status_code`, or `code` and
    `status` as the Google client names them)."""
    out: List[str] = []
    status = getattr(x, "pgcode", None) or getattr(x, "sqlstate", None)
    if isinstance(status, str) and _SQLSTATE.fullmatch(status):
        out.append(f"SQLSTATE {status}")
    name = getattr(x, "sqlite_errorname", None)
    if isinstance(name, str) and _SQLITE_NAME.fullmatch(name):
        out.append(name)
    diag = getattr(x, "diag", None)
    if diag is not None:
        for field_name, tag in (("constraint_name", "constraint"), ("table_name", "table"),
                                 ("column_name", "column")):
            v = getattr(diag, field_name, None)
            if isinstance(v, str) and _SCHEMA_NAME.fullmatch(v):
                out.append(f"{tag} {v}")
    for field_name in ("status_code", "code"):
        v = getattr(x, field_name, None)
        if isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 599:
            out.append(f"HTTP {v}")
            break
    v = getattr(x, "status", None)
    if isinstance(v, str) and _HTTP_STATUS.fullmatch(v):
        out.append(v)
    return out


def _message_length(x: BaseException) -> Optional[int]:
    try:
        return len(str(x))
    except Exception:  # noqa: BLE001 - a __str__ that fails is not the log's problem
        return None


def _one(x: BaseException) -> str:
    codes = _codes(x)
    n = _message_length(x)
    inside = codes + ([f"message of {n} characters withheld"] if n else [])
    return _class_name(x) + (f" ({', '.join(inside)})" if inside else "")


def error(e: BaseException) -> str:
    """An exception on one line, without its message: its class and those of
    what it came from, with their codes and the length of each message.
    "sqlalchemy.exc.IntegrityError (message of 512 characters withheld) <-
    psycopg2.errors.UniqueViolation (SQLSTATE 23505, constraint
    uq_agent_facts_one_open_per_triple, table agent_facts, ...)"."""
    return " <- ".join(_one(x) for x in _chain(e))


def stack(e: BaseException) -> str:
    """The stack of `e` and of what it came from, outermost first: file, line
    and function of each frame (`traceback.extract_tb`), no message and no
    line of code."""
    rows = ["stack (messages withheld):"]
    for x in _chain(e):
        rows.append(f"  {_one(x)}")
        for f in (traceback.extract_tb(x.__traceback__) if x.__traceback__ is not None else []):
            rows.append(f'    File "{f.filename}", line {f.lineno}, in {f.name}')
    return "\n".join(rows)
