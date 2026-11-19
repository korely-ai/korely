"""The dashboard's words and addresses against the server's own (audit of 17
Oct 2026).

The dashboard has no test runner of its own (package.json: tsc and vite;
adding one is a dependency of its own to decide), so these read its source,
as tests/test_no_hosted_leftovers.py does, and compare it with what the code
of the server says, never with a list written here:

- the key prefixes and the MCP door's path were written on the screen by
  hand (lib/target.ts, hosted/install.ts): /health says them now;
- the site's address was spelt out on four pages, two docs links were dead
  and the Webhooks button opened the API index;
- the Audit log's filter kept seven of the trail's eleven verbs, a provider
  without a logo wore xAI's "x", the consent page and Setup kept their own
  copies of the scopes' words and of the regions' names;
- Settings said "Extraction and embeddings now go through this key" after
  every save of a Google key;
- about forty sentences used the em dash the house style keeps out of
  product text.
"""
import ast
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from tests import cloud
from tests.test_no_hosted_leftovers import _visible_lines

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dashboard" / "src"


def _sources():
    return [p for p in sorted(SRC.rglob("*")) if p.suffix in (".ts", ".tsx")]


def _shown(path) -> str:
    return "\n".join(code for _i, code in _visible_lines(path))


def _literals(path: Path):
    """(line, value) of the string constants of a Python file that are not
    docstrings: what the code says, not what it explains."""
    tree = ast.parse(path.read_text())
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.body and isinstance(n.body[0], ast.Expr) \
                and isinstance(n.body[0].value, ast.Constant):
            docs.add(id(n.body[0].value))
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            yield n.lineno, n.value


# ── the key prefixes, the MCP door, the site ─────────────────────────────────

@pytest.fixture
def start(monkeypatch):
    from korely_agent.config import get_settings

    monkeypatch.setenv("RUN_WORKER", "false")
    get_settings.cache_clear()

    def go():
        from korely_agent.main import create_app

        return TestClient(create_app())

    yield go
    get_settings.cache_clear()


def test_health_says_what_a_key_begins_with_and_where_the_door_is(start):
    from korely_agent.mcp.server import published_path
    from korely_agent.services.keys import minted_prefix

    with start() as c:
        body = c.get("/health").json()
    assert body["key_prefix"] == minted_prefix()
    assert body["mcp_path"] == published_path()


@pytest.mark.cloud
def test_health_says_the_clouds_prefix_and_door_on_the_cloud(db, monkeypatch):
    from korely_agent.mcp.server import HOSTED_PATH
    from korely_agent.services.keys import PREFIX_LIVE
    from tests.hosted_helpers import hosted_client

    with hosted_client(db, monkeypatch) as c:
        body = c.get("/health").json()
    assert (body["key_prefix"], body["mcp_path"]) == (PREFIX_LIVE, HOSTED_PATH)


def test_the_screen_writes_no_key_prefix_and_no_door_of_its_own():
    """lib/target.ts wrote `kor_self_` and `/agent/mcp`, hosted/install.ts
    `kor_live_`: a change on the server would not have reached the screen."""
    from korely_agent.mcp.server import HOSTED_PATH, MOUNT_PATH
    from korely_agent.services.keys import PREFIX_LIVE, PREFIX_SELF

    offenders = []
    for p in _sources():
        for i, code in _visible_lines(p):
            said = [v for v in (PREFIX_LIVE, PREFIX_SELF, HOSTED_PATH) if v in code]
            said += [MOUNT_PATH] if re.search(rf"{re.escape(MOUNT_PATH)}[\"'`]", code) else []
            if said:
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {said} {code[:80]}")
    assert offenders == [], "written by hand on the screen:\n" + "\n".join(offenders)
    mode = _shown(SRC / "contexts" / "ModeContext.tsx")
    assert "key_prefix" in mode and "mcp_path" in mode, "the screen does not read /health"


def test_each_of_the_servers_values_is_written_in_one_place():
    """The prefixes in services/keys.py, the door in mcp/server.py, the
    support address in hosted/settings.py: the messages that name them read
    them from there (core/auth.py, hosted/signup.py, quotas.py, billing.py
    spelt them out)."""
    from korely_agent.mcp.server import HOSTED_PATH
    from korely_agent.services.keys import PREFIX_LIVE, PREFIX_SELF

    home = {PREFIX_LIVE: "korely_agent/services/keys.py",
            PREFIX_SELF: "korely_agent/services/keys.py",
            HOSTED_PATH: "korely_agent/mcp/server.py"}
    if cloud.HERE:
        from korely_agent.hosted.settings import SUPPORT_EMAIL

        home[SUPPORT_EMAIL] = "korely_agent/hosted/settings.py"
    offenders = []
    for p in sorted((ROOT / "korely_agent").rglob("*.py")):
        if "memory_core" in p.parts:
            continue
        rel = str(p.relative_to(ROOT))
        for line, value in _literals(p):
            said = [v for v, where in home.items() if v in value and rel != where]
            if said:
                offenders.append(f"{rel}:{line}: {said}")
    assert offenders == [], "a copy of a value that has a home:\n" + "\n".join(offenders)


def _docs_links():
    text = (SRC / "lib" / "docsLinks.ts").read_text()
    site = re.search(r'export const SITE = "([^"]+)"', text).group(1)
    body = text.split("export const DOCS", 1)[1]
    names = re.findall(r"^\s+(\w+): \{ href:", body, flags=re.M)
    return site, names


def test_the_site_is_written_once_on_the_screen():
    """Install, Home and Settings spelt https://korely.ai out; lib/docsLinks.ts
    is where it lives."""
    site, names = _docs_links()
    host = urlparse(site).netloc
    assert host and names, "the parser found nothing"
    offenders = [f"{p.relative_to(ROOT)}:{i}: {code[:80]}"
                 for p in _sources() if p.name != "docsLinks.ts"
                 for i, code in _visible_lines(p) if host in code]
    assert offenders == [], "the site's address, written by hand:\n" + "\n".join(offenders)


def test_every_docs_link_is_one_a_page_opens():
    """DOCS.home and DOCS.usage were opened by nothing, and usage pointed at a
    page the site does not have."""
    _site, names = _docs_links()
    text = "\n".join(_shown(p) for p in _sources() if p.name != "docsLinks.ts")
    unused = [n for n in names if not re.search(rf"\bDOCS\.{n}\b", text)]
    assert unused == [], f"docs links nothing opens: {unused}"


def test_the_webhooks_button_opens_the_webhooks_page():
    text = (SRC / "lib" / "docsLinks.ts").read_text()
    assert re.search(r"webhooks: \{ href: `\$\{BASE\}/webhooks`", text)


# ── the Audit log's verbs, the logos, the scopes, the regions ────────────────

@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_the_audit_log_offers_every_verb_of_the_trail(client, db, tenant):
    from korely_agent.services import audit
    from korely_agent.services import keys as key_service

    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE],
        expires_in_days=None)
    body = client.get("/admin/audit", headers={"Authorization": f"Bearer {full}"}).json()
    assert body["actions"] == sorted(audit.ACTIONS)
    assert {"fact_invalidate", "tenant_create", "key_change", "setting_change",
            "login"} <= set(body["actions"]), "the control: the five the page missed"


def test_the_audit_page_writes_no_verb_of_its_own():
    """A verb may be compared with (the badge's colours); it is never offered
    from a list written on the page."""
    from korely_agent.services import audit

    page = _shown(SRC / "pages" / "AuditPage.tsx")
    # The verbs the read answered (GET /admin/audit, `actions`).
    assert "const actions = base?.actions ?? [];" in page
    offered = [a for a in sorted(audit.ACTIONS)
               if re.search(rf'(?<!=== )"{a}"', page)]
    assert offered == [], f"verbs written on the page: {offered}"


def test_every_logo_is_a_providers_and_none_wears_another_ones_mark():
    from korely_agent.services.model_choice import PROVIDERS

    text = (SRC / "lib" / "providerLogos.tsx").read_text()
    paths = text.split("const PATHS", 1)[1].split("};", 1)[0]
    drawn = re.findall(r"^\s+(\w+):\s*$", paths, flags=re.M)
    assert drawn, "the parser found nothing"
    assert set(drawn) <= set(PROVIDERS), f"a logo of no provider: {set(drawn) - set(PROVIDERS)}"
    # The mark of a provider without a logo is read from its id, never one
    # provider's letter typeset for all of them.
    lone = [code for _i, code in _visible_lines(SRC / "lib" / "providerLogos.tsx")
            if re.fullmatch(r"\w", code)]
    assert lone == [], f"one letter for every provider without a logo: {lone}"


def test_the_consent_page_and_setup_keep_no_copy_of_their_own():
    from korely_agent.services.keys import KNOWN_SCOPES

    consent = _shown(SRC / "pages" / "OAuthConsentPage.tsx")
    written = [s for s in KNOWN_SCOPES if f'"{s}":' in consent]
    assert written == [], f"the scopes' words, copied on the consent page: {written}"
    labels = re.findall(r'^\s+\w+: "([^"]+)",$',
                        (SRC / "components" / "RegionSelect.tsx").read_text(), flags=re.M)
    assert labels, "the parser found nothing"
    copies = [(p.relative_to(ROOT), name) for p in _sources() if p.name != "RegionSelect.tsx"
              for name in labels if f'"{name}"' in _shown(p)]
    assert copies == [], f"a region's name, copied: {copies}"


# ── what a saved Google key does ─────────────────────────────────────────────

def test_the_saved_key_is_said_to_do_what_the_server_says_it_does(monkeypatch):
    """By default the vectors are local and the first login chooses a writer:
    the key then does neither, and the page said it did both."""
    from korely_agent.config import get_settings
    from korely_agent.core import llm
    from korely_agent.services import provider_key

    monkeypatch.setattr(llm, "screen_writers",
                        lambda: {"global": ("openai", "https://x", "k", "m")})
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("EXTRACTION_PROVIDER", "gemini")
    get_settings.cache_clear()
    try:
        assert provider_key.uses() == {"writes": False, "vectors": False}
        monkeypatch.setattr(llm, "screen_writers", lambda: {})
        assert provider_key.uses() == {"writes": True, "vectors": False}
        monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
        get_settings.cache_clear()
        assert provider_key.uses() == {"writes": True, "vectors": True}
    finally:
        get_settings.cache_clear()
    page = _shown(SRC / "pages" / "SettingsPage.tsx")
    assert "Extraction and embeddings now go through this key" not in page
    assert "whatTheKeyDoes(saved.uses)" in page


# ── no em dash in what the screen shows ──────────────────────────────────────

def test_no_em_dash_in_the_screens_sentences():
    """The house style keeps the em dash out of product text. A dash that is
    the whole value, the mark of an empty cell ("—", >—<), is not a
    sentence."""
    offenders = []
    for p in _sources():
        for i, code in _visible_lines(p):
            prose = re.sub(r'"—"|>—<', "", code)
            if "—" in prose or "–" in prose:
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {code[:90]}")
    assert offenders == [], "an em dash on the screen:\n" + "\n".join(offenders)
