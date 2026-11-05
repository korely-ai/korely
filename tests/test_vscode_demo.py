"""examples/vscode-demo holds together.

The demo is what a customer sees first, run by somebody talking at the same
time, so the ways it can break quietly are checked here: a task naming a file
that is gone, a graph link the page no longer reads, and `create_key.py`
reading an output of `manage` that has changed shape. The story itself runs
against a real model and is not a unit test.
"""
import importlib.util
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "vscode-demo"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"demo_{name}", DEMO / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_task_runs_a_file_that_exists():
    tasks = json.loads((DEMO / ".vscode" / "tasks.json").read_text())["tasks"]
    named = set()
    for t in tasks:
        named.update(re.findall(r"([\w.-]+\.(?:py|sh|txt))", t["command"]))
    assert named, "no task names a file: the parse is wrong, not the demo"
    missing = sorted(n for n in named if not (DEMO / n).exists())
    assert missing == [], f"tasks name files that are not there: {missing}"


def test_the_graph_link_uses_what_the_graph_page_reads():
    page = (ROOT / "dashboard" / "src" / "pages" / "GraphPage.tsx").read_text()
    read = set(re.findall(r'params\.get\("([a-z_]+)"\)', page))
    link = (DEMO / "open_graph.py").read_text()
    for name in ("user_id", "as_of", "history"):
        assert f"{name}=" in link, f"the demo no longer sends {name}"
        assert name in read, f"the graph page no longer reads {name} (it reads {read})"


def test_create_key_reads_what_manage_prints(db, monkeypatch, capsys):
    from korely_agent import manage

    monkeypatch.setattr(manage, "_session", lambda: db)
    assert manage.main(["create-tenant", "--name", "Demo"]) == 0
    out = capsys.readouterr().out
    create_key = _load("create_key")
    assert re.fullmatch(r"[0-9a-f-]{36}", create_key.field(out, "tenant"))
    assert create_key.field(out, "key").startswith("kor_self_")
