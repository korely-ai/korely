"""The erasures are kept beside the dumps, and a restore makes them again from there.

restore.sh read the erasures made after a dump from the live database only:
when that database is lost with its disk, the people erased after the dump
came back with it, and SKIP_ERASURES=1 restored nothing of them (the
verification of 1 Nov 2026). Now backup.sh writes every erasure record beside
each dump, and hourly on its own (`backup.sh erasures`), and restore.sh makes
again every one of those files after the live database's, the oldest
first; backup.sh removes a file only once the newest holds all its records
(the review of 16 Nov 2026: the newest by name was the only one made again,
and the rotation kept the last 48 by name, so after a loss the exports of an
empty database hid, then rotated away, the good files). Played with a
docker on PATH that logs its calls and answers in place of compose, and
runs `manage erasures-superseded` for real."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDS = '{"version": 2, "key_check": null, "records": []}'


def _docker(tmp_path, cases: str) -> dict:
    """A docker that logs each call (and what an `erasures-replay` reads)
    and plays `cases`, sh case arms."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text(
        '#!/bin/sh\necho "$*" >> "$CALLS"\ncase "$*" in\n'
        '  *erasures-replay*) cat >> "$CALLS.replayed"; echo >> "$CALLS.replayed" ;;\n'
        + cases +
        '  *erasures-superseded*) "$PY" -m korely_agent.manage erasures-superseded ;;\n'
        'esac\n')
    (fake / "docker").chmod(0o755)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", FORCE="1", PY=sys.executable,
                CALLS=str(tmp_path / "calls"), TMPDIR=str(tmp))


def _run(script, tmp_path, env, *args):
    return subprocess.run(["sh", str(ROOT / "deploy" / script), *args], env=env,
                          capture_output=True, text=True, cwd=tmp_path)


# ── backup.sh ─────────────────────────────────────────────────────────────────

DUMPS = ('  *pg_dump*) head -c 2000 /dev/zero ;;\n'
         f"  *erasures-export*) echo '{RECORDS}' ;;\n"
         '  *python\\ -c*) cat >/dev/null ;;\n')


def test_a_backup_keeps_the_erasures_beside_its_dump_for_its_owner_only(tmp_path):
    env = _docker(tmp_path, DUMPS)
    r = _run("backup.sh", tmp_path, env)
    assert r.returncode == 0, r.stderr
    [dump] = (tmp_path / "backups").glob("korely-2*.dump")
    [kept] = (tmp_path / "backups").glob("korely-erasures-*.json")
    assert json.loads(kept.read_text()) == json.loads(RECORDS)
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert kept.name.split("-")[-1][:-5] == dump.name.split("-")[-1][:-5], "one time for both"
    calls = (tmp_path / "calls").read_text()
    assert "exec -T api python -m korely_agent.manage erasures-export" in calls
    assert not list((tmp_path / "backups").glob("*.partial"))


def _file(*ids) -> str:
    return json.dumps({"version": 2, "key_check": None, "records": [{"id": i} for i in ids]})


def _exporting(*ids) -> str:
    """The fake docker's arms: a backup whose export holds these records."""
    return DUMPS.replace(f"echo '{RECORDS}'", f"echo '{_file(*ids)}'")


def test_the_erasures_alone_every_hour_and_the_files_the_newest_holds_go(tmp_path):
    env = _docker(tmp_path, _exporting("a", "b", "c"))
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "korely-erasures-20261101T010000Z.json").write_text(_file("a"))
    (backups / "korely-erasures-20261101T020000Z.json").write_text(_file("a", "b"))
    r = _run("backup.sh", tmp_path, env, "erasures")
    assert r.returncode == 0, r.stderr
    assert "pg_dump" not in (tmp_path / "calls").read_text(), "no dump in the hourly run"
    [left] = backups.glob("korely-erasures-*.json")
    assert [rec["id"] for rec in json.loads(left.read_text())["records"]] == ["a", "b", "c"]
    assert not list(backups.glob("korely-2*.dump"))


def test_after_a_loss_the_files_holding_what_the_newest_lacks_stay(tmp_path):
    """The database lost, a new empty one, and the hourly export goes on:
    its files go one after the other, the good one stays."""
    env = _docker(tmp_path, _exporting())
    backups = tmp_path / "backups"
    backups.mkdir()
    good = backups / "korely-erasures-20261101T010000Z.json"
    good.write_text(_file("a", "b"))
    for hour in (2, 3):
        (backups / f"korely-erasures-20261101T0{hour}0000Z.json").write_text(_file())
    r = _run("backup.sh", tmp_path, env, "erasures")
    assert r.returncode == 0, r.stderr
    names = sorted(f.name for f in backups.glob("korely-erasures-*.json"))
    assert names[0] == good.name and len(names) == 2, names
    assert "kept 1 older file(s) of erasures holding records the newest lacks" in r.stderr


def test_a_damaged_file_of_erasures_is_set_aside_and_the_restore_says_so(tmp_path):
    """A file that is no longer one of erasures was kept for ever, and every
    restore failed on it with the API stopped and a command that failed
    again (the review of 16 Nov 2026). The hourly run sets it aside under
    .unreadable and fails, saying so; a restore does not make it again, and
    names it."""
    env = _docker(tmp_path, _exporting("a") + '  *credentials-export*) echo \'{"keys": []}\' ;;\n')
    backups = tmp_path / "backups"
    backups.mkdir()
    damaged = backups / "korely-erasures-20261101T040000Z.json"
    damaged.write_text("{")
    r = _run("backup.sh", tmp_path, env, "erasures")
    assert r.returncode == 1 and f"{backups.name}/{damaged.name} is not a file of erasures" in r.stderr
    assert not damaged.exists() and (backups / (damaged.name + ".unreadable")).read_text() == "{"
    [kept] = backups.glob("korely-erasures-*.json")

    dump = backups / "korely-20261101T032000Z.dump"
    dump.write_bytes(b"not a dump")
    r = _run("restore.sh", tmp_path, env, str(dump))
    assert r.returncode == 0, r.stderr
    assert f"{damaged.name}.unreadable could not be read when backup.sh set it aside" in r.stderr
    assert len(_replayed(tmp_path)) == 2, "the live database's and the file kept, not the damaged one"


def test_only_what_is_set_aside_fails_the_run(tmp_path):
    """The tar of macOS adds an AppleDouble member (._<name>) for a file
    with extended attributes, which the rotation names as a file it cannot
    read: the run failed for it, with no word and nothing set aside (the
    upgrade check of 16 Nov 2026). The archive is plain, and a name the
    rotation gives that is none of the files kept fails nothing."""
    env = _docker(tmp_path, _exporting("a"))
    fake = tmp_path / "bin" / "docker"
    fake.write_text(fake.read_text().replace(
        '"$PY" -m korely_agent.manage erasures-superseded ;;',
        '"$PY" -m korely_agent.manage erasures-superseded;'
        ' echo "unreadable ._korely-erasures-20261101T010000Z.json" ;;'))
    backups = tmp_path / "backups"
    backups.mkdir()
    r = _run("backup.sh", tmp_path, env, "erasures")
    assert r.returncode == 0, r.stderr
    assert "COPYFILE_DISABLE=1 tar" in (ROOT / "deploy" / "backup.sh").read_text()
    assert not list(backups.glob("*.unreadable"))


def test_a_file_the_replay_cannot_read_is_named_with_what_to_do(tmp_path):
    """Damaged since the last hourly run, it is still among the files kept:
    the replay reads nothing of it, and running it again changes nothing, so
    the restore names it apart, with what to do instead, the API stopped."""
    backups = tmp_path / "backups"
    backups.mkdir()
    damaged = backups / "korely-erasures-20261101T090000Z.json"
    damaged.write_text("DAMAGED")
    dump = backups / "korely-20261101T032000Z.dump"
    dump.write_bytes(b"not a dump")
    env = _docker(tmp_path, '  *erasures-export*) echo "[]" ;;\n'
                            '  *credentials-export*) echo \'{"keys": []}\' ;;\n')
    fake = tmp_path / "bin" / "docker"
    fake.write_text(fake.read_text().replace(
        '  *erasures-replay*) cat >> "$CALLS.replayed"; echo >> "$CALLS.replayed" ;;\n',
        '  *erasures-replay*) cat > "$CALLS.in"; cat "$CALLS.in" >> "$CALLS.replayed";'
        ' echo >> "$CALLS.replayed"; if grep -q DAMAGED "$CALLS.in"; then'
        ' echo "erasures-replay: stdin is not the JSON erasures-export wrote" >&2; exit 1; fi ;;\n'))
    r = _run("restore.sh", tmp_path, env, str(dump))
    assert r.returncode == 1
    assert "start api" not in (tmp_path / "calls").read_text()
    assert "The replay read nothing of these files of erasures" in r.stderr
    assert f"./backups/{damaged.name}" in r.stderr and "replay a good copy of it" in r.stderr
    assert f"erasures-replay < ./backups/{damaged.name}" not in r.stderr, \
        "not the command that fails again"


def test_an_older_file_goes_only_when_the_newest_holds_its_records_by_id(tmp_path):
    """The newest holding as many records as the older one is not holding
    them: the older one stays until its records, by id, are in the newest."""
    for newest, stays in ((("c", "d"), True), (("a", "b", "c"), False)):
        here = tmp_path / ("as-many" if stays else "holds-them")
        here.mkdir()
        env = _docker(here, _exporting(*newest))
        backups = here / "backups"
        backups.mkdir()
        older = backups / "korely-erasures-20261101T010000Z.json"
        older.write_text(_file("a", "b"))
        r = _run("backup.sh", here, env, "erasures")
        assert r.returncode == 0, r.stderr
        assert older.exists() == stays, newest


def test_a_rotation_that_cannot_read_the_files_removes_none(tmp_path):
    env = _docker(tmp_path, '  *erasures-superseded*) exit 1 ;;\n' + _exporting("a"))
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "korely-erasures-20261101T010000Z.json").write_text(_file("a"))
    r = _run("backup.sh", tmp_path, env, "erasures")
    assert r.returncode == 1 and "none removed" in r.stderr
    assert len(list(backups.glob("korely-erasures-*.json"))) == 2, "the new one kept, the old too"


def test_an_export_that_fails_is_never_the_newest_file(tmp_path):
    """The newest file is the one a restore reads: an export that failed, or
    that wrote what restore.sh cannot read, leaves none and fails the run."""
    env = _docker(tmp_path, '  *erasures-export*) echo "{"; exit 3 ;;\n')
    r = _run("backup.sh", tmp_path, env, "erasures")
    assert r.returncode == 1 and "FAILED" in r.stderr
    assert not list((tmp_path / "backups").glob("korely-erasures-*"))

    again = tmp_path / "again"
    again.mkdir()
    env = _docker(again, "  *erasures-export*) echo '{}' ;;\n  *python\\ -c*) exit 1 ;;\n")
    r = _run("backup.sh", again, env, "erasures")
    assert r.returncode == 1 and "FAILED" in r.stderr
    assert not list((again / "backups").glob("korely-erasures-*"))


# ── restore.sh ────────────────────────────────────────────────────────────────

def _backups_with(tmp_path, *names):
    backups = tmp_path / "backups"
    backups.mkdir()
    for name in names:
        (backups / name).write_text(json.dumps({"version": 2, "key_check": None,
                                                "records": [{"id": name}]}))
    dump = backups / "korely-20261101T032000Z.dump"
    dump.write_bytes(b"not a dump")
    return dump


def _replayed(tmp_path) -> list:
    return [json.loads(line) for line in (tmp_path / "calls.replayed").read_text().split("\n")
            if line.strip()]


def test_with_the_live_database_gone_every_kept_file_is_made_again_the_oldest_first(tmp_path):
    dump = _backups_with(tmp_path, "korely-erasures-20261101T090000Z.json",
                         "korely-erasures-20261101T100000Z.json")
    env = _docker(tmp_path, '  *-export*) exit 1 ;;\n')
    r = _run("restore.sh", tmp_path, dict(env, SKIP_ERASURES="1"), str(dump))
    assert r.returncode == 0, r.stderr
    live, older, newest = _replayed(tmp_path)
    assert live == [], "the live database could not be read"
    assert [rec["id"] for rec in older["records"]] == ["korely-erasures-20261101T090000Z.json"]
    assert [rec["id"] for rec in newest["records"]] == ["korely-erasures-20261101T100000Z.json"]
    assert "2 file(s)" in r.stderr and "korely-erasures-20261101T100000Z.json" in r.stderr
    assert "any made after that file was written are not" in r.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    replay = max(i for i, c in enumerate(calls) if "erasures-replay" in c)
    assert replay < next(i for i, c in enumerate(calls) if "start api" in c), \
        "the erasures are made again before the API opens"


def test_a_newest_file_of_an_empty_database_does_not_hide_the_good_one(tmp_path):
    """The volume lost, compose made an empty one, the API started again and
    the hourly line kept an export with no record: the live database reads,
    and holds nothing. The good file before it is made again all the same."""
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "korely-erasures-20261101T090000Z.json").write_text(_file("maria", "carla"))
    (backups / "korely-erasures-20261101T100000Z.json").write_text(_file())
    dump = backups / "korely-20261101T032000Z.dump"
    dump.write_bytes(b"not a dump")
    env = _docker(tmp_path, '  *erasures-export*) echo "[]" ;;\n'
                            '  *credentials-export*) echo \'{"keys": []}\' ;;\n')
    r = _run("restore.sh", tmp_path, env, str(dump))
    assert r.returncode == 0, r.stderr
    made = [[rec["id"] for rec in (f if isinstance(f, list) else f["records"])]
            for f in _replayed(tmp_path)]
    assert made == [[], ["maria", "carla"], []]
    assert "start api" in (tmp_path / "calls").read_text()


def test_with_the_live_database_there_both_are_made_again_the_live_first(tmp_path):
    dump = _backups_with(tmp_path, "korely-erasures-20261101T100000Z.json")
    env = _docker(tmp_path, '  *erasures-export*) echo \'[{"id": "live"}]\' ;;\n'
                            '  *credentials-export*) echo \'{"keys": []}\' ;;\n')
    r = _run("restore.sh", tmp_path, env, str(dump))
    assert r.returncode == 0, r.stderr
    replayed = _replayed(tmp_path)
    assert replayed[0] == [{"id": "live"}]
    assert [rec["id"] for rec in replayed[1]["records"]] == ["korely-erasures-20261101T100000Z.json"]


def test_a_file_named_by_hand_is_the_one_made_again(tmp_path):
    """One copied back from off the machine, the folder lost with the disk."""
    dump = _backups_with(tmp_path, "korely-erasures-20261101T100000Z.json")
    mine = tmp_path / "from-the-bucket.json"
    mine.write_text(json.dumps({"version": 2, "key_check": None, "records": [{"id": "mine"}]}))
    env = _docker(tmp_path, '  *-export*) exit 1 ;;\n')
    r = _run("restore.sh", tmp_path, dict(env, SKIP_ERASURES="1", ERASURES_FILE=str(mine)),
             str(dump))
    assert r.returncode == 0, r.stderr
    live, only = _replayed(tmp_path)
    assert [rec["id"] for rec in only["records"]] == ["mine"], "that file alone"

    r = _run("restore.sh", tmp_path, dict(env, ERASURES_FILE=str(tmp_path / "missing.json")),
             str(dump))
    assert r.returncode == 1 and "no such file" in r.stderr


def test_a_failed_replay_of_the_file_keeps_the_api_stopped_and_says_how(tmp_path):
    dump = _backups_with(tmp_path, "korely-erasures-20261101T100000Z.json")
    env = _docker(tmp_path, '  *erasures-export*) echo "[]" ;;\n'
                            '  *credentials-export*) echo \'{"keys": []}\' ;;\n')
    # The second erasures-replay (the file's) fails.
    fake = tmp_path / "bin" / "docker"
    fake.write_text(fake.read_text().replace(
        '  *erasures-replay*) cat >> "$CALLS.replayed"; echo >> "$CALLS.replayed" ;;\n',
        '  *erasures-replay*) cat >> "$CALLS.replayed"; echo >> "$CALLS.replayed";'
        ' [ "$(grep -c erasures-replay "$CALLS")" -lt 2 ] || exit 4 ;;\n'))
    r = _run("restore.sh", tmp_path, env, str(dump))
    assert r.returncode == 1
    assert "start api" not in (tmp_path / "calls").read_text()
    assert "erasures-replay < ./backups/korely-erasures-20261101T100000Z.json" in r.stderr


# pg_restore as the fake plays it: `--list` reads the head of the archive
# only, every other run reads all of it, and refuses one cut short; an
# archive here begins with PGDMP and ends with END.
PG_RESTORE = ('  *pg_restore\\ --list*) head -c 5 | grep -q PGDMP || {'
              ' echo "pg_restore: error: input file does not appear to be a valid archive" >&2;'
              ' exit 1; } ;;\n'
              '  *pg_restore*) cat > "$CALLS.dump"; head -c 5 "$CALLS.dump" | grep -q PGDMP || {'
              ' echo "pg_restore: error: input file does not appear to be a valid archive" >&2;'
              ' exit 1; }; tail -c 3 "$CALLS.dump" | grep -q END || {'
              ' echo "pg_restore: error: could not read input file: end of file" >&2; exit 1; } ;;\n')
EXPORTS = ('  *erasures-export*) echo "[]" ;;\n'
           '  *credentials-export*) echo \'{"keys": []}\' ;;\n')
WHOLE = b"PGDMP" + bytes(2000) + b"END"


def test_a_file_of_erasures_given_as_the_dump_stops_nothing(tmp_path):
    """The files of erasures sit beside the dumps under the same prefix. The
    upgrade check of 16 Nov 2026 gave restore.sh the one backup.sh printed
    last: the script stopped the API, pg_restore refused the file with the
    database untouched, and the API stayed stopped under a message saying the
    database might hold part of the dump. pg_restore reads what it is given
    before anything is stopped."""
    backups = tmp_path / "backups"
    backups.mkdir()
    erasures = backups / "korely-erasures-20261116T032000Z.json"
    erasures.write_text(_file("maria"))
    env = _docker(tmp_path, EXPORTS + PG_RESTORE)
    r = _run("restore.sh", tmp_path, env, str(erasures))
    assert r.returncode == 1
    calls = (tmp_path / "calls").read_text()
    assert "stop api" not in calls and "export" not in calls, "nothing stopped, nothing read"
    assert f"{erasures} is not a whole dump pg_restore can read" in r.stderr
    assert "Nothing was stopped or changed" in r.stderr
    assert not list(tmp_path.glob("*.json")) and not list((tmp_path / "tmp").iterdir())

    dump = backups / "korely-20261116T032000Z.dump"
    dump.write_bytes(WHOLE)
    r = _run("restore.sh", tmp_path, env, str(dump))
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    checked = next(i for i, c in enumerate(calls) if "pg_restore -f /dev/null" in c)
    assert checked < next(i for i, c in enumerate(calls) if "stop api" in c)
    assert "start api" in calls[-1]


def test_a_dump_cut_short_stops_nothing(tmp_path):
    """The review of 16 Nov 2026: `pg_restore --list` reads only the table
    of contents, so a dump cut to 99% passed it, the API stopped, and the
    restore left the database half replaced. The check reads all of it."""
    backups = tmp_path / "backups"
    backups.mkdir()
    dump = backups / "korely-20261116T032000Z.dump"
    dump.write_bytes(WHOLE[:len(WHOLE) * 99 // 100])
    env = _docker(tmp_path, EXPORTS + PG_RESTORE)
    r = _run("restore.sh", tmp_path, env, str(dump))
    assert r.returncode == 1
    assert "stop api" not in (tmp_path / "calls").read_text()
    assert "end of file" in r.stderr and "Nothing was stopped or changed" in r.stderr


def test_a_restore_that_did_not_finish_is_named_or_nothing_starts(tmp_path):
    """The review of 16 Nov 2026: after a restore that did not finish, the
    database is half replaced, and the files it kept in the install folder
    are the only copy of what the live one held. A restore run again read
    the half-replaced database as the live one, and never made them again.
    Now it stops before anything while they are there unnamed, and named it
    reads them in place of the database, makes them again, and removes them."""
    backups = tmp_path / "backups"
    backups.mkdir()
    dump = backups / "korely-20261116T032000Z.dump"
    dump.write_bytes(WHOLE)
    kept_e = tmp_path / "erasures-20261116T040000Z.json"
    kept_c = tmp_path / "credentials-20261116T040000Z.json"
    kept_e.write_text('[{"id": "live"}]')
    kept_c.write_text('{"keys": ["live"]}')
    env = _docker(tmp_path, '  *erasures-export*) echo \'[{"id": "half"}]\' ;;\n'
                            '  *credentials-export*) echo \'{"keys": ["half"]}\' ;;\n'
                            '  *credentials-replay*) cat >> "$CALLS.credentials" ;;\n' + PG_RESTORE)
    r = _run("restore.sh", tmp_path, env, str(dump))
    assert r.returncode == 1 and "a restore that did not finish kept" in r.stderr
    assert f"./{kept_e.name}" in r.stderr and f"./{kept_c.name}" in r.stderr
    assert not (tmp_path / "calls").exists(), "nothing stopped, nothing read"

    r = _run("restore.sh", tmp_path, dict(env, KEPT_ERASURES=f"./{kept_e.name}"), str(dump))
    assert r.returncode == 1 and f"./{kept_c.name}" in r.stderr, "each one is named"

    r = _run("restore.sh", tmp_path, dict(env, KEPT_ERASURES=f"./{kept_e.name}",
                                          KEPT_CREDENTIALS=str(kept_c)), str(dump))
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "calls").read_text()
    assert "-export" not in calls, "the database here is not read as the live one"
    assert _replayed(tmp_path)[0] == [{"id": "live"}]
    assert (tmp_path / "calls.credentials").read_text().strip() == '{"keys": ["live"]}'
    assert not kept_e.exists() and not kept_c.exists(), "made again, and removed"
    assert "start api" in calls.splitlines()[-1]


def test_a_restore_that_fails_again_keeps_the_files_it_was_given(tmp_path):
    """The kept files named, and the restore fails again (pg_restore stops on
    the dump): what it keeps is the same two files, with the live
    database's words, never a second pair made from the half-replaced one,
    which a run after would refuse as kept and unnamed."""
    backups = tmp_path / "backups"
    backups.mkdir()
    dump = backups / "korely-20261116T032000Z.dump"
    dump.write_bytes(WHOLE)
    kept_e = tmp_path / "erasures-20261116T040000Z.json"
    kept_c = tmp_path / "credentials-20261116T040000Z.json"
    kept_e.write_text('[{"id": "live"}]')
    kept_c.write_text('{"keys": ["live"]}')
    env = _docker(tmp_path, '  *-export*) echo \'["half"]\' ;;\n'
                            '  *pg_restore\\ -f\\ /dev/null*) cat >/dev/null ;;\n'
                            '  *pg_restore*) cat >/dev/null; [ -f "$CALLS.whole" ] || {'
                            ' echo "pg_restore: error: could not execute query" >&2; exit 1; } ;;\n')
    named = dict(env, KEPT_ERASURES=f"./{kept_e.name}", KEPT_CREDENTIALS=f"./{kept_c.name}")
    r = _run("restore.sh", tmp_path, named, str(dump))
    assert r.returncode == 1 and "pg_restore reported errors" in r.stderr
    assert sorted(f.name for f in tmp_path.glob("*.json")) == sorted([kept_c.name, kept_e.name])
    assert kept_e.read_text() == '[{"id": "live"}]' and kept_c.read_text() == '{"keys": ["live"]}'
    assert f"KEPT_ERASURES=./{kept_e.name} KEPT_CREDENTIALS=./{kept_c.name}" in r.stderr

    (tmp_path / "calls.whole").touch()
    r = _run("restore.sh", tmp_path, named, str(dump))
    assert r.returncode == 0, r.stderr
    assert _replayed(tmp_path)[0] == [{"id": "live"}]
    assert not list(tmp_path.glob("*.json"))
