"""The lines tests/test_logs_carry_no_exception_text.py used to allow
(audit of 17 Oct 2026) say what they need without an exception's
message: the local embedder's from its folder, LLM_RATES's from the parser's
position, and the error by its class (`log_safe.error`).
"""
import logging

import pytest

from korely_agent.core import llm, local_embedder
from korely_agent.services import usage_report

# What a library's message could carry: the words of the text it was given.
WORDS = "Maria has type 2 diabetes"


@pytest.fixture
def clean_state():
    local_embedder._last.update({"ok": None, "at": None, "error": None})
    yield
    local_embedder._last.update({"ok": None, "at": None, "error": None})


class Failing:
    def run(self, names, feed):
        raise RuntimeError(f"cannot embed {WORDS!r}")


class Tokens:
    def encode(self, text):
        return [100] * len(text)


def test_a_library_failure_reaches_no_one_with_its_message(monkeypatch, clean_state, caplog):
    monkeypatch.setattr(local_embedder, "_model", (Failing(), Tokens(), ["input_ids"]))
    with pytest.raises(local_embedder.Unavailable) as e:
        local_embedder.embed("x", local_embedder.QUERY)
    assert WORDS not in str(e.value) and "RuntimeError" in str(e.value)
    st = local_embedder.state()
    assert st["state"] == "failing" and WORDS not in st["detail"]

    s = llm.get_settings().model_copy(update={"embedding_provider": "local"})
    monkeypatch.setattr(llm, "get_settings", lambda: s)
    with caplog.at_level(logging.ERROR):
        assert llm.embed("x") is None
    assert "local embedding failed" in caplog.text and WORDS not in caplog.text


def test_the_warm_up_names_the_folder_and_the_missing_files(monkeypatch, tmp_path,
                                                             clean_state, caplog):
    monkeypatch.setattr(local_embedder, "MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(local_embedder, "_model", None)
    with caplog.at_level(logging.ERROR):
        local_embedder.warm_up().join(5)
    line = next(r.getMessage() for r in caplog.records if "cannot run" in r.getMessage())
    assert str(tmp_path) in line and "onnx/model.onnx" in line
    assert "withheld" in line and "docker compose up -d --build" not in line


def test_a_broken_rate_card_says_where_not_what(caplog):
    with caplog.at_level(logging.ERROR):
        assert usage_report.parse_rates('{"currency": "EUR",\n "models": {"x": nope}}') is None
    assert "line 2, column" in caplog.text and "nope" not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert usage_report.parse_rates('["a list"]') is None
    assert 'no "models" object' in caplog.text
