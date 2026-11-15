"""scripts/memory_core_manifest.py refuses to write the manifest again when a
file of memory_core changed and VERSION did not (a code review of
17 Oct 2026: f1ce3ae changed __init__.py under the same 5.2.0)."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "memory_core_manifest", Path(__file__).resolve().parents[1] / "scripts" / "memory_core_manifest.py")
M = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(M)


def engine(tmp_path, version, body):
    (tmp_path / "__init__.py").write_text(f'VERSION = "{version}"\n')
    (tmp_path / "rules.py").write_text(body)


def test_a_change_under_the_same_version_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(M, "FOLDER", tmp_path)
    engine(tmp_path, "1.0.0", "A = 1\n")
    assert M.main([]) == 0
    written = (tmp_path / "MANIFEST.json").read_text()
    (tmp_path / "rules.py").write_text("A = 2\n")
    assert M.main([]) == 1
    assert "rules.py" in capsys.readouterr().err
    assert (tmp_path / "MANIFEST.json").read_text() == written, "nothing is written"
    engine(tmp_path, "1.1.0", "A = 2\n")
    assert M.main([]) == 0


def test_the_real_manifest_is_current():
    assert M.main(["--check"]) == 0
