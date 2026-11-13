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
    assert "HYBRID_SEARCH" not in (ROOT / "korely_agent/main.py").read_text()
    for rel in ("korely_agent/api/memories.py", "korely_agent/api/admin.py"):
        code = (ROOT / rel).read_text()
        assert "memories_forgotten: int" not in code and "facts_invalidated: int" not in code, rel


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
