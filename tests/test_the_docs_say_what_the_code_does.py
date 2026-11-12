"""The written promises (README, .env.example, CHANGELOG, docs, the VS Code
demo) checked against the code they describe, audit of 17 Oct 2026. Each
test names the sentence that was wrong, so a later edit that brings it back
fails here rather than in a customer's hands."""
import ipaddress
import re
from pathlib import Path

from tests import docs_text

ROOT = Path(__file__).resolve().parent.parent

# The texts a reader copies from. Not the tests: they may name a public
# address on purpose (an SSRF case, example.com's).
_TEXTS = ["README.md", ".env.example", "CHANGELOG.md", ".github/SECURITY.md", ".github/CONTRIBUTING.md"]
_TREES = ["docs", "examples"]
_SUFFIXES = {".md", ".py", ".sh", ".txt", ".example", ".json", ".yml", ".yaml"}


def _flat(path: str) -> str:
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def _texts():
    for name in _TEXTS:
        if (ROOT / name).is_file():
            yield ROOT / name
    for tree in _TREES:
        for path in sorted((ROOT / tree).rglob("*")):
            if (path.is_file() and path.suffix in _SUFFIXES
                    and "node_modules" not in path.parts):
                yield path


_DOTTED = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w-]|\.\d)")
_DASHED = re.compile(r"(?<![\w.-])(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})\.(?:nip|sslip)\.io")


def test_no_text_names_a_routable_address():
    """README, .env.example and the demo used 2.29.27.64, somebody else's
    routable address, and a measurement report named the rented test machine
    by its IP: an example address comes from RFC 5737 (192.0.2.0/24,
    198.51.100.0/24, 203.0.113.0/24), or is private or loopback."""
    found = []
    for path in _texts():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in (_DOTTED, _DASHED):
            for m in pattern.finditer(text):
                try:
                    ip = ipaddress.IPv4Address(".".join(m.groups()))
                except ValueError:
                    continue  # a version number or a date, not an address
                if ip.is_global:
                    line = text.count("\n", 0, m.start()) + 1
                    found.append(f"{path.relative_to(ROOT)}:{line}: {m.group(0)}")
    assert not found, "use a documentation address (RFC 5737): " + "; ".join(found)


def test_every_changelog_entry_says_what_to_do_before_and_after():
    """CONTRIBUTING.md: every entry tells whoever upgrades what to do before and
    after. 0.1.25, 0.1.29 and 0.1.31-0.1.35 had no such section; an entry
    with nothing to do says so ("Nothing to do"), as 0.1.36 does. Older
    entries name it "Before you upgrade"; from 0.1.36 on it is "Before and
    after upgrading"."""
    entries = re.split(r"^## ", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
                       flags=re.M)[1:]
    missing = [e.splitlines()[0] for e in entries
               if not re.search(r"^### Before (and after upgrading|you upgrade)$", e, re.M)]
    assert not missing, f"entries without what to do before and after: {missing}"


def test_the_readme_does_not_promise_a_project_delete_can_be_undone():
    """Without `erase` a deleted project's rows move to Default and nothing
    marks where they came from (services/projects.py): no route moves them
    out again, and a fact Default already held absorbs its twin. The README
    said the move "can be undone"; the dashboard's dialog never did."""
    docs = docs_text.flat()
    assert "because it can be undone" not in docs
    assert "nothing marks where it came from" in docs


def test_the_readme_says_a_rename_reaches_the_past():
    """The README's opening said "Nothing is overwritten". An alias and a
    review of the vocabulary rewrite the canonical names of past facts in
    place and merge open twins (memory_core.store.recanonicalize,
    merge_into, review_the_variants), so `as_of` reads today's names."""
    docs = docs_text.flat()
    assert "Nothing is overwritten" not in docs
    assert "A rename reaches the past." in docs


def test_the_readmes_shell_table_has_every_manage_command():
    """`recompute-vectors` was missing from the README's table of what an
    operator runs from a shell (audit of 17 Oct 2026); the table is read
    against the commands manage.py registers. Since the README became a
    landing page (29 Oct 2026) the table is the section "From a shell" of
    docs/guide/api.md, one heading level up."""
    commands = re.findall(r'add_parser\(\s*"([a-z-]+)"',
                          (ROOT / "korely_agent/manage.py").read_text(encoding="utf-8"))
    api = docs_text.page("docs/guide/api.md")
    table = api.split("\n## From a shell\n", 1)[1].split("\n## ", 1)[0]
    assert commands
    missing = [c for c in commands if f"`{c}`" not in table]
    assert not missing, f"commands not in the From a shell table of docs/guide/api.md: {missing}"


def test_the_readme_describes_health_and_status_as_they_answer():
    """After a restart or a restore /health reads `working` off the last
    extraction the store holds (main._extraction_now), not `unknown`;
    `manage status` prints an `embeddings` line with the default local
    vectors, so it is not "five lines", and STUCK is the `extraction` line,
    not the last one."""
    docs = docs_text.flat()
    assert "what it says again after a restart" not in docs
    assert "says `unknown` until the next model call" not in docs
    assert "the whole install in five lines" not in docs
    assert "the last line says `STUCK`" not in docs
    assert "embeddings  local, embeddinggemma-300m" in docs_text.text()


def test_the_readme_matches_the_code_on_errors_vocabulary_and_export():
    """Each sentence was a fact the code no longer kept: the Cloud runs this
    code and sends `detail` too (api/errors.error_body); the survival test
    meets every migration since its start, not four; the vocabulary is one
    per end user of a project since migration e7d1b3f9a2c6 (services/predicates.end_user_vocabulary;
    it was one per account, and the README said so until the audit of 18 Oct
    2026), and its manage commands ask each project's region
    (predicates._asked_in, manage._vocabularies); the export writes one file
    per end user id of the account, across its projects
    (services/export_md.export_tenant)."""
    docs = docs_text.flat()
    assert "answers `{code, message}` alone, which is why" not in docs
    assert "across four migrations" not in docs
    assert "the relations this install already knows" not in docs
    assert "a relation the install has not seen" not in docs
    assert "one per account, shared by its projects" not in docs
    assert "the writer of the account's region" not in docs
    assert "the relations this project already knows" not in docs
    assert "shared by its end users" not in docs
    assert "the relations this end user already has" in docs
    assert "the vocabulary is one per end user of a project" in docs
    assert "the same id in two projects, or from two agents, is one file" in docs


def test_the_readme_names_the_reads_that_leave_sessions_out():
    """Audit of 19 Oct 2026: the README said "Every read takes
    `exclude_sessions=true`", but GET /v1/events, a memory by id, its
    history, the users, agents, audit and batch reads have no such
    parameter and ignore it. The README names the reads that take it, and
    each one it names declares the parameter in its handler."""
    docs = docs_text.flat()
    assert "Every read takes `exclude_sessions=true`" not in docs
    sentence = docs.split("take `exclude_sessions=true`", 1)[0].rsplit(". ", 1)[1]
    named = re.findall(r"`(GET|POST) (/v1/[a-z/]+)`", sentence)
    tools = re.findall(r"`(korely_[a-z_]+)`", sentence)
    assert len(named) == 5 and len(tools) == 3, (named, tools)
    from korely_agent.api import context, facts, memories

    routes = [r for m in (context, facts, memories) for r in m.router.routes]
    for method, path in named:
        route = next(r for r in routes if r.path == path and method in r.methods)
        params = {p.name for p in route.dependant.query_params}
        params |= {f for p in route.dependant.body_params
                   for f in getattr(p.field_info.annotation, "model_fields", {})}
        assert "exclude_sessions" in params, (method, path)
    server = (ROOT / "korely_agent/mcp/server.py").read_text(encoding="utf-8")
    for tool in tools:
        signature = server.split(f"async def {tool}(", 1)[1].split(") -> str:", 1)[0]
        assert "exclude_sessions" in signature, tool


def test_the_readme_gives_the_vocabulary_no_project_or_account_scope():
    """Audit of 19 Oct 2026: three README sentences still gave the vocabulary
    the scope of a project or an account (a relation "the project has never
    seen" cost one call "once", a relation "the project has learned" held
    one value, the one-value vote was "kept for that account"), while it is
    one per end user of a project (services/predicates.end_user_vocabulary,
    decide_if_undecided votes in the end user's own)."""
    docs = docs_text.flat()
    for stale in ("name the project has never seen", "A relation the project has learned",
                  "majority of three answers is kept for that account"):
        assert stale not in docs, stale
    assert "name this end user has never used costs one model call, once for that end user" \
        in docs
    assert "majority of three answers is kept in that end user's vocabulary" in docs


def test_what_a_new_secret_key_costs_is_written_down():
    """SECRET_KEY seals the screen's model choice (services/model_choice),
    the OAuth client secrets (services/oauth) and keys the erasures'
    pseudonyms (services/pseudonyms). .env.example said changing it
    "Nothing breaks", and the README's backup section named the model key
    alone."""
    env = _flat(".env.example")
    assert "Nothing breaks" not in env
    for text in (env, docs_text.flat()):
        assert "OAuth client" in text
        assert "pseudonym" in text
        assert "no memory is mined until" in text


def test_every_install_time_says_what_it_measured():
    """The README and the demo's runsheet gave 1m 39s and 3m 41s as the
    install time; both were measured on 0.1.13-0.1.14 with a 627 MB image,
    before 0.1.19 made the build download EmbeddingGemma (Dockerfile, stage
    `model`). Each place that quotes the numbers says on what, and that it
    is older (the private repository's own notes: tests/test_private_texts.py)."""
    text = docs_text.flat()
    assert "1m 39s" in text or "1 min 39 s" in text
    assert "0.1.19" in text and "0.1.14-dev" in text


# SHA-256 of the Apache License 2.0 as the ASF publishes it, byte for byte,
# appendix included: https://www.apache.org/licenses/LICENSE-2.0.txt. The same
# bytes as korely_agent/docs_ui/LICENSE (Swagger UI's, taken unmodified from
# npm) and as SPDX's Apache-2.0 text file.
_APACHE_20_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


def test_the_licence_is_the_apache_licence_word_for_word():
    """The product moved from the BSL 1.1 to the Apache License
    2.0 for its first public release (27 Oct 2026). LICENSE is the licence
    itself, unmodified: the copyright line and the notices go in NOTICE
    (section 4(d)), never into the licence's text."""
    import hashlib

    text = (ROOT / "LICENSE").read_bytes()
    assert hashlib.sha256(text).hexdigest() == _APACHE_20_SHA256
    assert hashlib.sha256((ROOT / "korely_agent/docs_ui/LICENSE").read_bytes()).hexdigest() \
        == _APACHE_20_SHA256


def test_readme_notice_and_the_packages_name_the_same_licence():
    """The README, NOTICE, pyproject.toml and the dashboard's package.json
    say what LICENSE says, and NOTICE keeps the copyright line, the name the
    licence does not grant, and the third-party notices it carried."""
    import json
    import tomllib

    notice = _flat("NOTICE")
    assert "Copyright 2026 Massimiliano Martella" in notice
    assert "Licensed under the Apache License, Version 2.0. See LICENSE." in notice
    assert 'The name and logo "Korely" are not licensed' in notice
    assert "Gemma is provided under and subject to the Gemma Terms of Use" in notice
    assert "Swagger UI" in notice
    readme = _flat("README.md")
    licence = readme.split("## License", 1)[1].split(" ## ", 1)[0]
    assert licence.startswith(" Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).")
    assert "host it for other people" in licence
    assert "github.com/korely-ai/korely/issues" in readme.split("## Support", 1)[1]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "Apache-2.0"
    assert set(project["license-files"]) == {"LICENSE", "NOTICE"}
    assert not any(c.startswith("License ::") for c in project.get("classifiers", []))
    package = json.loads((ROOT / "dashboard/package.json").read_text(encoding="utf-8"))
    assert package.get("license") == "Apache-2.0"


# What the BSL left in the texts. A dated record keeps what was true on its
# date (the released CHANGELOG entries, docs/misure, the extraction map of
# September); every other text says what is true now.
_THE_OLD_LICENCE = re.compile(
    r"Business Source License|\bBUSL\b|\bBSL\b|source[- ]available|Additional Use Grant",
    flags=re.I)
_DATED_RECORDS = {"CHANGELOG.md", "docs/MIGRATION_MAP.md", Path(__file__).name}
_NOT_TEXT = {"package-lock.json", "requirements.lock.txt", "requirements-dev.lock.txt"}
_TEXT_SUFFIXES = {".md", ".py", ".sh", ".txt", ".toml", ".json", ".yml", ".yaml",
                  ".ts", ".tsx", ".html", ".example", ".ini", ".cfg", ""}
_NOT_SOURCE = {".git", "node_modules", "dist", "__pycache__", ".claude", "misure"}


def _current_lines():
    """(relative path, line number, line) of every text of the tree but the
    dated records: what a reader takes as true now."""
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if (not path.is_file() or path.suffix not in _TEXT_SUFFIXES
                or set(rel.parts) & _NOT_SOURCE or path.name in _NOT_TEXT
                or rel.as_posix() in _DATED_RECORDS or path.name in _DATED_RECORDS):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            yield rel, n, line


def test_no_text_still_claims_the_old_licence():
    """After the move to Apache 2.0 (27 Oct 2026) no text outside a dated
    record may say the product is under the BSL, source-available, or bound
    by a use grant: a reader believes the line that is wrong."""
    found = [f"{rel}:{n}: {line.strip()}" for rel, n, line in _current_lines()
             if _THE_OLD_LICENCE.search(line)]
    assert found == [], "still under the old licence:\n" + "\n".join(found)
    # The release that made the move says so: tests/test_private_texts.py,
    # since the public history starts after it.


# A repository of the public organisation, as an address names it: github.com,
# raw.githubusercontent.com, ghcr.io, a badge, `gh --repo`.
_PUBLIC_ORG_REPO = re.compile(r"\bkorely-ai/([A-Za-z0-9_.-]+?)(?:\.git)?(?![A-Za-z0-9_-])")


def test_every_text_names_the_public_repository_by_its_name():
    """The public repository was renamed (5 Nov 2026, CHANGELOG 1.0.9), and
    its CI pushes the image under the new name (ghcr.io/<owner>/<repository>,
    .github/workflows/image.yml): an installer, a compose comment or a guide
    left on the old name pulls an image no release has. install.sh holds the
    name once (PUBLIC_REPO); every other text that names a repository of the
    organisation names that one. The CHANGELOG's entries keep the name of
    their day."""
    public = re.search(r'^PUBLIC_REPO="([^"]+)"$', (ROOT / "install.sh").read_text(),
                       re.M).group(1)
    owner = public.split("/", 1)[0]
    assert owner == "korely-ai"
    found = [f"{rel}:{n}: {line.strip()}" for rel, n, line in _current_lines()
             for m in _PUBLIC_ORG_REPO.finditer(line) if f"{owner}/{m.group(1)}" != public]
    assert found == [], f"not {public}:\n" + "\n".join(found)


def test_every_release_from_0_1_41_has_a_security_section():
    """SECURITY.md promises a `### Security` section in every release's
    entry from 0.1.41 on, and 0.1.41 shipped without one although it carried
    security fixes (audit of 18 Oct 2026). Unreleased is held to it
    too, so the release cannot forget it."""
    promise = _flat(".github/SECURITY.md")
    assert "From 0.1.41 on, every release's entry in [CHANGELOG.md](../CHANGELOG.md) has a `### Security` section" in promise
    entries = re.split(r"^## ", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
                       flags=re.M)[1:]

    def version(head):
        m = re.match(r"(\d+)\.(\d+)\.(\d+)", head)
        return tuple(int(x) for x in m.groups()) if m else None

    held = [e for e in entries
            if e.startswith("Unreleased") or (version(e) or (0,)) >= (0, 1, 41)]
    assert held
    missing = [e.splitlines()[0] for e in held if not re.search(r"^### Security$", e, re.M)]
    assert not missing, f"entries without ### Security: {missing}"


def test_the_readme_says_what_a_correction_and_the_machine_are():
    """A PATCH of a fact dated ahead begins where the old value was to begin
    (facts/service.correct_fact, `begins = max(now, valid_from)`), not
    "today"; and both installs the README quotes ran on a 2 vCPU machine
    with about 3.8 GB, never on two gigabytes (the reports:
    tests/test_private_texts.py)."""
    docs = docs_text.flat()
    assert "the replacement starts today and the old value ends today" not in docs
    assert "both happen where the old value was to begin" in docs
    assert "two cores and two gigabytes" not in docs
    assert "two cores and four gigabytes" in docs


def test_the_swagger_ui_version_is_where_its_readme_says():
    """korely_agent/docs_ui/README.md sent whoever updates Swagger UI to
    korely_agent/main.py, which never held the version: every place
    it names holds the version it states. Its "Talking to it" is
    docs/guide/api.md since the README became a landing page (29 Oct 2026)."""
    note = _flat("korely_agent/docs_ui/README.md")
    v = re.search(r"`swagger-ui-dist` (\d+\.\d+\.\d+)", note).group(1)
    assert "korely_agent/main.py" not in note
    for named, text in (("docs/guide/api.md", _flat("docs/guide/api.md")), ("`NOTICE`", _flat("NOTICE")),
                        ("`docs/DEPENDENCIES.md`", _flat("docs/DEPENDENCIES.md"))):
        assert named in note, named
        assert v in text, named


def test_the_key_doors_comments_say_the_prefix_is_refused():
    """Audit of 19 Oct 2026: since 0.2.0 a `kor_live_` key on an install of
    one's own is refused on its prefix before any lookup (keys.refused_here),
    yet core/auth said a key is decided "never by its prefix", that the
    prefix "only chooses the wording", and reasoned about a revoked key
    minted here before 0.1.3; api/errors said every /v1 409 names
    `stale_write`. The next person changing these reads them first."""
    from korely_agent.core import auth

    for doc in (auth.verify_api_key.__doc__, auth.refusal_for.__doc__):
        flat = " ".join(doc.split())
        assert "never by its prefix" not in flat
        assert "The prefix only chooses the wording" not in flat
        assert "minted here before 0.1.3 get the same words" not in flat
    assert "every /v1 409 here names `stale_write`" not in _flat("korely_agent/api/errors.py")


# ── texts left behind by 0.2.0 (audit of 20 Oct 2026) ────────────────────────

def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# The documentation's pages, each read on its own like the other files.
_DOCS = [p.relative_to(ROOT).as_posix() for p in docs_text.pages()]


def test_no_text_describes_what_0_2_0_removed():
    """Each sentence below described a behaviour 0.2.0 removed or changed:
    the cost rows hold no end user since d4b7e2a9c8f1, a legacy endpoint is
    not sent at all, the judge's switch was measured, no kor_live_ key is
    live on an install of one's own, the reclassification asks the project's
    region, and /health's key is `writers`."""
    gone = {
        "the cost rows lose": ["korely_agent/api/memories.py", "korely_agent/services/projects.py"],
        "which its receiver expects": ["dashboard/src/lib/agentsApi.ts"],
        "Off until the\n# measure of 19 Oct": ["korely_agent/services/facts/contradiction.py"],
        "that is not a live key here": ["korely_agent/api/ping.py"],
        "model key for the account's region": ["korely_agent/services/predicates.py"],
        "`writers_refused`": ["CHANGELOG.md", *_DOCS],
        "memories_forgotten: number": ["dashboard/src/lib/agentsApi.ts"],
    }
    found = [(words, rel) for words, files in gone.items() for rel in files
             if words in _text(rel)]
    assert found == [], found


def test_the_dashboards_project_delete_type_is_the_routes():
    from korely_agent.api.admin import ProjectDeleted

    ts = _text("dashboard/src/lib/agentsApi.ts")
    body = ts[ts.index("export interface ProjectDeleteResult {"):]
    body = body[:body.index("\n}\n")]
    fields = set(re.findall(r"^\s+(\w+)\??:", body, re.M))
    assert fields == set(ProjectDeleted.model_fields), fields ^ set(ProjectDeleted.model_fields)


def test_the_fifth_search_is_described_door_by_door():
    """The notes said 503 for GET /v1/context too, which answers 200
    degraded (its design since it exists). The entry that said so:
    tests/test_private_texts.py."""
    flat = docs_text.flat()
    assert 'GET /v1/context` with 200' in flat and '`degraded_parts: ["memories"]`' in flat


def test_a_project_left_without_a_region_is_described_per_product():
    """The public spec (made from the Cloud's) said such a project
    follows the install's writer; on the Cloud it is stored Global."""
    from korely_agent.api import admin

    for fn in (admin.create_project, admin.update_project):
        doc = " ".join((fn.__doc__ or "").split())
        assert "Global on the Cloud" in doc or "on the Cloud the project is Global" in doc, fn


def test_the_dense_batch_refusal_is_in_the_spec_and_names_its_ceiling():
    """/docs said every 413 of POST /v1/batch is a body past the byte
    ceiling, refused before it is read, and the message named the values'
    ceiling when the strings' had refused the body."""
    from korely_agent.main import create_app
    from korely_agent.services import limits

    spec = create_app().openapi()
    text = spec["paths"]["/v1/batch"]["post"]["responses"]["413"]["description"]
    assert f"{limits.batch_json_strings_cap():,}" in text and f"{limits.batch_json_values_cap():,}" in text
    strings = b'{"memories":[' + b'"",' * (limits.batch_json_strings_cap() + 1) + b'""]}'
    assert "JSON strings" in limits.body_weighed(strings, limits._batch_items())[0]


def test_the_search_503_says_the_embedders_rule_of_today():
    """Audit of 27 Oct 2026: the OpenAPI, the README and the
    MCP's error still said 0.2.0's rule, four searches answered at once;
    since 0.2.1 the account's places are shared by its searches and its
    writes' vectors, and a request also gives up after WAIT_SECONDS. Since
    31 Oct 2026 a search past its places waits its turn, and the places are
    one count for REST and MCP (core/threads.waits_for_the_embedder)."""
    from korely_agent.core import threads
    from korely_agent.main import create_app

    spec = create_app().openapi()
    text = spec["paths"]["/v1/memories/search"]["post"]["responses"]["503"]["description"]
    assert f"{threads.CALLS_PER_ACCOUNT} places" in text and "writes" in text, text
    assert "waits its turn" in text and "REST and MCP" in text and "Retry-After" in text, text
    assert "per process" in text, text
    assert f"{threads.WAIT_SECONDS:.0f} s" in text, text
    docs = docs_text.flat()
    assert "four searches" not in docs
    assert "four vectors waiting there at once" not in docs
    assert ("an account has four places there per API process (two processes, eight), "
            "which its searches, its contexts and the writes of its requests share, over "
            "REST and MCP together") in docs
    server = _text("korely_agent/mcp/server.py")
    assert "embedding provider is not reachable" not in server


def test_the_public_field_texts_quote_no_benchmark():
    """Audit of 27 Oct 2026: SearchBody.min_score and GET /v1/context's
    token_budget quoted LongMemEval and LoCoMo numbers, with their dates, as
    guidance in the spec customers read; the spec says what a parameter does."""
    from korely_agent.main import create_app

    spec = create_app().openapi()
    min_score = spec["components"]["schemas"]["SearchBody"]["properties"]["min_score"]
    budget = next(p for p in spec["paths"]["/v1/context"]["get"]["parameters"]
                  if p["name"] == "token_budget")
    for what, text in (("min_score", min_score.get("description", "")),
                       ("token_budget", budget.get("description", ""))):
        assert text, what
        for word in ("LongMemEval", "LoCoMo", "%", "2026"):
            assert word not in text, (what, word, text)
