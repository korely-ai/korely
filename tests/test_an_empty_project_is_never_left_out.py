"""An empty `project_id` is refused wherever a request may give one (7 Nov 2026).

A project left out has a meaning of its own on each route: every project of
the account on a read of the screen, the Default project for a new key, the
key's own project for an alias. An empty one was read as left out, so a
value lost on the way widened a read or moved what it named; the reads of
the screen refused it since 6 Nov 2026 (api/admin.ProjectId), and the
bodies did not: POST /admin/keys with `"project_id": ""` made a key of the
Default project, and a move with one took the key out of its project. One
declaration, api/admin.GivenProjectId, holds both.

The routes are found from the app as it is built in each mode, not from a
list: every query parameter a request spells `project_id` (its alias, not
its name in the code), every part of a body that it spells so (a form's
field, a body of its own, `Body(embed=True)`), in the route's own parameters
and in those of every dependency at any depth, and every field of a body's
models that it spells so. Each is given an empty value through the validation the request goes
through (FastAPI's own for a query, the model's for a body). The Cloud's
mode adds the routes its own package mounts (hosted.install, and the parts
of the product it carries).
"""
from __future__ import annotations

import typing

import pytest
from fastapi.dependencies.utils import request_params_to_args
from pydantic import BaseModel, ValidationError
from starlette.datastructures import QueryParams

NAME = "project_id"


def _models(annotation) -> list:
    """The pydantic models an annotation names, through Optional, lists and
    other generics."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [m for arg in typing.get_args(annotation) for m in _models(arg)]


def _in_bodies(model, seen) -> list:
    """(model, the field's alias) of every field of `model`, and of the models
    its fields name, that a body spells `project_id`."""
    if model in seen:
        return []
    seen.add(model)
    found = []
    for name, info in model.model_fields.items():
        if (info.validation_alias or info.alias or name) == NAME:
            found.append((model, info.alias or name))
        for inner in _models(info.annotation):
            found += _in_bodies(inner, seen)
    return found


def _taking_the_project(dependant, seen_models=None):
    """('query', field), ('part', field) and ('body', (model, alias)) for
    every place a request to this dependant may give a project."""
    seen_models = set() if seen_models is None else seen_models
    for field in dependant.query_params:
        if field.alias == NAME:
            yield "query", field
    for field in dependant.body_params:
        if field.alias == NAME:
            yield "part", field
        for model in _models(field.field_info.annotation):
            for found in _in_bodies(model, seen_models):
                yield "body", found
    for sub in dependant.dependencies:
        yield from _taking_the_project(sub, seen_models)


def _empty_is_refused(kind, what) -> bool:
    if kind == "query":
        _values, errors = request_params_to_args([what], QueryParams(f"{NAME}="))
        return any(tuple(e.get("loc", ())) == ("query", NAME) for e in errors)
    if kind == "part":
        _value, errors = what.validate("", {}, loc=("body", NAME))
        return bool(errors)
    model, alias = what
    try:
        model.model_validate({alias: ""})
    except ValidationError as e:
        return any(err["loc"] == (alias,) for err in e.errors())
    return False


def _app(mode: str, monkeypatch):
    from korely_agent.config import get_settings
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    if mode == "hosted":
        monkeypatch.setenv("KORELY_MODE", "hosted")
        monkeypatch.setenv("HOSTED_FIREBASE_PROJECT_ID", "korely-test")
    get_settings.cache_clear()
    return create_app()


@pytest.mark.parametrize("mode", ["self-hosted", pytest.param("hosted", marks=pytest.mark.cloud)])
def test_every_project_a_request_may_give_refuses_an_empty_one(mode, monkeypatch):
    from korely_agent.api.errors import api_routes

    app = _app(mode, monkeypatch)
    found, open_ = set(), []
    # Through the routers FastAPI keeps as one node each (api/errors.every_route).
    for route in api_routes(app.router.routes):
        for kind, what in _taking_the_project(route.dependant):
            where = (route.path, ",".join(sorted(route.methods)), kind)
            found.add(where)
            if not _empty_is_refused(kind, what):
                open_.append(where)
    # Some of those there are now, so that a walk which found nothing passes nothing.
    assert {("/admin/facts", "GET", "query"), ("/admin/keys", "POST", "body"),
            ("/admin/keys/{key_id}/move", "POST", "body")} <= found, sorted(found)
    assert open_ == [], f"an empty project_id is read as no project on: {sorted(open_)}"
