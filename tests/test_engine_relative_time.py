"""memory_core 2.9.0: relative times dated by code (`relative_time`).

The model describes a relative time, the code dates it from the memory's
day. Moved from the tests of the design prototype (korely-agent f673ab3):
the four cases of the LoCoMo measurement of 5 October, the gold answers of LoCoMo for
the same kinds of expression, every row of the conventions table in the
module's docstring, sweeps over three years of days for the rules that must
hold on any day, the objects the reader refuses, the memory's annotation, the
dates written into a text, and the agreement with the schema and with the
prompt's examples (both in the module now). Added here: `times_of_the_day`,
the stored column read back from the day the block prints.
"""
import ast
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from korely_agent.services.memory_core import relative_time as tr
from korely_agent.services.memory_core.relative_time import (
    InvalidExpression,
    annotate,
    with_dates,
    read,
    in_the_text,
    resolve,
    times_of_the_day,
)


def obj(text, direction, **fields):
    return {"text": text, "direction": direction, **fields}


def value_of(o, day):
    return resolve(read(o), date.fromisoformat(day))


def days_from(since, until):
    d = date.fromisoformat(since)
    while d <= date.fromisoformat(until):
        yield d
        d += timedelta(days=1)


# Every day of 2023, 2024 (a leap year) and 2025: weeks 52 and 53, every
# weekday at every month end.
THREE_YEARS = list(days_from("2023-01-01", "2025-12-31"))


# ---------------------------------------------------------------- LoCoMo

def test_the_four_cases_of_the_measurement():
    """The LoCoMo measurement of 5 Oct 2026 (conversation 26): the reader
    counted these from today; the code counts them from the memory's
    day."""
    v = value_of(obj("yesterday", "last", unit="day"), "2023-05-08")
    assert (v.value, v.start, v.end, v.label) == ("2023-05-07", date(2023, 5, 7), date(2023, 5, 7), "2023-05-07")

    v = value_of(obj("last Saturday", "last", weekday="saturday"), "2023-05-25")
    assert (v.value, v.label) == ("2023-05-20", "2023-05-20")

    v = value_of(obj("last week", "last", unit="week"), "2023-06-09")
    assert (v.value, v.start, v.end) == ("2023-W22", date(2023, 5, 29), date(2023, 6, 4))
    assert v.label == "the week of 2023-05-29 to 2023-06-04"

    v = value_of(obj("two weekends ago", "before", quantity=2, unit="weekend"), "2023-07-17")
    assert (v.value, v.start, v.end) == ("2023-W27-WE", date(2023, 7, 8), date(2023, 7, 9))
    assert v.label == "the weekend of 2023-07-08 to 2023-07-09"


# LoCoMo (locomo10.json, CC BY-NC): only the dialogue id, the day, the words
# and what the gold answer means are written here. conv-26 D2:1's gold says
# "The sunday before 25 May 2023" for "last Saturday", a slip of the
# dataset: the text says Saturday.
LOCOMO = [
    ("conv-26 D1:3", "2023-05-08", obj("yesterday", "last", unit="day"), "2023-05-07"),
    ("conv-26 D2:1", "2023-05-25", obj("last Saturday", "last", weekday="saturday"), "2023-05-20"),
    ("conv-26 D2:7", "2023-05-25", obj("next month", "next", unit="month"), "2023-06"),
    ("conv-26 D3:1", "2023-06-09", obj("last week", "last", unit="week"), "2023-W22"),
    ("conv-26 D4:5", "2023-06-27", obj("ten years ago", "before", quantity=10, unit="year"), "2013"),
    ("conv-26 D5:1", "2023-07-03", obj("Last week", "last", unit="week"), "2023-W26"),
    ("conv-26 D5:4", "2023-07-03", obj("yesterday", "last", unit="day"), "2023-07-02"),
    ("conv-26 D5:13", "2023-07-03", obj("this month", "this", unit="month"), "2023-07"),
    ("conv-26 D6:4", "2023-07-06", obj("Yesterday", "last", unit="day"), "2023-07-05"),
    ("conv-26 D7:1", "2023-07-12", obj("two days ago", "before", quantity=2, unit="day"), "2023-07-10"),
    ("conv-26 D7:8", "2023-07-12", obj("last year", "last", unit="year"), "2022"),
    ("conv-26 D8:2", "2023-07-15", obj("Last Fri", "last", weekday="friday"), "2023-07-14"),
    ("conv-26 D9:1", "2023-07-17", obj("two weekends ago", "before", quantity=2, unit="weekend"), "2023-W27-WE"),
    ("conv-26 D9:2", "2023-07-17", obj("Last weekend", "last", unit="weekend"), "2023-W28-WE"),
    ("conv-26 D10:3", "2023-07-20", obj("last Tues", "last", weekday="tuesday"), "2023-07-18"),
    ("conv-26 D11:1", "2023-08-14", obj("Last night", "last", unit="day", part_of_day="night"), "2023-08-13TNI"),
    ("conv-26 D11:4", "2023-08-14", obj("last Friday", "last", weekday="friday"), "2023-08-11"),
    ("conv-26 D13:1", "2023-08-23", obj("this week", "this", unit="week"), "2023-W34"),
    ("conv-26 D13:11", "2023-08-23", obj("last week", "last", unit="week"), "2023-W33"),
    ("conv-26 D14:1", "2023-08-25", obj("last week", "last", unit="week"), "2023-W33"),
    ("conv-26 D14:4", "2023-08-25", obj("yesterday", "last", unit="day"), "2023-08-24"),
    ("conv-26 D15:11", "2023-08-28", obj("next month", "next", unit="month"), "2023-09"),
    ("conv-26 D16:1", "2023-09-13", obj("last weekend", "last", unit="weekend"), "2023-W36-WE"),
    ("conv-26 D17:8", "2023-10-13", obj("Last month", "last", unit="month"), "2023-09"),
    ("conv-26 D18:1", "2023-10-20", obj("this past weekend", "last", unit="weekend"), "2023-W41-WE"),
    ("conv-26 D19:1", "2023-10-22", obj("last Friday", "last", weekday="friday"), "2023-10-20"),
    ("conv-26 D19:2", "2023-10-22", obj("yesterday", "last", unit="day"), "2023-10-21"),
    # The other nine conversations: the readings that decide the table.
    ("next Saturday said on a Friday", "2023-01-27", obj("next Saturday", "next", weekday="saturday"), "2023-01-28"),
    ("next Sunday said on a Sunday", "2022-09-04", obj("next Sunday", "next", weekday="sunday"), "2022-09-11"),
    ("last Sunday said on a Tuesday", "2023-06-13", obj("last Sunday", "last", weekday="sunday"), "2023-06-11"),
    ("last Friday said on a Sunday", "2023-07-23", obj("last Friday", "last", weekday="friday"), "2023-07-21"),
    ("this weekend said on a Monday", "2022-08-22", obj("this weekend", "this", unit="weekend"), "2022-W34-WE"),
    ("this weekend said on a Friday", "2022-06-24", obj("this weekend", "this", unit="weekend"), "2022-W25-WE"),
    ("last summer said in August", "2023-08-11", obj("last summer", "last", season="summer"), "2022-SU"),
    ("last summer said in December", "2023-12-26", obj("last summer", "last", season="summer"), "2023-SU"),
    ("next summer said in December", "2023-12-09", obj("next summer", "next", season="summer"), "2024-SU"),
    ("the day before yesterday", "2023-06-26", obj("day before yesterday", "before", quantity=2, unit="day"),
     "2023-06-24"),
    ("three days ago", "2022-04-29", obj("three days ago", "before", quantity=3, unit="day"), "2022-04-26"),
    ("last month said on 2 January", "2024-01-02", obj("last month", "last", unit="month"), "2023-12"),
    ("tomorrow", "2023-06-19", obj("tomorrow", "next", unit="day"), "2023-06-20"),
    ("three years ago", "2023-08-30", obj("three years ago", "before", quantity=3, unit="year"), "2020"),
]


@pytest.mark.parametrize("where, day, o, expected", LOCOMO, ids=[r[0] for r in LOCOMO])
def test_the_gold_answers_of_locomo(where, day, o, expected):
    assert value_of(o, day).value == expected


# ------------------------------------------------- the conventions table

def test_row_1_the_anchor_is_the_memory_day_or_the_stated_date():
    assert value_of(obj("yesterday", "last", unit="day"), "1999-07-15").value == "1999-07-14"
    assert value_of(obj("yesterday", "last", unit="day"), "2042-01-01").value == "2041-12-31"
    o = obj("two days before 12 March 2023", "before", quantity=2, unit="day", anchor="2023-03-12")
    assert value_of(o, "2024-08-01").value == "2023-03-10"


def test_row_2_days():
    assert value_of(obj("today", "this", unit="day"), "2023-05-08").value == "2023-05-08"
    assert value_of(obj("tomorrow", "next", unit="day"), "2023-12-31").value == "2024-01-01"
    assert value_of(obj("yesterday", "last", unit="day"), "2024-03-01").value == "2024-02-29"
    assert value_of(obj("yesterday", "last", unit="day"), "2023-03-01").value == "2023-02-28"
    assert value_of(obj("in three days", "after", quantity=3, unit="day"), "2023-02-27").value == "2023-03-02"


def test_row_3_weeks_are_iso_weeks_across_the_year_end():
    v = value_of(obj("last week", "last", unit="week"), "2021-01-06")
    assert (v.value, v.start, v.end) == ("2020-W53", date(2020, 12, 28), date(2021, 1, 3))
    v = value_of(obj("next week", "next", unit="week"), "2024-12-27")
    assert (v.value, v.start, v.end) == ("2025-W01", date(2024, 12, 30), date(2025, 1, 5))
    assert value_of(obj("this week", "this", unit="week"), "2024-12-31").value == "2025-W01"
    assert value_of(obj("last week", "last", unit="week"), "2023-01-04").value == "2022-W52"
    # Monday to Sunday: said on the Sunday, "last week" is still the one before.
    assert value_of(obj("last week", "last", unit="week"), "2023-06-11").value == "2023-W22"
    assert value_of(obj("last week", "last", unit="week"), "2023-06-05").value == "2023-W22"


def test_row_4_the_week_before_a_date_is_last_week_said_that_day():
    said_on_9_june = value_of(obj("last week", "last", unit="week"), "2023-06-09")
    for o in (obj("the week before 9 June", "last", unit="week", anchor="XXXX-06-09"),
              obj("the week before 9 June", "before", unit="week", anchor="XXXX-06-09"),
              obj("the week before 9 June", "before", quantity=1, unit="week", anchor="2023-06-09")):
        assert value_of(o, "2023-06-20") == said_on_9_june


def test_row_5_last_weekday_said_on_that_weekday_and_on_the_day_after():
    o = obj("last Saturday", "last", weekday="saturday")
    assert value_of(o, "2023-05-20").value == "2023-05-13"  # a Saturday: a week back, never today
    assert value_of(o, "2023-05-21").value == "2023-05-20"  # a Sunday: the day before
    assert value_of(o, "2023-05-25").value == "2023-05-20"  # a Thursday
    # TimeML 1.2.1, 2.2.3c: "Last Friday" said on Friday 2002-07-12.
    assert value_of(obj("Last Friday", "last", weekday="friday"), "2002-07-12").value == "2002-07-05"
    # TReMu, table 4.
    assert value_of(o, "2020-01-28").value == "2020-01-25"


def test_row_6_next_weekday_is_the_nearest_after():
    o = obj("next Saturday", "next", weekday="saturday")
    assert value_of(o, "2023-05-25").value == "2023-05-27"  # a Thursday
    assert value_of(o, "2023-05-26").value == "2023-05-27"  # a Friday: the next day
    assert value_of(o, "2023-05-27").value == "2023-06-03"  # a Saturday: a week ahead
    # TIDES 2005, reference day Thursday 1999-07-15.
    assert value_of(obj("next Thursday", "next", weekday="thursday"), "1999-07-15").value == "1999-07-22"
    assert value_of(obj("next Tuesday", "next", weekday="tuesday"), "1999-07-15").value == "1999-07-20"


def test_row_7_this_weekday_is_that_day_of_the_anchor_week():
    o = obj("this Saturday", "this", weekday="saturday")
    assert value_of(o, "2023-05-25").value == "2023-05-27"
    assert value_of(o, "2023-05-28").value == "2023-05-27"  # a Sunday: already past
    assert value_of(obj("this Monday", "this", weekday="monday"), "2023-05-25").value == "2023-05-22"


def test_row_8_a_weekday_of_the_chosen_week():
    assert value_of(obj("Saturday next week", "next", unit="week", weekday="saturday"), "2023-05-25").value \
        == "2023-06-03"
    assert value_of(obj("Saturday of last week", "last", unit="week", weekday="saturday"), "2023-05-21").value \
        == "2023-05-13"
    assert value_of(obj("martedì di due settimane fa", "before", quantity=2, unit="week", weekday="tuesday"),
                  "2023-05-25").value == "2023-05-09"


def test_row_9_a_bare_weekday_takes_its_direction_from_the_model():
    # TIDES 2005 4.4 and 4.7: "on Friday" (future) and "on Tuesday" (past),
    # reference day Thursday 1999-07-15.
    assert value_of(obj("on Friday", "next", weekday="friday"), "1999-07-15").value == "1999-07-16"
    assert value_of(obj("on Tuesday", "last", weekday="tuesday"), "1999-07-15").value == "1999-07-13"


@pytest.mark.parametrize("day, this, last, next_", [
    ("2023-07-13", "2023-07-15", "2023-07-08", "2023-07-22"),  # Thursday
    ("2023-07-15", "2023-07-15", "2023-07-08", "2023-07-22"),  # Saturday
    ("2023-07-16", "2023-07-15", "2023-07-08", "2023-07-22"),  # Sunday
    ("2023-07-17", "2023-07-22", "2023-07-15", "2023-07-29"),  # Monday
])
def test_row_10_weekends_go_with_their_week(day, this, last, next_):
    for direction, saturday in (("this", this), ("last", last), ("next", next_)):
        v = value_of(obj("weekend", direction, unit="weekend"), day)
        assert (v.start.isoformat(), (v.end - v.start).days) == (saturday, 1)
        assert v.value == "%04d-W%02d-WE" % v.start.isocalendar()[:2]
    # TIDES 2005 4.3.6: "this weekend" said on Thursday 1999-07-15.
    assert value_of(obj("this weekend", "this", unit="weekend"), "1999-07-15").value == "1999-W28-WE"
    # Two weekends ago said on a Sunday: the current weekend does not count.
    assert value_of(obj("two weekends ago", "before", quantity=2, unit="weekend"), "2023-07-16").start \
        == date(2023, 7, 1)


def test_row_11_parts_of_the_day():
    v = value_of(obj("last night", "last", unit="day", part_of_day="night"), "2023-08-14")
    assert (v.value, v.type, v.label) == ("2023-08-13TNI", "TIME", "the night of 2023-08-13")
    assert value_of(obj("tonight", "this", unit="day", part_of_day="night"), "2023-08-14").value == "2023-08-14TNI"
    assert value_of(obj("this morning", "this", unit="day", part_of_day="morning"), "2023-08-14").value \
        == "2023-08-14TMO"
    assert value_of(obj("tomorrow evening", "next", unit="day", part_of_day="evening"), "2023-08-14").value \
        == "2023-08-15TEV"
    assert value_of(obj("Saturday afternoon", "last", weekday="saturday", part_of_day="afternoon"),
                  "2023-08-14").value == "2023-08-12TAF"
    # TIDES 2005 4.3.7, "Last Night is Yesterday": said just after midnight.
    assert value_of(obj("last night", "last", unit="day", part_of_day="night"), "2022-10-31").start \
        == date(2022, 10, 30)


def test_row_12_months_and_years_across_the_year_end():
    v = value_of(obj("next month", "next", unit="month"), "2023-12-15")
    assert (v.value, v.start, v.end, v.label) == ("2024-01", date(2024, 1, 1), date(2024, 1, 31), "January 2024")
    assert value_of(obj("last month", "last", unit="month"), "2024-01-31").value == "2023-12"
    assert value_of(obj("last month", "last", unit="month"), "2024-03-31").end == date(2024, 2, 29)
    assert value_of(obj("in three months", "after", quantity=3, unit="month"), "2023-11-30").value == "2024-02"
    v = value_of(obj("next year", "next", unit="year"), "2023-12-31")
    assert (v.value, v.start, v.end, v.label) == ("2024", date(2024, 1, 1), date(2024, 12, 31), "2024")


def test_row_13_a_count_names_the_calendar_unit_not_the_exact_day():
    # TIDES 2005 4.3.1: "a year ago" is 1998, "six weeks ago" a week value.
    assert value_of(obj("a year ago", "before", quantity=1, unit="year"), "1999-07-15").value == "1998"
    assert value_of(obj("six weeks ago", "before", quantity=6, unit="week"), "1999-07-15").value == "1999-W22"
    v = value_of(obj("two weeks ago", "before", quantity=2, unit="week"), "2023-08-11")
    assert (v.value, v.start, v.end) == ("2023-W30", date(2023, 7, 24), date(2023, 7, 30))
    assert value_of(obj("three months ago", "before", quantity=3, unit="month"), "2023-05-31").value == "2023-02"
    assert value_of(obj("in a year", "after", quantity=1, unit="year"), "2024-02-29").value == "2025"


def test_row_14_granularity_day_is_the_exact_shift_and_month_ends_are_clamped():
    # TIDES 2005 4.2.1: "two weeks ago today", reference day 1999-07-15.
    o = obj("two weeks ago today", "before", quantity=2, unit="week", granularity="day")
    assert value_of(o, "1999-07-15").value == "1999-07-01"
    o = obj("a year ago today", "before", quantity=1, unit="year", granularity="day")
    assert value_of(o, "2024-02-29").value == "2023-02-28"
    o = obj("a month before 31 March", "before", quantity=1, unit="month", granularity="day", anchor="XXXX-03-31")
    assert value_of(o, "2024-04-02").value == "2024-02-29"
    assert value_of(o, "2023-04-02").value == "2023-02-28"
    o = obj("a week after 3 June", "after", quantity=1, unit="week", granularity="day", anchor="XXXX-06-03")
    assert value_of(o, "2023-06-01").value == "2023-06-10"
    # The month of a count of years: LoCoMo's "Around August 2022" for "a year ago".
    o = obj("a year ago", "before", quantity=1, unit="year", granularity="month")
    assert value_of(o, "2023-08-03").value == "2022-08"


def test_row_15_vague_counts_are_periods_not_guesses():
    v = value_of(obj("a few days ago", "before", quantity="few", unit="day"), "2023-05-08")
    assert (v.value, v.mod, v.start, v.end) == ("PAST_REF", "APPROX", date(2023, 5, 2), date(2023, 5, 6))
    assert v.label == "a few days before 2023-05-08 (2023-05-02 to 2023-05-06)"
    v = value_of(obj("a couple of days ago", "before", quantity="couple", unit="day"), "2023-05-08")
    assert (v.start, v.end) == (date(2023, 5, 5), date(2023, 5, 6))
    v = value_of(obj("in a few days", "after", quantity="few", unit="day"), "2023-05-08")
    assert (v.value, v.start, v.end) == ("FUTURE_REF", date(2023, 5, 10), date(2023, 5, 14))
    v = value_of(obj("a few weeks ago", "before", quantity="few", unit="week"), "2023-05-08")
    assert (v.start, v.end, v.granularity) == (date(2023, 3, 27), date(2023, 4, 30), "week")
    v = value_of(obj("a few months ago", "before", quantity="few", unit="month"), "2023-05-08")
    assert (v.start, v.end) == (date(2022, 11, 1), date(2023, 3, 31))
    assert v.label == "a few months before 2023-05-08 (November 2022 to March 2023)"
    v = value_of(obj("a few years ago", "before", quantity="few", unit="year"), "2023-01-23")
    assert (v.start, v.end, v.label) == (date(2017, 1, 1), date(2021, 12, 31),
                                         "a few years before 2023-01-23 (2017 to 2021)")
    v = value_of(obj("a few Saturdays ago", "before", quantity="few", weekday="saturday"), "2023-05-08")
    assert (v.start, v.end) == (date(2023, 4, 1), date(2023, 4, 29))  # the 6th and the 2nd back
    assert v.label.startswith("a few Saturdays before")


def test_row_16_seasons():
    def season_of(o, day):
        v = value_of(o, day)
        return v.value, v.start, v.end, v.label

    assert season_of(obj("last summer", "last", season="summer"), "2023-02-04") \
        == ("2022-SU", date(2022, 6, 1), date(2022, 8, 31), "summer 2022")
    assert season_of(obj("last winter", "last", season="winter"), "2023-07-10") \
        == ("2023-WI", date(2022, 12, 1), date(2023, 2, 28), "winter 2022/23")
    assert season_of(obj("this winter", "this", season="winter"), "2023-10-20") \
        == ("2024-WI", date(2023, 12, 1), date(2024, 2, 29), "winter 2023/24")
    assert value_of(obj("this winter", "this", season="winter"), "2024-01-10").value == "2024-WI"
    assert value_of(obj("next winter", "next", season="winter"), "2024-01-10").value == "2025-WI"
    assert value_of(obj("next summer", "next", season="summer"), "2023-02-04").value == "2023-SU"
    assert value_of(obj("this summer", "this", season="summer"), "2023-02-04").value == "2023-SU"
    assert value_of(obj("this summer", "this", season="summer"), "2023-10-20").value == "2023-SU"
    assert value_of(obj("this autumn", "this", season="autumn"), "2023-10-20").value == "2023-FA"
    assert value_of(obj("two summers ago", "before", quantity=2, season="summer"), "2023-10-20").value == "2022-SU"
    # TIDES 2005 4.3.3: "last summer" said on 1999-07-15.
    assert value_of(obj("last summer", "last", season="summer"), "1999-07-15").value == "1998-SU"


def test_row_17_named_months():
    assert value_of(obj("last March", "last", month_name="march"), "2023-03-15").value == "2022-03"
    assert value_of(obj("last March", "last", month_name="march"), "2023-05-01").value == "2023-03"
    assert value_of(obj("next March", "next", month_name="march"), "2023-03-15").value == "2024-03"
    assert value_of(obj("next March", "next", month_name="march"), "2023-01-10").value == "2023-03"
    assert value_of(obj("this January", "this", month_name="january"), "1037-11-10").value == "1037-01"
    assert value_of(obj("in December", "next", month_name="december"), "2023-12-05").value == "2024-12"


def test_row_18_an_anchor_without_a_year_takes_the_nearest_occurrence():
    o = obj("two days before 12 March", "before", quantity=2, unit="day", anchor="XXXX-03-12")
    assert value_of(o, "2023-03-01").value == "2023-03-10"
    assert value_of(o, "2023-09-20").value == "2024-03-10"  # 174 days ahead against 192 back
    # A tie (183 days either way) takes the earlier.
    o = obj("the day after 11 March", "after", quantity=1, unit="day", anchor="XXXX-03-11")
    assert value_of(o, "2023-09-10").value == "2023-03-12"
    o = obj("the week before 29 February", "last", unit="week", anchor="XXXX-02-29")
    assert value_of(o, "2023-06-01").start == date(2024, 2, 19)


# ------------------------------------------------ rules for every day

@pytest.mark.parametrize("name", tr.WEEKDAYS)
def test_last_and_next_weekday_on_every_day(name):
    index = tr.WEEKDAYS.index(name)
    last_expr, next_expr, this_expr = (read(obj("x", d, weekday=name)) for d in ("last", "next", "this"))
    for d in THREE_YEARS:
        earlier, later, this_one = resolve(last_expr, d).start, resolve(next_expr, d).start, resolve(this_expr, d).start
        assert earlier.weekday() == index and 1 <= (d - earlier).days <= 7
        assert later.weekday() == index and 1 <= (later - d).days <= 7
        assert this_one.weekday() == index and this_one.isocalendar()[:2] == d.isocalendar()[:2]


def test_weeks_and_weekends_on_every_day():
    week_expr, weekend = read(obj("last week", "last", unit="week")), read(obj("x", "last", unit="weekend"))
    for d in THREE_YEARS:
        v = resolve(week_expr, d)
        assert v.start.weekday() == 0 and v.end == v.start + timedelta(days=6)
        assert v.end < d <= v.end + timedelta(days=7)
        assert v.value == "%04d-W%02d" % v.start.isocalendar()[:2]
        # SCATE's LAST: the latest weekend that ended before the day.
        w = resolve(weekend, d)
        assert w.start.weekday() == 5 and w.end < d <= w.end + timedelta(days=7)


def test_months_and_seasons_on_every_day():
    for d in THREE_YEARS:
        v = value_of(obj("next month", "next", unit="month"), d.isoformat())
        assert v.start.day == 1 and v.start > d and (v.start - timedelta(days=1)).month == d.month
        for s in tr.SEASONS:
            last, next_ = (value_of(obj("x", direction, season=s), d.isoformat()) for direction in ("last", "next"))
            assert last.end < d < next_.start
            assert (next_.start - last.end).days < 2 * 366  # nothing of that season in between but D's
            this = value_of(obj("x", "this", season=s), d.isoformat())
            assert this.start <= d <= this.end or this.value in (last.value, next_.value)


TARGETS = ([{"unit": u} for u in tr.UNITS] + [{"weekday": g} for g in tr.WEEKDAYS]
           + [{"season": s} for s in tr.SEASONS] + [{"month_name": m} for m in tr.MONTHS]
           + [{"unit": "week", "weekday": "friday"}])


@pytest.mark.parametrize("target", TARGETS, ids=["-".join(t.values()) for t in TARGETS])
def test_last_is_before_one_and_next_is_after_one(target):
    for d in THREE_YEARS[::5]:
        # One day takes its anchor (3.3.0): with none, before/after one day is
        # refused, and "the day before D" said on D is last day.
        keep_grounded = {"anchor": d.isoformat()} if target.get("unit") == "day" else {}
        assert resolve(read(obj("x", "last", **target)), d) == resolve(read(obj("x", "before", **target, **keep_grounded)), d)
        assert resolve(read(obj("x", "next", **target)), d) == resolve(read(obj("x", "after", **target, **keep_grounded)), d)


@pytest.mark.parametrize("unit", ["week", "month", "year"])
def test_the_exact_day_falls_inside_the_calendar_unit(unit):
    for d in THREE_YEARS[::3]:
        for n in (1, 2, 3, 6, 13):
            for direction in ("before", "after"):
                calendar = resolve(read(obj("x", direction, quantity=n, unit=unit)), d)
                exact = resolve(read(obj("x", direction, quantity=n, unit=unit, granularity="day")), d)
                assert calendar.start <= exact.start <= calendar.end


# --------------------------------------------- what the reader refuses

INVALID = [
    ("not an object", ["yesterday"], "not an object"),
    ("no text", {"direction": "last", "unit": "day"}, "text is missing"),
    ("blank text", obj("   ", "last", unit="day"), "text is missing"),
    ("a sentence", obj("x" * 121, "last", unit="day"), "longer than 120"),
    ("no direction", {"text": "yesterday", "unit": "day"}, "direction is missing"),
    ("unknown direction", obj("previously", "previous", unit="day"), "direction 'previous' is not one of"),
    ("unknown unit", obj("a fortnight ago", "before", unit="fortnight"), "unit 'fortnight'"),
    ("nothing counted", obj("recently", "before"), "say what is counted"),
    ("two names", obj("x", "last", weekday="saturday", season="summer"), "not weekday and season"),
    ("named month with a unit", obj("x", "last", month_name="march", unit="month"), "a named month takes no unit"),
    ("season with a unit", obj("x", "last", season="summer", unit="year"), "a named season takes no unit"),
    ("weekday of a month", obj("x", "last", weekday="friday", unit="month"), "a weekday goes alone"),
    ("a count with last", obj("the last two weeks", "last", quantity=2, unit="week"), "quantity goes with before"),
    ("a count with this", obj("x", "this", quantity=3, unit="day"), "quantity goes with before"),
    ("zero", obj("x", "before", quantity=0, unit="day"), "1 or more"),
    ("negative", obj("x", "after", quantity=-2, unit="day"), "1 or more"),
    ("a fraction", obj("x", "before", quantity=2.5, unit="day"), "not a whole number"),
    ("a boolean", obj("x", "before", quantity=True, unit="day"), "not a whole number"),
    ("a word", obj("x", "before", quantity="many", unit="day"), "not a whole number"),
    ("too many", obj("x", "before", quantity=10000, unit="day"), "more than 9999"),
    ("a vague night", obj("x", "before", quantity="few", unit="day", part_of_day="night"), "no part of day"),
    ("vague and exact", obj("x", "before", quantity="few", unit="week", granularity="day"), "cannot be exact"),
    ("exact last", obj("x", "last", unit="week", granularity="day"), "granularity goes with before/after"),
    ("exact weekday", obj("x", "before", quantity=2, weekday="friday", granularity="day"), "weeks, months or years"),
    ("exact days", obj("x", "before", quantity=2, unit="day", granularity="day"), "weeks, months or years"),
    ("exact weekend", obj("x", "before", quantity=2, unit="weekend", granularity="day"), "weeks, months or years"),
    ("months of weeks", obj("x", "before", quantity=2, unit="week", granularity="month"), "a count of years"),
    ("unknown granularity", obj("x", "before", quantity=2, unit="year", granularity="week"), "granularity 'week'"),
    ("the night of a week", obj("x", "last", unit="week", part_of_day="night"), "a single day"),
    ("unknown part of day", obj("x", "last", unit="day", part_of_day="noon"), "part_of_day 'noon'"),
    ("anchor in words", obj("x", "last", unit="week", anchor="9 June"), "anchor '9 June'"),
    ("anchor not a day", obj("x", "last", unit="week", anchor="2023-02-30"), "not a day of the calendar"),
    ("anchor without zeros", obj("x", "last", unit="week", anchor="2023-6-9"), "anchor '2023-6-9'"),
    ("unknown weekday", obj("x", "last", weekday="caturday"), "weekday 'caturday'"),
    ("too short a name", obj("x", "last", weekday="t"), "weekday 't'"),
    ("unknown season", obj("x", "last", season="monsoon"), "season 'monsoon'"),
    # 3.3.0: the day next to the memory's day is last/next day; a count of one
    # day from no stated date was the shape of the false dates (LoCoMo
    # conv-26: 8 of Gemini's 12, none of the true ones; TIDES 2005 §4.3.2).
    ("one day back, no anchor", obj("recently", "before", quantity=1, unit="day"), "one day back or ahead"),
    ("one day ahead, no anchor", obj("since we talked", "after", quantity=1, unit="day"), "one day back or ahead"),
    ("a day with no count", obj("x", "before", unit="day"), "one day back or ahead"),
]


@pytest.mark.parametrize("why, o, reason", INVALID, ids=[r[0] for r in INVALID])
def test_the_reader_refuses_with_a_reason(why, o, reason):
    with pytest.raises(InvalidExpression) as exc:
        read(o)
    assert reason in exc.value.reason


def test_a_date_outside_the_calendar_is_refused_when_resolved():
    for o, day in ((obj("x", "before", quantity=9999, unit="year"), date(2023, 5, 8)),
                      (obj("x", "after", quantity=9999, unit="year"), date(2023, 5, 8)),
                      (obj("x", "before", quantity=9999, unit="day"), date(5, 1, 1)),
                      (obj("x", "last", season="winter"), date(1, 1, 15))):
        with pytest.raises(InvalidExpression, match="outside the years 1 to 9999"):
            resolve(read(o), day)


def test_the_day_is_a_date_not_a_moment():
    e = read(obj("yesterday", "last", unit="day"))
    with pytest.raises(TypeError):
        resolve(e, datetime(2023, 5, 8, 23, 30))
    with pytest.raises(TypeError):
        resolve(e, "2023-05-08")


def test_the_reader_is_lenient_where_the_meaning_cannot_change():
    e = read({"text": " Last  Sat ", "direction": " LAST ", "weekday": " Saturday ", "unit": None, "quantity": "",
               "season": None, "part_of_day": "", "anchor": None, "granularity": None, "date": "2023-05-20"})
    assert e == tr.Expression(text="Last Sat", direction="last", weekday="saturday")
    assert read(obj("x", "before", quantity="2", unit="week")).as_json() \
        == {"text": "x", "direction": "before", "unit": "week", "quantity": 2}
    assert read(obj("x", "before", quantity=2.0, unit="day")).quantity == 2
    assert read(obj("x", "before", unit="week")).quantity == 1
    assert read(obj("x", "last", quantity=1, unit="week")).quantity is None
    assert read(obj("x", "last", unit="day", weekday="friday")).unit is None
    assert read(obj("x", "last", unit="week", anchor="xxxx-06-09")).anchor == "XXXX-06-09"
    assert read(obj("x", "before", quantity=" Few ", unit="day")).quantity == "few"


@pytest.mark.parametrize("fields", [
    {"unit": "weeks", "quantity": 2},
    {"weekday": "sat"},
    {"weekday": "Sat."},
    {"month_name": "Sept"},
    {"season": "fall"},
])
def test_no_english_word_is_repaired(fields):
    """3.3.0 (the rule of 8 Oct, no word lists in the code): an enum
    value is read as the schema names it; a plural, an abbreviation or "fall"
    is refused, not repaired."""
    direction = "before" if "quantity" in fields else "last"
    with pytest.raises(InvalidExpression):
        read(obj("x", direction, **fields))


# --------------------------------------------- the memory's annotation

def test_the_column_of_a_memory():
    column, discarded = annotate([obj("last week", "last", unit="week")], "2023-06-09",
                               text="I wanted to tell you about my school event last week. It was awesome!")
    assert column == {"v": 1, "anchor": "2023-06-09", "times": [{
        "text": "last week", "value": "2023-W22", "type": "DATE", "start": "2023-05-29", "end": "2023-06-04",
        "granularity": "week", "label": "the week of 2023-05-29 to 2023-06-04",
        "expr": {"text": "last week", "direction": "last", "unit": "week"}}]}
    assert discarded == []
    assert json.loads(json.dumps(column)) == column
    vague, _ = annotate([obj("a few days ago", "before", quantity="few", unit="day")], date(2023, 8, 19))
    assert vague["times"][0]["mod"] == "APPROX"


def test_the_column_leaves_out_what_it_cannot_use_and_says_why():
    entries = [obj("ieri", "last", unit="day"), obj("yesterday", "last", unit="day"), obj("Ieri", "last", unit="day"),
            {"text": "domani"}, "domani", obj("…", "last", unit="day")]
    column, discarded = annotate(entries, date(2023, 5, 8), text="Ieri sono andata al gruppo di sostegno.")
    assert [t["text"] for t in column["times"]] == ["ieri"]
    assert [entry for entry, _ in discarded] == entries[1:]
    assert [m.split(":")[0] for _, m in discarded] == [
        "the words are not in the memory", "the same words twice", "direction is missing", "not an object",
        "text has no words"]


def test_the_column_without_the_field_with_a_wrong_one_and_with_too_many():
    assert annotate(None, "2023-05-08") == ({"v": 1, "anchor": "2023-05-08", "times": []}, [])
    column, discarded = annotate({"text": "ieri"}, "2023-05-08")
    assert column["times"] == [] and discarded == [({"text": "ieri"}, "relative_times is not a list")]
    many = [obj(f"{n} days ago", "before", quantity=n, unit="day") for n in range(2, 16)]
    column, discarded = annotate(many, "2023-05-08")
    assert len(column["times"]) == tr.MAX_TIMES
    assert [m for _, m in discarded] == [f"more than {tr.MAX_TIMES} relative times in one memory"] * 2


def test_the_column_wants_the_day_of_the_memory():
    with pytest.raises(TypeError):
        annotate([], datetime(2023, 5, 8, 10, 0))
    with pytest.raises(TypeError):
        annotate([], "8 May 2023")


def test_words_of_a_script_without_spaces_are_found():
    column, discarded = annotate([obj("昨天", "last", unit="day")], "2023-05-08", text="我昨天去了公园。")
    assert (column["times"][0]["value"], discarded) == ("2023-05-07", [])
    assert in_the_text("我昨天去了公园。", column["times"])[0] == "我昨天 [2023-05-07]去了公园。"


def test_the_stored_object_dates_the_same_words_from_another_day():
    """An edited timestamp, or a new CONVENTIONS_VERSION: no model call."""
    column, _ = annotate([obj("last Saturday", "last", weekday="saturday")], "2023-05-25")
    again, _ = annotate([t["expr"] for t in column["times"]], "2023-06-01")
    assert (column["times"][0]["value"], again["times"][0]["value"]) == ("2023-05-20", "2023-05-27")


# --------------------------------- the stored column, read from a day

class TestTheDayOfTheColumn:
    """`times_of_the_day`: what the block reads. The column as stored when it
    was computed from the day the block prints; computed again, without the
    model, when that day moved or the conventions did."""

    COLUMN = annotate([obj("last Saturday", "last", weekday="saturday")], "2023-05-25")[0]

    def test_from_the_same_day_as_stored(self):
        assert times_of_the_day(self.COLUMN, "2023-05-25") == self.COLUMN["times"]
        assert times_of_the_day(self.COLUMN, date(2023, 5, 25)) == self.COLUMN["times"]
        assert times_of_the_day(self.COLUMN) == self.COLUMN["times"]

    def test_from_another_day_recomputed(self):
        [t] = times_of_the_day(self.COLUMN, "2023-06-01")
        assert (t["text"], t["value"], t["label"]) == ("last Saturday", "2023-05-27", "2023-05-27")

    def test_another_version_of_the_conventions_recomputed(self):
        old = {**self.COLUMN, "v": 0,
                   "times": [{**self.COLUMN["times"][0], "value": "wrong", "label": "wrong"}]}
        [t] = times_of_the_day(old, "2023-05-25")
        assert (t["value"], t["label"]) == ("2023-05-20", "2023-05-20")

    @pytest.mark.parametrize("column", [None, {}, [], "x", {"v": 1, "anchor": "2023-05-25", "times": []},
                                         {"v": 1, "anchor": "2023-05-25", "times": "x"},
                                         {"v": 1, "anchor": "nonsense", "times": [{"expr": {}}]}])
    def test_nothing_to_read(self, column):
        assert times_of_the_day(column, None) == []

    def test_a_broken_expr_is_left_out(self):
        broken = {"v": 0, "anchor": "2023-05-25", "times": [{"expr": {"text": "x"}}, *self.COLUMN["times"]]}
        assert [t["value"] for t in times_of_the_day(broken, "2023-05-25")] == ["2023-05-20"]


# ---------------------------------------------------- dates in the text

TIMES = annotate([obj("last week", "last", unit="week"), obj("last weekend", "last", unit="weekend")],
               "2023-06-09")[0]["times"]


def test_the_date_goes_after_the_words_it_dates():
    text = "We met last weekend, and Last Week I spoke at a school."
    marked, missing = in_the_text(text, TIMES)
    assert marked == ("We met last weekend [the weekend of 2023-06-03 to 2023-06-04], and Last Week "
                       "[the week of 2023-05-29 to 2023-06-04] I spoke at a school.")
    assert missing == []
    assert in_the_text(marked, TIMES) == (marked, [])  # already dated: left as it is


def test_whole_words_only_and_any_whitespace():
    assert in_the_text("We met last weekend.", TIMES[:1]) == ("We met last weekend.", TIMES[:1])
    assert in_the_text("Since last\n  week I paint.", TIMES[:1])[0] \
        == "Since last\n  week [the week of 2023-05-29 to 2023-06-04] I paint."


def test_a_time_the_observation_does_not_contain_goes_at_the_end():
    times = annotate([obj("ieri", "last", unit="day")], "2023-05-08", text="Ieri sono andata al gruppo.")[0]["times"]
    assert with_dates("Ieri sono andata al gruppo.", times) == "Ieri [2023-05-07] sono andata al gruppo."
    assert with_dates("The user went to a support group yesterday.", times) \
        == "The user went to a support group yesterday. (ieri: 2023-05-07)"
    assert with_dates("Nothing relative here.", []) == "Nothing relative here."


# ------------------------------------------ the schema and the prompt

SCHEMA = tr.SCHEMA


def _examples_of_the_prompt():
    """The objects of the prompt's paragraph: every line that starts with
    {"text" (in PROPOSTA.md of the proposal, in RULES_RELATIVE_TIMES here)."""
    lines = tr.RULES_RELATIVE_TIMES.splitlines()
    return [json.loads(r.strip()) for r in lines if r.strip().startswith('{"text"')]


def _validator():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    return jsonschema.Draft202012Validator(SCHEMA)


VALID = [o for _, _, o, _ in LOCOMO if "fri" != o.get("weekday") and "tues" != o.get("weekday")] + [
    obj("a few months ago", "before", quantity="few", unit="month"),
    obj("Saturday night", "last", weekday="saturday", part_of_day="night"),
    obj("Saturday of last week", "last", unit="week", weekday="saturday"),
    obj("a year ago", "before", quantity=1, unit="year", granularity="month"),
    obj("two weeks ago today", "before", quantity=2, unit="week", granularity="day"),
    obj("in a few days", "after", quantity="few", unit="day"),
    obj("the day before 3 June", "before", quantity=1, unit="day", anchor="XXXX-06-03"),
]


def test_the_examples_of_the_prompt_and_of_the_schema_are_read_and_dated():
    examples = _examples_of_the_prompt()
    assert len(examples) == 8
    for o in examples + SCHEMA["examples"]:
        assert resolve(read(o), date(2023, 6, 9)).value


def test_the_words_of_each_example_are_in_its_sentence():
    """Each shape of the output copies its words from the sentence it
    describes, as the paragraph asks ("text": copied exactly)."""
    lines = [r.strip() for r in tr.RULES_RELATIVE_TIMES.splitlines()]
    pairs = [(lines[i - 1], json.loads(r)) for i, r in enumerate(lines) if r.startswith('{"text"')]
    assert len(pairs) == 8
    for sentence, o in pairs:
        assert sentence.endswith("->")
        column, discarded = annotate([o], "2023-06-09", text=sentence[:-2].strip().strip('"'))
        assert discarded == [] and len(column["times"]) == 1, (sentence, o)


def test_the_schema_accepts_what_the_reader_accepts():
    validator = _validator()
    for o in VALID + _examples_of_the_prompt() + SCHEMA["examples"]:
        read(o)
        assert list(validator.iter_errors(o)) == [], o


def test_the_schema_names_the_fields_the_prompt_names():
    for field_name in SCHEMA["properties"]:
        assert f'"{field_name}"' in tr.RULES_RELATIVE_TIMES


# What only the code can check: the calendar (30 February matches the
# pattern) and a name too short to say which day it is (the schema sees an
# unknown word, which it also refuses, so it stays in the list).
ONLY_THE_CODE = {"anchor not a day"}


@pytest.mark.parametrize("why, o, reason", INVALID, ids=[r[0] for r in INVALID])
def test_the_schema_refuses_what_the_reader_refuses(why, o, reason):
    validator = _validator()
    if why in ONLY_THE_CODE:
        assert validator.is_valid(o)
    else:
        assert not validator.is_valid(o)


# --------------------------------------------------- the module itself

def test_standard_library_only_no_io_no_clock():
    """Standard library only, and the comparison of `extraction` (itself
    standard library), so that the module runs the same in both repositories;
    no I/O and no clock, so the same object and day give the same value."""
    source = Path(tr.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules, folder = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            (folder if node.level else modules).add((node.module or "").split(".")[0])
    assert modules <= {"__future__", "re", "unicodedata", "dataclasses", "datetime", "typing"}
    assert folder == {"extraction"}
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"open", "print", "input", "exec", "eval"}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & {"today", "now", "utcnow", "time"}
