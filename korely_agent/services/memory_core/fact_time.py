"""Time of a fact: its tense, its date, and how much its age weighs (memory_core).

Three questions, one module:

1. TENSE — was the fact stated as true now, as over, or as a plan?
   Graphiti (Rasmussen 2025, arXiv:2501.13956 §2.2.3) has the model read the
   time of a fact from the episode context. The minimal version: the model
   says current | past | planned (RC7 of the Layer 3 analysis: a note
   describing the June stack in the past tense produced three facts that
   looked present). The code does not second-guess it with a list of past
   verb forms: that list covered one language and thirty verbs, and it is the
   morphological baseline CESI (Vashishth 2018, §8.1.1) shows failing.

2. DATE (BUG-112) — when did the fact become true? Only when the TEXT says so,
   and only if the year the model returns is written in the text: a model
   asked for a date will invent one, and an invented date is worse than none
   because `as_of` would then answer with confidence.

3. AGE — how much is an old fact worth when ranking? Exponential decay with a
   floor, softened by how many times notes restated it (MemoryBank, Zhong 2024,
   arXiv:2305.10250: a memory strengthens every time it is recalled; Generative
   Agents, Park 2023 §4.1: recency is one score among relevance and importance,
   nothing is deleted).
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

TENSES = ("current", "past", "planned")

# Below this weight a fact never sinks: an old but pertinent fact must stay
# findable (Mem0 production floor, the same value the note decay uses).
FLOOR = 0.3


def effective_tense(emitted: Optional[str]) -> str:
    """The tense a fact is stored with: the model's answer, or "current" when
    the answer is missing or not one of the three (the recoverable direction:
    a past fact read as present is weighed down by recency, a present fact
    closed by mistake is not seen again)."""
    t = (emitted or "").strip().lower()
    return t if t in TENSES else "current"


def recency_weight(
    basis: Optional[datetime],
    observations: Optional[int],
    lambda_: float,
    now: datetime,
) -> float:
    """Recency weight of a fact.

        weight = max(0.3, exp(-lambda_eff * days_since_last_confirmation))
        lambda_eff = lambda / (1 + ln(observations))

    A fact stated by one note in June sinks; the same fact restated by ten
    notes sinks three times slower. `lambda_ <= 0` (historical or factual
    questions) returns 1.0: nothing is decayed when the question is about the
    past.
    """
    if lambda_ <= 0.0 or basis is None:
        return 1.0
    if basis.tzinfo is None:
        basis = basis.replace(tzinfo=timezone.utc)
    days = max(0.0, (now - basis).total_seconds() / 86400.0)
    n = max(1, int(observations or 1))
    lambda_eff = lambda_ / (1.0 + math.log(n))
    return max(FLOOR, math.exp(-lambda_eff * days))


_DATE = re.compile(r"^\s*(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?\s*$")


def date_from_text(value: Optional[str], text: str, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """The date a fact became true, as the model read it from the text, or None.

    Accepted: "YYYY", "YYYY-MM", "YYYY-MM-DD" (start of the period, UTC).
    Refused: anything else; a year that is not written in the text as four
    digits (the model inventing a date is the failure this guards against);
    a date more than one day in the future (a future date is a plan, not the
    start of a validity); a year before 1900.
    """
    if not value or not isinstance(value, str):
        return None
    m = _DATE.match(value)
    if not m:
        return None
    year = int(m.group(1))
    if year < 1900:
        return None
    if not re.search(rf"(?<!\d){year}(?!\d)", text or ""):
        return None
    month = int(m.group(2) or 1)
    day = int(m.group(3) or 1)
    try:
        dt = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None
    current_time = now or datetime.now(timezone.utc)
    if dt > current_time + timedelta(days=1):
        return None
    return dt
