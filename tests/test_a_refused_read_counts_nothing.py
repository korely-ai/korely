"""Every read the Cloud counts is counted by the route, once it answers.

The month's reads were counted in the dependency of every read route
(core/auth.require_read_with_quota), before FastAPI checked the route's own
query and body: a read refused 422 or 403 took a query of the month. The count
is now made by the route once the request is one it answers (audit of 17 Oct
2026). This guard reads the core's routes alone; what the Cloud counts
on each refusal is tested beside its package
(tests/test_hosted_refused_read_counts_nothing.py).
"""
import inspect


def _api_routes(routes):
    """Every APIRoute, through the routers FastAPI >= 0.140 keeps as one node
    each (tests/test_every_route_is_rate_limited.py walks them the same way)."""
    from fastapi.routing import APIRoute

    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _api_routes(inner.routes)
        elif isinstance(r, APIRoute):
            yield r


def test_every_counted_route_counts_in_its_body(monkeypatch):
    """A route that takes `require_read_with_quota` counts with
    `count_the_read`: the dependency no longer does, and a route that forgot
    would give the Cloud's reads away."""
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    counted = []
    for route in _api_routes(create_app().router.routes):
        if any(getattr(d.call, "counts_a_read", False) for d in route.dependant.dependencies):
            counted.append(route.path)
            assert "count_the_read(db, key)" in inspect.getsource(route.endpoint), route.path
    assert len(counted) >= 10, counted
