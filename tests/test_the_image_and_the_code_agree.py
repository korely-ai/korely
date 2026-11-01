"""The paths and the port the image, the compose file and the code each write
(audit of 17 Oct 2026). They cannot all read one value: the
Dockerfile, docker-compose.yml and the Caddyfile are not Python. So the code
writes each one once, and this holds the others to it, so that moving one in
the image without the code (or the other way) fails here and not on an
install that cannot find its screen, its model or its API.
"""
import re
from pathlib import Path
from urllib.parse import urlparse

from korely_agent.config import LOCAL_BASE_URL, AgentSettings
from korely_agent.core import local_embedder

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = (ROOT / "docker-compose.yml").read_text()
CADDY = (ROOT / "deploy" / "Caddyfile").read_text()


def _default(name):
    return AgentSettings.model_fields[name].default


def test_the_dashboard_is_where_the_image_puts_it():
    workdir = re.findall(r"^WORKDIR (\S+)", DOCKERFILE, re.M)[-1]
    target = re.search(r"^COPY --from=dashboard \S+ (\S+)", DOCKERFILE, re.M).group(1)
    assert str((Path(workdir) / target).resolve()) == _default("dashboard_dir")


def test_the_local_model_is_where_the_image_puts_it():
    target = re.search(r"^COPY --from=model \S+ (\S+)", DOCKERFILE, re.M).group(1)
    assert local_embedder.MODEL_DIR == f"{target}/{local_embedder.NAME}"


def test_one_port_for_the_api():
    port = urlparse(LOCAL_BASE_URL).port
    assert _default("public_base_url") == LOCAL_BASE_URL
    assert re.search(r"^EXPOSE (\d+)", DOCKERFILE, re.M).group(1) == str(port)
    assert f'"--port", "{port}"' in DOCKERFILE
    assert f"http://127.0.0.1:{port}/health" in DOCKERFILE
    assert re.search(r'ports: \["127\.0\.0\.1:\$\{PORT:-\d+\}:(\d+)"\]', COMPOSE).group(1) == str(port)
    assert f"reverse_proxy api:{port}" in CADDY


def test_the_export_directory_the_messages_name_is_the_one_compose_mounts():
    import yaml

    api = yaml.safe_load(COMPOSE)["services"]["api"]
    mounted = next(v.split(":", 1)[1] for v in api["volumes"] if v.startswith("./facts:"))
    assert f"FACTS_EXPORT_DIR={mounted}" in COMPOSE, "the comment that tells .env what to set"
    for rel in ("korely_agent/worker.py", "korely_agent/manage.py"):
        named = re.findall(r"the directory to name is (/\S+?)[,)]", (ROOT / rel).read_text())
        assert named and set(named) == {mounted}, rel


def test_the_shipped_postgres_logs_no_statement_and_no_detail_of_a_failure():
    """Audit of 27 Oct 2026: by default a failed statement is logged with its
    literals and the violated key's values (an end user's id, a fact's words),
    in a log an erasure never reaches."""
    db = COMPOSE.split("\n  db:\n", 1)[1].split("\n  caddy:", 1)[0]
    assert '"log_min_error_statement=panic"' in db and '"log_error_verbosity=terse"' in db
    assert "log_statement" not in db.replace("log_min_error_statement", "")
