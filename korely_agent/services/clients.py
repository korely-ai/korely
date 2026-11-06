"""Which tool wrote a memory: its client, a label outside the agent cap.

A developer who connects three coding assistants to one memory (Claude Code,
Codex, Cursor) wants to know which of them wrote what. The one way to say it
was an `agent_id` per tool, and the hosted plans count every distinct
`agent_id` (Hobby: 2), so the third assistant was refused (the open memory
analysis of 15 Oct 2026, section 3). The client is not a namespace: it scopes
no read, it is not counted by any cap, and a memory without one is the same
memory. It is recorded on the memory and in the audit trail.

Where it comes from:
- REST: the `X-Korely-Client` header (the SDKs and `korely hook` set it);
- MCP: the name the client gives when it connects, `clientInfo.name` of the
  `initialize` request (MCP specification 2025-06-18, Lifecycle,
  Initialization).
"""
from __future__ import annotations

from typing import Any, Optional

HEADER = "X-Korely-Client"
MAX_CHARS = 64


def client_name(value: Any) -> Optional[str]:
    """The label as stored: printable characters only, trimmed, at most
    64 characters; None for nothing. Never a refusal: a write is not refused
    for the way its tool describes itself."""
    if value is None:
        return None
    text = "".join(ch for ch in str(value) if ch.isprintable()).strip()
    return text[:MAX_CHARS].strip() or None
