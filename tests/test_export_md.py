"""The readable export.

The point of writing files is that somebody can look at them, and the point of
looking is `git diff`. Both of those die quietly: a file that changes on every
pass makes the diff useless, and an end_user_id that escapes the directory makes
the export a way to write anywhere on the host.
"""
import re
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent.db.models import AgentFact, Tenant
from korely_agent.services import export_md



def tenant(db, name="T"):
    """Committed before any fact references it: there is no ORM relationship
    between the two tables, so SQLAlchemy cannot order the inserts itself."""
    t = Tenant(id=uuid.uuid4(), name=name)
    db.add(t)
    db.commit()
    return t


def fact(tenant_id, *, subject="c1", predicate="is_on", obj="Business plan",
         end_user="u1", since="2026-03-01", until=None):
    d = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    return AgentFact(
        id=uuid.uuid4(), tenant_id=tenant_id, end_user_id=end_user,
        subject_label=subject, predicate_raw=predicate,
        predicate_canonical=predicate, object_label=obj,
        valid_from=d(since), invalid_at=d(until) if until else None,
        confidence=0.9,
    )


class TestTheNameCannotEscape:
    @pytest.mark.parametrize("evil,forbidden", [
        ("../../etc/passwd", "/"),
        ("..", ".."),
        (".", "."),
        ("a/b/c", "/"),
        ("..\\..\\windows", "\\"),
        ("with space", " "),
        ("\x00null", "\x00"),
    ])
    def test_no_separator_survives(self, evil, forbidden):
        name = export_md.safe_name(evil)
        assert forbidden not in name or forbidden in ("..", ".")
        assert "/" not in name and "\\" not in name
        assert name not in (".", "..")

    def test_a_written_file_stays_inside_the_directory(self, db, tmp_path):
        t = tenant(db)
        db.add(fact(t.id, end_user="../../../../tmp/escaped"))
        db.commit()

        export_md.export_tenant(db, t.id, tmp_path)
        written = list(tmp_path.rglob("*.md"))
        assert written, "nothing was written"
        for p in written:
            assert tmp_path in p.parents or p.parent.parent == tmp_path
            assert p.resolve().is_relative_to(tmp_path.resolve())

    def test_the_original_id_is_still_in_the_file(self, db, tmp_path):
        t = tenant(db)
        db.add(fact(t.id, end_user="customer 4821/eu"))
        db.commit()
        export_md.export_tenant(db, t.id, tmp_path)
        body = next(tmp_path.rglob("*.md")).read_text()
        assert "customer 4821/eu" in body, "flattening the name lost it"

    def test_an_empty_id_does_not_produce_a_dotfile(self):
        assert not export_md.safe_name("").startswith(".")
        assert not export_md.safe_name(None).startswith(".")


class TestTheDiffStaysMeaningful:
    def test_the_same_facts_produce_the_same_bytes(self, db, tmp_path):
        t = tenant(db)
        db.add_all([fact(t.id), fact(t.id, predicate="lives_in", obj="Milan")])
        db.commit()

        assert export_md.export_tenant(db, t.id, tmp_path) == 1
        first = next(tmp_path.rglob("*.md")).read_text()
        mtime = next(tmp_path.rglob("*.md")).stat().st_mtime

        # A second pass with nothing changed must write nothing at all.
        assert export_md.export_tenant(db, t.id, tmp_path) == 0
        assert next(tmp_path.rglob("*.md")).read_text() == first
        assert next(tmp_path.rglob("*.md")).stat().st_mtime == mtime

    def test_a_new_fact_is_the_only_thing_that_changes(self, db, tmp_path):
        t = tenant(db)
        db.add(fact(t.id))
        db.commit()
        export_md.export_tenant(db, t.id, tmp_path)
        before = next(tmp_path.rglob("*.md")).read_text().splitlines()

        db.add(fact(t.id, predicate="lives_in", obj="Milan", since="2026-05-01"))
        db.commit()
        export_md.export_tenant(db, t.id, tmp_path)
        after = next(tmp_path.rglob("*.md")).read_text().splitlines()

        added = [l for l in after if l not in before]
        assert len(added) == 1 and "Milan" in added[0], f"noisy diff: {added}"

    def test_a_pipe_in_a_value_does_not_break_the_table(self, db, tmp_path):
        t = tenant(db)
        db.add(fact(t.id, obj="Plan A | Plan B"))
        db.commit()
        export_md.export_tenant(db, t.id, tmp_path)
        line = [l for l in next(tmp_path.rglob("*.md")).read_text().splitlines()
                if "Plan A" in l][0]
        assert "\\|" in line, "the pipe was not escaped"
        # Count only the delimiters: five cells means six unescaped pipes.
        delimiters = len(re.findall(r"(?<!\\)\|", line))
        assert delimiters == 6, f"the pipe shifted the columns: {line}"


class TestSupersededFactsStay:
    def test_a_closed_fact_is_kept_with_its_end_date(self, db, tmp_path):
        t = tenant(db)
        db.add_all([
            fact(t.id, obj="Priority Support", since="2026-03-15", until="2026-06-20"),
            fact(t.id, obj="Basic plan", since="2026-06-20"),
        ])
        db.commit()
        export_md.export_tenant(db, t.id, tmp_path)
        body = next(tmp_path.rglob("*.md")).read_text()

        assert "What is true now" in body and "What was true before" in body
        now, before = body.split("What was true before")
        assert "Basic plan" in now and "Priority Support" not in now
        assert "Priority Support" in before and "2026-06-20" in before

    def test_a_user_with_only_closed_facts_still_says_so(self, db, tmp_path):
        t = tenant(db)
        db.add(fact(t.id, until="2026-06-20"))
        db.commit()
        export_md.export_tenant(db, t.id, tmp_path)
        body = next(tmp_path.rglob("*.md")).read_text()
        assert "_nothing._" in body
        assert "What was true before" in body


class TestTenantsNeverShareAFolder:
    def test_two_tenants_with_the_same_end_user(self, db, tmp_path):
        a, b = tenant(db, "A"), tenant(db, "B")
        db.add(fact(a.id, obj="ZZALPHA", end_user="same-person"))
        db.add(fact(b.id, obj="ZZBETA", end_user="same-person"))
        db.commit()

        export_md.export_tenant(db, a.id, tmp_path)
        export_md.export_tenant(db, b.id, tmp_path)

        fa = (tmp_path / str(a.id) / "same-person.md").read_text()
        fb = (tmp_path / str(b.id) / "same-person.md").read_text()
        assert "ZZALPHA" in fa and "ZZBETA" not in fa
        assert "ZZBETA" in fb and "ZZALPHA" not in fb


class TestItIsOffAndHarmlessByDefault:
    def test_no_directory_means_no_work(self, db):
        assert export_md.export_all(db, None) == {}
        assert export_md.export_all(db, "") == {}

    def test_a_write_failure_does_not_raise(self, db, tmp_path, monkeypatch):
        """The export is derived. A full disk must not stop fact extraction."""
        t = tenant(db)
        db.add(fact(t.id))
        db.commit()

        def boom(*a, **k):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr("pathlib.Path.write_text", boom)
        assert export_md.export_tenant(db, t.id, tmp_path) == 0   # no raise


def test_a_file_that_cannot_be_written_logs_no_end_user(db, tmp_path, caplog):
    """The error line carried the file's path, named after the end user, twice
    (audit of 17 Oct 2026); the container's log is kept where no erasure
    reaches."""
    import logging
    import os

    t = tenant(db)
    db.add(fact(t.id, end_user="collaudo+maria@example.test"))
    db.commit()
    folder = tmp_path / str(t.id)
    folder.mkdir()
    blocker = folder / (export_md.safe_name("collaudo+maria@example.test") + ".md.tmp")
    blocker.mkdir()  # the rename target is a directory
    with caplog.at_level(logging.ERROR):
        export_md.export_tenant(db, t.id, tmp_path)
    text = caplog.text
    assert "could not write" in text
    assert "maria" not in text and "example.test" not in text
    os.rmdir(blocker)


class TestOneFilePerEndUser:
    """Two ids that flatten to one name were written to one file in turn, on
    every pass, and one of the two people never appeared (audit of 17 Oct
    2026)."""

    @pytest.mark.parametrize("a,b", [("repo:a", "repo_a"), ("anna@example.com", "anna_example.com"),
                                     ("Anna", "anna"), ("a" * 130, "a" * 131),
                                     (".anna", "anna"), ("", None), ("_no-user", None)])
    def test_two_ids_never_share_a_name(self, a, b):
        assert export_md.safe_name(a).lower() != export_md.safe_name(b).lower()

    def test_a_plain_id_is_its_own_name(self):
        assert export_md.safe_name("c4821") == "c4821"
        assert export_md.safe_name("maria.rossi-2") == "maria.rossi-2"

    def test_each_has_a_file_and_a_pass_changes_nothing(self, db, tmp_path):
        t = tenant(db)
        db.add_all([fact(t.id, end_user="repo:a", obj="Rome"),
                     fact(t.id, end_user="repo_a", obj="Milan")])
        db.commit()
        assert export_md.export_tenant(db, t.id, tmp_path) == 2
        bodies = sorted(p.read_text() for p in (tmp_path / str(t.id)).glob("*.md"))
        assert len(bodies) == 2
        assert any("# repo:a\n" in b and "Rome" in b and "Milan" not in b for b in bodies)
        assert any("# repo_a\n" in b and "Milan" in b and "Rome" not in b for b in bodies)
        assert export_md.export_tenant(db, t.id, tmp_path) == 0, "rewritten on every pass"


# ── an export walk in another process (the audit before 1.0.0) ──────────────
# The lock and the owed marks of export_tenant are the process's; a walk in
# another process (RUN_WORKER=false with a worker apart, several workers,
# API replicas sharing FACTS_EXPORT_DIR) read the store before an erasure
# committed and wrote the erased person's file after the erasure's own
# export removed it. Another process here is a second session that erases
# and exports with no lock of this one (`_export_tenant`).

GONE, KEPT = "collaudo-r6-gone", "collaudo-r6-keep"
# The other process runs the module as it ships, not as a test patches it.
_AS_SHIPPED = {n: getattr(export_md, n) for n in ("_end_users", "_facts_of")}


def _the_other_process(engine, tenant_id, root, *, only=None):
    """An erasure committed in another process, and its export there: every
    fact of GONE, or only the fact whose object is `only`."""
    from sqlalchemy.orm import sessionmaker

    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        q = s.query(AgentFact).filter(AgentFact.tenant_id == tenant_id,
                                      AgentFact.end_user_id == GONE)
        if only is not None:
            q = q.filter(AgentFact.object_label == only)
        assert q.delete(synchronize_session=False)
        s.commit()
        patched = {n: getattr(export_md, n) for n in _AS_SHIPPED}
        try:
            for n, f in _AS_SHIPPED.items():
                setattr(export_md, n, f)
            export_md._export_tenant(s, tenant_id, root / str(tenant_id))
        finally:
            for n, f in patched.items():
                setattr(export_md, n, f)
    finally:
        s.close()


def _seeded(db, tmp_path):
    t = tenant(db)
    db.add_all([fact(t.id, end_user=KEPT, obj="Rome"),
                fact(t.id, end_user=GONE, obj="Zzsecretplace"),
                fact(t.id, end_user=GONE, obj="Milan", subject="c2")])
    db.commit()
    export_md.export_tenant(db, t.id, tmp_path)
    return t


def _files(tmp_path, t):
    return {p.name: p.read_text() for p in (tmp_path / str(t.id)).glob("*")}


def test_a_walk_that_listed_an_erased_person_writes_no_file_for_them(db, engine, tmp_path,
                                                                     monkeypatch):
    """check-09: the walk's page of end users read before the erasure; the
    erasure and its export in another process; then the walk writes. The
    erased person has no fact left, so no file: until the audit before 1.0.0 the walk
    wrote "# <id>" and "_nothing._" and kept it."""
    t = _seeded(db, tmp_path)
    real = export_md._end_users

    def listed_then_erased(db_, tenant_id):
        page = list(real(db_, tenant_id))
        assert GONE in page
        _the_other_process(engine, tenant_id, tmp_path)
        assert f"{GONE}.md" not in _files(tmp_path, t), "the erasure's export removed it"
        yield from page

    monkeypatch.setattr(export_md, "_end_users", listed_then_erased)
    export_md.export_tenant(db, t.id, tmp_path)
    assert set(_files(tmp_path, t)) == {f"{KEPT}.md"}


@pytest.mark.parametrize("erased", ["the person", "one fact"])
def test_a_walk_that_read_facts_before_the_erasure_leaves_none_of_them(db, engine, tmp_path,
                                                                       monkeypatch, erased):
    """The walk read the person's facts just before the erasure committed in
    another process, whose export then removed (or rewrote) the file; the
    walk writes after it. What it wrote is read again from the store: no
    erased fact stays on disk once the walk ends, whether the person was
    erased or one memory of theirs."""
    t = _seeded(db, tmp_path)
    real = export_md._facts_of
    once = []

    def read_then_erased(db_, tenant_id, end_user_id, *, live):
        rows = list(real(db_, tenant_id, end_user_id, live=live))
        if end_user_id == GONE and live and not once:
            once.append(True)
            _the_other_process(engine, tenant_id, tmp_path,
                               only=None if erased == "the person" else "Zzsecretplace")
        return iter(rows)

    monkeypatch.setattr(export_md, "_facts_of", read_then_erased)
    export_md.export_tenant(db, t.id, tmp_path)
    files = _files(tmp_path, t)
    assert not any("Zzsecretplace" in body for body in files.values()), files
    if erased == "the person":
        assert set(files) == {f"{KEPT}.md"}
    else:
        assert "Milan" in files[f"{GONE}.md"]


def test_a_person_written_to_all_along_keeps_the_last_write(db, tmp_path, monkeypatch):
    """Review of the audit before 1.0.0: a file the walk could not settle in three
    writes (its person's facts changing under every read) was removed,
    although the person has facts. It keeps the last write; the next pass
    writes what came after."""
    t = tenant(db)
    db.add(fact(t.id, end_user=KEPT, obj="Rome"))
    db.commit()
    calls = []

    def busy(db_, tenant_id, end_user_id):
        calls.append(1)
        return f"# {end_user_id}\nversion {len(calls)}\n"

    monkeypatch.setattr(export_md, "_file_of", busy)
    export_md.export_tenant(db, t.id, tmp_path)
    files = _files(tmp_path, t)
    assert set(files) == {f"{KEPT}.md"}
    assert files[f"{KEPT}.md"].startswith(f"# {KEPT}\nversion ")
