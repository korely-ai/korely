"""Agent fact extraction — the self-hosted adapter on memory_core.fact_extractor.

The prompts, the reading of the model's JSON, the grounding of every argument
in the memory's own text and what a failure means live in
`memory_core/fact_extractor.py` (written once for the hosted product and this
one; one codebase since 9 Oct 2026). This adapter adds what is this product's
own: the provider configured once in `core.llm` and the default
model for it. Usage is attributed by the caller (`llm_usage.stage`).

Extraction is direct, no NER pre-step, as in Mem0 (Chhikara et al. 2025,
arXiv:2504.19413); the result is typed into (subject, predicate, object)
triples with bi-temporal validity (the Zep/Graphiti graph model).

A broken reply is not an empty one (2026-09-11/12): a failed call, an empty
reply or JSON cut off by the token ceiling raises `ExtractionFailed`, the
memory goes to 'error', `manage status` counts it and it can be re-driven.
Until 2026-09-27 an EMPTY reply still slipped through as "no facts"; the
shared reader closes that too.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from ...config import settings
from ..memory_core import fact_extractor
from ..memory_core.fact_extractor import ExtractionProviderUnavailable

__all__ = ["extract_facts_and_observation"]

logger = logging.getLogger(__name__)


def _select_provider(model_name: Optional[str]):
    """The configured provider plus the model to use with it.

    Which provider is a settings decision, made once in core.llm; here we only
    resolve the model name, because the default differs per provider.

    Returns (None, None) when no provider is available. Callers must treat that
    as a failure and not as "no facts found": conflating the two is what let the
    hosted product report memories as processed while extracting nothing.
    """
    from ...core.llm import get_provider

    client = get_provider()
    if client is None:
        return None, None
    default = (
        settings.mistral_extraction_model
        if getattr(client, "name", "") == "mistral"
        else settings.extraction_model
    )
    return client, (model_name or default)


def _model_call(client, eff_model):
    def call(prompt: str, max_tokens: int) -> str:
        result = client.complete(
            messages=[{"role": "user", "content": prompt}],
            model=eff_model,
            max_tokens=max_tokens,
            temperature=0.0,
            json_only=True,
        )
        return result.text or ""
    return call


def _call_or_raise(model_name: Optional[str]):
    client, eff_model = _select_provider(model_name)
    if client is None:
        logger.error(
            "[agent_facts] no LLM provider for extraction "
            "(extraction_provider=%s); memory will NOT be marked done",
            settings.extraction_provider,
        )
        raise ExtractionProviderUnavailable("no LLM provider available for fact extraction")
    return _model_call(client, eff_model)


# The rules memory_core once added to the observation's prompt, each off and
# then removed after its measure: the events mentioned in passing
# (memory_core 3.3.0: the pack of 7-8 Oct 2026 measured them worse with
# both writers); the model asked to write relative times as dates (it
# converted 1 of 40 on LoCoMo conversation 26, 5 Oct 2026, and the dates
# computed by code below replaced it; out of memory_core with
# 4.0.0); the end user's standing instructions ("always answer in Italian",
# read by a section of /v1/context): 22 of 28 probes against 21,
# a tie, removed on 12 Oct 2026 (memory_core 5.0.0).

# memory_core 2.9.0, off until measured (2026-10-06):
# - the relative times of a memory ("yesterday", "two weekends ago") read by
#   the model as small structured objects and dated by code from the memory's
#   day (memory_core relative_time), stored in the memory's relative_times
#   and shown next to the words in /v1/context. Measured need: on LoCoMo every
#   wrong temporal answer had the evidence in front of the reader, and the
#   prompt rule asking the model to write the dates converted 1 of 40
#   (LoCoMo conversation 26, 5 Oct 2026);
# - the user's facts read only from the user's turns (memory_core `turni`,
#   with "a fact the assistant alone said closes nothing", LongMemEval
#   38146c39): measured with Gemini 3.1 Flash-Lite as the writer on LoCoMo
#   conversation 26, 127 against 129 of 152 (p 0.80), no gain, and removed
#   from this adapter on 2026-10-06; the engine's own code for
#   it left with memory_core 4.0.0 (2026-10-12).
#
# The relative times are ON since 0.1.18 (2026-10-06), measured twice on
# LoCoMo conversation 26 with gpt-oss-120b as writer, reader and judge: the
# dates in the block on the same memories, 130 against 120 (14 won, 4 lost,
# p 0.031; temporal 33 against 21); and the write itself, both arms written
# from scratch, 129 against 120 (16 won, 7 lost; temporal 31 against 24, p
# 0.039), no category worse, as many facts. Its price: the writer's prompt
# grows, about 17% more per memory (0.00104 against 0.00088 EUR on
# Scaleway), and more memories end with an empty observation (108 against
# 71 of 419: compliments and greetings, shown as their text instead).
_OBSERVATION_RELATIVE_TIMES = True

# memory_core 3.1, off until measured (2026-10-06):
# - the states: a change of state is the new state, in the present ("Globex
#   moved to the Starter plan" is plan: Starter plan), never an event of its
#   own. The Self-hosted's acceptance run with gpt-oss-120b as the writer
#   failed the plan change twice in a row on exactly that: a past event, so
#   the old plan stayed open. 3.1.0's wording, switched on there: 11 of 13,
#   the plan change still an event, and "a relation never contains its value"
#   pushed the unit out of "12 employees" (a bare 12, dropped). 3.1.1 drops
#   that sentence and shows an invented shape of the output: not measured;
# - a faithful observation: the user is named only for what the memory says
#   of, by or to them (no invented "The user's organization"), the caption of
#   a shared image is kept, and a time keeps the precision it was said with.
#   On LoCoMo whole about one reader error in ten came from an observation
#   that had lost the photo or what "we did it" meant (LoCoMo whole, 6 Oct
#   2026). 3.1.0's wording on 40 of those memories, one
#   extraction per side: the caption helped (the answer's words 47% against
#   33%, 3 better and 0 worse of 20 photos), the referent sentence did not (0
#   better, 1 worse of 20), and twice "last week" became vaguer. 3.1.2 keeps
#   the first, drops the second, fixes the third. It also wrote 20% more
#   facts (78 against 65), whose effect on the block is not measured.
# memory_core 3.3.0 `fixed_prefix` (the hosted product's
# AGENTS_FIXED_PREFIX before it ran this code): the rules name the end user END_USER and the handle is
# bound once, after them, so the ~3,000 tokens of rules are one prefix for every
# end user and a provider's prefix cache can serve them (agent_llm_usage
# .cached_tokens says how much). On since 0.1.23 (8 Oct 2026),
# part of the configuration measured on LoCoMo (7-8 Oct 2026, gpt-oss): with
# the lazy votes and the event day, list-free, 610 answers of 762 against 618
# of the same-day reading (inside noise), facts closed by mistake 53 -> 17,
# model calls per memory 2.84 -> 2.32. Neither Scaleway nor Gemini reports
# cache hits yet, so the saving is not read today.
_FIXED_PREFIX = True

# memory_core 5.5.0 `without_threshold` (audit of 18 Oct 2026, finding 2): the
# extraction prompts without "below 0.5 SKIP", a threshold on the confidence
# the model states that the engine stopped using in 2.11.0. On since the
# release after 0.1.41 (18 Oct 2026), measured on LoCoMo conversation 26 (c26b
# against c26n): answers
# 135 and 138 (p 0.51), facts 470 and 463, and not one fact under 0.5 in
# either arm, so the clause took nothing away.
_NO_CONFIDENCE_SKIP = True


# On by default from 0.1.20 (7 Oct 2026, after LoCoMo whole), for the
# gpt-oss-120b writer alone (`_rules_3_1` below).
_STATES_RULE = True
_FAITHFUL_OBSERVATION = True

# memory_core 3.2.0 (7 Oct 2026): an attribute said with the copula
# ("I am vegetarian", "Sono infermiera") named in the predicate (has_diet
# vegetarian, has_role nurse). The model answered (anna, is, vegetarian) and
# the engine drops a predicate under three letters, so every such attribute
# was lost, in every language. Measured on 37 sentences in three languages:
# with Gemini 3.1
# Flash-Lite as the writer 13 right without the rule, 33 with it, none worse;
# with gpt-oss-120b 25 without and 22 with (it names the attribute by itself,
# and the rule took intolerances, the age and a husband's diet away). So it
# follows the writer, as on the hosted product: even switched on it applies
# only when the writer in force is Gemini, and no other was measured.
# LoCoMo conversation 26 with Gemini as the writer, gpt-oss-120b reading and
# judging, the code of 12 Oct 2026:
# 134 against 135 of 152 with it (3 won, 4 lost, p 1.00), 624 facts against
# 602 and fewer closed (132 against 143). The run of 7 Oct, before the lazy
# votes, had lost 125 against 129 with the closures doubled; that no longer
# happens. On from 0.1.28 (12 Oct 2026), for the Gemini writer alone:
# Gemini writes the hosted product's "global" region, the
# default one.
_ATTRIBUTES_RULE = True


def _measured_writer(model_name: Optional[str]) -> Optional[str]:
    """Which of the two writers the prompt rules were measured on writes this
    call, by one rule for both (a code review found that until
    18 Oct 2026 the 3.1 rules asked for a "gpt-oss" in the model's name and the
    attributes rule for the Gemini client, each in its own function).

    "gemini" when the client is the Gemini one; "gpt-oss" when the model in
    force names the gpt-oss weights they were measured on, gpt-oss-120b,
    under any host's spelling (`core.llm.measured_gpt_oss`; the model the
    Models page chose wins over the one asked for, core/llm); None for every
    other writer. Another gpt-oss (gpt-oss-20b), a name that does not say its
    size, or Gemini behind an OpenAI-compatible door (OpenRouter), is None:
    no rule was measured on it, and None is the prompts of before."""
    from ...core.llm import measured_gpt_oss

    client, model = _select_provider(model_name)
    if getattr(client, "name", "") == "gemini":
        return "gemini"
    model = getattr(client, "forced_model", None) or model
    return "gpt-oss" if measured_gpt_oss(model) else None


def _rules_3_1(model_name: Optional[str]) -> bool:
    """Whether the writer of this call is the one the 3.1 rules (states, the
    faithful observation) were measured on: gpt-oss-120b. LoCoMo whole with
    it as writer, reader and judge (2026-10-07): 1,247 of 1,540 off, 1,262
    on (+15, 71 won and 56 lost, p 0.21), after +4 on conversation 26 (p
    0.48), and the memory test's plan change 13 of 13 with `states`. With
    Gemini as the writer they lose: LoCoMo conversation 26, gpt-oss-120b
    reading and judging (12 Oct 2026),
    131 against 135 of 152 with them (2 won, 6 lost, p 0.29; multi-hop 22
    against 24, temporal 31 against 33). No other writer was measured: the
    switches above apply to gpt-oss-120b alone."""
    return _measured_writer(model_name) == "gpt-oss"


def _states(model_name: Optional[str]) -> bool:
    return _STATES_RULE and _rules_3_1(model_name)


def _faithful(model_name: Optional[str]) -> bool:
    return _FAITHFUL_OBSERVATION and _rules_3_1(model_name)


def _attributes(model_name: Optional[str]) -> bool:
    """The attributes rule for this call: switched on, and the writer in force
    is Gemini (the only one measured better with it)."""
    return _ATTRIBUTES_RULE and _measured_writer(model_name) == "gemini"


def extract_facts_and_observation(
    content: str,
    *,
    end_user_id: Optional[str],
    db: Session,
    tenant_id: UUID,
    model_name: Optional[str] = None,
    previous: Sequence[str] = (),
    date: Optional[str] = None,
) -> fact_extractor.Extraction:
    """The facts of one memory and its observation, in one call (memory_core
    2.6.0): the short sentence about the end user that /v1/context shows in
    place of the whole text. `previous`: the end user's memories before this
    one, oldest first, read for pronouns and never mined; `date`: the
    memory's day, YYYY-MM-DD, for "yesterday" and "last week". No facts when
    the text holds none; ExtractionFailed when the model could not be read;
    ExtractionProviderUnavailable when there is no provider."""
    if not (content or "").strip():
        return fact_extractor.Extraction([], None)
    from ...core.llm import extract_json

    return fact_extractor.extract_with_observation(_call_or_raise(model_name), content, end_user_id,
                                              extract_json, previous=previous, memory_date=date,
                                              times=_OBSERVATION_RELATIVE_TIMES,
                                              states=_states(model_name),
                                              faithful=_faithful(model_name),
                                              attributes=_attributes(model_name),
                                              fixed_prefix=_FIXED_PREFIX,
                                              without_threshold=_NO_CONFIDENCE_SKIP)
