"""One cosine and one rule for a time without a zone (the code audit of
17 Oct 2026, R2-DUP-01): the vectors check took its own cosine, with its own
contract, and seven places wrote "a naive time is UTC" again by hand."""
from datetime import datetime, timezone

from korely_agent.core.times import aware
from korely_agent.services import vectors


def test_the_vectors_check_reads_the_engines_cosine():
    assert vectors._cosine([1.0, 0.0], [2.0, 0.0]) == 1.0
    assert abs(vectors._cosine([1.0, 0.0], [0.0, 3.0])) < 1e-12
    assert vectors._cosine([0.0, 0.0], [1.0, 0.0]) == 0.0, "a zero vector is not the model's"
    assert abs(vectors._cosine([1, 2, 3], [1, 2, 3]) - 1.0) < 1e-12


def test_a_naive_time_is_utc_and_none_stays_none():
    assert aware(None) is None
    assert aware(datetime(2026, 10, 18, 1, 0)) == datetime(2026, 10, 18, 1, 0, tzinfo=timezone.utc)
    rome = datetime.fromisoformat("2026-10-18T03:00:00+02:00")
    assert aware(rome) is rome
