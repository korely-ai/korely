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


def test_an_install_of_ones_own_is_not_asked_for_the_hosted_pages(tmp_path):
    """The hosted service's pages (dashboard/src/hosted/) read routes an
    install of one's own does not serve: left out there, kept on the Cloud,
    by the same rule as check_dashboard_endpoints.py."""
    (tmp_path / "hosted").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "api.ts").write_text(
        "export interface Keys {\n  total: number;\n}\n"
        'export const k = () => apiClient.get<Keys>("/admin/keys");\n')
    (tmp_path / "hosted" / "api.ts").write_text(
        "export interface Plan {\n  name: string;\n}\n"
        'export const l = () => apiClient.get<Plan>("/admin/plan");\n')
    mod = _load()
    ifaces = mod.interfaces(tmp_path)
    assert [p for p, _k, _f in mod.wanted(tmp_path, ifaces, hosted=False)] == ["/admin/keys"]
    assert sorted(p for p, _k, _f in mod.wanted(tmp_path, ifaces)) == [
        "/admin/keys", "/admin/plan"]
