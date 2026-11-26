"""The host chooser rewrites .env without touching the rest (2026-10-02).

A "very simple menu": pick a host, paste the key, accept the
model. What deserves tests is the rewriting, because a wrong .env is a store
that refuses every write: keys set exactly once, other lines untouched, and
the model names of another provider disarmed (sent by name to a host that
does not serve them, every judged write died on 422)."""
import importlib.util
import os
import stat
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "choose_models", Path(__file__).resolve().parents[1] / "scripts" / "choose_models.py")
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)


def test_sets_replaces_and_appends():
    text = "DATABASE_URL=postgres://x\n# MISTRAL_API_KEY=old\nPORT=8000\n"
    out = cm.updated_env(text, {"MISTRAL_API_KEY": "sk-new", "MISTRAL_API_BASE": "https://api.scaleway.ai/v1"})
    assert "DATABASE_URL=postgres://x" in out and "PORT=8000" in out
    assert out.count("MISTRAL_API_KEY=") == 1 and "MISTRAL_API_KEY=sk-new" in out
    assert out.rstrip().endswith("MISTRAL_API_BASE=https://api.scaleway.ai/v1")


def test_disarms_the_other_providers_model_names():
    text = "VOCABULARY_MODEL=gemini-3.6-flash\nEXTRACTION_MODEL=gemini-3.1-flash-lite\n"
    out = cm.updated_env(text, {"EXTRACTION_PROVIDER": "mistral"},
                         comment_out=("VOCABULARY_MODEL", "EXTRACTION_MODEL"))
    assert "# VOCABULARY_MODEL=gemini-3.6-flash" in out
    assert "# EXTRACTION_MODEL=gemini-3.1-flash-lite" in out
    assert "disarmed" in out


def test_duplicate_keys_collapse_to_one():
    text = "MISTRAL_API_KEY=a\nMISTRAL_API_KEY=b\n"
    out = cm.updated_env(text, {"MISTRAL_API_KEY": "c"})
    assert out.count("MISTRAL_API_KEY=") == 1 and "MISTRAL_API_KEY=c" in out


def test_every_name_the_menu_writes_is_a_real_setting():
    """The .env the menu writes must be made of names something reads."""
    from korely_agent.config import AgentSettings

    settings = {f.upper() for f in AgentSettings.model_fields}
    for name in ("EXTRACTION_PROVIDER", "MISTRAL_API_BASE", "MISTRAL_API_KEY",
                 "MISTRAL_EXTRACTION_MODEL", "GOOGLE_API_KEY", "EXTRACTION_MODEL"):
        assert name in settings, name


def test_the_menu_suggests_what_the_first_login_suggests():
    """The install script and the first-login screen propose the writer of a
    customer's memories in two places, and they had drifted: Mistral medium
    here and small on the screen, OpenAI gpt-4o-mini here and gpt-4.1-mini
    there (audit of 17 Oct 2026). The script runs with the system's
    python3, without the product's packages, so it keeps its own copy; this
    holds the copy equal: each host's address is a provider's `base` (Gemini
    on its own client, no address) and its suggestion that provider's first
    preference."""
    from korely_agent.services.model_choice import PROVIDERS

    for _key, (label, url, suggested, _note) in cm.HOSTS.items():
        found = [p for p in PROVIDERS.values()
                 if (url is None and p.id == "google") or (url is not None and p.base == url)]
        assert len(found) == 1, f"{label}: no provider of the screen at {url}"
        assert suggested == found[0].prefer[0], (label, suggested, found[0].prefer[0])


def test_the_files_defaults_are_the_screens_choice():
    """An install configured only in .env writes with the same models the
    first login would suggest for those providers, at the same address."""
    from korely_agent.config import AgentSettings
    from korely_agent.services.model_choice import PROVIDERS

    f = AgentSettings.model_fields
    assert f["extraction_model"].default == PROVIDERS["google"].prefer[0]
    assert f["mistral_extraction_model"].default == PROVIDERS["mistral"].prefer[0]
    assert f["mistral_api_base"].default == PROVIDERS["mistral"].base


def _run_the_menu(monkeypatch, env_file):
    answers = iter(["2", ""])   # Scaleway, the suggested model
    monkeypatch.setattr(cm, "ENV", env_file)
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr(cm.getpass, "getpass", lambda *_: "sk-secret")
    old = os.umask(0o022)
    try:
        assert cm.main() == 0
    finally:
        os.umask(old)
    assert "MISTRAL_API_KEY=sk-secret" in env_file.read_text()
    return stat.S_IMODE(env_file.stat().st_mode)


def test_a_new_env_is_the_owners_only(monkeypatch, tmp_path):
    """Path.write_text gave a new .env the umask's mode, 0644: every account
    on the machine could read the model key (audit of 17 Oct 2026)."""
    assert _run_the_menu(monkeypatch, tmp_path / ".env") == 0o600


def test_a_copied_env_is_made_the_owners_only(monkeypatch, tmp_path):
    """`cp .env.example .env` leaves it 0644, and the key goes into it."""
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=abc\n")
    env_file.chmod(0o644)
    assert _run_the_menu(monkeypatch, env_file) == 0o600
    assert "SECRET_KEY=abc" in env_file.read_text()
