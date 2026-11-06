"""No timeout is a bare number at its call (audit of 17 Oct 2026).

`timeout=10` beside a call says nothing of why ten, and the next reader
changes one and not its twin. Each timeout is a named constant next to the
call it bounds, with a sentence on why that long (hosted/alerts.py
SEND_TIMEOUT_SECONDS, hosted/firebase.py, hosted/polar.py, the webhooks'
_TIMEOUTS, model_choice.LIST_TIMEOUT_SECONDS; a model call is bounded by
LLM_TIMEOUT_SECONDS). The same for the dashboard's requests.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _a_number(node) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool))


def _literal_timeouts(rel: str, source: str) -> list:
    out = []
    for node in ast.walk(ast.parse(source, filename=rel)):
        if isinstance(node, ast.Call):
            # timeout=10, read_timeout=5.0, ...
            out += [f"{rel}:{node.lineno}" for k in node.keywords
                    if k.arg and "timeout" in k.arg.lower() and _a_number(k.value)]
            # httpx.Timeout(10), Timeout(5.0, connect=2)
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name == "Timeout" and any(_a_number(a) for a in node.args):
                out.append(f"{rel}:{node.lineno}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # def __init__(self, timeout: float = 20.0)
            a = node.args
            positional = a.posonlyargs + a.args
            pairs = list(zip(positional[len(positional) - len(a.defaults):], a.defaults))
            pairs += [(p, d) for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
            out += [f"{rel}:{d.lineno}" for p, d in pairs
                    if "timeout" in p.arg.lower() and _a_number(d)]
        elif isinstance(node, ast.Dict):
            # {"timeout": 10}
            out += [f"{rel}:{v.lineno}" for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and "timeout" in k.value.lower() and _a_number(v)]
    return out


def test_no_python_timeout_is_a_bare_number():
    found = []
    for path in sorted((ROOT / "korely_agent").rglob("*.py")):
        if "memory_core" in path.parts or "__pycache__" in path.parts:
            continue
        found += _literal_timeouts(str(path.relative_to(ROOT)), path.read_text())
    assert found == [], "\n".join(found)


def test_the_scan_sees_each_shape():
    probe = (
        "httpx.Client(timeout=10)\n"
        "session.post(url, json={}, timeout=-1.5)\n"
        "httpx.Timeout(5.0)\n"
        "def f(timeout: float = 20.0): pass\n"
        "def g(*, read_timeout=3): pass\n"
        "c.request('GET', u, extensions={'timeout': 4})\n"
        "httpx.Client(timeout=TIMEOUT_SECONDS)\n"
        "wait(fs, timeout=max(0.0, left))\n"
    )
    assert sorted(_literal_timeouts("p.py", probe)) == [f"p.py:{n}" for n in range(1, 7)]


# The dashboard's TypeScript, read as text: an axios `timeout: 30_000`, a
# fetch's `AbortSignal.timeout(2000)`. A shape of this code, never of a
# person's words.
_TS_LITERAL = re.compile(r"\btimeout\s*:\s*[\d_.]+|AbortSignal\.timeout\(\s*[\d_.]+")


def test_no_dashboard_timeout_is_a_bare_number():
    found = []
    for path in sorted((ROOT / "dashboard" / "src").rglob("*.ts*")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if _TS_LITERAL.search(line):
                found.append(f"{path.relative_to(ROOT)}:{n}")
    assert found == [], "\n".join(found)
