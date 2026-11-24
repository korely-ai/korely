"""`manage review-subjects` (memory_core 6.20.0, off by default): the dry run
always runs, costs nothing and prints the pairs; asking the model is refused
while the switch is off unless --while-off; a European project is never
asked; --apply writes the links of the file, then judges the groups again."""
import json

from korely_agent import manage
from korely_agent.core import llm
from korely_agent.db.models import AgentObjectLink
from tests.subject_world import Judge, Port, fact, memory, vec


def team(db, tenant):
    m1 = memory(db, tenant, "Cloud now runs 1.0.27.", days_ago=5)
    m2 = memory(db, tenant, "Korely Cloud has version 1.0.30.", days_ago=2)
    fact(db, tenant, "Cloud", "runs_version", "1.0.27", mem=m1, minutes_ago=5000, vector=vec(7))
    fact(db, tenant, "Korely Cloud", "has_version", "1.0.30", mem=m2, minutes_ago=2000, vector=vec(7))


def test_the_dry_run_prints_the_pairs_and_calls_nothing(db, tenant, monkeypatch, capsys):
    team(db, tenant)
    monkeypatch.setattr(manage, "_session", lambda: db)
    monkeypatch.setattr(llm, "decisions_provider", lambda *a: (_ for _ in ()).throw(AssertionError("asked")))
    assert manage.review_subjects(str(tenant.id), dry_run=True) == 0
    out = capsys.readouterr().out
    assert "'Korely Cloud'" in out and "~ 'Cloud'" in out
    assert "No model was called and nothing was written." in out
    assert db.query(AgentObjectLink).count() == 0


def test_asking_is_refused_while_off(db, tenant, monkeypatch, capsys):
    monkeypatch.setattr(manage, "_session", lambda: db)
    assert manage.review_subjects(str(tenant.id), cap=0.1, out="x.json") == 1
    assert "switch is off" in capsys.readouterr().err


def test_a_run_writes_the_file_and_apply_writes_the_links(db, tenant, monkeypatch, tmp_path, capsys):
    from korely_agent.config import get_settings

    team(db, tenant)
    monkeypatch.setattr(manage, "_session", lambda: db)
    monkeypatch.setattr(manage, "_writable_place", lambda p: (p, None))
    monkeypatch.setattr(get_settings(), "llm_rates",
                        '{"currency":"USD","models":{"typesafe/jev-1.13":{"input_per_million":0.042,'
                        '"output_per_million":0}}}')
    monkeypatch.setattr(llm, "decisions_provider", lambda region: Port("1", "same"))
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: Judge())
    out = tmp_path / "s.json"
    assert manage.review_subjects(str(tenant.id), cap=0.1, out=str(out), while_off=True) == 0
    records = json.loads(out.read_text())["proposals"]
    assert [(r["name"], r["same_as"]) for r in records] == [("Korely Cloud", "Cloud")]
    assert db.query(AgentObjectLink).count() == 0, "a run writes nothing"
    assert manage.review_subjects(apply_file=str(out)) == 1, "off, the apply is refused too"
    assert manage.review_subjects(apply_file=str(out), while_off=True) == 0
    [row] = db.query(AgentObjectLink).all()
    assert row.scope == "wall"
    printed = capsys.readouterr().out
    assert "1 linked of 1 proposed." in printed and "judged again: 1 of 2 facts" in printed


def test_a_run_of_every_account_is_refused(db, tenant, monkeypatch, capsys):
    """Names and facts of every account, and the memory's words around each
    name, would go to the decisions model, through OpenRouter: services the
    operator's own privacy notice must name among its processors. One account
    at a time, by the operator's choice; the dry run of every account still
    runs."""
    from korely_agent.core.llm import DECISIONS_MODEL
    from korely_agent.services.memory_core.objects import WORDS_MAX

    team(db, tenant)
    monkeypatch.setattr(manage, "_session", lambda: db)
    assert manage.review_subjects(every=True, cap=0.1, out="x.json", while_off=True) == 1
    err = capsys.readouterr().err
    assert "one account at a time" in err
    # It tells the operator what goes where and whose notice must say so,
    # never "the Privacy Policy" as if Korely's were theirs.
    assert DECISIONS_MODEL in err and "OpenRouter" in err and "your own privacy notice" in err
    assert f"up to {WORDS_MAX} characters of the memory's words around each name" in err
    assert "Privacy Policy" not in err and "sub-processors yet" not in err
    assert manage.review_subjects(every=True, dry_run=True) == 0


def test_apply_judges_each_group_once(db, tenant, monkeypatch, tmp_path, capsys):
    """Two links of one group in the file: the group is judged once."""
    from korely_agent.services.facts import subjects as sub
    from korely_agent.services.memory_core import objects as O

    m1 = memory(db, tenant, "Cloud now runs 1.0.27.", days_ago=5)
    m2 = memory(db, tenant, "Korely Cloud has version 1.0.30.", days_ago=2)
    m3 = memory(db, tenant, "Il Cloud di Korely has 1.0.12.", days_ago=1)
    fact(db, tenant, "Cloud", "runs_version", "1.0.27", mem=m1, minutes_ago=5000, vector=vec(7))
    fact(db, tenant, "Korely Cloud", "has_version", "1.0.30", mem=m2, minutes_ago=2000, vector=vec(7))
    fact(db, tenant, "Il Cloud di Korely", "has_version", "1.0.12", mem=m3, minutes_ago=1000, vector=vec(7))
    records = [sub.proposal_record(tenant.id, None, O.Proposal(name=n, canonical="Cloud", source_memory_id=s.id,
                                                               source_fact_id=None, canonical_memory_id=m1.id))
               for n, s in (("Korely Cloud", m2), ("Il Cloud di Korely", m3))]
    out = tmp_path / "s.json"
    out.write_text(json.dumps({"proposals": records}))
    calls = []
    monkeypatch.setattr(manage, "_session", lambda: db)
    monkeypatch.setattr(sub, "rejudge", lambda db, t, p, name, **k: calls.append(name) or
                        {"facts": 3, "judged": 0, "closed": 0, "deferred": None})
    assert manage.review_subjects(apply_file=str(out), while_off=True) == 0
    assert len(calls) == 1
