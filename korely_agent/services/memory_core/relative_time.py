"""Relative times dated by code, not by the model (memory_core 2.9.0).

Why. On LoCoMo conversation 26 (measured on 5 Oct 2026) every wrong answer about
time had its evidence in the block: the reader counted "yesterday", "last
week", "the week before 9 June" from the wrong day. Asking the writer to put
absolute dates in the observation (2.8.0 `date_assolute`, gone with 4.0.0)
changed 1 relative time in 40. Three papers, read and verified against their
texts (5 Oct 2026):

  * SCATE, Su, Yu, Howard, Bethard 2025 (arXiv:2507.06450), Table 2, on the
    TempEval-2013 test set (313 time expressions): the same model, Claude
    3.7, scores 0.49 accuracy when it writes the time operation and code
    executes it (SCATE prompt), 0.38 when it writes the interval itself
    (interval few-shot prompt).
  * TReMu, Ge et al. 2025 (arXiv:2502.01630), Table 5, GPT-4o: a memory
    written with each event and its date, inferred when the memory is written
    (Timeline + CoT, section 3.2.1), answers 71.50 overall against 41.67
    without (MemoChat + CoT), 83.33 against 51.14 on temporal anchoring; code
    doing the arithmetic of the question (TReMu, section 3.2.2) 77.67. There
    the dates are the model's; here the model describes the expression and
    the code dates it, as in SCATE.
  * PREMem, Kim et al. 2025 (arXiv:2509.10852), section 3.1: "specific past
    events convert relative expressions to absolute dates" when the memory is
    stored; without that temporal step (Table 4) scores fall by up to 16.4%.

The contract. In the extraction call the model returns, for each relative
time of a memory, a small object, never a date it computed:

    {"text": "two weekends ago", "direction": "before", "quantity": 2,
     "unit": "weekend"}

and this module computes a TIMEX3-style value from the memory's day (the
document creation time of TimeML):

    resolve(read(obj), date(2023, 7, 17))
    -> value "2023-W27-WE", start 2023-07-08, end 2023-07-09,
       label "the weekend of 2023-07-08 to 2023-07-09"

The model recognises the expression in any language ("ieri", "la settimana
scorsa", "hier"); the code sees only the object. Standard library only (and
`extraction.comparable`, itself standard library), Python 3.9+, no I/O and
no clock: the same object and the same day give the same value on every
machine and in every locale. What the model is asked is `RULES_RELATIVE_TIMES`
below, the JSON schema of the object is `SCHEMA`, and the reader is `read`:
the three describe the same object and live in one file, so that they cannot
drift apart (each is checked against the others by the tests).

The object:
  text         the words of the memory, copied exactly
  direction    this | last | next | before | after
  unit         day | week | weekend | month | year
  weekday      monday ... sunday: a named day, alone or with unit "week"
  month_name   january ... december: a month named without its year
  season       spring | summer | autumn | winter
  quantity     with before/after only: a whole number, "couple" or "few"
  part_of_day  morning | afternoon | evening | night, for a single day
  anchor       YYYY-MM-DD or XXXX-MM-DD when the count starts from a date the
               memory states; absent: the memory's day
  granularity  "day" (or "month" for a count of years): an exact shift
               instead of the calendar unit
"last" and "before" compute the same thing, and so do "next" and "after":
"last" and "next" name the adjacent one (no quantity), "before" and "after"
take a count. Whichever the model picks for "the week before 9 June", the
value is the same. One exception (3.3.0): one day back or ahead of the
memory's own day, with no anchor, is "last" or "next" day only. Written as
"before" or "after" with a count of one it is refused: on a reading of
LoCoMo conversation 26 (6 Oct 2026), 8 of the 12 false dates Gemini wrote had
that shape ("recently", "since our last chat" dated to the day before or
after), and none of the true dates of either writer did. Those words name the
past or the present without a date: TIDES 2005 (Ferro, Gerber, Mani, Sundheim,
Wilson, §4.3.2) gives "lately", "recently", "a while ago" the token PAST_REF,
not a calendar value, and "yesterday" a date only when it resolves to one.

Out of this version (agreed on 5 Oct 2026): the
southern hemisphere's seasons; stretches of time and intervals ("the past
three days", "since 2019"); the start, middle or end of a period ("early last
week", TIMEX3 mod START/MID/END); expressions anchored to an event ("two days
after the party"). The day is the UTC day, the one the block prints in front
of the memory. The facts' `valid_from` does not take these days: the guard on
invented dates (`fact_time.date_from_text`) stays as it is.

Conventions: TimeML where it is explicit, else the reading most annotators
use. D is the anchor day and W its ISO week. Each row is pinned by
tests/test_engine_relative_time.py.

| #  | expression (object)                     | value                                                     | basis           |
|----|-----------------------------------------|-----------------------------------------------------------|-----------------|
| 1  | the anchor                              | D: the memory's day (the UTC day of event_time, else      | [T] 2.2.3f, 4   |
|    |                                         | created_at, as the block prints it), or "anchor"          |                 |
| 2  | today, yesterday, tomorrow, n days ago  | D, D-1, D+1, D-n                                          | [T] 4           |
| 3  | this, last, next week                   | W, W-1, W+1: ISO weeks, Monday to Sunday, value YYYY-Www  | [X] 4.2.3, [S]  |
|    |                                         | in the ISO week-numbering year (2020-W53, 2025-W01)       | fig. 15, 17, [R]|
| 4  | the week before 9 June                  | the week before the anchor's week: the same as "last      | [S] LAST, [L]   |
|    | (last or before, week, anchor)          | week" said on 9 June                                      |                 |
| 5  | last Saturday (last, weekday)           | the nearest Saturday before D: said on a Saturday, D-7;   | [S] LAST, [T]   |
|    |                                         | said on a Sunday, D-1                                     | 2.2.3c, [L]     |
| 6  | next Saturday (next, weekday)           | the nearest Saturday after D: said on a Friday, D+1; on   | [S] NEXT, [X]   |
|    |                                         | a Saturday, D+7. For "Saturday of next week" the model    | 4.2.1, 4.2.3,   |
|    |                                         | writes next + week + saturday                             | [L]             |
| 7  | this Saturday (this, weekday)           | the Saturday of W, even if already past                   | [S2] This       |
| 8  | Saturday of next week                   | the Saturday of W+1                                       | composition     |
|    | (next, week, weekday)                   |                                                           |                 |
| 9  | on Saturday (a bare weekday)            | the model writes last or next from the tense              | [X] 4.4, 4.7    |
| 10 | this, last, next weekend; two weekends  | Saturday and Sunday of W, W-1, W+1, W-2: on a Monday      | [X] 4.3.6, [H], |
|    | ago                                     | "last weekend" is the one just ended; value YYYY-Www-WE   | [L]             |
| 11 | last night; tonight; this morning       | the night of D-1 (YYYY-MM-DDTNI, TIMEX3 type TIME); the   | [X] 4.3.7       |
|    | (part_of_day)                           | night of D; the morning of D. Tokens MO AF EV NI          |                 |
| 12 | this, last, next month or year          | calendar arithmetic: "next month" said in December is     | [T], [X]        |
|    |                                         | January of the next year                                  |                 |
| 13 | two weeks ago, three months ago, ten    | the calendar week, month or year n before D's, not the    | [X] 4.3.1, [H]  |
|    | years ago (before/after with a count)   | exact day: the exact day always falls inside it           |                 |
| 14 | ... with granularity "day"              | the exact shift (D-14); a day missing from the target     | [X] 4.2.1, [J]  |
|    |                                         | month becomes its last day (2024-02-29 - 1 year =         |                 |
|    |                                         | 2023-02-28)                                               |                 |
| 15 | a couple of days ago, a few weeks ago   | value PAST_REF (FUTURE_REF after), mod APPROX, and the    | [X] 4.3.2, [H]; |
|    | (quantity couple or few)                | period from 3 to 2 units (couple) or from 6 to 2 units    | the bounds are  |
|    |                                         | (few) before D's unit                                     | ours            |
| 16 | last, this, next summer (season)        | meteorological seasons, northern hemisphere: spring       | [X] 4.3.3, [S], |
|    |                                         | Mar-May, summer Jun-Aug, autumn Sep-Nov, winter YYYY =    | [L], [M]        |
|    |                                         | Dec YYYY-1 to Feb YYYY. last: the latest that ended       |                 |
|    |                                         | before D; next: the earliest that starts after D; this:   |                 |
|    |                                         | the one D is in, else the nearest (ties: the later)       |                 |
| 17 | last, this, next March (month_name)     | the latest March before D's month; the March of D's       | [S] LAST NEXT,  |
|    |                                         | year; the earliest March after D's month                  | [S2] This       |
| 18 | anchor XXXX-MM-DD (no year in the text) | the occurrence nearest D (ties: the earlier)              | ours            |

[T]  TimeML Annotation Guidelines 1.2.1 (Sauri, Littman, Knippen, Gaizauskas,
     Setzer, Pustejovsky 2006). 2.2.3c: "Last Friday" said on Friday
     2002-07-12 is 2002-07-05; the year left as XXXX when unknown. 2.2.3f:
     anchorTimeID. 4: "yesterday" and "2 days ago" are DATEs computed from
     the DCT. 2.2.3c also says the value is annotated "exactly as specified
     in TIDES(02) sections 3.2 and 3.3".
[X]  TIDES 2005 Standard for the Annotation of Temporal Expressions (Ferro,
     Gerber, Mani, Sundheim, Wilson), reference day Thursday 1999-07-15.
     4.2.1: "next Tuesday" is 1999-07-20, "two weeks ago today" 1999-07-01.
     4.2.3: "next week" is 1999-W29, "next Thursday" 1999-07-22. 4.4 and
     4.7: a bare weekday by its tense, "on Friday" (future) 1999-07-16, "on
     Tuesday" (past) 1999-07-13. 4.3.1, Expression Granularity rule: "a
     year ago" is 1998, "six weeks ago" a week value. 4.3.2: "several weeks
     ago" is PAST_REF anchored on the current week. 4.3.3: seasons SP SU FA
     WI, "last summer" is 1998-SU, winter takes the year of its January.
     4.3.6: "this weekend" is 1999-W28-WE: first the week, then WE. 4.3.7:
     parts of the day, "Last Night is Yesterday" rule.
[S]  Bethard and Parker, A Semantically Compositional Annotation Scheme for
     Time Normalization (SCATE), LREC 2016, 3.4: LAST(I, R, n) = the n latest
     of {[ts, te) in R : te <= t1}; NEXT(I, R, n) = the n earliest of
     {[ts, te) in R : t2 <= ts}; THIS(I, R) = the ones containing I;
     BEFORE(I, R, n) = the nth latest. Calendar weeks for "this" and "the
     coming week".
[S2] Su, Yu, Howard, Bethard 2025, arXiv:2507.06450, 3.2.2: This finds "the
     current shift containing a given interval"; "this January" spoken on
     10 November 1037 is This(Interval.of(1037, 11, 10), Repeating(MONTH,
     YEAR, value=1)), the January of the year that holds the day.
[R]  TReMu, fig. 4: "last week" said on Monday 2020-03-16 is the week of
     Monday 2020-03-09; table 4: "last Saturday" said on 2020-01-28 is
     2020-01-25.
[L]  LoCoMo gold answers (Maharana et al., ACL 2024): the evidence of the
     temporal questions of the 10 conversations holds 214 relative times,
     counted per question. "last Friday" said on Sunday 2023-10-22: "The
     Friday before 22 October 2023"; "next Saturday" said on Friday
     2023-01-27: "Saturday after 27 January, 2023"; "last week": "The week
     before D" 28 times and a calendar week 12 times ("last week of August
     2023", "The week of April 3rd to 9th"), of 58 (the others answer
     another part of the question); "this weekend" said on Monday
     2022-08-22: "The weekend of 22 August, 2022"; "two weekends ago" said
     on 2023-07-17: "two weekends before 17 July 2023"; "last summer" said
     on 2023-12-26: "summer 2023".
[H]  HeidelTime (Stroetgen and Gertz 2010), English resources: normUnit maps
     "weekend" to "week-WE"; "two weeks ago" is UNDEF-this-week-MINUS-2; "a
     few days ago" is PAST_REF.
[J]  java.time LocalDate.plusMonths and minusYears, and PostgreSQL date +
     interval: a day the target month lacks becomes its last day.
[M]  The meteorological seasons of climatology (DJF, MAM, JJA, SON, the
     three-month groups of the WMO climate normals): whole months, the
     dates TIDES leaves open on purpose.

The conventions table and its sources come from a design note (korely-agent
f673ab3), checked against the texts on 5 Oct 2026, each example cited above
found as written: [T] TimeML
1.2.1 (2.2.3c, 2.2.3f, 4.1); [X] TIDES 2005 (4.2.1, 4.2.3, 4.3.1, 4.3.2,
4.3.3 with the winter year, 4.3.6, 4.3.7, 4.4, 4.7); [S] SCATE 2016 (3.4, the
LAST, NEXT, THIS and BEFORE definitions, figures 15 and 17); [S2] and [R];
[L] the cited gold answers in locomo10.json (conv-26 D2:1, D9:1, D19:1;
conv-41 D27:4; conv-42 D19:9; conv-43 D26:23; conv-48 D2:30); [H]
HeidelTime's English normUnit and date rules r19a and r19b; [J] the
java.time javadoc and PostgreSQL itself. [M] is the standard climatology
convention, not checked against a WMO text.

Known split readings, kept as stated above: "next Saturday" said from Monday
to Friday (row 6) and "next weekend" said from Monday to Friday (row 10:
SCATE's NEXT would give the coming weekend, TIDES and HeidelTime the weekend
of the next week, and the coming one is "this weekend").
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from .extraction import comparable

# Stored with the annotations: a change to the table above bumps it, so the
# stored values can be recomputed from "expr" without asking the model again
# (`times_of_the_day` does it when it reads a column of another version).
CONVENTIONS_VERSION = 1

DIRECTIONS = ("this", "last", "next", "before", "after")
UNITS = ("day", "week", "weekend", "month", "year")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december")
SEASONS = ("spring", "summer", "autumn", "winter")
PARTS_OF_DAY = ("morning", "afternoon", "evening", "night")
GRANULARITIES = ("day", "month")
# Vague counts (row 15): how many units back or ahead, first and last. One
# unit has its own word (yesterday, last week); seven days are a week.
VAGUE = {"couple": (2, 3), "few": (2, 6)}

MAX_TEXT = 120
MAX_QUANTITY = 9999
MAX_TIMES = 12

# TIDES tokens (4.3.3 seasons, 4.3.7 parts of the day).
_SEASON_TOKEN = {"spring": "SP", "summer": "SU", "autumn": "FA", "winter": "WI"}
_PART_TOKEN = {"morning": "MO", "afternoon": "AF", "evening": "EV", "night": "NI"}
# Month names for the labels, never the locale's (calendar.month_name follows
# LC_TIME).
_MONTH_LABELS = ("January", "February", "March", "April", "May", "June", "July",
                   "August", "September", "October", "November", "December")
_ADJACENT_STEP = {"this": 0, "last": -1, "next": 1}
# The year of a winter is the year of its January (TIDES 4.3.3).
_SEASON_MONTHS = {"spring": (3, 4, 5), "summer": (6, 7, 8), "autumn": (9, 10, 11), "winter": (12, 1, 2)}


# ------------------------------------------------------- what the model is asked

# The paragraph of the extraction prompt (from the design note, section 2),
# added by
# `fact_extractor.extract_with_observation(times=True)` and its batched form. The
# model writes the operation, the code executes it: SCATE (arXiv:2507.06450,
# Table 2) 0.49 against 0.38 when the model writes the interval; dating at
# write time, TReMu (arXiv:2502.01630, Table 5) and PREMem (arXiv:2509.10852,
# section 3.1). It says WHAT to recognise, never which words signal it (the
# rule of 27 September): the quoted objects are shapes of the
# output, in four languages, and every one of them is read and dated by the
# tests. The list of fields is the one `read` reads and `SCHEMA` describes.
RULES_RELATIVE_TIMES = """\
RELATIVE TIMES (mandatory field "relative_times"): list every expression of
this memory, in every turn and whoever says it, that places something in time
by counting from the day the memory was said, or from a date the memory states: that day itself, a day,
a weekday, a week, a weekend, a month, a season or a year before or after
it, or a number of them back or ahead. Do NOT work out the date: describe
the expression with these fields and the date is computed from them. One
object per expression; [] when there is none.
- "text": the words of the memory, copied exactly (same language, same
  spelling, nothing added).
- "direction": "this" (the one the day is in), "last" (the one just
  before), "next" (the one just after), "before" or "after" (a number of
  them back or ahead, given in "quantity").
- What is counted, one of: "unit" ("day", "week", "weekend", "month",
  "year"); "weekday" ("monday" ... "sunday") for a named day, alone, or
  with "unit": "week" for that day of the week chosen; "month_name"
  ("january" ... "december") for a month named without its year; "season"
  ("spring", "summer", "autumn", "winter").
- "quantity", only with "before" or "after": a whole number, "couple" for a
  couple, "few" for a few, several or some.
- "part_of_day" when the expression names one: "morning", "afternoon",
  "evening", "night".
- "anchor", only when the count starts from a date the memory states: that
  date as YYYY-MM-DD, or XXXX-MM-DD when the memory gives no year.
- "granularity": "day" only for an exact count from the anchor ("a week
  after 3 June", "a year ago today").
Leave out the fields that do not apply. A weekday named on its own takes
"last" when it comes before the day of the memory and "next" when it comes
after. The day just before or after the memory's day is "last" or "next"
with unit "day", never "before" or "after" with a count of one. Leave out times with nothing to count (now, recently, a while ago),
stretches of time (for three days, since 2019, the past two weeks) and dates
the memory gives in full. In the observation keep these expressions in the
memory's words; never replace them with a date.
Shapes of the output, in any language:
  "I went to a support group yesterday" ->
    {"text": "yesterday", "direction": "last", "unit": "day"}
  "We ran a charity race last Saturday" ->
    {"text": "last Saturday", "direction": "last", "weekday": "saturday"}
  "we went camping two weekends ago" ->
    {"text": "two weekends ago", "direction": "before", "quantity": 2, "unit": "weekend"}
  "Ho parlato in una scuola la settimana scorsa" ->
    {"text": "la settimana scorsa", "direction": "last", "unit": "week"}
  "Ci vediamo sabato della settimana prossima" ->
    {"text": "sabato della settimana prossima", "direction": "next", "unit": "week", "weekday": "saturday"}
  "J'ai commencé la poterie il y a quelques jours" ->
    {"text": "il y a quelques jours", "direction": "before", "quantity": "few", "unit": "day"}
  "Anoche fuimos a un concierto" ->
    {"text": "Anoche", "direction": "last", "unit": "day", "part_of_day": "night"}
  "I moved the week before 9 June" ->
    {"text": "the week before 9 June", "direction": "last", "unit": "week", "anchor": "XXXX-06-09"}
  "Ultimamente dormo poco" -> nothing: no time to count
  "Je cours chaque matin" -> nothing: a habit, not a time"""

# The object as a JSON Schema (draft 2020-12), from the design note: for a
# provider that constrains its output to a schema, and for the tests, which
# check that it accepts what `read` accepts and refuses what it refuses.
SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:korely:memoria-core:relative-time:1",
    "title": "Relative time",
    "description": (
        "One item of the \"relative_times\" list the extraction model returns for a memory: a relative "
        "time described, never the date it means. relative_time.resolve computes the date from the "
        "memory's day. Fields that do not apply are left out (the reader, relative_time.read, also "
        "takes null or \"\" for them)."),
    "type": "object",
    "required": ["text", "direction"],
    "additionalProperties": False,
    "properties": {
        "text": {
            "description": "The words of the memory, copied exactly (same language, same spelling).",
            "type": "string", "minLength": 1, "maxLength": MAX_TEXT, "pattern": "\\S",
        },
        "direction": {
            "description": ("this: the one the day is in; last: the one just before; next: the one just "
                            "after; before / after: a number of them back or ahead (with quantity)."),
            "enum": list(DIRECTIONS),
        },
        "unit": {
            "description": "What is counted, when it is not a named day, month or season.",
            "enum": list(UNITS),
        },
        "weekday": {
            "description": ("A named day: alone (the nearest one in the direction), or with unit week "
                            "(that day of the chosen week)."),
            "enum": list(WEEKDAYS),
        },
        "month_name": {"description": "A month named without its year.", "enum": list(MONTHS)},
        "season": {
            "description": "A named season (meteorological, northern hemisphere).",
            "enum": list(SEASONS),
        },
        "quantity": {
            "description": "With before/after only: how many, or couple (a couple of) or few (a few, several, some).",
            "oneOf": [
                {"type": "integer", "minimum": 1, "maximum": MAX_QUANTITY},
                {"enum": sorted(VAGUE)},
            ],
        },
        "part_of_day": {
            "description": "For a single day: unit day or a weekday.",
            "enum": list(PARTS_OF_DAY),
        },
        "anchor": {
            "description": ("Only when the count starts from a date the memory states: YYYY-MM-DD, or "
                            "XXXX-MM-DD when the memory gives no year. Absent: the memory's day."),
            "type": "string",
            "pattern": "^(\\d{4}|XXXX)-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$",
        },
        "granularity": {
            "description": ("An exact shift from the anchor instead of the calendar unit: day for a count "
                            "of weeks, months or years; month for a count of years."),
            "enum": list(GRANULARITIES),
        },
    },
    "allOf": [
        {
            "description": ("Exactly one thing is counted: a unit, a weekday (alone or with unit day or "
                            "week), a month_name or a season."),
            "oneOf": [
                {"required": ["unit"],
                 "not": {"anyOf": [{"required": ["weekday"]}, {"required": ["month_name"]},
                                   {"required": ["season"]}]}},
                {"required": ["weekday"],
                 "properties": {"unit": {"enum": ["day", "week"]}},
                 "not": {"anyOf": [{"required": ["month_name"]}, {"required": ["season"]}]}},
                {"required": ["month_name"],
                 "not": {"anyOf": [{"required": ["unit"]}, {"required": ["weekday"]},
                                   {"required": ["season"]}]}},
                {"required": ["season"],
                 "not": {"anyOf": [{"required": ["unit"]}, {"required": ["weekday"]},
                                   {"required": ["month_name"]}]}},
            ],
        },
        {
            "description": ("One day back or ahead of the memory's day is last or next with unit day: "
                            "before/after with unit day and no anchor count more than one day."),
            "if": {
                "required": ["direction", "unit"],
                "properties": {"direction": {"enum": ["before", "after"]}, "unit": {"const": "day"}},
                "not": {"anyOf": [{"required": ["anchor"]}, {"required": ["weekday"]}]},
            },
            "then": {"required": ["quantity"], "not": {"properties": {"quantity": {"const": 1}}}},
        },
        {
            "description": "this, last and next name one instance: no quantity.",
            "if": {"required": ["direction"], "properties": {"direction": {"enum": ["this", "last", "next"]}}},
            "then": {"not": {"required": ["quantity"]}},
        },
        {
            "description": "A part of the day belongs to a single day, never to a vague count.",
            "if": {"required": ["part_of_day"]},
            "then": {
                "anyOf": [{"required": ["weekday"]},
                          {"required": ["unit"], "properties": {"unit": {"const": "day"}}}],
                "not": {"required": ["quantity"], "properties": {"quantity": {"enum": sorted(VAGUE)}}},
            },
        },
        {
            "description": "granularity: before/after, a whole count of weeks, months or years; month only for years.",
            "if": {"required": ["granularity"]},
            "then": {
                "required": ["unit"],
                "properties": {
                    "direction": {"enum": ["before", "after"]},
                    "unit": {"enum": ["week", "month", "year"]},
                    "quantity": {"type": "integer"},
                },
                "not": {"required": ["weekday"]},
                "if": {"required": ["granularity"], "properties": {"granularity": {"const": "month"}}},
                "then": {"properties": {"unit": {"const": "year"}}},
            },
        },
    ],
    "examples": [
        {"text": "yesterday", "direction": "last", "unit": "day"},
        {"text": "last Saturday", "direction": "last", "weekday": "saturday"},
        {"text": "two weekends ago", "direction": "before", "quantity": 2, "unit": "weekend"},
        {"text": "la settimana scorsa", "direction": "last", "unit": "week"},
        {"text": "sabato della settimana prossima", "direction": "next", "unit": "week", "weekday": "saturday"},
        {"text": "il y a quelques jours", "direction": "before", "quantity": "few", "unit": "day"},
        {"text": "anoche", "direction": "last", "unit": "day", "part_of_day": "night"},
        {"text": "the week before 9 June", "direction": "last", "unit": "week", "anchor": "XXXX-06-09"},
        {"text": "a year ago today", "direction": "before", "quantity": 1, "unit": "year", "granularity": "day"},
        {"text": "nächsten Sommer", "direction": "next", "season": "summer"},
        {"text": "lo scorso marzo", "direction": "last", "month_name": "march"},
    ],
}


class InvalidExpression(ValueError):
    """An object that cannot be read or resolved; `reason` says why, in words
    a log reader (or the prompt's author) can act on."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Expression:
    """A relative time as the model described it, read and normalized: the
    names are the JSON fields, the values are canonical (lowercase names,
    `quantity` an int for before/after, None for this/last/next)."""

    text: str
    direction: str
    unit: Optional[str] = None
    quantity: Union[int, str, None] = None
    weekday: Optional[str] = None
    month_name: Optional[str] = None
    season: Optional[str] = None
    part_of_day: Optional[str] = None
    anchor: Optional[str] = None
    granularity: Optional[str] = None

    def as_json(self) -> Dict[str, Any]:
        """The object as it is stored ("expr"): enough to compute the value
        again from another day, without the model."""
        fields = (("text", self.text), ("direction", self.direction), ("unit", self.unit),
                 ("quantity", self.quantity), ("weekday", self.weekday),
                 ("month_name", self.month_name), ("season", self.season),
                 ("part_of_day", self.part_of_day), ("anchor", self.anchor),
                 ("granularity", self.granularity))
        return {k: v for k, v in fields if v is not None}


@dataclass(frozen=True)
class Value:
    """The value of a relative time, TIMEX3-style.

    `value`: the TIMEX3 value ("2023-05-07", "2023-08-13TNI", "2023-W22",
    "2023-W27-WE", "2023-05", "2022-SU", "2022", or PAST_REF / FUTURE_REF for
    a vague count); `start` and `end`: the first and the last day, both
    included; `granularity`: day, week, weekend, month, season or year;
    `label`: the short English rendering the reader sees; `type`: the TIMEX3
    type (TIME for a part of the day); `mod`: APPROX for a vague count."""

    value: str
    start: date
    end: date
    granularity: str
    label: str
    type: str = "DATE"
    mod: Optional[str] = None

    def as_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"value": self.value, "type": self.type,
                               "start": self.start.isoformat(), "end": self.end.isoformat(),
                               "granularity": self.granularity, "label": self.label}
        if self.mod:
            out["mod"] = self.mod
        return out


# --------------------------------------------------------------- reading

def read(item: Any) -> Expression:
    """One object of the model's `relative_times`, validated.

    Raises InvalidExpression with the reason when the object is not one
    the table above can resolve. Lenient only where no word is involved:
    case and spaces, null or "" for an absent field, a count written "2" or
    2.0. The enum values are read exactly (3.3.0: no plural units, no
    abbreviated names, no "fall" for autumn). Fields the schema does not name
    are ignored."""
    if not isinstance(item, Mapping):
        raise InvalidExpression("not an object")
    text = item.get("text")
    text = " ".join(text.split()) if isinstance(text, str) else ""
    if not text:
        raise InvalidExpression("text is missing: the words of the memory, copied exactly")
    if len(text) > MAX_TEXT:
        raise InvalidExpression(f"text is longer than {MAX_TEXT} characters: a time expression, not a sentence")
    direction = _word(item, "direction", DIRECTIONS, required=True)
    unit = _unit(item.get("unit"))
    weekday = _name(item, "weekday", WEEKDAYS)
    month_name = _name(item, "month_name", MONTHS)
    season = _season(item.get("season"))
    part_of_day = _word(item, "part_of_day", PARTS_OF_DAY)
    granularity = _word(item, "granularity", GRANULARITIES)
    anchor = _anchor(item.get("anchor"))
    quantity = _quantity(item.get("quantity"))

    names = [k for k, v in (("weekday", weekday), ("month_name", month_name), ("season", season)) if v]
    if len(names) > 1:
        raise InvalidExpression(f"give one of weekday, month_name, season, not {' and '.join(names)}")
    if weekday:
        if unit == "day":
            unit = None  # a day named Saturday is the weekday itself
        if unit not in (None, "week"):
            raise InvalidExpression(
                f"a weekday goes alone, or with unit week for that day of the chosen week, not with unit {unit}")
    elif month_name or season:
        if unit is not None:
            raise InvalidExpression(f"a named {names[0].replace('_name', '')} takes no unit")
    elif unit is None:
        raise InvalidExpression("say what is counted: a unit, a weekday, a month_name or a season")

    if direction in _ADJACENT_STEP:
        if quantity not in (None, 1):
            raise InvalidExpression(
                "quantity goes with before/after: 'last' and 'next' name the adjacent one and 'this' the "
                "current one (the week before last is before with quantity 2)")
        quantity = None
    elif quantity is None:
        quantity = 1
    vague = isinstance(quantity, str)
    # 3.3.0: the day next to the memory's day is last or next day, the one
    # shape the schema gives it; a count of one day from no stated date is
    # the shape a time with nothing to count took (module docstring, TIDES
    # 2005 §4.3.2 PAST_REF). Anchored ("the day after 11 March") it stays.
    if direction in ("before", "after") and quantity == 1 and unit == "day" and not anchor and not weekday:
        raise InvalidExpression(
            "one day back or ahead of the memory's day is 'last' or 'next' with unit day; a time with "
            "nothing to count (recently, since we talked) is left out")

    if part_of_day:
        if not (weekday or unit == "day"):
            raise InvalidExpression("part_of_day goes with a single day: unit day or a weekday")
        if vague:
            raise InvalidExpression("a vague count of days has no part of day")
    if granularity:
        if direction not in ("before", "after"):
            raise InvalidExpression("granularity goes with before/after: an exact count from the anchor")
        if vague:
            raise InvalidExpression("a vague count cannot be exact")
        if weekday or unit not in ("week", "month", "year"):
            raise InvalidExpression("granularity applies to a count of weeks, months or years")
        if granularity == "month" and unit != "year":
            raise InvalidExpression("granularity month applies to a count of years")

    return Expression(text=text, direction=direction, unit=unit, quantity=quantity, weekday=weekday,
                       month_name=month_name, season=season, part_of_day=part_of_day, anchor=anchor,
                       granularity=granularity)


def _absent(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _word(item: Mapping, field_name: str, allowed: Sequence[str], required: bool = False) -> Optional[str]:
    v = item.get(field_name)
    if _absent(v):
        if required:
            raise InvalidExpression(f"{field_name} is missing: one of {', '.join(allowed)}")
        return None
    s = v.strip().lower() if isinstance(v, str) else None
    if s not in allowed:
        raise InvalidExpression(f"{field_name} {v!r} is not one of {', '.join(allowed)}")
    return s


# 3.3.0 (the rule of 8 Oct, no word lists in the code): the enum values are
# read exactly, as the prompt and the schema name them. Until then the readers
# repaired English forms ("weeks" for "week", "sat" or "sept" for the whole
# name, "fall" for "autumn"): English words handled by code. An answer outside
# the enum is refused like any other answer outside the schema
# (`InvalidExpression`): that time is not dated.


def _unit(v: Any) -> Optional[str]:
    if _absent(v):
        return None
    s = v.strip().lower() if isinstance(v, str) else None
    if s not in UNITS:
        raise InvalidExpression(f"unit {v!r} is not one of {', '.join(UNITS)}")
    return s


def _name(item: Mapping, field_name: str, names: Sequence[str]) -> Optional[str]:
    """A day or month name, one of the enum's (3.3.0: whole, as the schema
    names it; no abbreviations)."""
    return _word(item, field_name, names)


def _season(v: Any) -> Optional[str]:
    if _absent(v):
        return None
    s = v.strip().lower() if isinstance(v, str) else None
    if s not in SEASONS:
        raise InvalidExpression(f"season {v!r} is not one of {', '.join(SEASONS)}")
    return s


def _quantity(v: Any) -> Union[int, str, None]:
    if _absent(v):
        return None
    n: Optional[int] = None
    if isinstance(v, bool):
        n = None
    elif isinstance(v, int):
        n = v
    elif isinstance(v, float) and v.is_integer():
        n = int(v)
    elif isinstance(v, str):
        s = v.strip().lower()
        if s in VAGUE:
            return s
        if s.isascii() and s.isdigit():
            n = int(s)
    if n is None:
        raise InvalidExpression(f"quantity {v!r} is not a whole number, 'couple' or 'few'")
    if n < 1:
        raise InvalidExpression("quantity must be 1 or more: the current one is direction 'this'")
    if n > MAX_QUANTITY:
        raise InvalidExpression(f"quantity {n} is more than {MAX_QUANTITY}")
    return n


_ANCHOR = re.compile(r"^(\d{4}|XXXX)-(\d{2})-(\d{2})$")


def _anchor(v: Any) -> Optional[str]:
    if _absent(v):
        return None
    m = _ANCHOR.match(v.strip().upper()) if isinstance(v, str) else None
    if not m:
        raise InvalidExpression(f"anchor {v!r} is not YYYY-MM-DD, or XXXX-MM-DD when the memory gives no year")
    year, month, day = m.groups()
    try:
        # 2000 is a leap year: XXXX-02-29 is a real day, in some year.
        date(2000 if year == "XXXX" else int(year), int(month), int(day))
    except ValueError:
        raise InvalidExpression(f"anchor {v!r} is not a day of the calendar") from None
    return f"{year}-{month}-{day}"


# ------------------------------------------------------------- resolving

def resolve(expression: Expression, day: date) -> Value:
    """The value of `expression` said on `day`, the memory's day.

    `day` is a `datetime.date`: the UTC day of the memory's event_time,
    else of its created_at (the day `presentation._day` prints in front of
    the memory). A datetime is refused rather than cut: which day it is
    depends on a time zone this module cannot choose. Deterministic; raises
    InvalidExpression when the result falls outside the years 1 to 9999."""
    if isinstance(day, datetime) or not isinstance(day, date):
        raise TypeError("day is the memory's day as a datetime.date (the UTC day of event_time, else created_at)")
    try:
        keep_grounded = _anchor_day(expression.anchor, day)
        if isinstance(expression.quantity, str):
            return _vague(expression, keep_grounded)
        return _instance(expression, keep_grounded, _step(expression))
    except InvalidExpression:
        raise
    except (OverflowError, ValueError):
        raise InvalidExpression("the date falls outside the years 1 to 9999") from None


def _step(e: Expression) -> int:
    """How many units from the anchor's own: this 0, last -1, next +1,
    before -n, after +n."""
    if e.direction in _ADJACENT_STEP:
        return _ADJACENT_STEP[e.direction]
    n = int(e.quantity or 1)
    return -n if e.direction == "before" else n


def _anchor_day(anchor: Optional[str], day: date) -> date:
    """Row 1 and row 18: the memory's day, the anchor's date, or, for
    XXXX-MM-DD, its occurrence nearest the memory's day (ties: the earlier).
    Four years either side, so that XXXX-02-29 finds a leap year."""
    if anchor is None:
        return day
    year, month, gg = anchor.split("-")
    if year != "XXXX":
        return date(int(year), int(month), int(gg))
    candidates = []
    for a in range(day.year - 4, day.year + 5):
        try:
            candidates.append(date(a, int(month), int(gg)))
        except ValueError:
            continue
    if not candidates:
        raise InvalidExpression(f"anchor {anchor} has no year near {day.isoformat()}")
    return min(candidates, key=lambda c: (abs((c - day).days), c))


def _instance(e: Expression, keep_grounded: date, step: int) -> Value:
    """The instance `step` units from the anchor's own (rows 2 to 14, 16, 17)."""
    if e.weekday is not None:
        index = WEEKDAYS.index(e.weekday)
        if e.unit == "week":
            day = _monday(keep_grounded) + timedelta(weeks=step, days=index)
        else:
            day = _day_of_the_week(keep_grounded, index, step)
        return _day_value(day, e.part_of_day)
    if e.month_name is not None:
        return _month_value(*_named_month(keep_grounded, MONTHS.index(e.month_name) + 1, step))
    if e.season is not None:
        return _season_value(_named_season(keep_grounded, e.season, step), e.season)
    if e.unit == "day":
        return _day_value(keep_grounded + timedelta(days=step), e.part_of_day)
    if e.granularity == "day":
        return _day_value(_shift_exactly(keep_grounded, e.unit, step), None)
    if e.granularity == "month":
        return _month_value(keep_grounded.year + step, keep_grounded.month)
    if e.unit == "week":
        return _week_value(_monday(keep_grounded) + timedelta(weeks=step))
    if e.unit == "weekend":
        return _weekend_value(_monday(keep_grounded) + timedelta(weeks=step))
    if e.unit == "month":
        return _month_value(*_add_months(keep_grounded.year, keep_grounded.month, step))
    return _year_value(keep_grounded.year + step)


def _vague(e: Expression, keep_grounded: date) -> Value:
    """Row 15: "a few days ago" is not a day. TIDES 4.3.2 gives PAST_REF
    anchored on the unit; the period is the instances from the farthest to
    the nearest of VAGUE, counted like "n days ago"."""
    near, far = VAGUE[str(e.quantity)]
    mark = -1 if e.direction == "before" else 1
    first = _instance(e, keep_grounded, mark * (far if mark < 0 else near))
    last = _instance(e, keep_grounded, mark * (near if mark < 0 else far))
    phrase = ("a couple of " if e.quantity == "couple" else "a few ") + _plural(e)
    toward = "before" if mark < 0 else "after"
    tag = (f"{phrase} {toward} {keep_grounded.isoformat()} "
                 f"({_endpoint(first, start=True)} to {_endpoint(last, start=False)})")
    return Value(value="PAST_REF" if mark < 0 else "FUTURE_REF", start=first.start, end=last.end,
                  granularity=first.granularity, label=tag, mod="APPROX")


def _plural(e: Expression) -> str:
    if e.weekday:
        return e.weekday.capitalize() + "s"
    if e.month_name:
        name = e.month_name.capitalize()
        return name + ("es" if name.endswith("ch") else "s")
    if e.season:
        return e.season + "s"
    return str(e.unit) + "s"


def _endpoint(v: Value, start: bool) -> str:
    """One end of a vague period, at its granularity: a day for days and
    weeks, "November 2022" for months, the label for seasons and years."""
    if v.granularity in ("day", "week", "weekend"):
        return (v.start if start else v.end).isoformat()
    return v.label


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _day_of_the_week(keep_grounded: date, index: int, step: int) -> date:
    """Rows 5 to 7. this: that day of the anchor's ISO week. last and
    before: the nearest such day strictly before the anchor day, then whole
    weeks back (SCATE LAST: te <= t1). next and after: the nearest strictly
    after, then whole weeks ahead (SCATE NEXT: t2 <= ts)."""
    if step == 0:
        return _monday(keep_grounded) + timedelta(days=index)
    if step < 0:
        first = keep_grounded - timedelta(days=(keep_grounded.weekday() - index) % 7 or 7)
        return first - timedelta(weeks=-step - 1)
    first = keep_grounded + timedelta(days=(index - keep_grounded.weekday()) % 7 or 7)
    return first + timedelta(weeks=step - 1)


def _add_months(year: int, month: int, n: int) -> Tuple[int, int]:
    a, m = divmod(year * 12 + (month - 1) + n, 12)
    return a, m + 1


def _last_of_the_month(year: int, month: int) -> date:
    a, m = _add_months(year, month, 1)
    return date(a, m, 1) - timedelta(days=1)


def _shift_exactly(d: date, unit: Optional[str], n: int) -> date:
    """Row 14: the anchor moved by exactly n weeks, months or years; a day the
    target month lacks becomes its last day (java.time, PostgreSQL)."""
    if unit == "week":
        return d + timedelta(weeks=n)
    months = n if unit == "month" else 12 * n
    a, m = _add_months(d.year, d.month, months)
    return date(a, m, min(d.day, _last_of_the_month(a, m).day))


def _named_month(keep_grounded: date, month: int, step: int) -> Tuple[int, int]:
    """Row 17: this, the month of the anchor's year (SCATE This); last, the
    latest such month that ends before the anchor; next, the earliest that
    starts after it; a count moves by whole years."""
    if step == 0:
        return keep_grounded.year, month
    if step < 0:
        year = keep_grounded.year if month < keep_grounded.month else keep_grounded.year - 1
        return year + step + 1, month
    year = keep_grounded.year if month > keep_grounded.month else keep_grounded.year + 1
    return year + step - 1, month


def _season_span(year: int, season: str) -> Tuple[date, date]:
    """First and last day of a season, by the year TIDES names it with
    (winter 2024 runs from December 2023 to February 2024)."""
    if season == "winter":
        return date(year - 1, 12, 1), date(year, 3, 1) - timedelta(days=1)
    first, _, last = _SEASON_MONTHS[season]
    return date(year, first, 1), _last_of_the_month(year, last)


def _named_season(keep_grounded: date, season: str, step: int) -> int:
    """Row 16, the year of the season meant. last: the latest that ended
    before the anchor day (SCATE LAST), then whole years back; next: the
    earliest that starts after it (SCATE NEXT); this: the one the day is in,
    else the nearest by days, ties to the later (winter crosses the new year,
    so "the season of the anchor's year" is not defined for it)."""
    if step < 0:
        year = keep_grounded.year + 1
        while _season_span(year, season)[1] >= keep_grounded:
            year -= 1
        return year + step + 1
    if step > 0:
        year = keep_grounded.year - 1
        while _season_span(year, season)[0] <= keep_grounded:
            year += 1
        return year + step - 1

    def distance(year: int) -> int:
        first, last = _season_span(year, season)
        if first <= keep_grounded <= last:
            return 0
        return (first - keep_grounded).days if first > keep_grounded else (keep_grounded - last).days

    return min(range(keep_grounded.year - 1, keep_grounded.year + 3), key=lambda a: (distance(a), -a))


def _day_value(day: date, part: Optional[str]) -> Value:
    if part:
        return Value(value=f"{day.isoformat()}T{_PART_TOKEN[part]}", start=day, end=day,
                      granularity="day", label=f"the {part} of {day.isoformat()}", type="TIME")
    return Value(value=day.isoformat(), start=day, end=day, granularity="day",
                  label=day.isoformat())


def _iso_week(monday: date) -> str:
    year, week = monday.isocalendar()[:2]
    return f"{year:04d}-W{week:02d}"


def _week_value(monday: date) -> Value:
    sunday = monday + timedelta(days=6)
    return Value(value=_iso_week(monday), start=monday, end=sunday, granularity="week",
                  label=f"the week of {monday.isoformat()} to {sunday.isoformat()}")


def _weekend_value(monday: date) -> Value:
    saturday, sunday = monday + timedelta(days=5), monday + timedelta(days=6)
    return Value(value=_iso_week(monday) + "-WE", start=saturday, end=sunday, granularity="weekend",
                  label=f"the weekend of {saturday.isoformat()} to {sunday.isoformat()}")


def _month_value(year: int, month: int) -> Value:
    return Value(value=f"{year:04d}-{month:02d}", start=date(year, month, 1), end=_last_of_the_month(year, month),
                  granularity="month", label=f"{_MONTH_LABELS[month - 1]} {year}")


def _season_value(year: int, season: str) -> Value:
    first, last = _season_span(year, season)
    tag = f"winter {year - 1}/{year % 100:02d}" if season == "winter" else f"{season} {year}"
    return Value(value=f"{year:04d}-{_SEASON_TOKEN[season]}", start=first, end=last,
                  granularity="season", label=tag)


def _year_value(year: int) -> Value:
    return Value(value=f"{year:04d}", start=date(year, 1, 1), end=date(year, 12, 31), granularity="year",
                  label=f"{year}")


# ------------------------------------------------- the memory's annotation

def annotate(entries: Any, day: Union[date, str], text: Optional[str] = None
           ) -> Tuple[Dict[str, Any], List[Tuple[Any, str]]]:
    """The model's `relative_times` for one memory, read and dated.

    Returns the value of the memory's JSON column,

        {"v": 1, "anchor": "2023-06-09",
         "times": [{"text": "last week", "value": "2023-W22", "type": "DATE",
                    "start": "2023-05-29", "end": "2023-06-04",
                    "granularity": "week",
                    "label": "the week of 2023-05-29 to 2023-06-04",
                    "expr": {"text": "last week", "direction": "last",
                             "unit": "week"}}]}

    and the objects left out, each with its reason (the caller logs them;
    this module writes nothing). `day`: the memory's day, a date or
    "YYYY-MM-DD". `text`: the memory's text; when given, an object whose
    words are not in it is left out, as `extraction.read_evidence` does with
    the evidence (a paraphrase is the model's reading, not what was said).
    The same words twice keep the first; at most MAX_TIMES per memory.
    `entries` None gives an empty list (the extractor itself tells a reply
    without the field from an empty one: there the column stays None).

    To date the same times from another day (the memory's timestamp was
    edited, or CONVENTIONS_VERSION moved): annotate([t["expr"] for t in
    column["times"]], new_day), what `times_of_the_day` does."""
    day = _day(day)
    times: List[Dict[str, Any]] = []
    dropped: List[Tuple[Any, str]] = []
    if entries is None:
        entries = []
    elif not isinstance(entries, (list, tuple)):
        dropped.append((entries, "relative_times is not a list"))
        entries = []
    source = comparable(text) if text is not None else None
    seen = set()
    for entry in entries:
        try:
            e = read(entry)
            key = comparable(e.text)
            if not key:
                raise InvalidExpression("text has no words")
            if source is not None and not _contains(source, key):
                raise InvalidExpression("the words are not in the memory")
            if key in seen:
                raise InvalidExpression("the same words twice")
            if len(times) >= MAX_TIMES:
                raise InvalidExpression(f"more than {MAX_TIMES} relative times in one memory")
            value = resolve(e, day)
        except InvalidExpression as exc:
            dropped.append((entry, exc.reason))
            continue
        seen.add(key)
        times.append({"text": e.text, **value.as_json(), "expr": e.as_json()})
    return {"v": CONVENTIONS_VERSION, "anchor": day.isoformat(), "times": times}, dropped


def _day(g: Any) -> date:
    if isinstance(g, date) and not isinstance(g, datetime):
        return g
    if isinstance(g, str):
        try:
            return date.fromisoformat(g.strip())
        except ValueError:
            pass
    raise TypeError("the memory's day: a datetime.date or 'YYYY-MM-DD'")


def times_of_the_day(column: Any, day: Union[date, str, None] = None) -> List[Dict[str, Any]]:
    """The times of a memory's stored column (`annotate`'s value), dated from
    `day`, the day the reader sees in front of the memory.

    As stored when the column was computed from that day with these
    conventions. Computed again from each time's "expr", with no model call,
    when the day moved (the memory's timestamp was edited after its write)
    or CONVENTIONS_VERSION did: the date next to "yesterday" then always
    agrees with the day printed in front of the line. `day` None: the
    column's own anchor. [] for a column that is absent (written before
    2.9.0, or with the switch off), empty, or not one this module wrote."""
    if not isinstance(column, Mapping):
        return []
    times = column.get("times")
    if not isinstance(times, list) or not times:
        return []
    try:
        g = _day(day if day else column.get("anchor"))
    except TypeError:
        return []
    if column.get("v") == CONVENTIONS_VERSION and column.get("anchor") == g.isoformat():
        return [t for t in times if isinstance(t, Mapping)]
    again, _dropped = annotate([t.get("expr") for t in times if isinstance(t, Mapping)], g)
    return again["times"]


def _wide(c: str) -> bool:
    """A character of a script written without spaces between words (CJK)."""
    return unicodedata.east_asian_width(c) in ("W", "F")


def _contains(source: str, key: str) -> bool:
    """Whole words in a spaced script; a plain substring where words are not
    spaced, so "昨天" is found in "我昨天去了"."""
    if f" {key} " in f" {source} ":
        return True
    return any(_wide(c) for c in key) and key in source


# ------------------------------------------------------ dates in the text

def in_the_text(text: str, times: Sequence[Mapping[str, Any]]) -> Tuple[str, List[Mapping[str, Any]]]:
    """`text` with each relative time followed by its label, "yesterday
    [2023-05-07]": the first occurrence of its words, matched as whole words
    and in any case; the longer expressions first, never inside one already
    marked; a time already followed by its label is left as it is. The words
    of the text are never replaced, only followed. Returns the text and the
    times whose words it does not contain, in their order."""
    taken: List[Tuple[int, int]] = []
    insertions: List[Tuple[int, str]] = []
    found = set()
    for i, t in sorted(enumerate(times), key=lambda it: -len(str(it[1].get("text") or ""))):
        words = " ".join(str(t.get("text") or "").split())
        tag = str(t.get("label") or "").strip()
        if not words or not tag:
            continue
        for start, end in _occurrences(text, words):
            if any(start < f and i0 < end for i0, f in taken):
                continue
            taken.append((start, end))
            found.add(i)
            mark = f" [{tag}]"
            if not text.startswith(mark, end):
                insertions.append((end, mark))
            break
    for position, mark in sorted(insertions, reverse=True):
        text = text[:position] + mark + text[position:]
    return text, [t for i, t in enumerate(times) if i not in found]


def with_dates(text: str, times: Sequence[Mapping[str, Any]]) -> str:
    """The line the reader sees: `in_the_text`, and the times whose words are
    not in the text (an observation written in another language than the
    memory) once at the end, "(ieri: 2023-05-07)"."""
    marked, missing = in_the_text(text, times)
    queue = "; ".join(f"{' '.join(str(t.get('text')).split())}: {str(t.get('label')).strip()}"
                     for t in missing if t.get("text") and t.get("label"))
    return f"{marked} ({queue})" if queue else marked


def _occurrences(text: str, words: str) -> Iterator[Tuple[int, int]]:
    """Where `words` occurs in `text`: spaces between its words may be any
    whitespace; an edge that is a letter or a digit of a spaced script must
    not touch another one ("last week" is not in "last weekend")."""
    body = r"\s+".join(re.escape(p) for p in words.split())
    before = r"(?<!\w)" if _needs_boundary(words[0]) else ""
    after = r"(?!\w)" if _needs_boundary(words[-1]) else ""
    for m in re.finditer(before + body + after, text, flags=re.IGNORECASE | re.UNICODE):
        yield m.start(), m.end()


def _needs_boundary(c: str) -> bool:
    return c.isalnum() and not _wide(c)
