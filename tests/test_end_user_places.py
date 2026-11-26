"""The one list of where a person can be (services/end_user_places.py, audit of 20 Oct 2026).

Each audit found one more column, table or order that an erasure, the
pseudonym rewrite or the restore's replay missed, because each door kept a
list of its own. The guards on the one list:

- every text, JSON or array column of a table no place covers whole is in a
  place, or in NOT_AN_END_USER with the reason it cannot hold an end user's
  id (audit of 27 Oct 2026: only columns named like an end user, or JSON,
  were looked at, so a new text column of any other name was never asked);
- a marker id put in every place, and in the copies the product derives
  from them (a webhook delivery still to be sent, the Markdown export
  outside the database), is gone from every table and from the export after
  each erasure door: the end user's, the project's, the agent namespace's,
  the end user's after their project was deleted without erasing it, with
  SECRET_KEY and without;
- across the projects of one account: the same id in another project, a
  namesake who only declared names there, keeps every row of theirs, and
  so does one who declared names in the Default project, where
  0.2.0 filed the names made with no project: the upgrade gives those their
  person's project (migration e7c4b2d9a6f1), and every erasure stays behind
  its project's wall (audit of 27 Oct 2026: the guard's namesake lived in Gamma only,
  so an erasure reaching into the Default project passed it);
- in the orders the product allows (the audit before 1.0.0: the guard erased the rename
  memory only live, erased ids that always had rows in the erasing project,
  and finished a delivery only after the erasure committed): one memory
  forgotten first and then erased, an id whose only row is a name the
  upgrade could not place, erased in another project, and a delivery whose
  outcome the sender writes while the erasure rewrites the deliveries.
"""
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

from tests import cloud
import pytest
from sqlalchemy import ARRAY, JSON, String, Text, text

from korely_agent.core import extensions
from korely_agent.db import Base
from korely_agent.db.models import (AgentBatchJob, AgentEntityAlias, AgentFact, AgentObjectLink,
                                    AgentForgetAudit, AgentMemory, AgentReadMark,
                                    AgentWebhookDelivery, AgentWebhookEndpoint, AuditEvent,
                                    OAuthClient, OAuthGrant, PredicateAlias, PredicateCluster)
from korely_agent.services import end_user_places as places
from korely_agent.services import projects as project_service


def _whole(name: str) -> bool:
    return any(c == name for p in places.PLACES for c in p.covers)


def _candidates():
    """Every column that can hold text: a string, a text, a JSON or an
    array, in a table no place covers whole."""
    out = set()
    for table in Base.metadata.sorted_tables:
        if _whole(table.name):
            continue
        for column in table.columns:
            if isinstance(column.type, (String, Text, JSON, ARRAY)):
                out.add(f"{table.name}.{column.name}")
    return out


def _covered(name: str) -> bool:
    table = name.split(".")[0]
    return any(c == name or c == table for p in places.PLACES for c in p.covers)


def _excused(name: str) -> bool:
    return name in places.NOT_AN_END_USER or name.split(".")[0] in places.NOT_AN_END_USER


def test_every_column_that_can_hold_an_end_user_is_in_a_place():
    missing = sorted(c for c in _candidates() if not _covered(c) and not _excused(c))
    assert not missing, (
        f"{missing}: a column that can hold text and is in no place of "
        "services/end_user_places.py. Add it to the place that erases or pseudonymises it, "
        "or to NOT_AN_END_USER with the reason it cannot hold an end user's id.")


def test_the_registry_names_only_columns_that_exist():
    tables = {t.name: t for t in Base.metadata.sorted_tables}
    for p in places.PLACES:
        for c in p.covers:
            table, _, column = c.partition(".")
            assert table in tables, c
            assert not column or column in tables[table].columns, c
    from korely_agent.core import extensions

    for c in places.NOT_AN_END_USER:
        table, _, column = c.partition(".")
        if extensions.cloud_tables_without_models([table]):
            # The Cloud's, in a tree without its package: the migrations made
            # the table, no model declares it here.
            continue
        assert table in tables, f"stale exception {c}"
        assert not column or column in tables[table].columns, f"stale exception {c}"
        assert not _covered(c), f"{c} is both in a place and an exception"


# The text columns (a string, a text, a JSON or an array) of each table that
# NOT_AN_END_USER excuses whole, as they are today. `_excused` passes every
# column of such a table, so without this list a text column added to one
# later would be excused without anyone asking whether it can hold an end
# user's id (30 Oct 2026).
WHOLE_TABLE_TEXT_COLUMNS = {
    "tenants": {"merged_tenant_ids", "name"},
    "agent_projects": {"description", "name", "region", "slug"},
    "agent_rate_limits": {"bucket", "window_type"},
    "agent_idempotency_keys": {"fingerprint", "key_hash"},
    "agent_webhook_endpoints": {"events", "secret", "url"},
    "agent_llm_usage": {"agent_id", "model", "provider", "region", "stage", "vendor"},
    "install_settings": {"name", "updated_by", "value_encrypted"},
    "install_state": {"name", "value"},
    "login_attempts": {"ip", "username"},
    "operators": {"password_hash", "username"},
    "operator_sessions": {"ip", "token_hash", "user_agent"},
    "oauth_clients": {"client_id", "info", "network", "secret_encrypted"},
    "hosted_accounts": {"agent_caller", "email", "firebase_uid", "plan", "polar_customer_id",
                        "sign_in_method", "signup_network"},
    "hosted_brakes": {"provider", "reason"},
    "hosted_erasures": {"email_sha256", "firebase_uid_sha256", "rows", "via"},
    "hosted_polar_events": {"event_id", "type"},
    "hosted_polar_subscriptions": {"customer_id", "subscription_id"},
    "hosted_polar_unfiled": {"data", "event_id", "product_id", "type"},
    "hosted_signups": {"network"},
    "hosted_topups": {"currency", "month", "order_id"},
    "hosted_usage_months": {"month"},
}


def test_a_table_excused_whole_has_only_the_text_columns_it_was_excused_with():
    from korely_agent.core import extensions

    tables = {t.name: t for t in Base.metadata.sorted_tables}
    whole = {c for c in places.NOT_AN_END_USER if "." not in c}
    assert whole == set(WHOLE_TABLE_TEXT_COLUMNS), (
        "NOT_AN_END_USER and WHOLE_TABLE_TEXT_COLUMNS name different whole tables: list "
        f"the text columns of each one here ({sorted(whole ^ set(WHOLE_TABLE_TEXT_COLUMNS))})")
    for table in sorted(whole):
        if extensions.cloud_tables_without_models([table]):
            continue  # the Cloud's, in a tree without its package: no model here
        assert table in tables, f"stale exception {table}"
        have = {c.name for c in tables[table].columns
                if isinstance(c.type, (String, Text, JSON, ARRAY))}
        assert have == WHOLE_TABLE_TEXT_COLUMNS[table], (
            f"{table} is excused whole by NOT_AN_END_USER, and its text columns are now "
            f"{sorted(have)}, not {sorted(WHOLE_TABLE_TEXT_COLUMNS[table])}. Whoever adds or "
            "removes a column decides whether it can hold an end user's data: if it can, it "
            "goes in a place of services/end_user_places.py (and the table out of "
            "NOT_AN_END_USER); if not, update WHOLE_TABLE_TEXT_COLUMNS and the table's reason.")


def test_every_place_says_how_it_is_found_and_erased():
    """A place is either deleted with the person (and with a project) or
    keeps a record of them under their pseudonym, never neither."""
    for p in places.PLACES:
        assert p.find is not None, p.name
        deletes = p.erase is not None or p.lock is not None
        assert deletes != (p.pseudonymise is not None), p.name
        if deletes:
            assert p.holds and p.erase_project is not None, p.name


# ── a marker in every place, gone after each door ──────────────────────────

MARK = "zz-marker-7f3a"
OTHER = "zz-other-person"
AGENT = "zz-agent-5c1e"


@pytest.fixture(params=["keyed", "keyless"])
def keyed(request, monkeypatch, tmp_path):
    """With SECRET_KEY (the id becomes a pseudonym) and without (it goes);
    and the Markdown export written to a folder of the test's."""
    monkeypatch.setenv("SECRET_KEY", "k" * 48 if request.param == "keyed" else "")
    monkeypatch.setenv("FACTS_EXPORT_DIR", str(tmp_path / "export"))
    from korely_agent import config

    config.get_settings.cache_clear()
    yield tmp_path / "export"
    config.get_settings.cache_clear()


def _person(db, tenant, pid, who, *, user=None, agent=None, alias=True):
    """A memory, a fact, an object link, a relation and its phrase, an alias
    and a batch item of `who` in project `pid`."""
    now = datetime.now(timezone.utc)
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=user,
                    agent_id=agent, content=f"the memory of {who}", graph_sync_status="done")
    db.add(m)
    db.commit()
    db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=user,
                     agent_id=agent, subject_label=who, predicate_raw="lives in",
                     predicate_canonical="lives_in", object_label="Turin",
                     source_memory_id=m.id, valid_from=now))
    db.add(AgentObjectLink(tenant_id=tenant.id, project_id=pid, end_user_id=user, agent_id=agent,
                           name_label="Torino", canonical_label="Turin", family="places", decided_by="writer",
                           source_memory_id=m.id, reason={"descriptions": {"name": f"where {who} lives"}}))
    if user is not None:
        rel = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                               end_user_id=user, canonical="lives_in", member_count=1)
        db.add(rel)
        db.flush()
        db.add(PredicateAlias(tenant_id=tenant.id, project_id=pid, end_user_id=user,
                              surface="lives in", cluster_id=rel.id))
    if alias:
        db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=pid, end_user_id=user,
                                agent_id=agent, alias_lower="my wife", alias_label="My Wife",
                                canonical_label="Dummy Rossi"))
    item = {"content": "queued"}
    if user is not None:
        item["user_id"] = user
    if agent is not None:
        item["agent_id"] = agent
    db.add(AgentBatchJob(tenant_id=tenant.id, project_id=pid,
                         payload=[item, {"content": "queued too", "user_id": "zz-queued"}]))
    # A part of the product's rows of theirs (core/extensions.Part.places).
    for rows_of_a_part in cloud.of_the_parts("A_PERSONS_ROWS", tuple):
        rows_of_a_part(db, tenant, pid, who, user=user, agent=agent)
    db.commit()


def the_upgrade_places_the_names(db):
    """What the upgrade does to the names 0.2.0 filed under the Default
    project: each goes to its person's project (migration e7c4b2d9a6f1,
    `repair`), on the rows the test wrote."""
    import importlib.util

    path = next(pathlib.Path(__file__).resolve().parent.parent.glob(
        "alembic/versions/e7c4b2d9a6f1_*.py"))
    spec = importlib.util.spec_from_file_location("mig_names_find_their_person", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    out = mig.repair(db.connection())
    db.commit()
    db.expire_all()
    return out


def _seed(db, tenant, pid, default_id):
    """One row naming MARK in every place (the place's name is the key),
    in project `pid`; an alias of MARK the 0.2.0 migration filed under the
    Default project (made with no project up to 0.1.40, no trail of it
    there); and the copies the product derives: a delivery still to be sent,
    beside the delivered one."""
    now = datetime.now(timezone.utc)
    _person(db, tenant, pid, MARK, user=MARK)
    # A reader that named itself was handed the fact (agent notices); and a
    # reader named after the person, handed a fact of nobody's.
    (fid,) = db.query(AgentFact.id).filter(AgentFact.tenant_id == tenant.id,
                                           AgentFact.end_user_id == MARK).one()
    db.add(AgentReadMark(tenant_id=tenant.id, project_id=pid, reader="zz-reader", fact_id=fid,
                         end_user_id=MARK))
    nobodys = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                        subject_label="the office", predicate_raw="is in",
                        predicate_canonical="is_in", object_label="Turin", valid_from=now)
    db.add(nobodys)
    db.flush()
    db.add(AgentReadMark(tenant_id=tenant.id, project_id=pid, reader=MARK, fact_id=nobodys.id))
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=default_id, end_user_id=MARK,
                            alias_lower="my sister", alias_label="My Sister",
                            canonical_label="Dummy Verdi"))
    db.add(AgentForgetAudit(tenant_id=tenant.id, project_id=pid, end_user_id=MARK,
                            scope="memory", target_id="mem_x"))
    db.add(AuditEvent(tenant_id=tenant.id, project_id=pid, actor="rest", action="read",
                      result="ok", end_user_id=MARK))
    db.add(AuditEvent(tenant_id=tenant.id, project_id=None, actor="dashboard", action="read",
                      result="ok", end_user_id=MARK))
    db.add(AuditEvent(tenant_id=tenant.id, project_id=pid, actor="rest", action="key_create",
                      result="ok", meta={"users": str([MARK, OTHER])}))
    ep = AgentWebhookEndpoint(tenant_id=tenant.id, url="http://127.0.0.1:9/h", secret="whsec_x",
                              events=["*"])
    db.add(ep)
    db.flush()
    for status in ("delivered", "pending"):
        db.add(AgentWebhookDelivery(tenant_id=tenant.id, endpoint_id=ep.id,
                                    event_type="memory.created", status=status,
                                    payload={"project_id": str(pid), "data": {"user_id": MARK}}))
    from korely_agent.services import keys as key_service

    key_service.create_api_key(db, tenant_id=tenant.id, name="confined", scopes=["memories:read"],
                               project_id=pid, end_users=[MARK, OTHER])
    db.add(OAuthClient(client_id="zz-client", info={"client_name": "app"}))
    db.flush()
    db.add(OAuthGrant(kind="code", client_id="zz-client", tenant_id=tenant.id, project_id=pid,
                      scopes=["memories:read"], expires_at=now + timedelta(minutes=5),
                      end_users=[MARK, OTHER]))
    db.commit()
    return {"batch_items", "read_marks", "facts", "memories", "aliases", "object_links", "vocabulary",
            "erasure_records", "audit_events", "webhooks_delivered", "confined_keys",
            # A part of the product's places (core/extensions.Part.places).
            *(p.name for part in extensions.parts() for p in part.places())}


def _declared(db, tenant, pid, *, user, agent=None, label, canonical):
    """A name declared through POST /v1/users/{id}/names with a key of
    `pid`: the alias, and the trail row naming it there."""
    row = AgentEntityAlias(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                           end_user_id=user, agent_id=agent, alias_lower=label.lower(),
                           alias_label=label, canonical_label=canonical)
    db.add(row)
    db.add(AuditEvent(tenant_id=tenant.id, project_id=pid, actor="rest", action="fact_write",
                      result="ok", end_user_id=user, target_id=str(row.id),
                      meta={"endpoint": "names:declare"}))


def _namesake(db, tenant, gamma_id, default_id):
    """The same id in other projects of the account, namesakes there who
    only declared names (no memory, no fact): in Gamma, with a read of them
    there; and in the Default project, through the names door, the end user
    and a name under the agent namespace."""
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=gamma_id, end_user_id=MARK,
                            alias_lower="my boss", alias_label="My Boss",
                            canonical_label="Anna Bianchi"))
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=gamma_id, agent_id=AGENT,
                            alias_lower="the client", alias_label="The Client",
                            canonical_label="Acme"))
    db.add(AuditEvent(tenant_id=tenant.id, project_id=gamma_id, actor="rest", action="read",
                      result="ok", end_user_id=MARK))
    _declared(db, tenant, default_id, user=MARK, label="My Neighbour", canonical="Dummy Neri")
    _declared(db, tenant, default_id, user="zz-default-user", agent=AGENT, label="The Office",
              canonical="Dummy Plaza")
    db.commit()


# What the namesakes keep in the other projects, whatever is erased in Beta:
# Gamma's name and read, the Default project's name and its trail row.
_THE_NAMESAKES = {"agent_entity_aliases": 2, "audit_events": 2}
# Gamma's alone, once the same id is erased in the Default project.
_GAMMAS = {"agent_entity_aliases": 1, "audit_events": 1}


def _rows_naming(db, who) -> dict:
    """{table: rows whose text holds `who`}, over every table of the models."""
    out = {}
    for table in Base.metadata.sorted_tables:
        n = db.execute(text(f'SELECT count(*) FROM "{table.name}" AS t WHERE t::text LIKE :m'),
                       {"m": f"%{who}%"}).scalar()
        if n:
            out[table.name] = n
    return out


def _files_naming(folder: pathlib.Path, who) -> list:
    """The files outside the database (the Markdown export) that hold `who`."""
    if not folder.exists():
        return []
    return sorted(str(p) for p in folder.rglob("*")
                  if p.is_file() and who in p.read_text(errors="replace"))


def _finish_the_deliveries(db):
    """The worker delivers what was still to be sent, after the erasure."""
    from korely_agent.services import webhooks

    db.expire_all()
    for d in db.query(AgentWebhookDelivery).filter(AgentWebhookDelivery.status == "pending"):
        webhooks._write_outcome(db, d.id, d.attempts, d.endpoint_id, (True, 200, None))
    db.expire_all()


def _setup(db, tenant, export):
    from korely_agent.services import export_md

    default = project_service.get_or_create_default_project(db, tenant.id)
    beta = project_service.create_project(db, tenant_id=tenant.id, name="Beta")
    gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
    seeded = _seed(db, tenant, beta.id, default.id)
    assert seeded == {p.name for p in places.PLACES}, (
        "a place of services/end_user_places.py has no seed here: add one, so the marker "
        "test reaches it")
    _namesake(db, tenant, gamma.id, default.id)
    assert the_upgrade_places_the_names(db) == {"moved": 1, "kept": 2, "not_recorded": 0}
    export_md.export_all(db, str(export))
    assert _files_naming(export, MARK), "the export holds the marker before the erasure"
    return default, beta, gamma


def test_the_marker_is_found_in_every_place(keyed, db, tenant):
    default, beta, gamma = _setup(db, tenant, keyed)
    for p in places.PLACES:
        assert MARK in set(p.find(db, tenant.id, beta.id)), p.name
    assert places.held_in(db, tenant.id, beta.id) == [MARK, "zz-queued"]
    assert OTHER in places.named_in(db, tenant.id, beta.id)


def test_an_end_users_erasure_leaves_the_marker_nowhere(keyed, db, tenant):
    from korely_agent.services.facts import service as facts

    default, beta, gamma = _setup(db, tenant, keyed)
    facts.forget_end_user(db, tenant.id, MARK, project_id=beta.id)
    _finish_the_deliveries(db)
    assert _rows_naming(db, MARK) == _THE_NAMESAKES
    assert _files_naming(keyed, MARK) == []
    assert _rows_naming(db, OTHER), "another person's entries stay"


def test_a_projects_erasure_leaves_the_marker_nowhere(keyed, db, tenant):
    default, beta, gamma = _setup(db, tenant, keyed)
    project_service.delete_project(db, tenant.id, beta, erase=True)
    _finish_the_deliveries(db)
    assert _rows_naming(db, MARK) == _THE_NAMESAKES
    assert _files_naming(keyed, MARK) == []


def test_an_erasure_after_its_project_was_deleted_leaves_the_marker_nowhere(keyed, db, tenant):
    """Beta deleted without erasing it: its rows are the Default project's,
    and the end user's erasure there reaches every one of them, the
    deliveries about Beta too."""
    from korely_agent.services.facts import service as facts

    default, beta, gamma = _setup(db, tenant, keyed)
    project_service.delete_project(db, tenant.id, beta, erase=False)
    facts.forget_end_user(db, tenant.id, MARK, project_id=default.id)
    _finish_the_deliveries(db)
    assert _rows_naming(db, MARK) == _GAMMAS
    assert _files_naming(keyed, MARK) == []


def test_an_agents_erasure_leaves_the_namespace_only_in_its_records(keyed, db, tenant):
    """delete_agent reaches every place that holds an agent namespace
    (`Place.agents`). The namespace is not a person: what only names it
    keeps it, as the record of what was done (CONTRIBUTING.md, after an
    erasure: the pseudonym is for end users): the erasure's own
    record (`target_id`), the cost row of a call made in it
    (NOT_AN_END_USER, agent_llm_usage), and a delivered webhook's
    `data.agent_id`. The namesakes' declared names in Gamma and in the
    Default project stay."""
    from korely_agent.db.models import AgentLlmUsage
    from korely_agent.services.facts import service as facts

    default, beta, gamma = _setup(db, tenant, keyed)
    _person(db, tenant, beta.id, AGENT, agent=AGENT)
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=default.id, agent_id=AGENT,
                            alias_lower="my sister", alias_label="My Sister",
                            canonical_label="Dummy Verdi"))
    db.commit()
    assert the_upgrade_places_the_names(db)["moved"] == 1
    ep = db.query(AgentWebhookEndpoint).first()
    db.add(AgentWebhookDelivery(tenant_id=tenant.id, endpoint_id=ep.id, event_type="memory.created",
                                status="delivered",
                                payload={"project_id": str(beta.id),
                                         "data": {"user_id": None, "agent_id": AGENT}}))
    db.add(AgentLlmUsage(tenant_id=tenant.id, agent_id=AGENT, stage="extract",
                         provider="gemini", model="m", prompt_tokens=1, completion_tokens=1))
    db.commit()
    facts.delete_agent(db, tenant.id, AGENT, project_id=beta.id)
    db.expire_all()
    assert _rows_naming(db, AGENT) == {"agent_forget_audit": 1, "agent_entity_aliases": 2,
                                       "agent_llm_usage": 1, "agent_webhook_deliveries": 1}
    kept = {a.project_id for a in db.query(AgentEntityAlias)
            .filter(AgentEntityAlias.agent_id == AGENT)}
    assert kept == {gamma.id, default.id}, "the namesakes' declared names stay"


# A name only the memory erased below said: its rename's new name.
NAME = "Zzglobexmark"


@pytest.mark.parametrize("forgotten_first", [False, True], ids=["live", "forgotten-first"])
def test_one_memorys_erasure_leaves_its_words_nowhere(keyed, db, tenant, forgotten_first):
    """The door of one memory (DELETE /v1/memories/{id}?erase=true), where
    the audit of 27 Oct 2026 found the derived copies: the memory taught "Acme is now
    called <NAME>" and wrote first "Bob works for <NAME>", which a kept
    memory restated as "Bob works for Acme"; its item in a /v1/batch
    payload, the relation whose example is <NAME>, the export. After its
    erasure <NAME> is in no table and no file, whether it is erased live
    or forgotten first (DELETE /v1/memories/{id}, or the dashboard's
    Remove, then the erasure): the forget re-points the restated fact to
    the kept memory, and the audit before 1.0.0 found it kept <NAME>."""
    from korely_agent.services import batch, export_md
    from korely_agent.services.facts import entities
    from korely_agent.services.facts import service as facts
    from korely_agent.services.memory_core import store
    from korely_agent.services.memory_core.identity import SOURCE_IDENTITY_FACT

    default, beta, gamma = _setup(db, tenant, keyed)
    user = "zz-memory-door"
    said = f"Acme is now called {NAME}. Bob works for {NAME}."
    job = AgentBatchJob(tenant_id=tenant.id, project_id=beta.id,
                        payload=[{"content": said, "user_id": user}])
    db.add(job)
    db.flush()
    m1 = AgentMemory(id=batch.item_memory_id(job.id, 0), tenant_id=tenant.id, project_id=beta.id,
                     end_user_id=user, content=said, graph_sync_status="done")
    m2 = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=beta.id, end_user_id=user,
                     content="Bob works for Acme.", graph_sync_status="done")
    db.add_all([m1, m2])
    db.commit()
    now = datetime.now(timezone.utc)

    def fact(s, pc, o, sources):
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=beta.id, end_user_id=user,
                      subject_label=s, subject_canonical=s, predicate_raw=pc.replace("_", " "),
                      predicate_canonical=pc, object_label=o, object_canonical=o,
                      source_memory_id=sources[0], source_memory_ids=[str(x) for x in sources],
                      observation_count=len(sources), valid_from=now)
        db.add(f)
        db.commit()
        return f.id

    identity = fact("Acme", "renamed_to", NAME, [m1.id])
    store.add_alias(db, entities.TABLES, entities.memory_of(tenant.id, beta.id, user, None),
                            "Acme", NAME, source=SOURCE_IDENTITY_FACT, fact_id=identity)
    fact("Bob", "works_for", NAME, [m1.id, m2.id])
    db.add(PredicateCluster(tenant_id=tenant.id, project_id=beta.id, end_user_id=user,
                            canonical="works_for", member_count=1, example_object=NAME))
    db.commit()
    export_md.export_all(db, str(keyed))
    assert _rows_naming(db, NAME) and _files_naming(keyed, NAME)
    if forgotten_first:
        assert facts.forget_memory(db, tenant.id, m1.id, project_id=beta.id)
    facts.erase_memory(db, tenant.id, m1.id, project_id=beta.id)
    db.expire_all()
    assert _rows_naming(db, NAME) == {}
    assert _files_naming(keyed, NAME) == []
    assert db.get(AgentMemory, m2.id).graph_sync_status == "pending", "the kept one is read again"


# ── the names the upgrade could not place (the audit before 1.0.0) ─────────

UNPLACED = "zz-unplaced-9d2b"


@pytest.mark.parametrize("door", ["end_user", "agent"])
def test_an_erasure_in_any_project_takes_the_unplaced_names_of_an_id_with_nothing(
        keyed, db, tenant, door):
    """A name the upgrade could not give a project (`project_not_recorded`,
    migration e7c4b2d9a6f1) of an id with no memory or fact anywhere goes
    with the id's erasure, wherever it is made (end_user_places.
    unplaced_names). Until the audit before 1.0.0 an erasure in a project other than
    the Default one asked only when it had deleted something there, so the
    id and the name stayed."""
    from korely_agent.services.facts import service as facts

    default = project_service.get_or_create_default_project(db, tenant.id)
    beta = project_service.create_project(db, tenant_id=tenant.id, name="Beta")
    who = {"end_user_id": UNPLACED} if door == "end_user" else {"agent_id": UNPLACED}
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=default.id, alias_lower="my aunt",
                            alias_label="My Aunt", canonical_label="Dummy Gialli",
                            project_not_recorded=True, **who))
    db.commit()
    if door == "end_user":
        facts.forget_end_user(db, tenant.id, UNPLACED, project_id=beta.id)
    else:
        facts.delete_agent(db, tenant.id, UNPLACED, project_id=beta.id)
    db.expire_all()
    assert "agent_entity_aliases" not in _rows_naming(db, UNPLACED)


# ── a delivery that finishes inside the erasure (the audit before 1.0.0) ───

def test_a_delivery_finishing_while_the_erasure_classifies_it_takes_the_pseudonym(
        keyed, db, tenant, engine, monkeypatch):
    """The sender writes a pending delivery's outcome under its row lock
    (webhooks._write_outcome) while the end user's erasure rewrites the
    deliveries naming them (end_user_places.rename_in_deliveries). Until
    the audit before 1.0.0 the erasure classified the rows in two statements, the
    finished ones then the others: the delivery, pending at the first and
    delivered by the time the second had its lock, was in neither, and kept
    the id in clear for WEBHOOK_DELIVERY_RETENTION_DAYS."""
    import threading
    import time

    from sqlalchemy.orm import sessionmaker

    from korely_agent.services import webhooks
    from korely_agent.services.facts import service as facts

    project_service.get_or_create_default_project(db, tenant.id)
    beta = project_service.create_project(db, tenant_id=tenant.id, name="Beta")
    _person(db, tenant, beta.id, MARK, user=MARK)
    ep = AgentWebhookEndpoint(tenant_id=tenant.id, url="http://127.0.0.1:9/h", secret="whsec_x",
                              events=["*"])
    db.add(ep)
    db.flush()
    d = AgentWebhookDelivery(tenant_id=tenant.id, endpoint_id=ep.id, event_type="memory.created",
                             status="pending", attempts=1,
                             payload={"project_id": str(beta.id), "data": {"user_id": MARK}})
    db.add(d)
    db.commit()
    S = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    locked, go_on, out = threading.Event(), threading.Event(), {}
    real = webhooks._renamed_if_erased_meanwhile

    def held(*a, **k):
        # The sender holds the delivery's row lock from here to its commit.
        locked.set()
        assert go_on.wait(20)
        return real(*a, **k)

    monkeypatch.setattr(webhooks, "_renamed_if_erased_meanwhile", held)

    def send():
        s = S()
        try:
            out["sent"] = webhooks._write_outcome(s, d.id, 1, ep.id, (True, 200, None))
        finally:
            s.close()

    def erase():
        s = S()
        try:
            out["pid"] = s.execute(text("SELECT pg_backend_pid()")).scalar()
            facts.forget_end_user(s, tenant.id, MARK, project_id=beta.id)
        except Exception as e:  # noqa: BLE001 - reported to the test
            out["error"] = e
        finally:
            s.close()

    sender, eraser = threading.Thread(target=send), threading.Thread(target=erase)
    sender.start()
    try:
        assert locked.wait(10)
        eraser.start()
        # The erasure reaches the delivery and waits for the sender's lock.
        deadline = time.monotonic() + 15
        waiting = False
        while time.monotonic() < deadline and not waiting:
            with engine.connect() as c:
                waiting = "pid" in out and c.execute(
                    text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :p"),
                    {"p": out["pid"]}).scalar() is True
            time.sleep(0.05)
        assert waiting, "the erasure never waited for the delivery's lock"
    finally:
        go_on.set()
        sender.join(20)
        eraser.join(30)
    assert "error" not in out, out.get("error")
    assert out["sent"] is True
    db.expire_all()
    row = db.get(AgentWebhookDelivery, d.id)
    assert row.status == "delivered"
    assert row.payload["data"]["user_id"] != MARK, "the delivered row kept the id in clear"


def test_a_delivery_dropped_while_the_erasure_marks_it_takes_the_pseudonym(
        keyed, db, tenant, engine, monkeypatch):
    """The sibling of the test above (review of the audit before 1.0.0): a delivery claimed, its
    endpoint off, is dropped (webhooks._to_send) while the end user's
    erasure, not yet committed, has marked it. The drop decided on the row
    as the claim had read it, and its write landed after the erasure's
    commit: spent, with the id in clear for its 30 days."""
    import threading

    from sqlalchemy.orm import sessionmaker

    from korely_agent.services import pseudonyms, webhooks
    from korely_agent.services.facts import service as facts

    project_service.get_or_create_default_project(db, tenant.id)
    beta = project_service.create_project(db, tenant_id=tenant.id, name="Beta")
    _person(db, tenant, beta.id, MARK, user=MARK)
    ep = AgentWebhookEndpoint(tenant_id=tenant.id, url="http://127.0.0.1:9/h", secret="whsec_x",
                              events=["*"], active=False)
    db.add(ep)
    db.flush()
    d = AgentWebhookDelivery(tenant_id=tenant.id, endpoint_id=ep.id, event_type="memory.created",
                             status="pending",
                             payload={"project_id": str(beta.id), "data": {"user_id": MARK}})
    db.add(d)
    db.commit()
    S = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sender = S()
    rows, _ = webhooks._claim(sender, 10)
    assert [r.id for r in rows] == [d.id]
    marked, go_on, out = threading.Event(), threading.Event(), {}
    real = pseudonyms.after_an_erasure

    def held(*a, **k):
        # The erasure has rewritten the deliveries and holds their rows.
        r = real(*a, **k)
        marked.set()
        assert go_on.wait(20)
        return r

    monkeypatch.setattr(pseudonyms, "after_an_erasure", held)

    def erase():
        s = S()
        try:
            facts.forget_end_user(s, tenant.id, MARK, project_id=beta.id)
        except Exception as e:  # noqa: BLE001 - reported to the test
            out["error"] = e
        finally:
            s.close()

    def drop():
        try:
            out["pid"] = sender.execute(text("SELECT pg_backend_pid()")).scalar()
            out["sent"] = webhooks._to_send(sender, rows)
        except Exception as e:  # noqa: BLE001 - reported to the test
            out["error"] = e
        finally:
            sender.close()

    eraser, dropper = threading.Thread(target=erase), threading.Thread(target=drop)
    eraser.start()
    try:
        assert marked.wait(15)
        # Since 1.0.40 the drop skips a delivery the erasure holds (SKIP
        # LOCKED: the round is every account's), neither sending nor
        # dropping it; it is dropped at a later round, after the erasure.
        dropper.start()
        dropper.join(15)
        assert not dropper.is_alive(), "the drop waited for the erasure's lock"
        assert out.get("sent") == ([], 0), out
    finally:
        go_on.set()
        eraser.join(30)
        dropper.join(30)
    assert "error" not in out, out.get("error")
    db.expire_all()
    later = S()
    try:
        again = later.get(AgentWebhookDelivery, d.id)
        assert webhooks._to_send(later, [again]) == ([], 1)
    finally:
        later.close()
    db.expire_all()
    row = db.get(AgentWebhookDelivery, d.id)
    assert row.status == "failed" and row.attempts >= row.max_attempts
    assert row.payload["data"]["user_id"] != MARK, "the dropped row kept the id in clear"


def _written_after(db, tenant, pid):
    """A row of MARK in each place, written after the moment of
    `test_an_erasure_up_to_a_moment_keeps_only_what_was_written_after_it`,
    with values of their own (the unique indexes): a memory and its fact, a
    fact posted by itself, a read mark, an alias, an object link, a relation
    and its phrase, a queued item, a row of a part's place, and what only
    names them: the trail, a delivery, a key confined to them."""
    from korely_agent.services import keys as key_service

    now = datetime.now(timezone.utc)
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=MARK,
                    content="written after", graph_sync_status="done")
    db.add(m)
    db.flush()
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=MARK,
                  subject_label=MARK, predicate_raw="moved to", predicate_canonical="moved_to",
                  object_label="Rome", source_memory_id=m.id, valid_from=now)
    db.add(f)
    db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=MARK,
                     subject_label=MARK, predicate_raw="works at", predicate_canonical="works_at",
                     object_label="Acme", valid_from=now))
    db.flush()
    db.add(AgentReadMark(tenant_id=tenant.id, project_id=pid, reader="zz-reader-after",
                         fact_id=f.id, end_user_id=MARK))
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=pid, end_user_id=MARK,
                            alias_lower="my brother", alias_label="My Brother",
                            canonical_label="Dummy Bruni"))
    db.add(AgentObjectLink(tenant_id=tenant.id, project_id=pid, end_user_id=MARK,
                           name_label="Roma", canonical_label="Rome", family="places",
                           decided_by="writer", source_memory_id=m.id,
                           reason={"descriptions": {"name": "where they moved"}}))
    rel = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                           end_user_id=MARK, canonical="moved_to", member_count=1)
    db.add(rel)
    db.flush()
    db.add(PredicateAlias(tenant_id=tenant.id, project_id=pid, end_user_id=MARK,
                          surface="moved to", cluster_id=rel.id))
    db.add(AgentBatchJob(tenant_id=tenant.id, project_id=pid,
                         payload=[{"content": "queued after", "user_id": MARK}]))
    for rows_of_a_part in cloud.of_the_parts("A_PERSONS_ROWS", tuple):
        rows_of_a_part(db, tenant, pid, "written-after", user=MARK, agent=None)
    db.add(AuditEvent(tenant_id=tenant.id, project_id=pid, actor="rest", action="read",
                      result="ok", end_user_id=MARK))
    ep = db.query(AgentWebhookEndpoint).filter(AgentWebhookEndpoint.tenant_id == tenant.id).first()
    db.add(AgentWebhookDelivery(tenant_id=tenant.id, endpoint_id=ep.id,
                                event_type="memory.created", status="delivered",
                                payload={"project_id": str(pid), "data": {"user_id": MARK}}))
    key_service.create_api_key(db, tenant_id=tenant.id, name="confined after",
                               scopes=["memories:read"], project_id=pid, end_users=[MARK])
    db.commit()


def test_an_erasure_up_to_a_moment_keeps_only_what_was_written_after_it(keyed, db, tenant):
    """An erasure made again after a restore, of someone who wrote again
    after it (services/erasure_replay.py; review of 16 Nov 2026): every
    place of this one list loses the rows written up to the erasure's
    moment, and what only names them up to then takes the pseudonym; every
    row written after it stays as it is, in each place."""
    from korely_agent.services.facts import service as facts

    default, beta, gamma = _setup(db, tenant, keyed)
    moment = db.execute(text("SELECT clock_timestamp()")).scalar()
    db.commit()
    before = _rows_naming(db, MARK)
    _written_after(db, tenant, beta.id)
    after = {t: n - before.get(t, 0) for t, n in _rows_naming(db, MARK).items()
             if n - before.get(t, 0)}
    # The core's eleven places, and one more for each part of the product
    # this tree has that holds rows of a person.
    assert len(after) >= 11 + len(cloud.of_the_parts("A_PERSONS_ROWS", tuple)), after
    with places.up_to(moment):
        facts.forget_end_user(db, tenant.id, MARK, project_id=beta.id)
    _finish_the_deliveries(db)
    kept = dict(_THE_NAMESAKES)
    for t, n in after.items():
        kept[t] = kept.get(t, 0) + n
    assert _rows_naming(db, MARK) == kept, "only what was written after the moment stays"
