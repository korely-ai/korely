"""Korely Agent."""
from pathlib import Path as _Path


def _read_version() -> str:
    """The version of this install, from the VERSION file next to the source.

    There was no answer to "what am I running" and no way to go back: no tags,
    no VERSION, `pyproject.toml` said 0.0.0, and the only version the product
    reported anywhere was the MCP library's own, leaking through the handshake
    as if it were ours. A customer who upgrades into a fault needs somewhere to
    return to, and "read the commit history" is not somewhere.

    Read from the file rather than from package metadata because this is
    installed by cloning, not by pip, so there is no metadata to read.
    """
    try:
        return (_Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except OSError:
        return "unknown"


__version__ = _read_version()
