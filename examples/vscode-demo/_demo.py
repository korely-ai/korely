"""What the demo's scripts share: the settings in .env, the story, a client.

Standard library only, apart from the client itself: a demo that needs three
packages to read a settings file has already lost the room.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The README's story, and the only end user the demo touches.
CUSTOMER = "c4821"

# (the day of the conversation, what the support bot learned in it)
CONVERSATIONS = [
    ("2026-03-15", "Customer 4821 is on the Priority Support plan."),
    ("2026-03-15", "Customer 4821 prefers to be contacted by email."),
    ("2026-06-20", "Customer 4821 moved to the Basic plan."),
]

# The day the customer's screenshot was taken.
SCREENSHOT_DAY = "2026-04-01"


def settings() -> dict:
    """KEY=value lines from .env, with the environment winning over the file."""
    found = {}
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                found[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("KORELY_BASE_URL", "KORELY_API_KEY", "KORELY_CA_FILE"):
        if os.environ.get(k):
            found[k] = os.environ[k]
    return found


def client():
    try:
        from korely_memory import Korely
    except ImportError:
        sys.exit("korely-memory is not installed here: run the task "
                 "'Korely 0: Python environment' first.")
    s = settings()
    if not s.get("KORELY_API_KEY") or not s.get("KORELY_BASE_URL"):
        sys.exit("No key yet: run the task 'Korely 2: a key' first "
                 "(it writes KORELY_API_KEY and KORELY_BASE_URL to .env).")
    # https://localhost on a laptop: Caddy's root, as the README's "On a
    # laptop" says, from KORELY_CA_FILE in .env or the environment. The SDK
    # reads only the environment, and this demo reads .env too.
    return Korely(api_key=s["KORELY_API_KEY"], base_url=s["KORELY_BASE_URL"],
                  ca_file=s.get("KORELY_CA_FILE") or None)


def wait_until_mined(korely, memory_id: str, *, timeout: float = 90) -> tuple[str, float]:
    """Facts are mined by a worker a few seconds after the write. Returns the
    final status and how long it took."""
    start = time.monotonic()
    while True:
        status = korely.get(memory_id).status
        if status != "processing" or time.monotonic() - start > timeout:
            return status, time.monotonic() - start
        time.sleep(1)


def line(f) -> str:
    since = (f.valid_from or "")[:10]
    until = f"  until {f.invalid_at[:10]}" if f.invalid_at else ""
    # The store keys a fact on its canonical relation (`predicate`); the context
    # block a model reads shows the relation as the text said it
    # (`predicate_raw`). Both, when they differ, so the two screens match.
    raw = getattr(f, "predicate_raw", None)
    said = f" (said: {raw})" if raw and raw != f.predicate else ""
    return f"{f.subject} · {f.predicate}{said} · {f.object}   (since {since}{until})"
