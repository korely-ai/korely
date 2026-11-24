"""The one search of the memories, as POST /v1/memories/search, the MCP's
korely_search and the dashboard's search answer it (5 Nov 2026).

Each of the three doors checks its question, its key and its turn, then asks
`the_search(account)` for the hits. The core's own is the memories' and the facts'
search (services/facts/search.py, 1.0.13), and nothing more: this class. A
part of the product the Cloud's package carries that answers in the same
search gives a subclass (core/extensions.Part.search), with the kinds it
adds to `types`, its ranking of them, what it adds to the body, the results,
the trail and the MCP's lines, and (1.0.19) its section of the context
block; a tree without the package has none.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from .facts import search as fact_search


class MemorySearch:
    """The memories' and the facts' search. Every method is a step of the
    doors' one search; a part overrides the steps it adds to."""

    #: The kinds `types` may name, in the order their rankings are merged
    #: (services/facts/search.KINDS): a part's are the core's and its own.
    kinds: Sequence[str] = fact_search.KINDS

    def checked(self, body: Any) -> Any:
        """What the body asks of a part, read and checked before the turn at
        the embedder (an HTTPException 422 for what it cannot honour): the
        state `hits` takes. `body` has the core's fields as attributes
        (`types` among them, already read by `kinds`) and the part's own;
        the core asks nothing more."""
        return None

    def hits(self, db, engine, query: str, narrowed: Any, *, tenant_id, project_id,
             end_users, **kw) -> list:
        """The hits closest to `query` (`kw`: the search's arguments, `types`
        and the memories' filters, as services/facts/search.search takes
        them), each a dict with its `type`; raises MemorySearchUnavailable as
        that search does."""
        return fact_search.search(engine, query, **kw)

    def reads(self, hits: list, types: Sequence[str], narrowed: Any) -> dict:
        """What the trail keeps of the hits: `log_v1_request`'s arguments."""
        return fact_search.reads(hits, types)

    def is_memory(self, hit: dict) -> bool:
        """Whether `hit` is a memory (the screen shows it as a memory)."""
        return hit.get("type", fact_search.MEMORY) == fact_search.MEMORY

    def public_id(self, hit: dict) -> str:
        """A hit's id as a result says it: `mem_`, `fct_`, or a part's."""
        return fact_search.public_id(hit)

    def result(self, hit: dict) -> dict:
        """What a part's hit adds to the fields of its SearchResult: none for
        the core's kinds."""
        return {}

    def head(self, hit: dict) -> Optional[tuple]:
        """A part's hit as korely_search prints it, before its text: (the
        line that follows the score, the id that ends it); None for the
        core's kinds, which print their text on that line."""
        return None

    def read_whole(self, hit: dict) -> Optional[str]:
        """How to read `hit` whole when korely_search cut its text, as its
        NOTE says it after "Ask with a smaller limit, or": a memory by its id
        (GET /v1/memories/{id}); None when a smaller limit is the only way (a
        fact: no door reads one fact by its id)."""
        if self.is_memory(hit):
            return "read a memory whole with GET /v1/memories/{id}"
        return None

    def tool(self, mcp, answer) -> bool:
        """Register a korely_search of its own on `mcp`, with the part's
        arguments, whose body is `answer` (mcp/server.answer_search): True
        when it did; the core registers its own otherwise."""
        return False

    def in_the_block(self):
        """Its section of the context block (services/context.Section),
        which GET /v1/context and korely_get_context hand to the block;
        None for the core, which adds none."""
        return None

    def on_the_screen(self, hits: list, memories: list) -> list:
        """The dashboard's results: `memories` (the memory hits as the
        Memories page reads them, in order) with the part's hits among
        them."""
        return memories


def the_search(tenant_id) -> MemorySearch:
    """The search the doors run for the account `tenant_id`: a part's the
    mode serves to that account (core/extensions.served; Part.search,
    mounted only on the Cloud), or the memories' and the facts' alone, as
    for no account (None)."""
    from ..core import extensions

    return _of(extensions.served(tenant_id))


def the_search_offered() -> MemorySearch:
    """The search of the parts the mode serves to any account: what the
    MCP registers its korely_search from, once per server, since a tool's
    arguments are the same for every account; each call is answered by
    the caller's own (`the_search`)."""
    from ..core import extensions

    return _of(extensions.mounted())


def _of(parts: tuple) -> MemorySearch:
    for part in parts:
        search = part.search()
        if search is not None:
            return search
    return MemorySearch()
