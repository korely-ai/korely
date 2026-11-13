"""The credential changes made after a dump survive its restore (audit of 17
Oct 2026, S1).

restore.sh put the credentials back as the dump had them: a key revoked since
worked again, an old password let its owner in again, a session ended since
resolved again, a rotated webhook secret signed again, and the OAuth codes
and refresh tokens already spent were unspent. Now the live database's state
is kept before the restore and made again after it, and every session and
OAuth grant ends whatever it said (services/credential_replay.py). A restore
is played here by putting the rows back as the dump had them."""
import signal
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from korely_agent import manage
from korely_agent.db.models import (AgentWebhookEndpoint, AuditEvent, OAuthClient, OAuthGrant,
                                    OperatorSession)
from korely_agent.services import credential_replay
from korely_agent.services import keys as key_service
from korely_agent.services import operators as operator_service

ROOT = Path(__file__).resolve().parent.parent


def _key(db, tenant, **kw):
    row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}", **kw)
    return row, raw


def _hook(db, tenant, secret="whsec_dump"):
    ep = AgentWebhookEndpoint(id=uuid.uuid4(), tenant_id=tenant.id, url="https://example.com/hook",
                              secret=secret, events=["*"])
    db.add(ep)
    db.commit()
    return ep


class TestTheChangesSinceTheDumpAreMadeAgain:
    def test_a_key_revoked_after_the_dump_is_revoked_again(self, db, tenant):
        row, raw = _key(db, tenant)
        key_service.revoke_api_key(db, key_id=row.id, tenant_id=tenant.id)
        live = credential_replay.export(db)

        row.revoked_at = None  # the dump
        db.commit()
        assert key_service.verify_api_key(db, raw) is not None, "the restore brought it back"

        out = credential_replay.replay(db, live)
        assert out["keys_revoked"] == 1
        assert key_service.verify_api_key(db, raw) is None
        assert db.query(AuditEvent).filter(AuditEvent.action == "key_revoke",
                                           AuditEvent.target_id == str(row.id)).count() == 1

    def test_the_password_in_force_before_the_restore_stays(self, db, tenant):
        op, old = operator_service.create(db, tenant_id=tenant.id, username="anna")
        dump_hash = op.password_hash
        operator_service.set_password(db, op, "the-new-one-after-a-leak")
        live = credential_replay.export(db)

        op.password_hash = dump_hash  # the dump
        db.commit()
        out = credential_replay.replay(db, live)

        assert out["passwords"] == 1
        db.refresh(op)
        assert operator_service.verify_password("the-new-one-after-a-leak", op.password_hash)
        assert not operator_service.verify_password(old, op.password_hash)

    def test_an_operator_disabled_or_no_longer_admin_stays_so(self, db, tenant):
        op, _ = operator_service.create(db, tenant_id=tenant.id, username="bruno")
        op.disabled_at = datetime.now(timezone.utc)
        op.install_admin = False
        db.commit()
        live = credential_replay.export(db)

        op.disabled_at, op.install_admin = None, True  # the dump
        db.commit()
        out = credential_replay.replay(db, live)

        db.refresh(op)
        assert op.disabled_at is not None and op.install_admin is False
        assert out["operators_disabled"] == 1 and out["install_admin_removed"] == 1

    def test_a_rotated_secret_stays_and_a_deleted_endpoint_is_off(self, db, tenant):
        rotated, gone = _hook(db, tenant), _hook(db, tenant)
        rotated.secret = "whsec_rotated"
        db.delete(gone)
        db.commit()
        live = credential_replay.export(db)

        rotated.secret = "whsec_dump"  # the dump
        db.add(AgentWebhookEndpoint(id=gone.id, tenant_id=tenant.id, url=gone.url,
                                    secret="whsec_dump", events=["*"]))
        db.commit()
        out = credential_replay.replay(db, live)

        db.expire_all()
        assert db.get(AgentWebhookEndpoint, rotated.id).secret == "whsec_rotated"
        assert db.get(AgentWebhookEndpoint, gone.id).active is False
        assert out["webhook_secrets"] == 1 and out["webhooks_off"] == 1

    def test_a_key_moved_after_the_dump_stays_where_it_was_moved(self, db, tenant):
        """Moving a key is how an operator narrows it without revoking it,
        and the restore put it back in the project it had been taken out of,
        with nothing in the listing to show it (audit of 18 Oct 2026)."""
        from korely_agent.services import projects as project_service

        a = project_service.get_or_create_default_project(db, tenant.id)
        b = project_service.create_project(db, tenant_id=tenant.id, name="Project B")
        row, raw = _key(db, tenant, project_id=a.id)
        assert key_service.move_key(db, key_id=row.id, tenant_id=tenant.id, project_id=b.id)
        live = credential_replay.export(db)

        row.project_id = a.id  # the dump
        db.commit()
        out = credential_replay.replay(db, live)

        db.expire_all()
        assert key_service.verify_api_key(db, raw).project_id == b.id
        assert out["keys_moved"] == 1
        assert db.query(AuditEvent).filter(AuditEvent.action == "key_change",
                                           AuditEvent.target_id == str(row.id)).count() == 1

    def test_a_key_moved_into_a_project_the_dump_does_not_hold_is_revoked(self, db, tenant):
        """Its live project is not in the restored database, and the
        project of the dump is the one it was taken out of: the closed side
        is no project at all."""
        from korely_agent.db.models import AgentApiKey
        from korely_agent.services import projects as project_service

        a = project_service.get_or_create_default_project(db, tenant.id)
        row, raw = _key(db, tenant, project_id=a.id)
        live = credential_replay.export(db)
        live["keys"] = [dict(k, project=str(uuid.uuid4())) if k["id"] == str(row.id) else k
                        for k in live["keys"]]  # moved, live, into a project made after the dump
        out = credential_replay.replay(db, live)

        db.expire_all()
        assert key_service.verify_api_key(db, raw) is None
        assert db.get(AgentApiKey, row.id).project_id == a.id, "revoked, not moved"
        assert out["keys_revoked"] == 1 and out["keys_moved"] == 0

    def test_an_export_of_before_the_project_was_kept_moves_nothing(self, db, tenant):
        """A file written by an older credentials-export has no project: the
        key stays where the dump has it, which is what the restore did."""
        from korely_agent.services import projects as project_service

        a = project_service.get_or_create_default_project(db, tenant.id)
        row, raw = _key(db, tenant, project_id=a.id)
        live = credential_replay.export(db)
        for k in live["keys"]:
            k.pop("project")
        out = credential_replay.replay(db, live)
        assert key_service.verify_api_key(db, raw).project_id == a.id
        assert out["keys_moved"] == 0

    def test_nothing_is_reopened(self, db, tenant):
        """The replay closes; a key the dump has revoked stays revoked even
        when the live database had it live."""
        row, raw = _key(db, tenant)
        live = credential_replay.export(db)
        key_service.revoke_api_key(db, key_id=row.id, tenant_id=tenant.id)
        credential_replay.replay(db, live)
        assert key_service.verify_api_key(db, raw) is None


class TestEverySessionAndGrantEnds:
    def test_sessions_codes_refresh_tokens_and_app_keys_end(self, db, tenant):
        op, _ = operator_service.create(db, tenant_id=tenant.id, username="carla")
        _session, token = operator_service.open_session(db, op)
        db.add(OAuthClient(client_id="claude", info={}))
        db.commit()
        app_key, app_raw = _key(db, tenant, oauth_client_id="claude",
                                expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        for kind in ("request", "code", "refresh"):
            db.add(OAuthGrant(kind=kind, token_hash=uuid.uuid4().hex, client_id="claude",
                              tenant_id=tenant.id, key_id=app_key.id, scopes=["memories:read"],
                              expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
        db.commit()
        person_key, person_raw = _key(db, tenant)
        assert operator_service.resolve(db, token) is not None

        # Even with nothing read from the live database (SKIP_ERASURES=1).
        out = credential_replay.replay(db, {})

        assert operator_service.resolve(db, token) is None
        assert db.query(OperatorSession).count() == 0 and db.query(OAuthGrant).count() == 0
        assert key_service.verify_api_key(db, app_raw) is None, "the app connects again"
        assert key_service.verify_api_key(db, person_raw) is not None, "a person's key is not an app's"
        assert out["sessions_ended"] == 1 and out["oauth_grants_ended"] == 3
        assert out["oauth_keys_revoked"] == 1

    def test_the_command_prints_what_can_sign_in(self, db, tenant, monkeypatch, capsys):
        import io

        monkeypatch.setattr(manage, "_session", lambda: db)
        row, _ = _key(db, tenant)
        operator_service.create(db, tenant_id=tenant.id, username="dora")
        monkeypatch.setattr("sys.stdin", io.StringIO(credential_replay.dumps(credential_replay.export(db))))
        assert manage.main(["credentials-replay"]) == 0
        out = capsys.readouterr().out
        assert "What can sign in now" in out
        assert str(row.id) in out and "dora" in out and str(tenant.id) in out
        assert "project Default" in out, "the project a key reads and writes is shown"
        assert "everybody signs in again" in out

    def test_the_command_refuses_what_is_not_the_export(self, monkeypatch, capsys):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("Applying migrations...\n{}"))
        assert manage.main(["credentials-replay"]) == 1
        assert "not the JSON" in capsys.readouterr().err


class TestRestoreRunsIt:
    script = (ROOT / "deploy" / "restore.sh").read_text()

    def test_kept_before_the_restore_and_made_again_after(self):
        s = self.script
        export, restore = s.index("credentials-export"), s.index("pg_restore -U")
        replay, start = s.index('credentials-replay < "$CREDENTIALS"'), s.index("docker compose start api >/dev/null")
        assert export < restore < replay < start

    def test_a_failed_replay_keeps_the_file_and_the_api_stopped(self):
        s = self.script
        replay = s.index('credentials-replay < "$CREDENTIALS"')
        failed = s[replay:s.index("docker compose start api >/dev/null", replay)]
        assert 'cp "$CREDENTIALS" "$KEEP_CREDENTIALS"' in failed and "exit 1" in failed
        assert "credentials-replay < $KEEP_CREDENTIALS" in failed, "it says how to make them again"

    def test_a_failed_pg_restore_says_the_api_is_stopped_and_keeps_both_files(self, tmp_path):
        """pg_restore exiting with an error ended the script under set -e: the
        API stayed stopped without a word, and the live database's erasures
        and credentials went with the temporary files (audit of 17 Oct 2026). Played with a docker that answers the exports and fails the
        restore."""
        import os
        import stat

        fake = tmp_path / "bin"
        fake.mkdir()
        (fake / "docker").write_text(
            '#!/bin/sh\n'
            'echo "$*" >> "$CALLS"\n'
            'case "$*" in\n'
            '  *erasures-export*) echo \'[{"kind": "end_user"}]\' ;;\n'
            '  *credentials-export*) echo \'{"keys": []}\' ;;\n'
            '  *pg_restore*) cat >/dev/null; echo "pg_restore: error: bad dump" >&2; exit 1 ;;\n'
            'esac\n')
        (fake / "docker").chmod(0o755)
        dump = tmp_path / "korely.dump"
        dump.write_bytes(b"not a dump")
        calls = tmp_path / "calls"
        env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", FORCE="1", CALLS=str(calls))
        r = subprocess.run(["sh", str(ROOT / "deploy" / "restore.sh"), str(dump)], env=env,
                           capture_output=True, text=True, cwd=tmp_path)
        assert r.returncode == 1
        assert "the API stays stopped" in r.stderr
        log = calls.read_text()
        assert "stop api" in log and "start api" not in log and "replay" not in log
        kept_e = list(tmp_path.glob("erasures-*.json"))
        kept_c = list(tmp_path.glob("credentials-*.json"))
        assert len(kept_e) == len(kept_c) == 1, "what the live database had is gone"
        assert '"end_user"' in kept_e[0].read_text() and '"keys"' in kept_c[0].read_text()
        assert stat.S_IMODE(kept_c[0].stat().st_mode) == 0o600
        assert f"credentials-replay < ./{kept_c[0].name}" in r.stderr

    @staticmethod
    def _fake_docker(tmp_path, cases):
        """A docker on PATH that logs its calls and plays `cases` (sh case
        arms) instead of compose."""
        import os

        fake = tmp_path / "bin"
        fake.mkdir()
        (fake / "docker").write_text('#!/bin/sh\necho "$*" >> "$CALLS"\ncase "$*" in\n'
                                     + cases + 'esac\n')
        (fake / "docker").chmod(0o755)
        dump = tmp_path / "korely.dump"
        dump.write_bytes(b"not a dump")
        tmp = tmp_path / "tmp"
        tmp.mkdir()
        env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", FORCE="1",
                   CALLS=str(tmp_path / "calls"), TMPDIR=str(tmp))
        return dump, env, tmp

    def test_the_api_stops_before_the_live_database_is_read(self, tmp_path):
        """The exports ran with the API up, so an erasure or a revocation
        committed between them and the stop was in neither file, and the
        restore undid it (audit of 19 Oct 2026). Played with a docker
        whose exports fail: nothing is restored, and the operator is told the
        API is stopped and how to start it."""
        s = self.script
        stop = s.index("docker compose stop api")
        assert stop < s.index("erasures-export") and stop < s.index("credentials-export")

        dump, env, _ = self._fake_docker(tmp_path, '  *-export*) exit 1 ;;\n')
        r = subprocess.run(["sh", str(ROOT / "deploy" / "restore.sh"), str(dump)], env=env,
                           capture_output=True, text=True, cwd=tmp_path)
        assert r.returncode == 1
        calls = (tmp_path / "calls").read_text().splitlines()
        at = {what: next(i for i, c in enumerate(calls) if what in c)
              for what in ("stop api", "erasures-export", "credentials-export")}
        assert at["stop api"] < at["erasures-export"] and at["stop api"] < at["credentials-export"]
        assert not any("pg_restore" in c for c in calls), "nothing is restored"
        assert "the API is stopped" in r.stderr and "docker compose start api" in r.stderr

    def _interrupted(self, tmp_path, slow: str, sig):
        """restore.sh run by `sh` with a docker whose `slow` step sleeps,
        interrupted by `sig` while it does: (TMPDIR's files, the files kept in
        the install folder, stderr)."""
        import os
        import time

        cases = {"erasures-export": '  *erasures-export*) echo "[]" ;;\n',
                 "credentials-export": '  *credentials-export*) echo \'{"keys": []}\' ;;\n'}
        cases[slow] = f'  *{slow}*) cat >/dev/null; touch "$CALLS.slow"; sleep 30 ;;\n'
        dump, env, tmp = self._fake_docker(tmp_path, "".join(cases.values()))
        p = subprocess.Popen(["sh", str(ROOT / "deploy" / "restore.sh"), str(dump)], env=env,
                             cwd=tmp_path, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 20
            while not (tmp_path / "calls.slow").exists():
                assert time.monotonic() < deadline and p.poll() is None, f"{slow} never ran"
                time.sleep(0.05)
            assert len(list(tmp.iterdir())) == 2, "the two files are there while it works"
            os.killpg(p.pid, sig)
            _out, err = p.communicate(timeout=20)
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
        kept = sorted(f.name.split("-")[0] for f in tmp_path.glob("*.json"))
        modes = {oct(f.stat().st_mode & 0o777) for f in tmp_path.glob("*.json")}
        assert modes <= {"0o600"}, modes
        return list(tmp.iterdir()), kept, err

    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
    @pytest.mark.parametrize("slow", ["pg_restore", "erasures-replay", "credentials-replay"])
    def test_an_interrupt_after_the_restore_began_keeps_both_files(self, tmp_path, slow, sig):
        """Under dash (`sh` on Debian and Ubuntu) the EXIT trap alone does not
        run on a signal: an interrupted restore left the live database's
        erasures and credentials in $TMPDIR. From pg_restore on they are the
        only copy of what the live database did since the dump: an interrupt
        deleted them (audit of 20 Oct 2026). Now TMPDIR is left empty and both are kept,
        owner-only, in the install folder, with the commands to finish."""
        tmp, kept, err = self._interrupted(tmp_path, slow, sig)
        assert tmp == [], "the erasures and the credentials stayed in $TMPDIR"
        assert kept == ["credentials", "erasures"], "the only copy of the changes since the dump"
        assert "erasures-replay <" in err and "credentials-replay <" in err

    def test_an_interrupt_before_the_restore_keeps_nothing(self, tmp_path):
        """Before pg_restore the live database is intact: the files go."""
        tmp, kept, _err = self._interrupted(tmp_path, "credentials-export", signal.SIGTERM)
        assert tmp == [] and kept == []

    def test_the_kept_files_are_ignored_by_git(self):
        """Kept in the install folder, a git clone, they were listed by
        `git status` and staged by `git add -A`."""
        ignored = (ROOT / ".gitignore").read_text().splitlines()
        assert "/erasures-*.json" in ignored and "/credentials-*.json" in ignored

    def test_the_entrypoint_prints_only_the_commands_output(self, tmp_path):
        """restore.sh pipes the exports through the entrypoint into files: its
        own words go to stderr (the acceptance run of 0.1.40)."""
        (tmp_path / "alembic").write_text("#!/bin/sh\nexit 0\n")
        (tmp_path / "alembic").chmod(0o755)
        import os

        env = {"PATH": f"{tmp_path}:{os.environ['PATH']}",
               "DATABASE_URL": "postgresql://korely:secret@db:5432/korely"}
        out = subprocess.run(["sh", str(ROOT / "deploy" / "entrypoint.sh"), "sh", "-c", "printf '{}'"],
                             capture_output=True, text=True, env=env, check=True)
        assert out.stdout == "{}"
        assert "Applying migrations" in out.stderr
