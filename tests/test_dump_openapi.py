"""scripts/dump_openapi.py: the spec of the code without a server, the
Cloud's with --hosted, which the site's public spec is made from (11 Oct
2026). Run in a process of its own: it sets the mode before the settings are
read, and this process has read them already."""
import functools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import cloud

ROOT = Path(__file__).resolve().parent.parent
# The documents this tree can make: the Cloud's (--hosted) needs its package.
MODES = ((), ("--hosted",)) if cloud.HERE else ((),)


@functools.lru_cache(maxsize=None)
def _spec(*args):
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "dump_openapi.py"), *args],
                          capture_output=True, text=True, check=True, cwd=ROOT,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})
    return json.loads(done.stdout)


def _paths(*args):
    return _spec(*args)["paths"]


@pytest.mark.cloud
def test_the_cloud_spec_has_the_routes_only_the_service_mounts():
    paths = _paths("--hosted")
    assert "/v1/agents/init" in paths and "/v1/account" in paths
    assert "/v1/memories" in paths and "/v1/users/{end_user}/memories" in paths


def test_an_install_of_ones_own_has_none_of_them():
    paths = _paths()
    assert "/v1/agents/init" not in paths and "/v1/account" not in paths
    assert "/v1/memories" in paths


def test_writing_a_fact_is_not_described_as_model_free():
    """The forget route's description called closing facts by hand "the
    configuration with no model in the write path at all", and POST /v1/facts
    had no description, while that write asks the model for the vocabulary
    and the contradiction check whenever a key is configured (audit of 17 Oct
    2026). The Cloud's spec where the tree can make it: the site's
    is made from it."""
    paths = _paths(*MODES[-1])
    forget = paths["/v1/facts/{fact_id}/forget"]["post"].get("description", "")
    write = paths["/v1/facts"]["post"].get("description", "")
    assert "no model in the write path" not in forget
    assert "asks no model" in forget and "POST /v1/facts" in forget
    assert "contradiction check" in write and "model_unavailable" in write
    assert "With no model key" in write


def test_the_spec_says_every_memory_left_out_is_named():
    """The description of `memories_left_out` said that once the block shows
    a memory, the first one left out ends the memories and the ones after it
    are not named; since the second round of 15 Nov 2026 the packing goes on
    past every memory it leaves out and names each one
    (services/context._pack_memory_lines,
    test_a_memory_left_out_does_not_end_the_packing). Review of 23 Nov
    2026: the site's spec is made from this one."""
    for mode in MODES:
        schemas = _spec(*mode)["components"]["schemas"]
        [text] = [s["properties"]["memories_left_out"]["description"]
                  for s in schemas.values() if "memories_left_out" in s.get("properties", {})]
        assert "not named" not in text and "ends the memories" not in text, text
        assert "names each one it does not show" in text, text


def test_every_webhook_the_document_names_is_one_that_is_sent():
    """/docs told developers to receive a `fact_extracted` webhook, an event
    no code sends and POST /admin/webhooks refuses as unknown (audit of 17
    Oct 2026). An event name is dotted or snake case; a word before
    "webhook" that is neither ("a webhook", "the webhook") is not one."""
    import re

    from korely_agent.services.webhooks import _OWN_EVENTS

    sent = set(_OWN_EVENTS)
    if cloud.HERE:
        from korely_agent.hosted.quotas import WARNING_EVENT

        sent.add(WARNING_EVENT)
    for mode in MODES:
        text = json.dumps(_spec(*mode))
        named = {m for m in re.findall(r"`?([a-z]+(?:[._][a-z]+)+)`? webhook", text)}
        assert named <= sent, f"{mode or 'self-hosted'}: the document names {sorted(named - sent)}"


def _without_description(paths, prefix="/v1"):
    return sorted(f"{method.upper()} {path}" for path, item in paths.items()
                  if path.startswith(prefix) for method, op in item.items()
                  if isinstance(op, dict) and not (op.get("description") or "").strip())


def test_every_v1_operation_has_a_description():
    """The README calls the REST API the contract and says /docs has all of
    it, and the core of it had no description: POST and GET /v1/memories,
    PATCH /v1/memories/{id}, the search, /v1/facts, /v1/context, /v1/batch
    (audit of 17 Oct 2026). The Cloud's spec whole: POST
    /v1/agents/init and DELETE /v1/account, the two doors only the service
    mounts, had none either."""
    for mode in MODES:
        assert _without_description(_paths(*mode)) == [], mode


def test_every_admin_operation_has_a_description():
    """The public spec publishes /admin as the API a script with the `admin`
    scope calls, and eighteen of its operations had no description: issuing,
    revoking and moving a key, the projects, the webhooks, the aliases, the
    request reports, one memory and its history, the OAuth consent (audit of
    17 Oct 2026). Checked as /v1 is: this code's spec, and the Cloud's on the
    routes it shares with it."""
    assert _without_description(_paths(), "/admin") == []
    if cloud.HERE:
        shared = _paths().keys()
        hosted = {p: v for p, v in _paths("--hosted").items() if p in shared}
        assert _without_description(hosted, "/admin") == []


def test_forgetting_one_memory_is_not_called_reversible():
    """The one text about DELETE /v1/memories/{id} called it "a reversible
    drop-from-reads", and nothing brings a forgotten memory back."""
    paths = _paths()
    erase_user = paths["/v1/users/{end_user}/memories"]["delete"]["description"]
    forget_one = paths["/v1/memories/{memory_id}"]["delete"]["description"]
    assert "reversible" not in erase_user
    assert "no call brings" in forget_one and "erase=true" in forget_one


@pytest.mark.cloud
def test_the_public_spec_has_the_administration_api_as_a_section():
    """The spec the site publishes covered /v1 alone, and /admin (projects,
    keys, webhooks, requests, usage), which a key with the `admin` scope calls
    from a script as the dashboard does, was in no document a developer reads
    (audit of 17 Oct 2026). `--public` is that spec, made from the
    Cloud's: both APIs, each a section saying who calls it."""
    pub = _spec("--public", "--draft")
    hosted = _paths("--hosted")
    paths = pub["paths"]
    assert {p for p in hosted if p.startswith("/v1")} <= set(paths)
    assert {"/admin/projects", "/admin/keys", "/admin/webhooks", "/admin/requests",
            "/admin/usage"} <= set(paths)
    # The dashboard's own (its sign-in and billing) and a person's consent stay out.
    assert not [p for p in paths if p.startswith(("/hosted", "/admin/oauth"))]
    sections = {t["name"]: t["description"] for t in pub["tags"]}
    assert "`admin` scope" in sections["admin"] and "v1" in sections
    assert "/admin" in pub["info"]["description"]


@pytest.mark.cloud
def test_the_public_spec_stands_on_its_own():
    """Every schema it points at is in it, and its one security scheme is the
    one every operation names."""
    import re

    pub = _spec("--public", "--draft")
    text = json.dumps(pub)
    refs = set(re.findall(r'"#/components/schemas/([^"]+)"', text))
    assert refs and refs <= set(pub["components"]["schemas"])
    named = {name for item in pub["paths"].values() for op in item.values()
             for req in op["security"] for name in req}
    assert named == set(pub["components"]["securitySchemes"]) == {"bearerAuth"}
    assert pub["paths"]["/v1/agents/init"]["post"]["security"] == []


@pytest.mark.cloud
def test_the_clouds_429_names_the_months_quota_and_the_agent_cap():
    """A client generated from the Cloud's spec branched on
    `rate_limit_exceeded` alone and read a spent month as "retry in a
    minute" (audit of 17 Oct 2026). Every code hosted/quotas.py
    raises with a 429 is in the Cloud's 429, and none in an install's."""
    import re

    src = (ROOT / "korely_agent" / "hosted" / "quotas.py").read_text()
    raised = set(re.findall(r'HTTP_429_TOO_MANY_REQUESTS, detail=\{\s*"code": "(\w+)"', src))
    assert raised == {"quota_exceeded", "agent_cap_exceeded"}, raised
    cloud_paths, own_paths = _paths("--hosted"), _paths()
    # What each operation can be refused for, beyond the request ceiling: a
    # write names both, a read the month's queries, a correction the month's
    # writes, and the sign-in, the keys and the webhooks neither (audit of
    # 18 Oct 2026: the sentence was on every 429, 53 of them /admin).
    expected = {
        ("/v1/memories", "post"): {"quota_exceeded", "agent_cap_exceeded"},
        ("/v1/batch", "post"): {"quota_exceeded", "agent_cap_exceeded"},
        ("/v1/facts", "post"): {"quota_exceeded", "agent_cap_exceeded"},
        ("/v1/memories/search", "post"): {"quota_exceeded"},
        ("/v1/facts", "get"): {"quota_exceeded"},
        ("/v1/context", "get"): {"quota_exceeded"},
        ("/v1/facts/{fact_id}", "patch"): {"quota_exceeded"},
        ("/v1/memories/{memory_id}", "patch"): {"quota_exceeded"},
        ("/admin/facts/{fact_id}", "patch"): {"quota_exceeded"},
        ("/admin/session", "post"): set(),
        ("/admin/keys", "get"): set(),
        ("/admin/webhooks", "post"): set(),
        ("/v1/memories/{memory_id}", "delete"): set(),
        ("/v1/audit", "get"): set(),
    }
    for (path, method), codes in expected.items():
        cloud = cloud_paths[path][method]["responses"]["429"]["description"]
        own = own_paths[path][method]["responses"]["429"]["description"]
        assert "rate_limit_exceeded" in cloud and "rate_limit_exceeded" in own
        for code in raised:
            assert (code in cloud) == (code in codes), (path, method, code)
            assert code not in own, (path, method, code)
    queries = cloud_paths["/v1/memories/search"]["post"]["responses"]["429"]["description"]
    writes = cloud_paths["/v1/memories"]["post"]["responses"]["429"]["description"]
    assert "queries of the month" in queries and "top-up" not in queries
    assert "writes of the month" in writes and "top-up" in writes
    # 1.0.27: the body says what the account may still take (`left`), and a
    # batch, counted whole, fits with `left` writes or fewer.
    batch = cloud_paths["/v1/batch"]["post"]["responses"]["429"]["description"]
    assert all("`left`" in said for said in (queries, writes, batch))
    assert "a batch of `left` writes or fewer fits" in batch
    # Under /admin only the fact correction counts against the month.
    admin = sorted(f"{m.upper()} {p}" for p, item in cloud_paths.items() if p.startswith("/admin")
                   for m, op in item.items()
                   if "quota_exceeded" in op.get("responses", {}).get("429", {}).get("description", ""))
    assert admin == ["DELETE /admin/aliases/{alias_id}", "PATCH /admin/facts/{fact_id}",
                         "POST /admin/aliases"], admin


def _texts(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _texts(v, f"{path}/{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _texts(v, f"{path}[{i}]")
    elif isinstance(o, str):
        yield path, o


# What the published texts do not do (audit of 17 Oct 2026): /docs and
# the MCP's words are read by every customer of both products, so they say
# what the API does now. An incident's story ("Until 2026-09-28 it read the
# whole account") goes after `\f` in a route's docstring, which FastAPI leaves
# out of the document, or in a comment.
_NOT_PUBLISHED = {
    "an em dash": r"—",
    "the server as ours": r"\bour side\b",
    "an incident's date": r"\b[Uu]ntil 20\d\d-\d\d|\(20\d\d-\d\d-\d\d\)",
    "what it used to do": r"\bused to\b",
    "an audit's reference": r"\baudit of\b",
}


def test_the_published_texts_say_what_the_api_does_now():
    import re

    found = []
    for mode in MODES:
        for path, text in _texts(_spec(*mode)):
            if not re.search(r"/(description|summary|title)$", path):
                continue
            if path.endswith("/summary") and text != text.strip():
                found.append(f"{mode} {path}: a summary with a space at its end")
            found += [f"{mode} {path}: {what}" for what, pattern in _NOT_PUBLISHED.items()
                      if re.search(pattern, text)]
    assert found == [], found


def test_the_mcp_words_say_what_it_does_now():
    """The instructions an MCP client is given and its tools' descriptions,
    the same rules as /docs."""
    import ast
    import re

    tree = ast.parse((ROOT / "korely_agent" / "mcp" / "server.py").read_text())
    texts = [ast.get_docstring(n) or "" for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("korely_")]
    texts += [ast.literal_eval(n.value) for n in ast.walk(tree) if isinstance(n, ast.Assign)
              and any(getattr(t, "id", "") in ("_AGENT_MCP_INSTRUCTIONS", "_AGENT_MCP_ADMIN_KEY_NOTE",
                                               "_AGENT_MCP_ANOTHER_KEY")
                      for t in n.targets)]
    assert len(texts) >= 7, "the tools, the instructions or the admin key's note were not found"
    found = [f"{what}: {t[:60]!r}" for t in texts for what, pattern in _NOT_PUBLISHED.items()
             if re.search(pattern, t)]
    assert found == [], found


@pytest.mark.parametrize("flag", ["--hosted", "--public"])
def test_without_the_cloud_its_documents_are_refused_in_one_line(flag, monkeypatch, capsys):
    """A tree without the Cloud's package (the public repository) cannot build
    the Cloud's spec: the dump says so in one line and exits 2, never a
    traceback from the settings."""
    import importlib.util

    from korely_agent.core import extensions

    spec = importlib.util.spec_from_file_location("_dump_openapi", ROOT / "scripts" / "dump_openapi.py")
    dump = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dump)
    monkeypatch.setattr(extensions, "cloud", lambda part=None: None)
    assert dump.main([flag]) == 2
    out = capsys.readouterr()
    assert out.out == "" and out.err == f"dump_openapi: {extensions.NO_CLOUD}\n"


@pytest.mark.cloud
@pytest.mark.parametrize("version, refused", [("1.0.40-dev", True), ("1.0.40", False),
                                              ("1.0.40-rc.1", True)])
def test_the_public_spec_is_the_one_of_a_release(version, refused, monkeypatch, capsys):
    """The site's spec printed `1.0.23-dev`, corrected by hand at every
    release (review of 14 Nov 2026): `--public` now refuses a version that is
    not a release, unless `--draft` says it is a page still in work."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_dump_openapi", ROOT / "scripts" / "dump_openapi.py")
    dump = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dump)
    full = {"openapi": "3.1.0", "info": {"version": version}, "paths": {}, "tags": []}
    monkeypatch.setattr(dump, "spec", lambda hosted: full)
    assert dump.main(["--public"]) == (2 if refused else 0)
    out = capsys.readouterr()
    if refused:
        assert out.out == "" and f"this tree is {version}, not a release" in out.err
        assert dump.main(["--public", "--draft"]) == 0
        assert json.loads(capsys.readouterr().out)["info"]["version"] == version
    else:
        assert json.loads(out.out)["info"]["version"] == version
