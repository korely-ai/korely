"""No function of the package runs past 150 lines, and the facts' service
stays under 1,000 (audit of 17 Oct 2026).

The long ones were cut where they had parts of their own: create_app,
process_deliveries and _list_models first, then the MCP's `_get_agent_mcp`
(about 300 lines, one function per tool now), `manage status` and `main`,
and `projects.delete_project`. services/facts/service.py was 1,990 lines:
the erasures, the drain and the reads have modules of their own.

Read from the source, no database and no app: the whole package, but the
facts engine (`services/memory_core/`, which changes only after review
and has its own guards).
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "korely_agent"
MAX_FUNCTION_LINES = 150
MAX_FACTS_SERVICE_LINES = 1000


def test_no_function_runs_past_150_lines():
    long = []
    for f in sorted(PACKAGE.rglob("*.py")):
        if "memory_core" in f.relative_to(ROOT).parts:
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines = n.end_lineno - n.lineno + 1
                if lines > MAX_FUNCTION_LINES:
                    long.append(f"{f.relative_to(ROOT)}:{n.lineno} {n.name}: {lines} lines")
    assert long == [], "\n".join(long)


def test_the_facts_service_stays_under_1000_lines():
    f = PACKAGE / "services" / "facts" / "service.py"
    assert len(f.read_text().splitlines()) < MAX_FACTS_SERVICE_LINES
