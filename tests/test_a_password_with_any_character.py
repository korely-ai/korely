"""A Postgres password with `@`, `#` or `%` in it (audit of 17 Oct 2026).

docker-compose.yml spliced POSTGRES_PASSWORD raw into DATABASE_URL: `p@ss#word`
made a URL whose host was `ss#word@db`, and a `%` (or anything the URL had to
quote) made alembic's set_main_option raise a ValueError whose message was the
whole URL, password included. The password now travels apart and the
application quotes it in; README's "anything long and random" holds."""
import os
import subprocess
from pathlib import Path

import yaml
from sqlalchemy.engine import make_url

from korely_agent.config import AgentSettings

ROOT = Path(__file__).resolve().parent.parent
NASTY = "p@ss#w%rd:/ ?&=+"


def _settings(monkeypatch, url, password):
    monkeypatch.setenv("DATABASE_URL", url)
    if password is None:
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("POSTGRES_PASSWORD", password)
    return AgentSettings(_env_file=None)


def test_the_password_goes_into_the_url_quoted(monkeypatch):
    s = _settings(monkeypatch, "postgresql://korely@db:5432/korely", NASTY)
    url = make_url(s.database_url)
    assert url.password == NASTY
    assert (url.drivername, url.username, url.host, url.port, url.database) == (
        "postgresql+psycopg2", "korely", "db", 5432, "korely")


def test_a_url_with_its_own_password_is_left_alone(monkeypatch):
    s = _settings(monkeypatch, "postgresql://u:mine@h:5432/k", NASTY)
    assert make_url(s.database_url).password == "mine"


def test_no_password_apart_changes_nothing(monkeypatch):
    s = _settings(monkeypatch, "postgresql://u@h:5432/k", None)
    assert s.database_url == "postgresql+psycopg2://u@h:5432/k"


def test_the_password_is_not_in_the_settings_repr(monkeypatch):
    s = _settings(monkeypatch, "postgresql://korely@db:5432/korely", "zzsecretzz")
    assert "zzsecretzz" not in repr(s).replace(s.database_url, "")


def test_compose_no_longer_splices_it_into_the_url():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    env = compose["services"]["api"]["environment"]
    assert "POSTGRES_PASSWORD" not in env["DATABASE_URL"]
    assert make_url(env["DATABASE_URL"]).password is None
    # The same fallback as the database's, so the entrypoint refuses it.
    assert env["POSTGRES_PASSWORD"] == compose["services"]["db"]["environment"]["POSTGRES_PASSWORD"]


def test_alembic_does_not_print_the_url(tmp_path):
    """Port 1 refuses the connection; what matters is what is said before."""
    for given in ({"DATABASE_URL": "postgresql://korely@127.0.0.1:1/korely", "POSTGRES_PASSWORD": NASTY},
                  # Written into the URL by hand, percent-encoded as it must be.
                  {"DATABASE_URL": "postgresql://korely:p%40ss%23w%25rd@127.0.0.1:1/korely",
                   "POSTGRES_PASSWORD": ""}):
        env = {k: v for k, v in os.environ.items() if k != "TEST_DATABASE_URL"}
        env.update(given)
        r = subprocess.run(["alembic", "-c", "alembic/alembic.ini", "current"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=60)
        out = r.stdout + r.stderr
        assert r.returncode != 0
        assert "interpolation" not in out, out[-400:]
        assert "p%40ss" not in out and "p@ss" not in out, "the password is in the output"
        assert "Connection refused" in out, out[-400:]


def test_the_entrypoint_refuses_the_default_password_given_apart(tmp_path):
    env = dict(os.environ, DATABASE_URL="postgresql://korely@db:5432/korely",
               POSTGRES_PASSWORD="change-me")
    r = subprocess.run(["sh", str(ROOT / "deploy" / "entrypoint.sh"), "true"], env=env,
                       capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert r.returncode == 1
    assert "POSTGRES_PASSWORD is not set" in r.stderr
    assert "Applying migrations" not in r.stderr
