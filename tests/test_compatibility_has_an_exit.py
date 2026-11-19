"""Every compatibility branch named its exit (audit of 17 Oct 2026),
and 0.2.0 took the four exits the CHANGELOG had promised since 0.1.41: the
HYBRID_SEARCH warning, the webhook secrets of before 0.1.11, the `kor_live_`
keys of before 0.1.3 on an install of one's own and the erasure's first
names. Here: they are gone (0.2.0's entry says so: tests/test_private_texts.py).
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_branches_are_gone():
    """The erasure's first names were aliases of its counts, on the answers
    that count rows physically deleted (`memories_deleted`): the end user's
    erasure and the project's. Read from those answers' models, every one
    of the two files that has the count: `facts_invalidated` is also the
    forget's own count, the facts a forgotten memory closed and kept in
    history (DELETE /v1/memories/{memory_id}, `MemoryDeletion`), a field of
    its own and no alias, which a text search of the file mistook for one
    (the 1.0.27 gate)."""
    import inspect

    from pydantic import BaseModel

    from korely_agent.api import admin, memories

    assert "HYBRID_SEARCH" not in (ROOT / "korely_agent/main.py").read_text()
    for module in (memories, admin):
        assert "memories_forgotten: int" not in inspect.getsource(module), module.__name__
        erasures = [m for _name, m in inspect.getmembers(module, inspect.isclass)
                    if issubclass(m, BaseModel) and "memories_deleted" in m.model_fields]
        assert erasures, f"{module.__name__} has no answer that counts deletions: this reads nothing"
        for answer in erasures:
            assert not {"memories_forgotten", "facts_invalidated"} & set(answer.model_fields), \
                answer.__name__


def test_an_endpoint_of_before_0_1_11_is_not_sent():
    from korely_agent.services import webhooks

    old = "whsec_" + "A" * 32  # the shape of `token_urlsafe(24)`
    assert webhooks.is_legacy_secret(old)
    ok, status, error = webhooks._deliver("https://example.com/hook", old, "msg_1", {"event": "x"})
    assert (ok, status) == (False, None) and "rotate-webhook-secret" in error


class _LookedUp(Exception):
    pass


class _NoLookup:
    """A session that fails the test if the key is looked up."""

    def query(self, *args, **kwargs):
        raise _LookedUp


def test_no_text_says_such_an_endpoint_still_works():
    """The page, /admin's description and `manage rotate-webhook-secret` told
    the owner of such an endpoint that their receiver kept working, after
    0.2.0 had stopped sending it (audit of 19 Oct 2026)."""
    for rel in ("dashboard/src/pages/WebhooksPage.tsx", "korely_agent/api/admin.py",
                "korely_agent/manage.py"):
        text = " ".join((ROOT / rel).read_text().split())
        for promise in ("keeps working", "keep working", "which its receiver expects",
                        "still signs the old way"):
            assert promise not in text, (rel, promise)


def test_a_kor_live_key_is_refused_on_an_install_of_ones_own(monkeypatch):
    from types import SimpleNamespace

    from korely_agent import config
    from korely_agent.services import keys

    # A key of the Cloud's own shape, minted the way the Cloud mints it: it
    # passes `_KEY_SHAPE`, so only the 0.2.0 refusal can stop it before the
    # lookup.
    key = keys.generate_api_key(keys.PREFIX_LIVE).full_key
    assert keys.has_the_shape_of_a_key(key)

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(hosted=True))
    with pytest.raises(_LookedUp):  # on the Cloud the same key is looked up
        keys.verify_api_key(_NoLookup(), key)

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(hosted=False))
    assert keys.verify_api_key(_NoLookup(), key) is None


def test_the_mcp_door_does_not_say_a_kor_live_key_gets_in():
    """Its docstring still described the branch 0.2.0 took out: a
    `kor_live_` key of before 0.1.3 getting in on an install of one's own
    (audit of 19 Oct 2026). It goes through verify_api_key, refused above."""
    from korely_agent.mcp.server import KorelyAgentKeyVerifier

    doc = " ".join((KorelyAgentKeyVerifier.__doc__ or "").split())
    assert "gets in" not in doc
    assert "refused" in doc and "verify_api_key" in doc
