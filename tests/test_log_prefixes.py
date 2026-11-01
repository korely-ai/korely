"""Every log line of a subsystem starts with that subsystem's prefix (audit
of 17 Oct 2026): an operator who greps `[agent_webhooks]` found two
lines of the webhooks and missed the others, written `[webhooks]` or with
no prefix at all, as were seven lines of the application's start.

The rest of the same finding: the facts wrote [agent_facts] and
[facts], the context [agent_context] and [context], and the memory engine,
the dashboard's server and four lines of core/llm none. A file that holds
two subsystems names both: core/llm.py calls the models ([llm]) and the
vectors' provider ([embeddings]), services/vectors.py fills the vectors
([vectors]) and checks their model at start ([embeddings], as main.py and
core/local_embedder.py say it).
"""
import ast
import pathlib

import pytest

from tests import cloud
from tests.log_calls import is_log_call, logger_names, message_args

ROOT = pathlib.Path(__file__).resolve().parents[1]

# File, the prefixes its lines start with (None: some "[...]" prefix).
FILES = {
    "korely_agent/services/webhooks.py": ("[agent_webhooks]",),
    "korely_agent/main.py": None,
    "korely_agent/hosted/__init__.py": ("[hosted]",),
    "korely_agent/services/facts/service.py": ("[agent_facts]",),
    "korely_agent/services/facts/drain.py": ("[agent_facts]",),
    "korely_agent/services/facts/contradiction.py": ("[agent_facts]",),
    "korely_agent/services/facts/extractor.py": ("[agent_facts]",),
    "korely_agent/services/context.py": ("[agent_context]",),
    "korely_agent/services/memory_engine.py": ("[agent_memory]",),
    "korely_agent/services/vectors.py": ("[vectors]", "[embeddings]"),
    "korely_agent/core/llm.py": ("[llm]", "[embeddings]"),
    "korely_agent/core/local_embedder.py": ("[embeddings]",),
    "korely_agent/api/spa.py": ("[dashboard]",),
}


def _first_texts(rel):
    tree = ast.parse((ROOT / rel).read_text())
    names = logger_names(tree) - {"logging"}
    for call in ast.walk(tree):
        if is_log_call(call, names) and message_args(call):
            first = message_args(call)[0]
            parts = first.values if isinstance(first, ast.JoinedStr) else [first]
            head = parts[0].value if isinstance(parts[0], ast.Constant) else ""
            yield call.lineno, str(head)


@pytest.mark.parametrize("rel,prefix", cloud.in_this_tree(FILES).items())
def test_one_prefix_per_subsystem(rel, prefix):
    wrong = [f"{rel}:{n} {t[:40]!r}" for n, t in _first_texts(rel)
             if not (t.startswith(prefix) if prefix else t.startswith("[") and "]" in t)]
    assert list(_first_texts(rel)), f"{rel} logs nothing: an entry for nothing"
    assert wrong == [], "\n".join(wrong)


def test_a_line_through_logger_log_is_read_past_its_level(tmp_path, monkeypatch):
    """Audit of 20 Oct 2026: core/llm.py's `logger.log(level, "[embeddings]
    ...")` was not read; its text is its second argument."""
    import tests.test_log_prefixes as me

    (tmp_path / "probe.py").write_text(
        "import logging\nlogger = logging.getLogger(__name__)\n"
        "logger.log(logging.WARNING, 'no prefix here')\n")
    monkeypatch.setattr(me, "ROOT", tmp_path)
    assert list(me._first_texts("probe.py")) == [(3, "no prefix here")]


def test_a_line_through_a_logger_made_on_the_spot_is_read(tmp_path, monkeypatch):
    """Audit of 27 Oct 2026: `logging.getLogger(__name__).warning(...)` (two
    lines of hosted/__init__.py) and `self.log.error(...)` were not read."""
    import tests.test_log_prefixes as me

    (tmp_path / "probe.py").write_text(
        "import logging\n"
        "logging.getLogger(__name__).error('no prefix %s', 1)\n"
        "class A:\n    def f(self):\n        self.log.warning('none either')\n")
    monkeypatch.setattr(me, "ROOT", tmp_path)
    assert list(me._first_texts("probe.py")) == [(2, "no prefix %s"), (5, "none either")]
