"""POST /v1/memories/search's results say what each field is (the code
review of 5 Nov 2026: a refactor had left them bare; `score` above all, a
cosine and not an answer's confidence)."""
from korely_agent.api.memories import SearchResult


def test_every_field_of_a_result_is_described():
    missing = [n for n, f in SearchResult.model_fields.items() if not f.description]
    assert missing == []
    score = SearchResult.model_fields["score"].description
    assert "cosine" in score and "not an answer" in score and "min_score" in score
