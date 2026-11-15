"""What a write or a read may carry, the same at every door (2026-10-06).

The rules were written door by door and had drifted apart: MCP's korely_add
took 20,000 characters where POST /v1/memories stops at 16,000, let a NUL
byte reach the driver (an unreadable error instead of REST's clean refusal)
and did not bound `run_id`; korely_get_context and korely_search did not
bound the question; /v1/batch took metadata of 20 KB that the single write
refuses past 8 KB (the audit of 6 October, row 5). The same divergence on ids
and timestamps had been closed by hand on 2026-09-28, which is how it reopens:
so the numbers and the sentences live here, and REST, the batch and MCP ask.

Each check answers the problem as a sentence, or None. A door turns it into
its own refusal: a 422 on REST and in the batch, an `error:` line on MCP.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from dataclasses import dataclass
from typing import Any, Callable, Optional

# A long memory is an expensive extraction and a worse one: the model reads
# it whole and returns more facts than any reader keeps.
MAX_CONTENT_CHARS = 16000
# A question is embedded whole; past this it is a document, not a question.
MAX_QUERY_CHARS = 2000
# The bounds of a search's `min_score` (2026-10-07), the same on REST and MCP
# and as the hosted product's: a score is a cosine similarity read as 0 to 1.
MIN_SCORE_MIN = 0.0
MIN_SCORE_MAX = 1.0
# A search of the memories: how many results by default, the most one search
# returns, and how much of each memory a result shows, the same on REST (POST
# /v1/memories/search, its `snippet`) and MCP (korely_search, its line). REST
# said 15, 50 and 280 characters and MCP 10, a silent cut to 50 and 200, as
# bare literals (audit of 17 Oct 2026). The block of a large
# budget asks the engine for more than MAX_SEARCH_RESULTS on purpose
# (services/context.py, `_pool_for`); no caller of the API can.
DEFAULT_SEARCH_RESULTS = 15
MAX_SEARCH_RESULTS = 50
SNIPPET_CHARS = 280
# The one bound of every id a caller names: user_id, agent_id, run_id, on
# every door. The id columns are String(255) (tests/test_one_number_per_limit
# holds them to it): a larger value needs a migration first. It lived in
# two constants, this and ids.NAMESPACE_ID_MAX, and in some twenty-five
# literals (audit of 17 Oct 2026).
MAX_ID_CHARS = 255
# A fact's subject and relation, and its object: what the columns hold
# (agent_facts.subject_label and predicate_canonical are String(200),
# object_label String(500); tests/test_every_read_input_by_the_rule holds
# them to it). A read that narrows on a longer name can match nothing.
MAX_FACT_NAME_CHARS = 200
MAX_FACT_OBJECT_CHARS = 500
# The largest page of a listing of memories, events, end users, agents and
# facts: each route's `le=` and each service's own clamp read it, so a
# route that accepts more than its service returns, the silent half page
# of 2026-09-28, cannot come back (audit of 17 Oct 2026).
MAX_PAGE = 200
# The largest page of the audit trail, GET /v1/audit and the operator's
# screen alike: the screen clamped at 500 where the API took 1000.
MAX_AUDIT_PAGE = 1000
# The furthest a page may start: what SQL's OFFSET takes, a bigint. Past it
# Postgres refused the query and the listing answered 500 where the route's
# `ge=0` had let it through (audit of 19 Oct 2026); every route that takes an
# `offset` reads this `le=`.
MAX_OFFSET = 2**63 - 1
# The developer's metadata comes back on every read.
MAX_METADATA_BYTES = 8192
# How deep metadata may nest: objects and arrays inside one another. 8 KB of
# JSON spells some 4,000 levels, and every walk of it (this module's, the
# encoder's, the driver's) went one level of the stack each: from a few
# hundred levels a write answered 500, and a batch row the door took failed
# its whole job in the worker at every attempt (audit of 27 Oct 2026). A
# developer's metadata is a few levels; past this it is not a record.
MAX_METADATA_DEPTH = 32
# A list of names a caller picks from a set of a few (a key's scopes, a
# webhook's events): far more than any such set holds, so a name repeated is
# still taken; past it the list is one validation error, never one per value
# (audit of 27 Oct 2026, the class of the validation errors that amplify a
# counted body).
MAX_LISTED_NAMES = 64
# The most validation errors one 422 lists in `detail`; `message` counts them
# all. Each error is an object in the answer, and a body under every ceiling
# could spell over a million of them (audit of 27 Oct 2026).
MAX_VALIDATION_ERRORS_SHOWN = 20

_NUL = "\x00"

# The moments a caller may send (a memory's `timestamp`, a fact's
# `valid_from`, `as_of` and the other dates of a read), as UTC instants.
# Python and the driver read back the years 1 to 9999 only, and Postgres
# stores far more: "0001-01-01T00:00:00+14:00" (or .NET's DateTime.MinValue
# written in a European zone, "+01:00") is a moment of the year 0 in UTC,
# which was stored and could not be read back, and the facts drain, which
# loads the earliest event first, raised on it at every tick for every
# account (audit of 20 Oct 2026). One day inside each end, so the database
# session's own zone, whatever it is (no zone is a day away from UTC), never
# shows a stored bound outside those years.
EARLIEST_MOMENT = datetime(1, 1, 2, tzinfo=timezone.utc)
LATEST_MOMENT = datetime(9999, 12, 30, 23, 59, 59, 999999, tzinfo=timezone.utc)
#: The same, as every door's document says it (the moment fields' and the
#: 422's descriptions), from the bounds above so the numbers cannot drift
#: (audit of 27 Oct 2026: the rule was in the CHANGELOG only).
MOMENTS_STORED = (f"from {EARLIEST_MOMENT.date().isoformat()} to "
                  f"{LATEST_MOMENT.date().isoformat()}, in UTC")

# What a request body may weigh, checked before it is read and before any key
# is (main._BodyCap, audit of 17 Oct 2026): every JSON body was read and
# decoded whole first, with no ceiling anywhere, so anybody without a key could
# hold the one process's memory. Derived from the limits above, at the worst a
# client's JSON can spell them: a character escaped as \uXXXX is six bytes.
_JSON_BYTES_PER_CHAR = 6
#: The characters one item's values may hold: its content, its metadata
#: (whose JSON is at most MAX_METADATA_BYTES, so at most that many
#: characters) and its four ids (user_id, agent_id, run_id, timestamp).
_ITEM_CHARS = MAX_CONTENT_CHARS + MAX_METADATA_BYTES + 4 * MAX_ID_CHARS
#: Room for what is not a value: field names, quotes, marks, spaces.
_ITEM_ENVELOPE = 1024
_ITEM_BYTES = _ITEM_CHARS * _JSON_BYTES_PER_CHAR + _ITEM_ENVELOPE
#: Any route: one write is at most one item, and nothing else here is bigger.
MAX_BODY_BYTES = 2 * 1024 * 1024
#: The MCP transport: the SDK's own ceiling (mcp 1.30, 4 MiB), so ours is
#: not the one a client meets first.
MAX_MCP_BODY_BYTES = 4 * 1024 * 1024


def max_batch_body_bytes() -> int:
    """POST /v1/batch: its most items, each at the most it may carry."""
    from .batch import MAX_BATCH_ITEMS

    return MAX_BATCH_ITEMS * _ITEM_BYTES + MAX_BODY_BYTES


#: How many bodies past MAX_BODY_BYTES (a write key's large batches) the
#: process decodes at once (main._BodyCap). A batch of empty objects decodes
#: at some 26 times its bytes (audit of 18 Oct 2026); a write key costs
#: nothing on the Cloud (POST /v1/agents/init), so its large bodies wait for
#: one of these instead of piling up in the one process. Derived against the
#: API's memory (audit of 27 Oct 2026: a batch past its ceiling decoded past
#: the memory limit; tests/test_a_body_past_its_ceiling measures it at 20 items and
#: scales it): the costliest body the counts admit, one-key objects filling
#: the values' count and one string past U+FFFF the characters', holds some
#: 344 MiB raw and decoding; two, and BODIES_IN_FLIGHT_BYTES, are some 752
#: MiB of the 832 MiB the API has above its 704 MiB at rest
#: (docker-compose.yml, mem_limit 1536m). The whole text of a body decodes
#: to a str of its characters at the width of its widest one, so it is
#: counted too (`body_text_bytes`): 78 MB of spaces and one character past
#: U+FFFF decoded at five times its bytes, and two such bodies were past
#: mem_limit.
LARGE_BODIES_AT_ONCE = 2
#: How many of those places one account holds at once: its next large body
#: waits for its own first one, holding none of the process's (audit of 19
#: Oct 2026: one write key's two
#: stalled uploads held both, and every other account's large batch waited
#: until that sender stopped).
LARGE_BODIES_PER_ACCOUNT = 1
#: How long a large body has, once it holds a place, to arrive whole; past
#: it the place is given back and the request answered 408
#: `request_timeout`. The client sets its pace, and nothing else bounded it
#: (uvicorn as the image starts it and the shipped Caddy keep a stalled body
#: open). 120 s carries a body at the batch's ceiling (78 MB) at about 5
#: Mbit/s, and a usual large batch (a few MB) at a fraction of that.
LARGE_BODY_READ_SECONDS = 120.0
#: How long any other body has to arrive whole, from the first piece asked
#: for: past it the request is answered 408 `request_timeout`. Bodies under
#: the common ceiling were read with no deadline, so a stalled upload with
#: no key held its bytes for as long as the client kept the connection
#: (audit of 27 Oct 2026: a body under the common ceiling decoded with no
#: bound on the whole). 30 s carries the MCP's 4 MiB at about 1.1 Mbit/s.
BODY_READ_SECONDS = 30.0
#: What the bodies at or under the common ceiling cost at once, across every
#: request of the process: their bytes while they arrive, then what decoding
#: them costs at most (`body_weighed`), until their request ends
#: (main._BodyCap); a body that would take more is answered 503
#: `unavailable` with Retry-After at once. Nothing counted them, and any
#: number of keyless uploads could together fill the one process's memory
#: (the same finding); bytes alone did not bound it, a body of small objects
#: decoding at some 25 times its bytes. Some 4,000 of the usual write of a
#: few KB; 8 bodies at the most a body may cost here (BODIES_PER_NETWORK).
BODIES_IN_FLIGHT_BYTES = 64 * 1024 * 1024
#: The share of it one network holds (services/rate_limit.network_of, the
#: caller's address or its /64), as an account holds one large place:
#: one keyless host's 32 stalled bodies held the whole of it, and every
#: account's write answered 503 (the review of the same finding). A body
#: that would cost more than this once decoded never fits, and a 503 that
#: no retry clears is not an answer (the audit before 1.0.0: a lawful batch
#: of 1.4 to 2 MiB on an idle server): a write key's batch is decoded in one
#: of the large
#: places, as a body past the ceiling is, and any other is refused 413. A
#: lawful single write costs well under 1 MiB.
BODIES_PER_NETWORK = BODIES_IN_FLIGHT_BYTES // 8
#: Bytes what each mark of a body builds may cost at most once decoded
#: (CPython 3.11, sys.getsizeof; held to measurements by tests/
#: test_a_body_past_its_ceiling): an object, up to five members, 184; an
#: array 64; a member or an element, its slot and a number in it, 56; a
#: string 96 before its characters (its header at its widest, 76, its end
#: and the allocator's rounding), each character at the width of the
#: widest a string of the body may hold (`_strings_width`), one, two or
#: four bytes. Every character was charged four bytes whatever its width,
#: so a lawful batch of 1.4 to 2 MiB of plain text cost more than a
#: network's share and was answered 503 on an idle server, every retry the
#: same (the audit before 1.0.0); a string's header was charged less than an
#: accented one
#: takes.
_DECODED_PER_OBJECT = 184
_DECODED_PER_ARRAY = 64
_DECODED_PER_VALUE = 56
_DECODED_PER_STRING = 96

#: The most JSON values a body may spell, counted before it is decoded
#: (main._BodyCap). Bytes alone do not bound what decoding costs: a body of
#: empty objects, `{"memories":[{},{},...]}`, decodes at some 21 times its
#: bytes (78 MB to 1.7 GB, measured 18 Oct 2026), and two of them at once
#: restarted an API held to 1.5 GB (audit of 19 Oct 2026). An item that keeps every
#: rule spells at most its few fields and its metadata, whose JSON is at
#: most MAX_METADATA_BYTES. Every mark counted (`marks_by_kind`) but a
#: container's first is followed by a value of at least one byte and a mark
#: of its own or a closing one, so the densest JSON, `[[],[],...]` or
#: `[{},{},...]`, spells two marks in three bytes: this is what `items` such
#: items can hold, with room for the envelope. It said one a byte, twice
#: what any lawful item spells, and a write key's batch of one-key objects
#: filling it decoded to some 440 MiB (the review of the audit of 27 Oct
#: 2026).
_ITEM_FIELDS = 16
_ITEM_MARKS = MAX_METADATA_BYTES * 2 // 3 + 2 * _ITEM_FIELDS

#: How many items' worth a body under the common ceiling is counted as
#: (audit of 20 Oct 2026): what MAX_BODY_BYTES holds of items at their largest.
#: Those bodies were decoded unread and uncounted, before any key, at up to
#: some 26 times their bytes; held to what lawful items of their size spell,
#: a body decodes to about what it weighs.
COMMON_ITEMS = max(1, MAX_BODY_BYTES // _ITEM_BYTES)


def _batch_items() -> int:
    from .batch import MAX_BATCH_ITEMS

    return MAX_BATCH_ITEMS


def batch_json_values_cap(items: Optional[int] = None) -> int:
    """The most structural marks (see `marks_by_kind`) a body of `items` items
    (the batch's most, by default) may hold."""
    items = _batch_items() if items is None else items
    return items * _ITEM_MARKS + _ITEM_ENVELOPE


def batch_json_strings_cap(items: Optional[int] = None) -> int:
    """The most strings a body of `items` items may hold: each item's fields
    and their values, and its metadata, where the smallest string with what
    must follow it (`"",`, or `"":` for a key) takes three bytes."""
    items = _batch_items() if items is None else items
    return items * (MAX_METADATA_BYTES // 3 + 2 * _ITEM_FIELDS) + _ITEM_ENVELOPE


def batch_json_chars_cap(items: Optional[int] = None) -> int:
    """The most characters the strings of a body of `items` items may decode
    to: each item's values at their longest (`_ITEM_CHARS`) and its field
    names. Neither the quotes nor the marks bound it: one string of 78 MB of
    ASCII with one character past U+FFFF decoded at 9 times its bytes, the
    whole text and the value both four bytes a character (audit of 20 Oct
    2026)."""
    items = _batch_items() if items is None else items
    return items * (_ITEM_CHARS + _ITEM_ENVELOPE) + _ITEM_ENVELOPE


def batch_json_text_cap(items: Optional[int] = None) -> int:
    """The most bytes the whole text of a body of `items` items may take
    once decoded (`body_text_bytes`): what such a body may weigh on the
    wire, the batch's ceiling for its most items (`max_batch_body_bytes`).
    The densest lawful batch, escaped, is all ASCII and decodes at one byte
    a character; one written in UTF-8 has at most its characters at four."""
    items = _batch_items() if items is None else items
    return items * _ITEM_BYTES + MAX_BODY_BYTES


# The first bytes of a character of UTF-8 past U+FFFF, and past U+00FF: a
# str holds every character at the width of its widest, four bytes or two.
_ASTRAL_LEAD = re.compile(rb"[\xf0-\xf4]")
_WIDE_LEAD = re.compile(rb"[\xc4-\xef]")


def _text_width(body: bytes) -> int:
    """The bytes a character of the whole text of a UTF-8 body takes once
    decoded to one str: the width of its widest (CPython's str, PEP 393)."""
    return 4 if _ASTRAL_LEAD.search(body) else 2 if _WIDE_LEAD.search(body) else 1


def body_text_bytes(body: bytes, width: Optional[int] = None) -> int:
    """What the whole of a UTF-8 body decoded to one str holds, as every
    JSON decoder here decodes it first (json.loads of the bytes): its
    characters, at the width of its widest (`width`, `_text_width`'s),
    counted without decoding. Spaces and digits are characters that no
    other count sees."""
    width = _text_width(body) if width is None else width
    return (len(body) - len(body.translate(None, _NOT_CONTINUATION))) * width


def _decoding_text_bytes(body: bytes, width: int) -> int:
    """The most decoding the whole of a UTF-8 body to one str takes at once:
    CPython's decoder writes a text with a character past ASCII into a
    buffer as long as the body's bytes, then copies it at the width of its
    widest character, so its bytes at one plus that width (measured: 2, 3
    and 5 times the bytes for one, two and four), and an ASCII one its
    bytes. More than the text it leaves (`body_text_bytes`) when its
    characters take more than one byte of UTF-8 each. `width` is
    `_text_width`'s."""
    return len(body) * (1 if body.isascii() else 1 + width)


def body_encoding_problem(body: bytes) -> Optional[str]:
    """A JSON body not in UTF-8, as the sentence of its 400; or None. JSON
    between systems is UTF-8 (RFC 8259, section 8.1), and the counts of
    `body_weighed` read UTF-8; the decoder also takes UTF-16 and UTF-32,
    where an escaped quote is not the bytes the counts set aside, so a body
    in either could hide its values from them (audit of 27 Oct 2026)."""
    if json.detect_encoding(body[:4]) in ("utf-8", "utf-8-sig"):
        return None
    return NOT_UTF8


#: The sentence of that 400, for a decoder that finds it out itself.
NOT_UTF8 = "The request body is not UTF-8: send JSON in UTF-8 (RFC 8259, section 8.1)."


# What is left of a JSON body once everything but its quotes and its marks
# is set aside: the letters, digits and spaces of every value go, so what is
# counted next is as small as the structure itself.
NOT_STRUCTURE = bytes(b for b in range(256) if b not in b'"{[,:')
# Everything but the bytes that continue a character of UTF-8 (10xxxxxx):
# what is left is how many bytes the characters spend past their first.
_NOT_CONTINUATION = bytes(b for b in range(256) if not 0x80 <= b < 0xC0)

# A string in that remainder, its escapes already gone: a quote, no quote, a
# quote. One repeat of one class, which the regex engine walks without a
# state per character; a pattern that repeats a group (`(?:\\.[^"\\]*)*`, the
# usual string with its escapes) kept one per escape, and a body of 78 MB of
# escaped quotes took more than 1.5 GB to count (18 Oct 2026).
_BARE_STRING = re.compile(rb'"[^"]*"')


def without_escaped_quotes(body: bytes) -> tuple:
    """The body with its escaped backslashes, then its escaped quotes, gone,
    as JSON reads them, left to right (so every quote left opens or closes a
    string), and how many of the two there were."""
    a = body.replace(b"\\\\", b"")
    b = a.replace(b'\\"', b"")
    return b, (len(body) - len(a)) // 2, (len(a) - len(b)) // 2


def marks_by_kind(bare: bytes) -> tuple:
    """(objects, arrays, members and elements after the first) outside the
    strings of `bare`, a JSON body without its escapes and every byte that is
    neither a quote nor a mark: counted without building any of its values.
    Once the strings' contents are set aside, every value but the outermost
    follows one `[`, `{`, `,` or `:` (a key after `{` or `,`, a member's
    value after `:`, an element after `[` or `,`), so a decoder builds at
    most one value more than their sum. A body that is not JSON is the
    route's to refuse."""
    bare = _BARE_STRING.sub(b"", bare)
    return bare.count(b"{"), bare.count(b"["), bare.count(b",") + bare.count(b":")


def _escaped_high_surrogates(plain: bytes) -> int:
    """How many escapes of `plain` (a body without its escaped backslashes)
    are a high surrogate, \\uD800 to \\uDBFF: each begins a character past
    U+FFFF."""
    return sum(plain.count(b"\\u" + d + h) for d in (b"d", b"D")
               for h in (b"8", b"9", b"a", b"A", b"b", b"B"))


def _string_chars(body: bytes, plain: bytes, pairs: int, quotes: int) -> int:
    """At most how many characters the strings of a JSON body decode to,
    counted without decoding: their bytes, less the bytes a character spends
    past its first in UTF-8, less what an escape spends past the character
    it stands for (`\\uXXXX` five, any other one). `plain` is the body
    without its escaped backslashes (`pairs`) and quotes (`quotes`)."""
    outside, strings = _BARE_STRING.subn(b"", plain)
    inside = len(plain) - len(outside) - 2 * strings
    del outside
    unicode = plain.count(b"\\u")
    other = plain.count(b"\\") - unicode
    # A character past U+FFFF escaped is two escapes, a high surrogate and
    # a low one, twelve bytes for one character.
    pairs_of_escapes = _escaped_high_surrogates(plain)
    extra = len(body.translate(None, _NOT_CONTINUATION))
    return max(0, inside - extra - 5 * unicode - other - pairs_of_escapes) + pairs + quotes


def _strings_width(plain: bytes, width: int) -> int:
    """At most how many bytes a character of the strings of a JSON body takes
    once decoded: a str holds every character at the width of its widest
    (CPython's str, PEP 393), written in UTF-8 as the whole text's
    (`_text_width`) or escaped, four from an escaped surrogate
    (`\\uD800` to `\\uDBFF` begins a character past U+FFFF), two from any
    other escape past `\\u00FF`. `plain` is the body without its escaped
    backslashes and quotes (`without_escaped_quotes`); `width` its whole
    text's."""
    if width < 4 and _escaped_high_surrogates(plain):
        return 4
    if width < 2 and plain.count(b"\\u") > plain.count(b"\\u00"):
        return 2
    return width


def body_weighed(body: bytes, items: int) -> tuple:
    """(why a body spells more than `items` items that keep every rule can
    hold, or None; and at most what it costs once decoded), counted before it
    is decoded (main._BodyCap). The sentence is its 413's, naming the
    ceiling it crossed (more strings, more values, longer strings, or more
    text); it named the values' ceiling whichever had refused the body
    (audit of 20 Oct 2026). Its strings are counted first, from its quotes
    alone: setting each string aside costs the regex engine a piece of its
    answer, and a body of millions of short strings took close to 700 MB to
    count that way (18 Oct 2026), so a body with more strings than its items
    can hold is refused on that count. The cost is its bytes, what decoding
    its whole text takes (`_decoding_text_bytes`), and each of its marks, strings and characters at
    the most it decodes to (`_DECODED_PER_OBJECT` and its kin, the
    characters at `_strings_width`); a refused body's cost is 0."""
    plain, pairs, quotes = without_escaped_quotes(body)
    bare = plain.translate(None, NOT_STRUCTURE)
    strings = bare.count(b'"') // 2
    if strings > batch_json_strings_cap(items):
        return (f"The request body holds more than {batch_json_strings_cap(items):,} JSON "
                f"strings, the most this route takes."), 0
    objects, arrays, values = marks_by_kind(bare)
    if objects + arrays + values > batch_json_values_cap(items):
        return (f"The request body spells more than {batch_json_values_cap(items):,} JSON "
                f"values, the most this route takes."), 0
    del bare
    chars = _string_chars(body, plain, pairs, quotes)
    if chars > batch_json_chars_cap(items):
        return (f"The request body's strings hold more than {batch_json_chars_cap(items):,} "
                f"characters, the most this route takes."), 0
    text_width = _text_width(body)
    width = _strings_width(plain, text_width)
    del plain
    # A text that weighs no more than its bytes is the ceiling's to bound.
    text = body_text_bytes(body, text_width)
    if text > len(body) and text > batch_json_text_cap(items):
        return (f"The request body decodes to more than {batch_json_text_cap(items):,} bytes of "
                f"text, the most this route takes: send it without padding, or escape the "
                f"characters past U+FFFF."), 0
    return None, (len(body) + _decoding_text_bytes(body, text_width) + objects * _DECODED_PER_OBJECT + arrays * _DECODED_PER_ARRAY
                  + values * _DECODED_PER_VALUE + strings * _DECODED_PER_STRING
                  + chars * width)


# ── upload doors (5 Nov 2026) ─────────────────────────────────────────────

@dataclass(frozen=True)
class UploadDoor:
    """A route a file is uploaded to as multipart/form-data: such a body
    reaches its route as it arrives (main._BodyCap: counted and timed, never
    held whole nor weighed as JSON), and the route spools it as it parses
    it. Offered by a part of the product the Cloud's package carries
    (core/extensions.Part.upload_doors); a tree without the package has none.

    `name`: the door, as its route is written. `matches(path)`: whether
    `path`, as the request spells it (main._BodyCap sees no route), is this
    door. `cap`: the most bytes its body may weigh for a writer, the common
    ceiling for anybody else. `administered`: the writer is whoever
    administers the account (an operator's session, the gateway's header, a
    key with the admin scope: core/auth.admin_principal), not a key that may
    write."""
    name: str
    matches: Callable[[str], bool]
    cap: int
    administered: bool = False


def upload_door(path: str) -> Optional[UploadDoor]:
    """The upload door `path` is, or None."""
    from ..core import extensions

    for part in extensions.mounted():
        for door in part.upload_doors():
            if door.matches(path):
                return door
    return None


#: `door` of `body_is_streamed` and `body_cap` not given: they look it up.
_LOOK = object()


def body_is_streamed(path: str, content_type: Optional[str], *, door: Any = _LOOK) -> bool:
    """Whether a body to `path` of `content_type` goes to its route as it
    arrives, counted and timed but never held whole nor weighed as JSON
    (main._BodyCap): a file uploaded to an upload door as
    multipart/form-data. Any other body to those routes is JSON, held to the
    common ceiling and weighed as every other. `door`: the upload door
    `path` is, when the caller looked it up already (main._BodyCap)."""
    door = upload_door(path) if door is _LOOK else door
    return (door is not None
            and (content_type or "").split(";", 1)[0].strip().lower() == "multipart/form-data")


def body_cap(path: str, *, writer: bool = False, door: Any = _LOOK) -> int:
    """The most bytes the body of a request to `path` may have, as the router
    sees the path (mcp/server.routed_path). `writer`: the request carries a
    live key with memories:write (to an administered upload door, whoever
    administers the account: `UploadDoor`). The batch's ceiling is that key's, never
    anybody's: FastAPI reads and decodes the body before the route's key is
    checked, so a batch ceiling given by the path alone was 78 MB decoded for
    a request with no key (audit of 18 Oct 2026). The MCP's needs no key
    here: the SDK checks the key from the headers before it reads a byte.
    `door`: as body_is_streamed's."""
    if path == "/v1/batch" or path.startswith("/v1/batch/"):
        return max_batch_body_bytes() if writer else MAX_BODY_BYTES
    if path == "/mcp" or path.startswith("/mcp/"):
        return MAX_MCP_BODY_BYTES
    door = upload_door(path) if door is _LOOK else door
    if door is not None:
        # A file's, to a key that may write (to an administered door, to
        # whoever administers the account: main._writer_account), as the
        # batch's; streamed (`body_is_streamed`). A JSON body there has the
        # common ceiling whoever sends it (main._BodyCap).
        return door.cap if writer else MAX_BODY_BYTES
    return MAX_BODY_BYTES


def _place(entry) -> str:
    """The name of a place `first_in` reached (`memories[1].run_id`), built
    only for the one it answers about."""
    steps = []
    while entry[0] is not None:
        entry, step = entry
        steps.append(step)
    where = entry[1]
    for kind, key in reversed(steps):
        if kind == ".":
            where = f"{where}.{key}" if where else str(key)
        else:
            where = f"{where}[{key}]"
    return where


def first_in(field: str, value: Any, problem, *, fields=None) -> Optional[str]:
    """The first answer of `problem(value, where)` over `value` and everything
    it holds (the items of a list, tuple or set; the values of an object; the
    fields of a request body), in the order they are written, or None.
    `where()` names the place being looked at, `field` the place of `value`
    (empty: `value` is itself the named fields, a body, a tool call's
    arguments). `fields(model)`, when given, names the fields of a model the
    walk goes into; without it, every one.

    Walked with a stack of its own, never by recursion: a value nested a few
    hundred levels deep took each recursive walk past Python's stack, and
    the request answered 500 (audit of 27 Oct 2026). The name of a place is
    built only for the place answered about."""
    from pydantic import BaseModel

    stack = [((None, field), value)]
    while stack:
        entry, node = stack.pop()
        if isinstance(node, BaseModel):
            node = {name: getattr(node, name)
                    for name in (type(node).model_fields if fields is None else fields(type(node)))}
        found = problem(node, lambda entry=entry: _place(entry))
        if found:
            return found
        if isinstance(node, dict):
            stack.extend(((entry, (".", k)), v) for k, v in reversed(list(node.items())))
        elif isinstance(node, (list, tuple, set, frozenset)):
            stack.extend(((entry, ("[]", i)), v) for i, v in reversed(list(enumerate(node))))
    return None


def not_storable(value: str) -> Optional[str]:
    """What in `value` no column can hold, or None: a NUL byte, which
    Postgres text and jsonb refuse, or a lone UTF-16 surrogate (U+D800 to
    U+DFFF unpaired, which a JSON body spells "\\ud800"), which has no UTF-8
    at all. Two escapes that make one character past U+FFFF are decoded to
    that character and are not one. The lone surrogate went past the rule:
    the driver could not encode it (500), and a json column kept its escape,
    which every read of the column that decodes it then failed on, an
    erasure's among them (the audit before 1.0.0: a lone surrogate passed
    the one input rule)."""
    if _NUL in value:
        return "a NUL byte (U+0000)"
    if value.isascii():
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as e:
        return (f"a lone UTF-16 surrogate (U+{ord(value[e.start]):04X}), which is not text "
                "and cannot be stored")
    return None


def _nul_here(value: Any, where) -> Optional[str]:
    if isinstance(value, str):
        what = not_storable(value)
        return f"{where()} contains {what}" if what else None
    if isinstance(value, dict):
        what = _first_unstorable_name(list(value))
        if what:
            return f"{where() or 'a name'} contains {what}"
    return None


def nul_problem(field: str, value: Any) -> Optional[str]:
    """Where the first NUL byte (U+0000) or lone UTF-16 surrogate of what a
    caller sent is (`not_storable`), as the one sentence every door refuses
    it with, or None.

    `value` is read whole: a string, the items of a list, the keys and
    values of an object, the fields of a request body, at any depth, and
    the sentence names the place (`memories[1].run_id`, `metadata.tier`),
    the first in the order `first_in` walks them (an object's names before
    what it holds). `field` is where `value` is; empty, `value` is itself
    the named fields (a body, a tool call's arguments). Postgres text and
    jsonb cannot hold the byte and the driver raises on the way in, so a
    value that reached a query with one answered 500 with nothing to fix:
    0.1.41 and the audits after it closed it field by field, and each audit
    found the fields left (audit of 20 Oct 2026). REST's bodies
    (api/_bodies.Checked), its paths and queries (main._NulInTheAddress),
    the MCP's tool calls (mcp/server._arguments_checked) and the OAuth
    forms of the MCP SDK (services/oauth.KorelyOAuthProvider) all ask
    this.

    Read in one pass of the JSON encoder, which runs in C, and the place
    found in that one text (`_located`): walked value by value in Python,
    the rule cost some 4 µs a value on the event loop, which a body under
    its ceiling spelled millions of times (the review of the audit before
    1.0.0), and 4.6 s to
    name the place of a NUL at the end of 2.6 million values (the review of
    138b8cb); found by halves, each half encoded again, a heavy value first
    at every level of a deep one was encoded again at every halving, 82 s
    on /v1/batch (the review of 1818649). Every step here is linear in the
    text. The walk (`first_in`) reads only what the encoder cannot write: a
    key that is not a name, a value nested deeper than it goes."""
    text = _written(value)
    if text is None:
        return first_in(field, value, _nul_here)
    found = _first_unstorable(text)
    if found is None:
        return None
    found = _located(field, value, *found)
    # Never expected: a text the scan cannot follow is walked, not let through.
    return found if found is not None else first_in(field, value, _nul_here)


def _as_the_walk_reads_it(value: Any) -> Any:
    """What the walk (`first_in`) reads of a value JSON cannot write: a
    model's fields, a set's items. Anything else (a moment, a number of
    its own kind) holds no text the rule reads."""
    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        return {name: getattr(value, name) for name in type(value).model_fields}
    if isinstance(value, (set, frozenset)):
        return list(value)
    return None


def _encoded(value: Any, **how) -> str:
    """json.dumps(value, **how), and when the stack it is called on has no
    room for how deep `value` nests, once more on a thread of its own,
    whose stack starts empty. The body's rule runs some frames deeper than
    the parser that read the body, so a value the parser took could be
    deeper than the encoder had room for here: it was then walked value by
    value, 2.6 million of them beside a chain 950 deep some 5 s on
    /v1/batch (the review of 5b9e806). Raises as json.dumps does when that
    thread has no room either."""
    try:
        return json.dumps(value, **how)
    except RecursionError:
        from ..core import threads

        return threads.pool("encoder", threads.ENCODER_THREADS).submit(json.dumps, value, **how).result()


def _written(value: Any) -> Optional[str]:
    """`value` as the JSON encoder writes it, in C, with ensure_ascii off (a
    lone surrogate stays as it is, which UTF-8 cannot encode; U+0000 is the
    escape `\\u0000`), each model and set as the walk reads it (`_encoded`);
    None when the encoder cannot (a key that is not a name, which JSON never
    spells, or nested deeper than a thread's whole stack)."""
    try:
        return _encoded(value, ensure_ascii=False, default=_as_the_walk_reads_it)
    except (TypeError, ValueError, RecursionError):
        return None


# A JSON string as the encoder writes it; possessive, so a long one keeps
# no state to go back to.
_WRITTEN_STRING = re.compile(r'"[^"\\]*+(?:\\.[^"\\]*+)*+"', re.S)
_BRACKET = re.compile(rb"[\[\]{}]")
_NOT_SHAPE = bytes(b for b in range(256) if b not in b"[]{},:")


def _first_unstorable(text: str) -> Optional[tuple]:
    """(`text`, as `_written` writes it, with its backslashes' escapes set
    aside; where in that the first character no column can hold is), or
    None: the backslash of a NUL's escape, or a lone surrogate (where UTF-8
    first fails). Exact both ways, in C: a backslash the text holds is
    written `\\\\`, so with those pairs set aside every backslash begins an
    escape, `\\u0000` is a NUL and nothing else, and a text that only looks
    like one (a backslash before `u0000`) is clean. Setting them aside
    changes no string's bounds, so the shape of the text is the same."""
    if "\\\\" in text:
        text = text.replace("\\\\", "")
    first = text.find("\\u0000")
    first = None if first < 0 else first
    if not text.isascii():
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as e:
            first = e.start if first is None else min(first, e.start)
    return None if first is None else (text, first)


def _open_at(text: str, at: int):
    """The objects and arrays of `text` open at `at` (inside a string),
    outermost first, each [its opening byte, how many of its items are
    before `at`], and whether the string at `at` is a name; or None.

    One pass: the strings before `at` are set aside (the one at `at` closed
    where it is cut), then what is left but the marks; each mark is read
    once, and the commas between two marks are counted for the one open
    there."""
    shape = _WRITTEN_STRING.sub("", text[:at] + '"')
    try:
        shape = shape.encode("ascii").translate(None, _NOT_SHAPE)
    except UnicodeEncodeError:
        return None
    opened, last = [], 0
    for m in _BRACKET.finditer(shape):
        here = m.start()
        if opened:
            opened[-1][1] += shape.count(b",", last, here)
        if shape[here] in b"[{":
            opened.append([shape[here], 0])
        elif opened:
            opened.pop()
        else:
            return None
        last = here + 1
    if opened:
        opened[-1][1] += shape.count(b",", last)
    in_name = bool(opened) and opened[-1][0] == ord("{") and not shape.endswith(b":")
    return opened, in_name


def _located(field: str, value: Any, text: str, at: int) -> Optional[str]:
    """The sentence for the character at `at` of `text` (`value` written, as
    `_first_unstorable` gives it back), named as `first_in` names it: the
    place of the string that holds it, unless an object on the way has a
    name no column can hold, which the walk reads first (an object's names
    before what it holds; everything before the place in the text is
    clean). Each object on the way has its names written once, and nothing
    is written again. None when the text and the value do not agree."""
    opened = _open_at(text, at)
    if opened is None:
        return None
    path, in_name = opened
    entry, node = (None, field), value
    for depth, (mark, before) in enumerate(path, 1):
        if not isinstance(node, (dict, list, tuple, str)):
            node = _as_the_walk_reads_it(node)
        if isinstance(node, dict) and mark == ord("{"):
            names = list(node)
            if depth == len(path) and in_name:
                # The names before it are before it in the text: it is the first.
                name = names[before] if before < len(names) else None
                what = not_storable(name) if isinstance(name, str) else None
                return f"{_place(entry) or 'a name'} contains {what}" if what else None
            what = _first_unstorable_name(names)
            if what:
                return f"{_place(entry) or 'a name'} contains {what}"
            if before >= len(names):
                return None
            entry, node = (entry, (".", names[before])), node[names[before]]
        elif isinstance(node, (list, tuple)) and mark == ord("["):
            if before >= len(node):
                return None
            entry, node = (entry, ("[]", before)), node[before]
        else:
            return None
    what = not_storable(node) if isinstance(node, str) and not in_name else None
    return f"{_place(entry)} contains {what}" if what else None


def _first_unstorable_name(names: list) -> Optional[str]:
    """What the first name of `names` (an object's) no column can hold has
    (`not_storable`), or None: found in one pass, as a value's place is."""
    text = _written(names)
    if text is not None:
        found = _first_unstorable(text)
        if found is None:
            return None
        opened = _open_at(*found)
        if opened is not None and len(opened[0]) == 1 and opened[0][0][1] < len(names):
            name = names[opened[0][0][1]]
            what = not_storable(name) if isinstance(name, str) else None
            if what:
                return what
    return next((what for what in (not_storable(n) for n in names if isinstance(n, str)) if what),
                None)


def moment_problem(field: str, moment: Optional[date]) -> Optional[str]:
    """A moment a caller sent, outside what can be stored and read back
    (EARLIEST_MOMENT to LATEST_MOMENT, in UTC), as the sentence of its 422; or
    None. A value with no offset is UTC, as every parser here reads it; a
    date is its midnight."""
    if moment is None:
        return None
    if not isinstance(moment, datetime):
        moment = datetime(moment.year, moment.month, moment.day)
    try:
        utc = (moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None
               else moment.astimezone(timezone.utc))
    except OverflowError:
        utc = None
    if utc is not None and EARLIEST_MOMENT <= utc <= LATEST_MOMENT:
        return None
    # ISO dates, the same on every platform: strftime's %Y does not pad a
    # year below 1000 on glibc, and the sentence said "from 1-01-02", a
    # date its own parser refuses (audit of 27 Oct 2026).
    return f"{field} is outside the moments that can be stored: {MOMENTS_STORED}"


def content_problem(text: Optional[str]) -> Optional[str]:
    """A memory's text, stripped: empty, too long, or holding a NUL byte."""
    s = (text or "").strip()
    if not s:
        return "content must not be empty or whitespace"
    if len(s) > MAX_CONTENT_CHARS:
        return f"content is {len(s)} characters, more than {MAX_CONTENT_CHARS}"
    return nul_problem("content", s)


def query_problem(query: Optional[str]) -> Optional[str]:
    s = (query or "").strip()
    if not s:
        return "query must not be empty"
    if len(s) > MAX_QUERY_CHARS:
        return f"query is {len(s)} characters, more than {MAX_QUERY_CHARS}"
    return nul_problem("query", s)


def min_score_problem(value, field: str = "min_score") -> Optional[str]:
    """A search's floor (`min_score`, or `field` for another): absent, or
    a number from MIN_SCORE_MIN to MIN_SCORE_MAX (a boolean is not a number
    here)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{field} must be a number"
    if not MIN_SCORE_MIN <= value <= MIN_SCORE_MAX:
        return f"{field} must be from {MIN_SCORE_MIN} to {MIN_SCORE_MAX}"
    return None


def range_problem(field: str, value, low: int, high: int) -> Optional[str]:
    """A whole number a door takes within bounds (a search's `limit`, a
    block's `token_budget`, a page of facts): outside them it is refused, on
    MCP as REST's `ge`/`le` refuse it with a 422, never cut to fit nor read
    as the default (audit of 17 Oct 2026). A boolean is not a number
    here."""
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{field} must be a whole number"
    if not low <= value <= high:
        return f"{field} must be from {low} to {high}, got {value}"
    return None


def id_problem(field: str, value: Optional[str]) -> Optional[str]:
    """An optional free-form id (`run_id` on a write, any id a read narrows
    on): not empty, bounded, and no NUL.

    Empty is no value at all, the rule of the namespace ids
    (ids.namespace_id_problem) and of GET /v1/audit: "none" is spelled by
    leaving the field out. A read with `user_id=` filtered on the string ''
    and answered an empty store, and a write kept `run_id: ""` as a run of
    its own, distinct from no run (audit of 17 Oct 2026)."""
    if value is None:
        return None
    if value == "":
        return f"{field} must not be empty: leave it out instead"
    if len(value) > MAX_ID_CHARS:
        return f"{field} is longer than {MAX_ID_CHARS} characters"
    return nul_problem(field, value)


def name_problem(field: str, value: Optional[str], most: int = MAX_FACT_NAME_CHARS) -> Optional[str]:
    """A name a read of the facts narrows on (`subject`, `entity`,
    `predicate`, `predicate_family`): bounded by the column it is compared
    with, and no NUL. Empty stays no filter, as these always read it, unlike
    an id (id_problem): '' never matched anything as a name.

    A NUL byte reached the driver and came back as a 500 on REST and as the
    driver's own words on MCP (audit of 18 Oct 2026)."""
    if not value:
        return None
    if len(value) > most:
        return f"{field} is longer than {most} characters"
    return nul_problem(field, value)


def _not_finite_at(value: Any, path: str = "") -> Optional[str]:
    """Where the first NaN or infinity of a JSON value is ("limits.max",
    "scores[1]"), or None."""
    return first_in(path, value, lambda v, where: (where() or "(the whole value)")
                    if isinstance(v, float) and not math.isfinite(v) else None)


def _depth(value: Any, most: Optional[int] = None) -> int:
    """How many objects and arrays deep a JSON value nests, walked with a
    stack of its own (`first_in`'s reason); past `most`, the first depth
    past it, without walking the rest."""
    deepest, stack = 0, [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, (dict, list, tuple)):
            depth += 1
            deepest = max(deepest, depth)
            if most is not None and deepest > most:
                return deepest
            stack.extend((v, depth) for v in (node.values() if isinstance(node, dict) else node))
    return deepest


def metadata_problem(metadata: Any) -> Optional[str]:
    if metadata is None:
        return None
    # First what it weighs, in one pass of the encoder, which runs in C:
    # each walk below goes value by value in Python, and an oversized
    # metadata of 2.6 million values was walked twice before it was weighed
    # (the review of 138b8cb). Its characters are at most its bytes of
    # UTF-8, so more of them than the limit is surely past it; at or under
    # the limit it holds a few thousand values, and the walks cost nothing.
    # One the encoder cannot write (nested deeper than a thread's whole
    # stack, `_encoded`) is walked for its depth below.
    try:
        written = len(_encoded(metadata, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        written = 0
    if written > MAX_METADATA_BYTES:
        return f"metadata exceeds {MAX_METADATA_BYTES} bytes"
    # Then, before any walk that goes one level of the stack each (the
    # encoder below, the driver's): how deep it nests (audit of 27 Oct 2026).
    if _depth(metadata, MAX_METADATA_DEPTH) > MAX_METADATA_DEPTH:
        return f"metadata nests deeper than {MAX_METADATA_DEPTH} levels of objects and arrays"
    # Python's JSON parser reads NaN and Infinity, PostgreSQL refuses them
    # ("invalid input syntax for type json"), and the write died with a 500
    # inside its transaction (the hosted product found it on 6 Oct, e99d809).
    # Refused here, at any depth, naming where.
    where = _not_finite_at(metadata)
    if where is not None:
        return (f"metadata value at '{where}' is NaN or infinite: JSON has no such "
                "number, so it cannot be stored")
    # Postgres's jsonb refuses U+0000, in a key as in a value, and a lone
    # surrogate (`"\ud800"`, which JSON can spell) has no UTF-8 at all: by
    # the rule of every door, naming where, before the size is counted in
    # the UTF-8 it cannot be written in.
    problem = nul_problem("metadata", metadata)
    if problem:
        return problem
    # Bytes of UTF-8, what the limit says and what the column stores. The
    # default ASCII escaping wrote each non-ASCII character as \uXXXX, six
    # bytes, so Japanese metadata was refused at about half the limit and
    # Cyrillic at a third, while English had all of it (audit of 17 Oct
    # 2026).
    if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > MAX_METADATA_BYTES:
        return f"metadata exceeds {MAX_METADATA_BYTES} bytes"
    return None


# The most keys one search's metadata filter names (POST /v1/memories/search).
# Each key is its own condition of up to four comparisons, ANDed into the
# vector query, and planning grew some four times per doubling: 4,000 keys in
# one 47 KB request held the database 4.35 s, and the 2 MiB body allowed
# 157,730 (audit of 18 Oct 2026). A memory's metadata rarely has more.
MAX_METADATA_FILTER_KEYS = 32


def metadata_filter_problem(metadata: Any) -> Optional[str]:
    """A search's metadata filter: at most MAX_METADATA_FILTER_KEYS keys, and
    what metadata may hold (`metadata_problem`: MAX_METADATA_BYTES, no NaN, no
    NUL, no lone surrogate). A filter is matched against stored metadata, so
    one that metadata could not hold only adds cost, and a NUL failed the
    search with a 503 instead of refusing it."""
    if not metadata:
        return None
    if len(metadata) > MAX_METADATA_FILTER_KEYS:
        return (f"a metadata filter names at most {MAX_METADATA_FILTER_KEYS} keys, "
                f"this one {len(metadata)}")
    return metadata_problem(metadata)
