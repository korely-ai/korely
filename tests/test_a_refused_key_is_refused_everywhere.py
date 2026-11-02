"""A key every door refuses (a `kor_live_` key on an install of one's own,
since 0.2.0: keys.refused_here) is refused by every reader of a key's state
(audit of 20 Oct 2026).

The expiry job still told the account that such a key was "about to stop
working", then that it had expired, and a project could not be archived
while one sat in it. The guard below finds every query of the keys not
revoked: each one asks the rule (refused_here, or state_of which asks it), or
is named here with why it needs not.
"""
import ast
import uuid
from pathlib import Path


from korely_agent.db.models import AgentWebhookDelivery, AgentWebhookEndpoint
from korely_agent.services import keys as key_service
from korely_agent.services import projects

ROOT = Path(__file__).resolve().parent.parent

_NEED_NOT = {
    # How many keys each project holds, for the Projects page: not revoked,
    # whatever else; the keys page says which are refused (state_of).
    ("korely_agent/api/admin.py", "list_projects"),
    # The credentials replay puts back every key not revoked as the live
    # install held it, the refused ones included: a restore gives back what
    # was there, and the door refuses them as before.
    ("korely_agent/services/credential_replay.py", "replay"),
    # OAuth's keys only, which are minted with the install's own prefix
    # (keys.default_prefix), never kor_live_ on an install of one's own:
    # their purge, and their revocation when their project is deleted.
    ("korely_agent/services/oauth.py", "purge_expired"),
    # The cap on keys bounds the rows an account holds: a key refused here is
    # a row as well, and counts.
    ("korely_agent/services/keys.py", "_room_for_a_key"),
    ("korely_agent/services/projects.py", "_hand_over_keys"),
    # How many of a project's confined keys its erasure revoked
    # (`keys_revoked`): a count before and after, of every key not revoked.
    ("korely_agent/services/projects.py", "_live_confined_keys"),
}


def _readers():
    for path in sorted((ROOT / "korely_agent").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/memory_core/" in rel:
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, ast.FunctionDef):
                src = ast.unparse(fn)
                if "AgentApiKey.revoked_at.is_(None)" in src:
                    yield rel, fn.name, src


def test_every_reader_of_the_live_keys_asks_the_rule():
    found = list(_readers())
    assert len(found) >= 5, found
    blind = {(rel, name) for rel, name, src in found
             if not any(w in src for w in ("refused_here", "state_of", "key_state"))
             and (rel, name) not in _NEED_NOT}
    assert blind == set(), blind


def test_no_expiry_notice_for_a_key_refused_here(db, tenant):
    db.add(AgentWebhookEndpoint(tenant_id=tenant.id, url="https://example.com/h",
                                secret="whsec_test", events=["key.expiring", "key.expired"]))
    db.commit()
    key_service.create_api_key(db, tenant_id=tenant.id, name="old", prefix="kor_live_",
                               expires_in_days=3)
    assert key_service.warn_about_expiring(db) == {}
    assert db.query(AgentWebhookDelivery).count() == 0
    key_service.create_api_key(db, tenant_id=tenant.id, name="new", expires_in_days=3)
    assert key_service.warn_about_expiring(db) == {"warned": 1, "expired": 0}


def test_a_refused_key_does_not_keep_a_project_open(db, tenant):
    p = projects.create_project(db, tenant_id=tenant.id, name=f"P-{uuid.uuid4().hex[:4]}")
    key_service.create_api_key(db, tenant_id=tenant.id, name="old", prefix="kor_live_",
                               project_id=p.id)
    assert projects.archive_project(db, p).archived_at is not None
