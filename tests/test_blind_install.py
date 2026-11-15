"""What two blind installs of 0.1.13 found, kept from coming back.

On 2026-09-28 two agents that had never seen this code installed it from the
README alone, one on a Debian 13 server and one on a Mac. The findings here are the ones code can hold: an example value that
named a directory the container did not have, and backups anybody on the
machine could read.
"""
import os
import re
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _export_dir_in_the_example():
    m = re.search(r"^#?\s*FACTS_EXPORT_DIR=(\S+)", (ROOT / ".env.example").read_text(), re.M)
    assert m, ".env.example no longer shows FACTS_EXPORT_DIR"
    return m.group(1)


class TestTheExportDirectoryExists:
    """`FACTS_EXPORT_DIR=/data/facts` was the example, and the container had no
    /data: Permission denied on every tick, and the feature the README sells as
    `git diff` of what the agent learned did not work for anybody who
    followed it."""

    def test_the_example_value_is_the_directory_compose_mounts(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        api = compose.split("\n  api:", 1)[1].split("\n  caddy:", 1)[0].split("\nvolumes:", 1)[0]
        assert f"./facts:{_export_dir_in_the_example()}" in api, (
            "the directory .env.example names is not the one the api service mounts")

    def test_the_directory_is_handed_to_the_uid_the_api_runs_as(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        chown = re.search(r"chown (\d+):(\d+) /facts", compose)
        uid = re.search(r"useradd --uid (\d+)", (ROOT / "Dockerfile").read_text())
        assert chown and uid, "the owner step or the named uid went missing"
        assert chown.group(1) == uid.group(1) == chown.group(2)
        assert "facts-dir: {condition: service_completed_successfully}" in compose, (
            "the API can start before the directory is its own")

    def test_the_export_stays_out_of_git_and_out_of_the_image(self):
        # Anchored at the root: `facts/` alone also matched the facts package,
        # korely_agent/services/facts/, and a new module there would never
        # have been committed (2026-10-01).
        rules = (ROOT / ".gitignore").read_text().split()
        assert "/facts/" in rules and "facts/" not in rules
        # The build context is an allow-list: anything not named is not sent.
        ignore = [l.strip() for l in (ROOT / ".dockerignore").read_text().splitlines()
                  if l.strip() and not l.lstrip().startswith("#")]
        assert ignore[0] == "*" and not any(l.startswith("!facts") for l in ignore)

    def test_a_directory_it_cannot_write_says_which_one_to_use(self, monkeypatch, tmp_path):
        from korely_agent import worker
        from korely_agent.services import export_md

        blocked = tmp_path / "a-file"
        blocked.write_text("")
        monkeypatch.setattr(worker, "get_settings",
                            lambda: SimpleNamespace(facts_export_dir=str(blocked / "facts")))

        def export_all(db, root):
            Path(root).mkdir(parents=True, exist_ok=True)   # what export_tenant does

        monkeypatch.setattr(export_md, "export_all", export_all)
        with pytest.raises(OSError) as e:
            worker._export(None)
        assert "FACTS_EXPORT_DIR=" in str(e.value) and "/data/facts" in str(e.value)


class TestBackupsAreTheOwnersOnly:
    """A dump is every memory in clear. backup.sh wrote it 644."""

    def test_the_dump_and_its_directory_are_private(self, tmp_path):
        fake = tmp_path / "bin"
        fake.mkdir()
        # `docker compose exec -T db pg_dump ...` answers with a dump-sized body.
        (fake / "docker").write_text("#!/bin/sh\nhead -c 4096 /dev/zero\n")
        (fake / "docker").chmod(0o755)
        out = tmp_path / "backups"
        env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", BACKUP_DIR=str(out))
        r = subprocess.run(["sh", str(ROOT / "deploy/backup.sh")], env=env,
                           capture_output=True, text=True, cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        dumps = list(out.glob("korely-*.dump"))
        assert len(dumps) == 1
        assert stat.S_IMODE(dumps[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(out.stat().st_mode) == 0o700


class TestAFailedDumpLeavesNothing:
    """pg_dump failing (the database stopped, the disk full) left the file the
    redirection had created: `set -e` left before the size check, and the
    empty or half dump counted as one of the KEEP backups (audit of 17 Oct
    2026)."""

    def _backup(self, tmp_path, docker_body):
        fake = tmp_path / "bin"
        fake.mkdir(exist_ok=True)
        (fake / "docker").write_text("#!/bin/sh\n" + docker_body)
        (fake / "docker").chmod(0o755)
        out = tmp_path / "backups"
        env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", BACKUP_DIR=str(out), KEEP="2")
        r = subprocess.run(["sh", str(ROOT / "deploy/backup.sh")], env=env,
                           capture_output=True, text=True, cwd=tmp_path)
        return r, out

    def test_a_pg_dump_that_fails_halfway_keeps_no_file(self, tmp_path):
        # Half a dump on stdout, then the error pg_dump exits with.
        r, out = self._backup(tmp_path, "head -c 4096 /dev/zero\nexit 1\n")
        assert r.returncode != 0
        assert list(out.iterdir()) == [], "a failed dump left a file behind"
        assert "FAILED" in r.stderr

    def test_an_empty_dump_keeps_no_file(self, tmp_path):
        r, out = self._backup(tmp_path, "exit 0\n")
        assert r.returncode != 0
        assert list(out.iterdir()) == []

    def test_failures_do_not_rotate_the_good_dumps_away(self, tmp_path):
        out = tmp_path / "backups"
        out.mkdir(mode=0o700)
        good = []
        for day in ("20261001", "20261002"):
            p = out / f"korely-{day}T030000Z.dump"
            p.write_bytes(b"x" * 4096)
            good.append(p)
        for _ in range(3):
            r, _ = self._backup(tmp_path, "head -c 10 /dev/zero\nexit 1\n")
            assert r.returncode != 0
        assert sorted(out.iterdir()) == good


class TestAFailingExportIsVisible:
    """Both blind installs saw the export fail on every tick while /health said
    "ok" and `manage status` said nothing."""

    def health(self, monkeypatch, export_dir):
        from fastapi.testclient import TestClient

        from korely_agent import config

        monkeypatch.setenv("RUN_WORKER", "false")
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@localhost/x")
        if export_dir is None:
            monkeypatch.delenv("FACTS_EXPORT_DIR", raising=False)
        else:
            monkeypatch.setenv("FACTS_EXPORT_DIR", export_dir)
        config.get_settings.cache_clear()
        from korely_agent.main import create_app

        try:
            with TestClient(create_app()) as c:
                return c.get("/health").json()
        finally:
            config.get_settings.cache_clear()

    def test_health_says_the_export_is_failing_and_why(self, monkeypatch, tmp_path):
        from korely_agent import worker
        from korely_agent.services import export_md

        blocked = tmp_path / "a-file"
        blocked.write_text("")
        where = str(blocked / "facts")
        monkeypatch.setattr(worker, "_export_state", {})
        monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(facts_export_dir=where))
        monkeypatch.setattr(export_md, "export_all",
                            lambda db, root: Path(root).mkdir(parents=True, exist_ok=True))
        with pytest.raises(OSError):
            worker._export(None)

        body = self.health(monkeypatch, where)
        assert body["status"] == "degraded"
        assert body["export"]["state"] == "failing"
        assert where in body["export"]["detail"]

    def test_an_install_that_exports_nothing_reports_nothing(self, monkeypatch):
        assert "export" not in self.health(monkeypatch, None)

    def test_status_names_a_directory_that_is_not_there(self, tmp_path):
        from korely_agent import manage

        line = manage._export_line(str(tmp_path / "nowhere"))
        assert line.startswith("export      FAILING") and "/data/facts" in line

    def test_status_counts_what_it_wrote(self, tmp_path):
        from korely_agent import manage

        (tmp_path / "t1").mkdir()
        (tmp_path / "t1" / "c4821.md").write_text("# c4821\n")
        line = manage._export_line(str(tmp_path))
        assert "1 files" in line and "last changed" in line
