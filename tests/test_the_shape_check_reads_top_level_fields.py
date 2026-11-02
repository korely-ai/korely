"""scripts/check_response_shapes.py reads an interface's own fields, not those
of an inline object type nested in it (the acceptance of 0.1.41, 18 Oct 2026:
ModelsState.active is `{ provider; label; region; model; last_four } | null`,
and the check said /admin/models lacked those five fields)."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_response_shapes.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_response_shapes", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_nested_object_fields_belong_to_their_field(tmp_path):
    (tmp_path / "api.ts").write_text(
        "export interface State {\n"
        "  configured: boolean;\n"
        "  active: {\n"
        "    provider: string;\n"
        "    last_four: string;\n"
        "  } | null;\n"
        "  by_region: Record<Region, { provider: string; model: string } | null>;\n"
        "  note?: string;\n"
        "}\n")
    fields = _load().interfaces(tmp_path)["State"]
    assert fields == {"configured": True, "active": True, "by_region": True, "note": False}
