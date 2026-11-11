"""How an agent reads a fact: the MCP line, the context line, the ranking (memory_core).

Both Agents products (hosted and self-hosted) render facts to the agent in two
places, the MCP tools and the `/v1/context` block, and rank the facts of the
context by relevance and age. The same fact must read the same way on both,
so the text and the ranking live here.

A row may be an ORM object or the dict `serialize_fact` returns: every reader
goes through `_g`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Optional, Tuple

from . import relative_time as _relative_time
from .identity import name_with_history
from .fact_time import recency_weight

# The context endpoint has no question classifier, so it uses the gentle
# default of the earlier personal-memory chat (lambda 0.01, half-life ~69
# days, floor 0.3): a tie-breaker between facts equally relevant, never a filter.
CONTEXT_LAMBDA = 0.01


def _g(f: Any, k: str):
    return f.get(k) if isinstance(f, dict) else getattr(f, k, None)


#: What every line of a text after its first starts with, wherever one item
#: is printed a line (`in_its_line`).
CONTINUED = "    "


def in_its_line(text: str) -> str:
    """`text` as one item of an answer that prints one item a line: the MCP's
    korely_search results, korely_get_facts' and korely_add's facts (since
    1.0.13, where this rule was written, in the MCP server), and since 6.8.0
    the memories and the facts of the `/v1/context` block (`memory_line`,
    `context_line`). Every line after its first begins with CONTINUED, so a
    line break of a memory or of a fact, which anyone who can write may put
    there, never begins a line that reads as another item (`- [day] ...`,
    `[0.999] ...`, a NOTE, a heading). A line break is whatever Python's
    `str.splitlines` breaks a line on, in every alphabet: LF, CR and CR LF,
    VT, FF, FS, GS, RS, NEL and Unicode's line and paragraph separators. A
    text with none is itself, byte for byte.

    Why the text is marked and not the item: the block's items are told
    apart by a delimiter, a line at the margin, and a delimiter the data can
    hold is no boundary ("it would be simple to craft a string that contains
    our delimiters", Hines et al. 2024, "Defending Against Indirect Prompt
    Injection Attacks With Spotlighting", arXiv:2403.14720, §III-B and
    §V-D); their datamarking transforms the data itself so that it carries a
    mark throughout (§III-C). Here the mark is the indentation of every line
    the text adds, so no line of a text is at the margin."""
    return ("\n" + CONTINUED).join(text.splitlines())


def _day(v) -> str:
    """The UTC calendar day of a moment, `YYYY-MM-DD` ("" when absent).

    The store's times are instants (timestamptz), and the database hands them
    back in the time zone of its session. `str(v)[:10]` printed the day of that
    zone: on a session at UTC+14 a fact that ended 2026-05-31T20:00Z read
    "superseded 2026-06-01" (found on korely-agent, 2026-09-28). An offset
    names the same instant (RFC 3339 §4.2), so the day is taken in UTC, the
    zone the REST answers and the rest of this folder reason in. A value with
    no offset is UTC (the columns are UTC; SQLite returns them naive); a
    string that is not ISO keeps its first ten characters, as before."""
    if not v:
        return ""
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return str(v)[:10]
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date().isoformat()


def _ended(v, now: datetime) -> bool:
    """Whether an end date (`invalid_at`) has come, as of `now`.

    An unreadable date counts as come: the listing does not fall over on one
    odd row, and "already closed" is the safe half of the guess, the one every
    date we can read already satisfies (the same choice as the SDK CLI)."""
    if isinstance(v, datetime):
        end = v
    else:
        try:
            end = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return True
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end <= now


def _shown(f: Any, side: str, label: Optional[str]) -> str:
    """One side of a fact under its current name with its written one. A
    name a link "the same as" resolved (6.1.0, `objects`: the reads put the
    link's id beside the canonical, `<side>_link_id`) reads "Italia
    (written: Italy)": one thing in two languages, not a rename's "then"."""
    canonical = _g(f, f"{side}_canonical")
    if _g(f, f"{side}_link_id"):
        lab, can = (label or "").strip(), (canonical or "").strip()
        return lab if not can or can.lower() == lab.lower() else f"{can} (written: {lab})"
    return name_with_history(label, canonical)


def mcp_line(f: Any, now: Optional[datetime] = None) -> str:
    """One fact as the MCP tools print it: the entity under its current name
    with the one it was written with ("Globex (then: Acme)"), when it was
    learned, when a memory last restated it and how many did, and whether it
    is history (past / superseded), an end still to come (until) or an
    intention (planned).

    An `invalid_at` in the future is a known end date (a contract that ends on
    31 December): the store reads the fact as true until then (`true_at` in
    both products: invalid_at > now), and the default read returns it. This
    line said "superseded 3026-01-01" for it, the past tense for a date that
    has not come, on a fact the same call had just returned as current.
    Fixed 2026-09-28, as the SDK CLI was (korely-memory 52e29b7)."""
    written_subject = _g(f, "subject") if isinstance(f, dict) else _g(f, "subject_label")
    written_object = _g(f, "object") if isinstance(f, dict) else _g(f, "object_label")
    predicate = _g(f, "predicate") if isinstance(f, dict) else _g(f, "predicate_canonical")
    subj = _shown(f, "subject", written_subject)
    obj = _shown(f, "object", written_object)
    line = f"{subj} · {predicate} · {obj}"
    vf = _g(f, "valid_from")
    if vf:
        line += f"  (since {_day(vf)}"
        lc = _g(f, "last_confirmed_at")
        n = _g(f, "observation_count") or 1
        if lc and _day(lc) != _day(vf):
            line += f", confirmed {_day(lc)}"
        if int(n) > 1:
            line += f", seen {int(n)}x"
        line += ")"
    tense = _g(f, "tense") or "current"
    ia = _g(f, "invalid_at")
    if ia and not _ended(ia, now or datetime.now(timezone.utc)):
        line += f"  [until {_day(ia)}]"
        if tense == "planned":
            line += "  [planned, not done]"
    elif tense == "past" and ia:
        line += f"  [past, ended by {_day(ia)}]"
    elif ia and tense == "planned":
        # 3.0.0: a plan that came true is closed by the done fact, a row of
        # its own (`writing.write_fact`); without the word the history
        # read as the state itself, ended the day it began.
        line += f"  [planned, superseded {_day(ia)}]"
    elif ia:
        line += f"  [superseded {_day(ia)}]"
    elif tense == "planned":
        line += "  [planned, not done]"
    return line


def _is_the_user(label: Optional[str], canonical: Optional[str], end_user_id: Optional[str]) -> bool:
    """The name IS the end user: written as their handle, or brought to it by
    an alias (5.7.0: a name the end user declared, `POST
    /v1/users/{id}/names`, reads as them; it showed "<id> (then: Maria
    Rossi)", the code audit of 19 Oct 2026). The handle as
    written; the canonical case-folded, as the aliases compare."""
    if not end_user_id:
        return False
    return label == end_user_id or (canonical or "").strip().lower() == end_user_id.strip().lower()


def context_line(f: Any, end_user_id: Optional[str]) -> str:
    """One fact in the `/v1/context` block. The subject that IS the end user
    reads "The user", so the reader binds it to the question's "I/my" instead
    of an opaque handle (a failure measured on LongMemEval). The same for the
    object (3.1.4, found on the 3.1 bench, LoCoMo conv-26): "Melanie
    supports gb-0-caroline", "Her manager mentors c4821" showed the handle,
    in 142 blocks of 152 with the 3.1 rules off and 97 with them on, and in
    every case the handle was the block's own end user.

    6.8.0: in its line (`in_its_line`): a label with a line break no longer
    begins a line of the block that reads as another fact or a memory."""
    since = f" (since {_day(_g(f, 'valid_from'))})" if _g(f, "valid_from") else ""
    label = _g(f, "subject_label")
    subj = ("The user" if _is_the_user(label, _g(f, "subject_canonical"), end_user_id)
            else _shown(f, "subject", label))
    written_object = _g(f, "object_label")
    obj = ("the user" if _is_the_user(written_object, _g(f, "object_canonical"), end_user_id)
           else _shown(f, "object", written_object))
    plan = " [planned, not done]" if (_g(f, "tense") or "current") == "planned" else ""
    return in_its_line(f"- {subj} {_g(f, 'predicate_raw')} {obj}{since}{plan}")


def rank_with_recency(
    pairs: Iterable[Tuple[Any, Optional[float]]],
    now: datetime,
    lambda_: float = CONTEXT_LAMBDA,
) -> List[Any]:
    """(fact, cosine distance) pairs ranked by relevance times the recency
    weight from the LAST confirmation, softened by how many memories restated
    the fact (fact_time.recency_weight)."""
    def score(pair):
        f, d = pair
        return (1.0 - float(d or 0.0)) * recency_weight(
            _g(f, "last_confirmed_at") or _g(f, "valid_from"),
            _g(f, "observation_count"), lambda_, now,
        )
    return [f for f, _d in sorted(pairs, key=score, reverse=True)]


def _plain_text(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def memories_for_the_context(
    rows: Iterable[Any],
    facts_state: Callable[[Any], Tuple[int, int]],
) -> List[Any]:
    """The memories that go in the context block, in the order given (the
    search's relevance), 2.3.0. The public conformance test of 29 Sep found
    the same text twice, and "Maria lives in Rome." under a block that says it
    holds the current truth, next to "Maria lives in Milan". The rule, the same
    on both Agents products:
      (a) a text already shown (normalized: spaces and case) is not shown again,
          the first by relevance stays;
      (b) a memory that stated at least one fact, all of them superseded
          (closed by a newer fact), is out; a fact stated in the past tense
          is history, not superseded, so its memory stays (2.5.1,
          `store.state_of_the_source`);
      (c) a memory that stated no fact stays (it may hold what extraction
          missed).
    `facts_state(memory_id)` -> (facts it stated, facts still standing).
    A row is a dict with `id` and `content`, or an object with those.
    3.3.0: the same rows `memories_with_their_state` keeps with its switch off."""
    return [r for r, _superseded in memories_with_their_state(rows, facts_state)]


def memories_with_their_state(
    rows: Iterable[Any],
    facts_state: Callable[[Any], Tuple[int, int]],
    *,
    superseded_as_history: bool = False,
) -> List[Tuple[Any, bool]]:
    """The memories of the block with, for each, whether every fact it stated
    was superseded (3.3.0). Off (`superseded_as_history=False`, the default),
    the rule of `memories_for_the_context`: such a memory is left out, so every
    flag is False.

    On, it stays, flagged, and `block_line(superseded=True)` shows it as
    history. Rule (b) turns one error into two: a WRONG closure by the judge
    (in the 3.3.0 confirmation run, a feeling that closed other feelings and
    "values support") also hides the memory that holds the evidence, and the
    reader cannot recover it. Graphiti keeps an invalidated edge with the
    time it stopped holding instead of deleting it (Rasmussen et al., 2025,
    section 2.2.3), so history stays readable; the memory comes back the same
    way, with its day in front and a mark that a later memory changed it.
    Off until measured on a reread: on 180 LongMemEval questions it was
    measured that the closed facts shown in the block as "no longer true"
    lowered the score (134, p 0.011, the temporal questions lost 8; 2 Oct),
    so the effect of history in the block is not assumed."""
    seen = set()
    kept: List[Tuple[Any, bool]] = []
    for r in rows:
        text = _g(r, "content") or ""
        key = _plain_text(text)
        if not key or key in seen:
            continue
        states, true_ones = facts_state(_g(r, "id"))
        superseded = states > 0 and true_ones == 0
        if superseded and not superseded_as_history:
            continue
        seen.add(key)
        kept.append((r, superseded))
    return kept


def memory_line(r: Any, *, excerpt_chars: int = 0) -> str:
    """One memory in the `/v1/context` block (2.6.0).

    With an observation (the short sentence about the end user that
    `fact_extractor.extract_with_observation` writes with the facts), that sentence,
    dated by the memory's event time: Mem0 hands the agent short memories about
    the user (Chhikara et al., 2025, section 2.1), and LongMemEval (Wu et al.,
    ICLR 2025, section 5) needs the date of each round for time-aware reading.
    `excerpt_chars` > 0 adds the start of the memory's own words under it, for
    the questions about what exactly was said. Without an observation (a memory
    written before 2.6.0) the whole text, as before; with an empty one (the
    write found nothing about the user) the start of the text, at most 1,200
    characters, so a long turn does not fill the block.

    2.9.0: a row that carries its relative times (`relative_times`, the column
    `relative_time.annotate` writes when the engine's switch is on) has each of
    them dated next to its words, in the text and in the observation:
    "yesterday [2023-05-07]". The memory's words are never replaced, only
    followed (`relative_time.in_the_text`); a time the observation does not
    contain (written in another language than the memory) goes once at its
    end, "(ieri: 2023-05-07)" (`with_dates`). The dates are counted from the
    day printed in front of the line (`relative_time.times_of_the_day`).
    Without relative times the line is the 2.8.0 one, byte for byte.

    6.8.0: the memory's own words in their line (`in_its_line`): a line break
    in them, the whole text's or the start's, begins an indented line, never
    one at the margin that reads as another memory ("- [day] ..."), a fact or
    a heading of the block. The observation's line too: its whitespace is
    joined by single spaces, the dates of its relative times are not. The
    excerpt is its own second line, as before. A text with no line break is
    the 6.7.1 line, byte for byte."""
    text = (_g(r, "content") or "").strip()
    when = _g(r, "event_time") or _g(r, "created_at")
    day = _day(when) if when else ""
    # 2.8.0: the day in front of every line, the whole text included
    # (2026-10-05: "[YYYY-MM-DD] before every whole memory, no special
    # cases"; see `block_line`). Before, a memory without an observation
    # came undated.
    day_prefix = f"[{day}] " if day else ""
    times = _relative_time.times_of_the_day(_g(r, "relative_times"), day or None)
    obs = _g(r, "observation")
    if obs is None:
        return in_its_line(f"- {day_prefix}{_dated(text, times)}")
    obs = " ".join(str(obs).split())
    if not obs:
        short = text[:1200]
        return in_its_line(f"- {day_prefix}{_dated(short, times)}") + ("..." if len(text) > len(short) else "")
    row = in_its_line(f"- {day_prefix}{_relative_time.with_dates(obs, times) if times else obs}")
    if excerpt_chars > 0 and text:
        piece = " ".join(text.split())[:excerpt_chars]
        row += f"\n  said: {piece}" + ("..." if len(" ".join(text.split())) > len(piece) else "")
    return row


def _dated(text: str, times: list) -> str:
    """The memory's own words with the date after each relative time they
    hold (2.9.0); the words themselves as they are when there is none. A time
    whose words this text does not hold is not added: the text is the
    memory's, and the column was computed from it (`annotate(text=...)`)."""
    return _relative_time.in_the_text(text, times)[0] if times else text


# The hybrid block (2.8.0, from korely-agent, where it was measured on
# 2026-10-02). The first memories of the search are shown WHOLE, the rest through
# `memory_line` (their dated observation when the write gave one). The whole
# ones keep the details a single-session question needs (the evidence is
# almost always at the top: MRR 0.976); the dated short lines after them give
# breadth. On the 60-question bench, 50/60 against 46 with every memory whole
# and 36-39 with every memory short. At a small budget the packer stops inside
# the whole ones and the block is the block of before.
WHOLE = 5

# How many memories the search brings to the packer. 15 filled the block when
# every line was a whole memory; short dated lines left most of a large budget
# empty at 15 (882 tokens of a 4,000 budget, measured in korely-agent), so the
# pool is wider and the budget decides, as it always did.
POOL = 40


# The mark of a memory every fact of which was superseded, when it is shown
# (3.3.0, `memories_with_their_state(superseded_as_history=True)`). It says the
# memory held when it was said, never that it is false: with gpt-oss-120b
# "(no longer true)" after an event read as the negation of the event itself
# (the 3.3.0 pack; see contradiction.PAST_MARK). It goes right after
# the day, so a line the packer cuts keeps it.
SUPERSEDED_MEMORY_MARK = "(true when said, changed later) "


def _marked(row: str, r: Any) -> str:
    """`row` (the line `memory_line` wrote for the row `r`, "- [day] text")
    with the mark of a superseded memory after its day, or after the dash
    when it has none. The day is the row's, as `memory_line` prints it, never
    read back from the line: a text may itself begin with brackets."""
    when = _g(r, "event_time") or _g(r, "created_at")
    day = _day(when) if when else ""
    head = f"- [{day}] " if day else "- "
    if not row.startswith(head):
        head = "- " if row.startswith("- ") else ""
    return f"{head}{SUPERSEDED_MEMORY_MARK}{row[len(head):]}"


def block_line(r: Any, position: int, *, excerpt_chars: int = 0, superseded: bool = False) -> str:
    """The line of a memory in the `/v1/context` block, at `position` (0-based)
    among the memory lines already in it (2.8.0, the hybrid block above).

    The first WHOLE memories whole, under the day the memory was said; the others
    through `memory_line`. The day is the store's (`event_time`, else
    `created_at`), never read from the text: a client passes the time of what
    was said as the write's `timestamp`. LoCoMo (Maharana et al., ACL 2024)
    and LongMemEval (Wu et al., ICLR 2025, section 5) ask about time across
    sessions; with the whole memories undated the reader counted "yesterday"
    from today. Measured on 2026-10-05, LoCoMo conversation 26:
    107 to 121 of 152 with the day, the temporal questions 11 to 24 of 37
    (18 won, 4 lost, p = 0.004).

    2.9.0: with its relative times (`relative_times`), the whole text comes
    with their dates next to the words, "I went to a support group
    yesterday [2023-05-07]", under the same day (see `memory_line`).

    3.3.0: `superseded` (from `memories_with_their_state`) puts
    SUPERSEDED_MEMORY_MARK after the day."""
    if position >= WHOLE:
        row = memory_line(r, excerpt_chars=excerpt_chars)
    else:
        # Whole: the text, with the day `memory_line` puts in front of any line.
        row = memory_line({"content": _g(r, "content"), "observation": None,
                             "event_time": _g(r, "event_time"), "created_at": _g(r, "created_at"),
                             "relative_times": _g(r, "relative_times")})
    # 3.3.0: a memory kept though every fact it stated was superseded
    # (`memories_with_their_state`), marked as history. Not given, the line of 3.2.0.
    return _marked(row, r) if superseded else row
