#!/usr/bin/env python3
"""Choose where your models run: three questions, writes your .env.

    python3 scripts/choose_models.py          # interactive
    python3 scripts/choose_models.py --list   # just print the hosts

Pick a host, paste your key, accept or change the suggested model. The
script rewrites the model lines of .env (everything else is left alone) and
tells you to `docker compose up -d` to apply. Every host below speaks the
same OpenAI-compatible protocol, so switching is configuration, never code.

Vectors are computed locally by default (EMBEDDING_PROVIDER=local): the text
of your memories never leaves your machine to be searched. The reader — the
model that answers your users — belongs to your agent, not to this store.

When you switch host, any VOCABULARY_MODEL / EXTRACTION_MODEL line naming a
model of ANOTHER provider is commented out: sent by name to a host that does
not serve it, every write that needed it died on 422 (measured 2026-10-02).
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env"

# host key -> (label, base URL, suggested model, region note). The first two
# are our writers, one per region (chosen 2026-10-06), both measured as the
# whole writer on LoCoMo conversation 26: 129 right answers of 152 each
# (2026-10-05). gpt-oss through OpenRouter was the fastest on LongMemEval
# (2026-10-02). OVH is no longer offered: setting it up was found too
# cumbersome.
#
# Each suggestion is the first-login screen's for the same provider (the first
# of `services/model_choice.PROVIDERS[...].prefer`) and each address is its
# `base`: written apart because this script runs with the system's python3,
# without the product's packages, and held equal by tests/test_choose_models.py.
# Until the audit of 17 Oct 2026 they had drifted: Mistral medium here
# and small on the screen, OpenAI gpt-4o-mini here and gpt-4.1-mini there.
HOSTS = {
    "1": ("Google Gemini (Global)", None,
          "gemini-3.1-flash-lite",
          "our Global writer, measured; uses GOOGLE_API_KEY"),
    "2": ("Scaleway Generative APIs (Europe)", "https://api.scaleway.ai/v1",
          "gpt-oss-120b",
          "our European writer, measured; servers in Paris"),
    "3": ("OpenRouter", "https://openrouter.ai/api/v1",
          "openai/gpt-oss-120b:nitro",
          "US gateway; the fastest writer we measured, ~3 s per memory"),
    "4": ("Mistral (la Plateforme)", "https://api.mistral.ai/v1",
          "mistral-small-latest",
          "EU — Paris, the model maker's own API; not measured by us"),
    "5": ("OpenAI", "https://api.openai.com/v1",
          "gpt-4.1-mini",
          "US — not measured by us"),
}


def updated_env(text: str, values: dict, comment_out: tuple = ()) -> str:
    """`text` with each KEY=VALUE of `values` set exactly once (replacing a
    live or commented line when one exists, appended otherwise), and every
    live line of a `comment_out` key disarmed with a note. Pure, so the test
    can hold it still."""
    lines = text.splitlines()
    done = set()
    out = []
    for line in lines:
        stripped = line.lstrip("# ").strip()
        name = stripped.split("=", 1)[0] if "=" in stripped else None
        if name in values and name not in done:
            out.append(f"{name}={values[name]}")
            done.add(name)
        elif name in values and name in done:
            continue  # a duplicate of a key we already wrote
        elif name in comment_out and "=" in line and not line.lstrip().startswith("#"):
            out.append(f"# {line}  # disarmed by choose_models.py: another host does not serve this model name")
        else:
            out.append(line)
    for name, value in values.items():
        if name not in done:
            out.append(f"{name}={value}")
    return "\n".join(out) + ("\n" if text.endswith("\n") or not text else "")


def write_owner_only(path: Path, text: str) -> None:
    """Write `path` readable by its owner alone: it holds the model key, the
    database password and SECRET_KEY. `Path.write_text` gave a new .env the
    umask's mode, usually 0644, and left a copied one as it was, so any
    account on the machine could read the key (audit of 17 Oct 2026). The mode
    is set before the first byte, so the key never sits in a readable file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(text)


def main() -> int:
    print("Where should the models that WRITE your memories run?\n")
    for k, (label, _url, model, note) in HOSTS.items():
        print(f"  {k}) {label:34} [{note}]")
    if "--list" in sys.argv:
        return 0
    choice = input(f"\nHost [1-{len(HOSTS)}]: ").strip()
    if choice not in HOSTS:
        print("No such host.")
        return 2
    label, url, suggested, _note = HOSTS[choice]
    key = getpass.getpass(f"Your {label} API key (not echoed): ").strip()
    if not key:
        print("No key, nothing written.")
        return 2
    model = input(f"Model [{suggested}]: ").strip() or suggested

    text = ENV.read_text() if ENV.exists() else ""
    if url is None:  # Gemini keeps its own client
        new = updated_env(text, {"GOOGLE_API_KEY": key, "EXTRACTION_PROVIDER": "gemini",
                                 "EXTRACTION_MODEL": model})
    else:
        new = updated_env(
            text,
            {"EXTRACTION_PROVIDER": "mistral",  # the OpenAI-compatible door
             "MISTRAL_API_BASE": url,
             "MISTRAL_API_KEY": key,
             "MISTRAL_EXTRACTION_MODEL": model},
            comment_out=("VOCABULARY_MODEL", "EXTRACTION_MODEL"),
        )
    write_owner_only(ENV, new)
    print(f"\nWritten to {ENV.name}: extraction, the contradiction judge and the "
          f"relation vocabulary now run on {label} with {model}.")
    print("Apply with: docker compose up -d")
    # A provider saved on the screen wins over .env (core/llm.get_provider):
    # without this line the script announced a change the install did not
    # make (audit of 17 Oct 2026).
    print("If a provider was ever chosen on the screen, it still writes until you press "
          "'Use what .env says instead' in Settings, Writing model.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
