"""The entrypoint serves only once every chain of migrations is applied
(5 Nov 2026, a finding of the skeptic's review).

After the core's `alembic upgrade head` it asks `python -m
korely_agent.db.chains --list` for the other chains (a part of the product
the Cloud's package carries; none on an install of one's own) and applies
each. The list was read in a command substitution, where `set -e` does not
reach: a list that failed (an ImportError) was an empty list, and the
service started on a database without a chain, saying "Migrations applied."
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "deploy" / "entrypoint.sh"


def _run(tmp_path, chains_source: str):
    """The entrypoint, with an `alembic` that does nothing and a
    korely_agent.db.chains of `chains_source` in its working directory
    (`python -m` reads the working directory first)."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "alembic").write_text("#!/bin/sh\necho \"alembic $*\" >&2\nexit 0\n")
    (bin_ / "alembic").chmod(0o755)
    pkg = tmp_path / "work" / "korely_agent" / "db"
    pkg.mkdir(parents=True)
    (pkg.parent / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "chains.py").write_text(chains_source)
    env = {"PATH": f"{bin_}:{os.environ['PATH']}",
           "DATABASE_URL": "postgresql://korely:secret@db:5432/korely"}
    return subprocess.run(["sh", str(ENTRYPOINT), "sh", "-c", "printf served"],
                          cwd=tmp_path / "work", capture_output=True, text=True, env=env)


def test_a_list_of_chains_that_fails_stops_the_start(tmp_path):
    out = _run(tmp_path, 'raise ImportError("a dependency of a chain is not there")\n')
    assert out.returncode != 0, out.stderr
    assert "served" not in out.stdout
    assert "Migrations applied." not in out.stderr
    assert "chains" in out.stderr


def test_each_chain_listed_is_applied_and_then_the_service_runs(tmp_path):
    out = _run(tmp_path, 'print("/x/one.ini")\nprint("/x/two.ini")\n')
    assert out.returncode == 0, out.stderr
    assert out.stdout == "served"
    assert "alembic -c /x/one.ini upgrade head" in out.stderr
    assert "alembic -c /x/two.ini upgrade head" in out.stderr


def test_no_chain_listed_is_the_core_alone(tmp_path):
    out = _run(tmp_path, "")
    assert out.returncode == 0 and out.stdout == "served", out.stderr
    assert out.stderr.count("alembic ") == 1
