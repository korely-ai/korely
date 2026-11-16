"""memory_core 5.5.0: the extraction prompts without their confidence
threshold, off by default (audit of 18 Oct 2026, finding 2).

Every extraction prompt asked the model to skip a fact it gave under 0.5, a
threshold on stated confidence that the engine itself dropped in 2.11.0. Off,
the prompts are the 5.4.0 ones byte for byte; on, the clause is gone from
every extraction prompt, single, batch, with the observation and its relative
times, and the confidence line stays.
"""
import pytest

from korely_agent.services.memory_core import fact_extractor

PROMPTS = (fact_extractor.PROMPT_SINGLE, fact_extractor.PROMPT_BATCH,
           fact_extractor.PROMPT_SINGLE_OBSERVATION, fact_extractor.PROMPT_BATCH_OBSERVATION,
           fact_extractor.PROMPT_SINGLE_OBSERVATION_TIMES, fact_extractor.PROMPT_BATCH_OBSERVATION_TIMES)


def _seen(**kw):
    seen = []

    def call(prompt, *a, **k):
        seen.append(prompt)
        return '{"facts": [], "memories": []}'

    for f, arg in ((fact_extractor.extract, "I live in Turin."),
                   (fact_extractor.extract_with_observation, "I live in Turin."),
                   (fact_extractor.extract_batch_with_observation,
                    [("m1", "I live in Turin."), ("m2", "I am a nurse.")])):
        try:
            f(call, arg, "anna", **kw)
        except fact_extractor.ExtractionFailed:
            pass
    return seen


@pytest.mark.parametrize("template", PROMPTS)
def test_on_the_clause_leaves_every_prompt_and_the_estimate_stays(template):
    assert "SKIP" in template
    without = fact_extractor._without_threshold(template, True)
    assert "below 0.5" not in without and "0.9" in without and "confidence is your 0-1 estimate" in without
    assert fact_extractor._without_threshold(template, False) is template


def test_off_the_prompts_carry_it_on_they_do_not():
    off, on = _seen(), _seen(without_threshold=True)
    assert len(off) == len(on) == 3
    assert all("below 0.5" in p for p in off)
    assert not any("below 0.5" in p for p in on)


def test_a_prompt_without_the_clause_fails_loudly():
    with pytest.raises(RuntimeError):
        fact_extractor._without_threshold("Memory:\n{CONTENT}", True)


class _RecordingWriter:
    """A provider that keeps every prompt it is sent and answers no facts."""

    def __init__(self, name):
        self.name = name
        self.prompts = []

    def complete(self, *, messages, **kw):
        from types import SimpleNamespace

        self.prompts.extend(m["content"] for m in messages)
        return SimpleNamespace(text='{"facts": [], "memories": []}')


@pytest.mark.parametrize("writer, model", [("mistral", "gpt-oss-120b"), ("gemini", "gemini-flash"),
                                           ("mistral", "another-model")])
def test_the_product_passes_it_on(monkeypatch, writer, model):
    """The product's door, extract_facts_and_observation, with each writer's
    rules (extractor._measured_writer): the prompt the provider is sent has
    no clause (audit of 19 Oct 2026)."""
    from korely_agent.services.facts import extractor

    stub = _RecordingWriter(writer)
    monkeypatch.setattr(extractor, "_select_provider", lambda m: (stub, model))
    try:
        extractor.extract_facts_and_observation("user: I live in Turin.", end_user_id="anna",
                                                db=None, tenant_id=None)
    except fact_extractor.ExtractionFailed:
        pass
    sent = stub.prompts
    assert sent and "I live in Turin." in sent[0]
    assert not any("below 0.5" in p for p in sent)
