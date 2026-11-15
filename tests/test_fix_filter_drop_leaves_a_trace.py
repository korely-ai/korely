"""When the quality filter drops every fact the writer declared, the engine
says so (E1 of the engine list, 4 Nov 2026; Notion row, in English: "the
writer declares a fact, the quality filter drops it, the memory ends done with
no fact").

`keep_grounded` drops a triple whose entity is not in the memory's text. Each
drop was a DEBUG line, so a memory whose only fact was dropped ended 'done'
with no fact and no trace above DEBUG: nobody could tell it from a text that
holds none. Now a round that declared facts and kept none writes one INFO
line with counts only, never a word of the text or of the fact.
"""
import logging

from korely_agent.services.memory_core.fact_extractor import ExtractedFact, keep_grounded

TEXT = "Marco bought a car last week."


def fact(subject, obj):
    return ExtractedFact(subject, "owns", obj, 0.9, subject_type="person")


def infos(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]


def test_every_fact_dropped_leaves_one_info_line_with_counts_only(caplog):
    with caplog.at_level(logging.DEBUG):
        kept = keep_grounded([fact("Marco", "Panda"), fact("Marco", "Zorblax")], TEXT, "u1")
    assert kept == []
    lines = infos(caplog)
    assert len(lines) == 1 and "2" in lines[0]
    assert "Panda" not in lines[0] and "Zorblax" not in lines[0] and "Marco" not in lines[0]


def test_a_fact_that_stays_leaves_no_info_line(caplog):
    with caplog.at_level(logging.DEBUG):
        kept = keep_grounded([fact("Marco", "car"), fact("Marco", "Panda")], TEXT, "u1")
    assert [f.object for f in kept] == ["car"]
    assert infos(caplog) == []


def test_no_fact_declared_leaves_no_info_line(caplog):
    with caplog.at_level(logging.DEBUG):
        assert keep_grounded([], TEXT, "u1") == []
    assert infos(caplog) == []
