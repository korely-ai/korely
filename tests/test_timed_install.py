"""The timing script refuses a machine with something to lose (audit of 17
Oct 2026).

timed_install.sh deleted --dir (by default /opt/korely-agent) whole at the
clone phase and, without --keep, ended with `docker compose down -v`: run on a
machine with a real install there, the .env, backups/ and facts/ went, and the
compose project, named after the directory, lost its database volume. Now a
directory it did not make and a project that already has volumes are refused
before anything is touched. Played with a docker and a curl that answer
nothing, in temporary directories."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARK = ".made-by-timed-install"


def _run(tmp_path, target, volumes="", from_local=None):
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "docker").write_text(
        '#!/bin/sh\necho "$*" >> "$CALLS"\n'
        f'case "$*" in "volume ls"*) printf "%s" "{volumes}" ;; esac\nexit 0\n')
    (fake / "curl").write_text("#!/bin/sh\nexit 1\n")
    for f in ("docker", "curl"):
        (fake / f).chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", CALLS=str(tmp_path / "calls"))
    env.pop("GOOGLE_API_KEY", None)
    src = from_local or (tmp_path / "no-such-source")
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "timed_install.sh"), "--label", "test", "--skip-docker",
         "--from-local", str(src), "--dir", str(target)],
        env=env, capture_output=True, text=True, cwd=tmp_path, timeout=60)


def test_a_directory_it_did_not_make_is_refused_and_kept(tmp_path):
    target = tmp_path / "korely-agent"
    (target / "backups").mkdir(parents=True)
    (target / ".env").write_text("SECRET_KEY=the-only-copy\n")
    r = _run(tmp_path, target)
    assert r.returncode == 2, r.stderr
    assert "did not make it" in r.stderr
    assert (target / ".env").read_text() == "SECRET_KEY=the-only-copy\n"
    assert (target / "backups").is_dir()


def test_a_project_with_volumes_is_refused(tmp_path):
    r = _run(tmp_path, tmp_path / "korely-agent", volumes="korely-agent_pgdata")
    assert r.returncode == 2, r.stderr
    assert "'korely-agent' already has volumes" in r.stderr
    assert "compose" not in (tmp_path / "calls").read_text().replace("com.docker.compose", "")


def test_a_directory_it_made_is_replaced_and_marked_again(tmp_path):
    """A rerun on the same VM is what the script is for."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("no .env.example, so the run stops at configure\n")
    target = tmp_path / "korely-agent"
    target.mkdir()
    (target / MARK).write_text("")
    (target / "install.env").write_text("from the run before\n")
    r = _run(tmp_path, target, volumes="korely-agent_pgdata", from_local=src)
    assert "no .env.example" in r.stderr, r.stderr
    assert not (target / "install.env").exists(), "the earlier run's directory was not replaced"
    assert (target / MARK).exists(), "the next run could not replace it"
