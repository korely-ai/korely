"""A failure on the dashboard is said as a failure (audit of 17 Oct 2026).

The dashboard has no test runner of its own (package.json: tsc and vite), so
these read its source, as tests/test_the_screen_says_what_it_shows.py does,
for the places that turned a failure into an answer or into silence:

- two rules read the server's error body: agentsApi.errorMessage took a
  string `detail` only, hosted.hostedError the `message` every body carries;
- a failed read became an empty list, a zero or an endless "Loading…", each
  page writing the read by hand;
- revoke, move and delete failed with nothing on screen;
- the copy buttons showed their tick when nothing was copied;
- a disabled webhook endpoint looked like a live one and could not be
  switched back on, and Delete asked nothing.
"""
import re
from pathlib import Path

from tests import cloud

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dashboard" / "src"


def _code(rel: str) -> str:
    """The file without its comments."""
    text = (SRC / rel).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def _all_sources():
    for path in SRC.rglob("*.ts*"):
        yield path.relative_to(SRC).as_posix(), _code(path.relative_to(SRC).as_posix())


def test_one_rule_reads_the_servers_error_body():
    reader = _code("lib/apiError.ts")
    # The top-level message error_body always sends (korely_agent/api/errors.py).
    assert "data.message" in reader and "data.code" in reader
    assert '"internal_error"' in reader
    api = _code("lib/agentsApi.ts")
    assert "apiError(e)?.message" in api and "apiError(e)?.code" in api
    assert 'typeof detail === "string" ? detail : null' not in api
    if cloud.HERE:
        assert "return apiError(e);" in _code("hosted/api.ts")
    # Nobody else opens the axios error to read its body.
    readers = [name for name, code in _all_sources()
               if "response?.data" in code or "isAxiosError" in code]
    assert readers == ["lib/apiError.ts"], readers


MY_PAGES = ("pages/HomePage.tsx", "pages/ApiKeysPage.tsx", "pages/WebhooksPage.tsx")


def test_one_read_pattern_keeps_failure_loading_and_answer_apart():
    hook = _code("lib/useLoad.ts")
    assert "errorMessage(e) ?? failedRef.current" in hook
    # A late answer to an earlier read is dropped (the 7-day chart picked
    # while the 30-day series is still out).
    assert hook.count("mine !== latest.current") == 2
    shell = _code("components/LoadState.tsx")
    assert "load.error" in shell and "onRetry={load.reload}" in shell
    for page in MY_PAGES:
        code = _code(page)
        assert "useLoad(" in code, page
        # A failure turned into an empty list, a zero or nothing at all.
        assert ".catch(() =>" not in code, page


def test_the_home_page_says_which_number_could_not_be_read():
    home = _code("pages/HomePage.tsx")
    assert "Promise.allSettled" not in home
    assert "?? 0).toLocaleString()" not in home
    assert "setHasKey(false)" not in home
    assert "keys.data ? keys.data.some(" in home and "keys.error" in home
    assert "<Loaded load={daily}" in home and "<Loaded load={summary}>" in home
    for read in ("memoriesStored", "usage", "summary"):
        assert f"error={{{read}.error}}" in home, f"the {read} card hides its failure"


def test_the_keys_page_shows_an_empty_list_only_when_the_list_was_read():
    keys = _code("pages/ApiKeysPage.tsx")
    assert "<Loaded load={listing}>" in keys
    # The lifetimes and scopes the dialog offers come from the server or are
    # said to be missing, never from a list written here.
    assert '{ days: 30, label: "30 days" }' not in keys
    assert "lifetimes.error" in keys and "scopeRead.error" in keys


def test_the_webhooks_page_says_when_events_or_projects_could_not_be_read():
    hooks = _code("pages/WebhooksPage.tsx")
    assert "<Loaded load={listing}>" in hooks
    # The projects are the switcher's one list (ProjectContext's
    # `projectsLoad`, audit of 17 Oct 2026), its failure said here too.
    assert "eventsRead.error" in hooks and "projectsLoad.error" in hooks
    assert '(projectsLoad.data ? "a deleted project"' in hooks


def _awaited_in_a_try(code: str, call: str) -> bool:
    """`await <call>` is the first statement of a try block."""
    return re.search(r"try \{\s*await " + re.escape(call), code) is not None


def test_revoke_move_and_delete_say_when_they_did_not_happen():
    keys = _code("pages/ApiKeysPage.tsx")
    assert _awaited_in_a_try(keys, "keysApi.revoke(id)")
    assert _awaited_in_a_try(keys, "keysApi.move(id, projectId)")
    assert "{actionError && <ErrorNote" in keys
    hooks = _code("pages/WebhooksPage.tsx")
    assert "webhooksApi.remove(h.id).then(load)" not in hooks
    assert _awaited_in_a_try(hooks, "webhooksApi.remove(")


def test_a_copy_button_shows_its_tick_only_when_the_text_was_copied():
    button = _code("components/CopyButton.tsx")
    # navigator.clipboard is missing off a secure context, and writeText can
    # refuse: neither may end in a tick.
    assert "window.isSecureContext" in button
    assert 'setState(ok ? "copied" : "failed")' in button
    assert "Not copied" in button
    for page in MY_PAGES:
        code = _code(page)
        assert "navigator.clipboard" not in code, page
        assert "<CopyButton" in code, page


def test_a_disabled_webhook_endpoint_is_shown_and_can_be_switched_back_on():
    api = _code("lib/agentsApi.ts")
    assert "/admin/webhooks/${id}/enable" in api
    hooks = _code("pages/WebhooksPage.tsx")
    assert "h.active ?" in hooks and "!h.active &&" in hooks
    assert "webhooksApi.enable(id)" in hooks
    # Delete asks first: the signing secret goes with the endpoint.
    assert re.search(r"const remove = async \(h: Webhook\) => \{\s*if \(\s*!confirm\(", hooks)
    # A test the receiver refused is not "sent".
    assert "out.delivered" in hooks
    # The route the screen now calls exists, and says what it keeps.
    admin = (ROOT / "korely_agent" / "api" / "admin.py").read_text(encoding="utf-8")
    assert '@router.post("/webhooks/{endpoint_id}/enable"' in admin
