"""The sentences an operator reads (README, .env.example, the dashboard, the
settings' comments) tied to what the code does, so that the day one of the two
moves the other has to move with it (audit of 17 Oct 2026, the documentation
findings). Each test names the code value it reads, never a copy of it."""
from pathlib import Path

from korely_agent.config import AgentSettings

from tests import docs_text

ROOT = Path(__file__).resolve().parent.parent


def _flat(path: str) -> str:
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def _comments(path: str) -> str:
    """The prose of a file of comments, .env.example: each line's leading
    `#` taken out, so that a sentence reads across its lines."""
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
    return " ".join(" ".join(line.lstrip("#").strip() for line in lines).split())


def _default(field: str):
    return AgentSettings.model_fields[field].default


def test_no_text_says_a_read_costs_nothing_because_it_is_sql():
    """A search and a context embed their question once (services/context.py
    `_query_vector`, services/memory_engine.py `search`), billed to the Google
    key with EMBEDDING_PROVIDER=gemini; only the other reads are SQL alone."""
    docs = docs_text.flat()
    assert "Reads cost nothing because they are SQL" not in docs
    assert "a search and a context compute one vector of the question" in docs
    assert _default("embedding_provider") == "local"
    assert "with the default `EMBEDDING_PROVIDER=local`" in docs
    assert "Reads are SQL." not in _flat("korely_agent/config.py")


def test_the_model_layer_does_not_say_reads_are_sql_alone():
    assert "Reads are SQL." not in _flat("korely_agent/core/llm.py")


def _rate_cards(text: str) -> list:
    """Every example rate card written in `text`: the JSON object that
    starts at each `{"currency"`, read with its braces balanced (a comment's
    `#` taken out first, since a card spread over comment lines carries it)."""
    import json

    text = text.replace("#", " ")
    cards, at = [], text.find('{"currency"')
    while at != -1:
        depth = 0
        for end in range(at, len(text)):
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            if depth == 0:
                break
        cards.append(json.loads(text[at:end + 1]))
        at = text.find('{"currency"', end)
    return cards


EXAMPLE_CARDS = (".env.example", "korely_agent/config.py",
                 "dashboard/src/pages/UsagePage.tsx")


def test_the_example_rate_card_prices_the_writer_an_install_has():
    """The Usage page tells an operator to copy its LLM_RATES: it priced
    gemini-2.5-flash, which a new Google account cannot use and no install
    calls, while .env.example priced gemini-3.6-flash with the same numbers.
    One card, naming the default writer (audit of 17 Oct 2026)."""
    from korely_agent.services.usage_report import parse_rates

    found = {path: _rate_cards((ROOT / path).read_text(encoding="utf-8"))
             for path in EXAMPLE_CARDS}
    found["usage_report.parse_rates"] = _rate_cards(parse_rates.__doc__)
    assert all(len(cards) == 1 for cards in found.values()), found
    cards = [cards[0] for cards in found.values()]
    assert all(card == cards[0] for card in cards), found
    assert list(cards[0]["models"]) == [_default("extraction_model")]


def test_the_install_times_name_the_version_they_were_measured_on():
    """1m 39s and 3m 41s were measured on 0.1.14 and 0.1.13, with an image
    of 627 MB; since 0.1.19 the build downloads the embedding model, about
    1.2 GB (the Dockerfile's `model` stage). The README presented them as
    today's (audit of 17 Oct 2026). The reports they come from:
    tests/test_private_texts.py."""
    docs = docs_text.flat()
    assert "fact, scripted, on 0.1.14-dev, with an application image of 627 MB" in docs
    # The 3m 41s was an agent that had never seen the code, allowed only the
    # public documentation (docs/benchmarks.md, "Install time").
    assert "following only the public documentation, on 0.1.13" in docs
    assert "AS model" in _flat("Dockerfile")
    assert "Both are older than 0.1.19" in docs


# The settings that choose a model or change how a write runs, which no text
# for the operator named (audit of 17 Oct 2026): with EXTRACTION_PROVIDER=mistral
# and a host of one's own, the model asked was `mistral-small-latest`, and only
# scripts/choose_models.py ever wrote that line.
DOCUMENTED_SETTINGS = ("extraction_model", "mistral_extraction_model", "vocabulary_model",
                       "embedding_model", "write_async", "drain_stale_minutes",
                       "dashboard_dir", "korely_mode")


def test_env_example_names_every_model_setting_with_its_default():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for field in DOCUMENTED_SETTINGS:
        default = _default(field)
        if default is None:
            line = f"# {field.upper()}="
        elif isinstance(default, bool):
            line = f"# {field.upper()}={str(default).lower()}"
        else:
            line = f"# {field.upper()}={default}"
        assert line in example.splitlines(), line


def test_env_example_says_extraction_runs_in_the_request_only_when_asked():
    """LLM_TIMEOUT_SECONDS' comment said extraction runs inside the request
    that wrote the memory; with WRITE_ASYNC on, the default, the worker runs
    it (api/memories.py)."""
    assert _default("write_async") is True
    assert "Extraction runs inside the request that wrote the memory" not in _comments(".env.example")


def test_env_example_says_a_memory_without_a_key_waits():
    """With no writer the worker claims nothing (worker._facts): a memory
    written then stays 'pending' and the first tick after a key mines it.
    .env.example said it was marked 'error' until `manage reextract`
    (audit of 17 Oct 2026)."""
    import inspect

    from korely_agent import worker

    assert '"no_writer": True' in inspect.getsource(worker._facts)
    example = _comments(".env.example")
    assert "is marked `error`" not in example
    assert "A memory written while there is no key waits, `pending`" in example


def test_the_workers_start_says_a_memory_without_a_key_waits(monkeypatch, caplog):
    """The worker's warning at start, with no key, said the same as
    .env.example: 'error', and `manage reextract` (audit of 17 Oct 2026)."""
    import asyncio
    import logging

    from korely_agent import worker
    from korely_agent.core import llm

    monkeypatch.setattr(llm, "provider_configured", lambda: False)
    monkeypatch.setattr(worker, "_JOBS", [])
    with caplog.at_level(logging.WARNING, logger=worker.logger.name):
        asyncio.run(worker.run_forever(asyncio.Event()))
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "starting with no model key" in said
    assert "'error'" not in said and "reextract" not in said
    assert "'pending'" in said


def test_the_readme_does_not_say_a_lost_secret_key_costs_only_the_model_key():
    """SECRET_KEY also keys the erasures' pseudonyms (services/pseudonyms.py):
    restored under another key, `GET /v1/audit?user_id=` no longer finds an
    erased end user's records. 'Which is the whole damage' was false (audit
    of 17 Oct 2026)."""
    import inspect

    from korely_agent.services import pseudonyms

    assert "secret_key" in inspect.getsource(pseudonyms._key) + inspect.getsource(
        getattr(pseudonyms, "derived_key", pseudonyms._key))
    docs = docs_text.flat()
    assert "which is the whole damage" not in docs
    assert "the pseudonyms the erasures left no longer match" in docs


def test_a_vulnerability_has_an_address_and_no_issue_takes_it():
    """No SECURITY.md, no CONTRIBUTING.md, no issue templates: a vulnerability
    found by a stranger would have gone to an issue (audit of 17 Oct 2026).
    The address is the one korely.ai/.well-known/security.txt publishes."""
    import yaml

    address = "security@korely.ai"
    assert address in _flat(".github/SECURITY.md")
    assert "SECURITY.md" in _flat(".github/CONTRIBUTING.md") and address in _flat(".github/CONTRIBUTING.md")
    assert address in docs_text.flat()
    templates = ROOT / ".github" / "ISSUE_TEMPLATE"
    config = yaml.safe_load((templates / "config.yml").read_text(encoding="utf-8"))
    assert config["blank_issues_enabled"] is False
    assert any(address in link["about"] for link in config["contact_links"])
    for form in templates.glob("*.yml"):
        if form.name != "config.yml":
            loaded = yaml.safe_load(form.read_text(encoding="utf-8"))
            assert loaded["name"] and loaded["body"], form.name
    assert address in _flat(".github/ISSUE_TEMPLATE/bug_report.yml")
