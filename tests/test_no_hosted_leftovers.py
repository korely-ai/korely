"""What the self-hosted product still said about the hosted one.

This codebase was extracted from the hosted service, and the extraction left
sentences behind that were true there and are false here: a dashboard README
about Firebase sign-in and a Cloudflare Pages site, a `.env.example` asking
for seven Firebase values nothing reads, a Cloudflare Pages redirect file
served as a static asset, `/v1/ping` documented against api.korely.ai, comments
naming environment variables (`AGENT_F3_PROVIDER`, `AGENT_WRITE_ASYNC`) that
this configuration has never had. Each one sends somebody reading it to do
something that does not exist on their machine.
"""
import re
from pathlib import Path

import pytest

from tests import docs_text
from tests.test_which_version_and_the_way_back import _what_a_customer_reads, is_hosted_only

ROOT = Path(__file__).resolve().parents[1]


def test_no_address_of_the_hosted_service_to_call():
    """Naming the hosted service to explain the difference is fine (a key
    beginning `kor_live_` belongs to it). Its address as something to call is
    an instruction to send your data somewhere else."""
    address = re.compile(r"https?://api\.korely\.ai")
    offenders = [str(f.relative_to(ROOT)) for f in _what_a_customer_reads(ROOT)
                 if address.search(f.read_text())]
    assert offenders == [], f"the hosted address, in: {offenders}"


def test_the_dashboard_declares_only_the_variables_it_reads():
    declared = set(re.findall(r"readonly\s+(VITE_[A-Z0-9_]+)",
                              (ROOT / "dashboard/src/vite-env.d.ts").read_text()))
    assert declared, "the parser found nothing to check"
    sources = [p for p in (ROOT / "dashboard/src").rglob("*")
               if p.suffix in (".ts", ".tsx") and p.name != "vite-env.d.ts"]
    sources.append(ROOT / "dashboard/vite.config.ts")
    text = "\n".join(p.read_text() for p in sources)
    unread = sorted(v for v in declared if v not in text)
    assert unread == [], f"declared and read by nothing: {unread}"


def test_no_example_configuration_for_a_login_this_install_does_not_have():
    for f in ("dashboard/README.md", "dashboard/.env.example"):
        p = ROOT / f
        if p.exists():
            assert "FIREBASE" not in p.read_text().upper(), f"{f} still describes Firebase"


def test_no_file_for_a_hosting_platform_in_the_screen():
    """dashboard/public is copied into the build as it is and served by the
    application. `_redirects` is a Cloudflare Pages rule file; here it was a
    static asset at /_redirects, and the fallback it described is api/spa.py's
    job."""
    platform_files = {"_redirects", "_headers", "_routes.json", "netlify.toml",
                      "vercel.json"}
    present = sorted(p.name for p in (ROOT / "dashboard/public").iterdir()
                     if p.name in platform_files)
    assert present == []


def test_no_environment_variable_named_the_hosted_way():
    """The settings here have no prefix: `write_async` is WRITE_ASYNC. A
    comment that says AGENT_WRITE_ASYNC describes a variable an operator can
    set and nothing will read."""
    named = re.compile(r"(?<![A-Za-z0-9_])AGENT_[A-Z0-9_]+")
    files = [*docs_text.pages(), ROOT / ".env.example", ROOT / "docker-compose.yml"]
    for top in ("korely_agent", "scripts", "tests"):
        files += [p for p in (ROOT / top).rglob("*.py")
                  if "memory_core" not in p.parts and p != Path(__file__).resolve()
                  and not is_hosted_only(ROOT, p)]
    files += sorted((ROOT / "scripts").glob("*.sh"))
    offenders = {}
    for f in files:
        found = sorted(set(named.findall(f.read_text())))
        if found:
            offenders[str(f.relative_to(ROOT))] = found
    assert offenders == {}, f"no such variable here: {offenders}"


def _string_constants(path: Path) -> set:
    import ast

    return {n.value for n in ast.walk(ast.parse(path.read_text()))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def test_every_webhook_event_you_can_subscribe_to_is_one_the_product_sends():
    """`quota.warning` fired at 80% of the hosted plan's monthly write quota.
    The quota did not come across and the event stayed subscribable: an
    endpoint could wait for it for ever."""
    from korely_agent.services import webhooks

    sent = set()
    for p in (ROOT / "korely_agent").rglob("*.py"):
        if "memory_core" not in p.parts and p.name != "webhooks.py":
            sent |= _string_constants(p)
    never = sorted(webhooks.VALID_EVENTS - sent)
    assert never == [], f"subscribable and never emitted: {never}"


def test_the_dashboard_offers_exactly_the_events_there_are():
    """The screen listed memory.created, fact.invalidated and quota.warning.
    Ticking all three subscribed to `*`; unticking quota.warning, the one that
    never fires, subscribed to the other two and so, silently, to neither
    key.expiring nor key.expired, which the page never offered: the warning
    the README promises two weeks before a key stops working. Since 8 Oct 2026
    the page asks the server (GET /admin/webhooks/events), which answers
    VALID_EVENTS, the hosted service's own included; no list is written on
    the page."""
    from korely_agent.api import admin

    page = (ROOT / "dashboard/src/pages/WebhooksPage.tsx").read_text()
    assert "webhooksApi" in page and ".events()" in page, "the page does not ask the server"
    assert not re.search(r"const EVENTS\s*=\s*\[", page), "a list written on the page"
    from korely_agent.services import webhooks

    assert admin.webhook_events(key=None) == {"events": sorted(webhooks.VALID_EVENTS)}


def test_the_screen_does_not_describe_a_quota():
    """The Requests page told operators that writes count toward a monthly
    quota and reads are rate-limited and free; the Webhooks page promised a
    POST at 80% of quota. There is no quota, and the per-minute ceiling, when
    the operator sets one, counts reads and writes alike. Comments may say what
    used to be there; what the page shows may not."""
    offenders = []
    for p in sorted((ROOT / "dashboard/src").rglob("*")):
        if p.suffix not in (".ts", ".tsx") or is_hosted_only(ROOT, p):
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            code = line.strip()
            if code.startswith(("//", "*", "/*", "{/*")):
                continue
            if "quota" in code.lower():
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {code[:90]}")
    assert offenders == [], "a quota that does not exist, on the screen:\n" + "\n".join(offenders)


# The screen's words for one mode, checked from what the code says of each
# mode rather than one word ('quota'), which let the Agents page tell every
# install about the Cloud's plans and the Requests page tell Cloud customers
# that nothing is metered (audit of 17 Oct 2026). The Cloud's plans come from
# hosted/plans.py, the install's settings from config.AgentSettings, and which
# page each mode mounts from App.tsx: no list written here.

def _visible_lines(path):
    """The lines of a source that can reach the screen: not its comments,
    neither a `/* ... */` that runs over several lines (a JSX `{/* */}` note
    included) nor what follows `//` at the end of a line, and not its
    imports. Only a `//` after a space or at the start opens a comment, so an
    address (`https://`) stays code."""
    inside = False
    for i, line in enumerate(path.read_text().splitlines(), 1):
        code = line
        if inside:
            end = code.find("*/")
            if end < 0:
                continue
            code, inside = code[end + 2:], False
        while "/*" in code:
            start = code.find("/*")
            end = code.find("*/", start + 2)
            if end < 0:
                code, inside = code[:start], True
                break
            code = code[:start] + code[end + 2:]
        comment = re.search(r"(^|\s)//", code)
        if comment:
            code = code[:comment.start()]
        code = code.strip()
        # What a JSX comment leaves behind (`{}`) is no text either.
        if code and code != "{}" and not code.startswith("import "):
            yield i, code


def _mounted_for_one_mode():
    """{file: "hosted" | "self_hosted"} for the pages App.tsx mounts in one
    mode only (`hosted ? <A /> : <B />`, `hosted && ...<A />`)."""
    app = (ROOT / "dashboard/src/App.tsx").read_text()
    where = {name: ROOT / "dashboard/src" / f"{path}.tsx"
             for name, path in re.findall(r'import \{ (\w+)[^}]*\} from "@/([\w/]+)"', app)}
    # The pages loaded as their own chunks: `const X = page(() =>
    # import("@/pages/X"), "X")`. The Cloud's are `cloud.X` (lib/cloud.ts),
    # in its own folder.
    where.update({name: ROOT / "dashboard/src" / f"{path}.tsx"
                  for name, path in re.findall(
                      r'const (\w+) = (?:page|lazy)\(\(\) =>\s*import\("@/([\w/]+)"\)', app)})
    one = {}
    for a, b in re.findall(r"(?:hosted|cloud) \? <([\w.]+) /> : <(\w+) />", app):
        for name, mode in ((a, "hosted"), (b, "self_hosted")):
            if name in where:
                one[where[name]] = mode
    for name in re.findall(r"(?:hosted|cloud) && <Route[^>]*element=\{<([\w.]+) />", app):
        if name in where:
            one[where[name]] = "hosted"
    return one


def _screens():
    return [p for p in sorted((ROOT / "dashboard/src").rglob("*"))
            if p.suffix in (".ts", ".tsx") and not is_hosted_only(ROOT, p)]


def test_the_mode_reader_sees_the_pages_of_one_mode():
    """The control of the two tests below: without it they would pass on a
    parser that finds nothing."""
    one = _mounted_for_one_mode()
    assert one.get(ROOT / "dashboard/src/pages/UsagePage.tsx") == "self_hosted"
    assert one.get(ROOT / "dashboard/src/pages/LoginPage.tsx") == "self_hosted"


@pytest.mark.cloud
def test_no_plan_of_the_cloud_on_a_screen_that_does_not_ask_the_mode():
    from korely_agent.hosted import plans

    names = [p.name.capitalize() for p in plans.PLANS.values()]
    assert names, "no plan read"
    one = _mounted_for_one_mode()
    offenders = []
    for p in _screens():
        text = p.read_text()
        if one.get(p) != "self_hosted" and "useMode(" in text:
            continue
        for i, code in _visible_lines(p):
            said = [n for n in names if re.search(rf"\b{n}\b", code)]
            if said:
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {said} {code[:80]}")
    assert offenders == [], ("the Cloud's plans, on a screen every install shows:\n"
                             + "\n".join(offenders))


# The one shared page an install's settings may name without asking the mode:
# on the hosted service only the superadmin, who runs the server, reaches it
# (App.tsx, the /setup route).
_READ_BY_WHOEVER_RUNS_THE_SERVER = {ROOT / "dashboard/src/pages/SetupPage.tsx"}


def test_no_setting_of_the_install_on_a_shared_screen_that_does_not_ask_the_mode():
    """A Cloud customer sets no environment variable: a page both modes
    mount names one only in the install's branch, so it asks the mode."""
    from korely_agent.config import AgentSettings

    settings = [n.upper() for n in AgentSettings.model_fields]
    one = _mounted_for_one_mode()
    offenders = []
    for p in _screens():
        if p in one or p in _READ_BY_WHOEVER_RUNS_THE_SERVER or "useMode(" in p.read_text():
            continue
        for i, code in _visible_lines(p):
            said = [n for n in settings if re.search(rf"\b{n}\b", code)]
            if said:
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {said} {code[:80]}")
    assert offenders == [], ("an install's setting, told to the Cloud's customers too:\n"
                             + "\n".join(offenders))


# Two more readings of the same kind (audit of 17 Oct 2026): what is
# the Cloud's alone, read from the hosted package, on a screen every install
# shows. Its settings are HostedSettings' fields (HOSTED_*), its routes those
# of its routers; neither is written here.

def _hosted_routes():
    from fastapi import APIRouter

    from korely_agent.hosted import api_billing, api_identity

    paths = set()
    for module in (api_identity, api_billing):
        for value in vars(module).values():
            if isinstance(value, APIRouter):
                paths |= {r.path.split("{", 1)[0].rstrip("/") for r in value.routes}
    # A /v1 route the Cloud adds is called by agents, not by the screen; the
    # screen's own calls to the Cloud are its /hosted ones.
    return sorted(p for p in paths if not p.startswith("/v1"))


def _calls(code: str, path: str) -> bool:
    return re.search(rf"[\"'`]{re.escape(path)}(?=[/\"'`?$])", code) is not None


@pytest.mark.cloud
def test_the_route_reader_finds_the_clouds_calls_in_its_own_pages():
    """The control of the test below."""
    routes = _hosted_routes()
    assert routes, "no route read"
    api = (ROOT / "dashboard/src/hosted/api.ts").read_text()
    assert any(_calls(api, r) for r in routes), "the reader finds nothing where calls are"


@pytest.mark.cloud
def test_no_route_of_the_cloud_called_from_a_screen_that_does_not_ask_the_mode():
    """On an install of one's own the route is not there: a 404 in a page
    that never asked which product it is on."""
    routes = _hosted_routes()
    one = _mounted_for_one_mode()
    offenders = []
    for p in _screens():
        if one.get(p) == "hosted" or "useMode(" in p.read_text():
            continue
        for i, code in _visible_lines(p):
            said = [r for r in routes if _calls(code, r)]
            if said:
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {said} {code[:80]}")
    assert offenders == [], ("a route of the Cloud, called on every install:\n"
                             + "\n".join(offenders))


@pytest.mark.cloud
def test_no_setting_of_the_cloud_on_a_screen_that_does_not_ask_the_mode():
    """HOSTED_* is the service's configuration: an operator of an install
    never sets it, and a page every install shows names it only in the
    Cloud's branch."""
    from korely_agent.hosted.settings import HostedSettings

    prefix = HostedSettings.model_config["env_prefix"]
    settings = [f"{prefix}{n}".upper() for n in HostedSettings.model_fields]
    assert settings, "no setting read"
    one = _mounted_for_one_mode()
    offenders = []
    for p in _screens():
        if one.get(p) == "hosted" or "useMode(" in p.read_text():
            continue
        for i, code in _visible_lines(p):
            said = [n for n in settings if re.search(rf"\b{n}\b", code)]
            if said:
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {said} {code[:80]}")
    assert offenders == [], ("a setting of the Cloud, told to every install:\n"
                             + "\n".join(offenders))
