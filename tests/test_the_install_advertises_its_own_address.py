"""PUBLIC_BASE_URL follows DOMAIN (audit of 17 Oct 2026).

The OAuth and MCP metadata, and the 401s that point at them, advertised
PUBLIC_BASE_URL, which nothing documented and which defaulted to
http://localhost:8000: an install with DOMAIN set sent ChatGPT and Claude to
localhost. entrypoint.sh now derives it from DOMAIN and HTTPS_PORT when it is
not set. Run here as the container runs it, with a stand-in for alembic."""
import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "deploy" / "entrypoint.sh"


def _advertised(tmp_path, **env):
    (tmp_path / "alembic").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "alembic").chmod(0o755)
    run_env = {"PATH": f"{tmp_path}:{os.environ['PATH']}",
               "DATABASE_URL": "postgresql://korely:secret@db:5432/korely", **env}
    out = subprocess.run(["sh", str(ENTRYPOINT), "sh", "-c", 'printf "URL=%s" "$PUBLIC_BASE_URL"'],
                         capture_output=True, text=True, env=run_env, check=True)
    return out.stdout.rsplit("URL=", 1)[1]


@pytest.mark.parametrize("env,expected", [
    ({"DOMAIN": "memory.example.com"}, "https://memory.example.com"),
    ({"DOMAIN": "memory.example.com", "HTTPS_PORT": "443"}, "https://memory.example.com"),
    ({"DOMAIN": "memory.example.com", "HTTPS_PORT": "8443"}, "https://memory.example.com:8443"),
    ({"DOMAIN": "memory.example.com", "PUBLIC_BASE_URL": "https://mem.corp"}, "https://mem.corp"),
    # no DOMAIN, Caddy serving the install (the compose passes HTTPS_PORT,
    # 443 when .env leaves it out):
    # Caddy's own default name, localhost (the acceptance run of 0.1.40)
    ({"HTTPS_PORT": "8543"}, "https://localhost:8543"),
    ({"HTTPS_PORT": "443"}, "https://localhost"),
    ({"DOMAIN": "", "HTTPS_PORT": "443"}, "https://localhost"),
    # Outside the compose (the image run alone, a test): nothing to build it from.
    ({}, ""),
])
def test_the_address_follows_the_domain(tmp_path, env, expected):
    assert _advertised(tmp_path, **env) == expected


def test_the_compose_hands_the_api_what_caddy_serves():
    """HTTPS_PORT reached the API only from a .env that set it, and the
    example leaves it commented: a default install advertised
    http://localhost:8000 while the 0.1.40 notes said https://localhost
    (audit of 18 Oct 2026). The API gets both values as Caddy does."""
    import yaml

    compose = yaml.safe_load((ENTRYPOINT.parent.parent / "docker-compose.yml").read_text())
    api = compose["services"]["api"]["environment"]
    caddy = compose["services"]["caddy"]
    assert api["HTTPS_PORT"] == "${HTTPS_PORT:-443}"
    assert any(p.startswith(api["HTTPS_PORT"] + ":") for p in caddy["ports"])
    assert api["DOMAIN"] == "${DOMAIN:-}", "empty, not localhost: the entrypoint names localhost"


def test_stdout_is_the_commands_alone(tmp_path):
    """restore.sh pipes `manage erasures-export` through the entrypoint into a
    file: the entrypoint's own words go to stderr (the acceptance run of 0.1.40)."""
    (tmp_path / "alembic").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "alembic").chmod(0o755)
    env = {"PATH": f"{tmp_path}:{os.environ['PATH']}",
           "DATABASE_URL": "postgresql://korely:secret@db:5432/korely"}
    out = subprocess.run(["sh", str(ENTRYPOINT), "sh", "-c", "printf '[]'"],
                         capture_output=True, text=True, env=env, check=True)
    assert out.stdout == "[]"
    assert "Applying migrations" in out.stderr
