"""A memory WITH an end user, and one that names neither an end user nor an
agent, is asked the prompts of memory_core 6.3.0, byte for byte (memory_core
6.4.0, the extraction told whose memory it is).

6.4.0 changes the prompts of an agent's memory with no end user (a team's
memory, an agent's own notes, written with an `agent_id` and no `user_id`):
until then they were framed around an end user named "user" that does not
exist. The memories of an end user must not move by one byte: every number
measured on them (LoCoMo, LongMemEval, the writer verdicts) was measured on
these prompts. Nor must a memory that names nobody: one person using the MCP
or an SDK with no ids is the "user" of those prompts, and their first person
("I prefer the dark theme") is theirs. So the prompts are fingerprinted here,
every switch of every extractor in every combination, for three handles and
for no ids at all, and compared with the fingerprints taken on 6.3.0
(origin/main 70a40f67, 7 Nov 2026) before a line of 6.4.0 was written.

"user" is one of the handles: a memory whose end user is literally named
"user" is an end user like any other. On 6.3.0 a memory with no end user was
asked exactly those prompts (the anchor fell back to "user"; the two
fingerprints below are equal), which is how a measure of 6.4.0 with a real
writer asks the prompts of before.

No model is called: the model is a function that records the prompts.
"""
from __future__ import annotations

import hashlib
import itertools
import json

import pytest

from korely_agent.services.memory_core import fact_extractor as est


class _Model:
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt, max_tokens):
        self.prompts.append(prompt)
        ids = [r.split("]")[0].split()[0] for r in prompt.split("[mem_id=")[1:]]
        if ids:
            return json.dumps({"memories": [{"mem_id": i, "observation": "", "facts": []} for i in ids]})
        return json.dumps({"observation": "", "facts": []})


# " ana " and "  ": ids the product accepts and stores as they are; 6.3.0
# named them "ana" and "user" (its anchor, the id stripped or "user"). An id
# of spaces is an end user, never "no end user" (review of 7 Nov 2026).
HANDLES = ("ana-77", "user", 'Customer 9031 "zq"', " ana ", "  ")
TEXT = "I moved to Lyon last week. Maria mentors me on Fridays; Acme pays 20 EUR."
PREVIOUS = ["An earlier memory about the gym.", "She said the plan is Pro."]
# Long enough for three windows (fact_extractor.windows), so the prompt of a
# window that is not the first is fingerprinted too.
LONG = "\n\n".join(f"Paragraph {i}: the team met Lucia at the Lyon office and agreed on plan {i}." * 6
                   for i in range(30))


def _switches(*names):
    for values in itertools.product((False, True), repeat=len(names)):
        yield dict(zip(names, values))


def _extract(m, user, kw):
    est.extract(m, TEXT, user, **kw)


def _observation(m, user, kw):
    est.extract_with_observation(m, TEXT, user, previous=PREVIOUS, memory_date="2026-11-03", **kw)


def _batch(m, user, kw):
    est.extract_batch_with_observation(m, [("m1", TEXT), ("m2", "Globex moved to the Starter plan.")], user,
                                       previous=PREVIOUS, date={"m1": "2026-11-03"}, **kw)


def _windows(m, user, kw):
    est.extract_with_observation(m, LONG, user, previous=PREVIOUS, memory_date="2026-11-03", **kw)


# The product's three writers (services/facts/extractor.py): gpt-oss-120b
# (states and faithful on), Gemini (attributes on), any other writer (both off).
_PRODUCT = [dict(times=True, fixed_prefix=True, without_threshold=True, states=s, faithful=f, attributes=a)
            for s, f, a in ((True, True, False), (False, False, True), (False, False, False))]

FAMILIES = {
    "extract": (_extract, list(_switches("states", "attributes", "fixed_prefix", "without_threshold"))),
    "observation": (_observation, list(_switches("times", "states", "attributes", "faithful", "fixed_prefix",
                                                 "without_threshold"))),
    "observation_batch": (_batch, list(_switches("times", "states", "attributes", "faithful", "fixed_prefix",
                                                 "without_threshold"))),
    "observation_windows": (_windows, _PRODUCT),
}

# Taken on memory_core 6.3.0 (origin/main 70a40f67) with this file's
# `fingerprint`: the first three handles before 6.4.0 changed a line, the
# five again, on a `git archive` of 70a40f67, after the review of 7 Nov 2026.
FINGERPRINTS_6_3_0 = {
    "extract": "7cf894f86831a8f2cb87cf32144b779491926dd4f3c779380a812e4a97351f4b",
    "observation": "52ae971c23fe976ca83b01f8588148f29533a6857aa17ad496515f35864cd532",
    "observation_batch": "e78117483a28a3f0f3ed7675fc3c78764bbfb44128454b3b7d73f7129ed1ebf4",
    "observation_windows": "017e950c510cd3402e08ffe87d8d34a2b7f5dc85788ecd272525e72e0f2eec2d",
}


def prompts(family: str, handles=HANDLES, **extra):
    """Every prompt of one family, in a fixed order: handle, then switches."""
    function, combinations = FAMILIES[family]
    out = []
    for user in handles:
        for kw in combinations:
            m = _Model()
            function(m, user, {**kw, **extra})
            assert m.prompts, "no prompt was asked"
            out.extend(m.prompts)
    return out


def fingerprint(family: str, handles=HANDLES, **extra) -> str:
    h = hashlib.sha256()
    for p in prompts(family, handles, **extra):
        h.update(hashlib.sha256(p.encode("utf-8")).digest())
    return h.hexdigest()


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_the_prompts_of_an_end_user_are_those_of_6_3_0(family):
    assert fingerprint(family) == FINGERPRINTS_6_3_0[family], (
        f"a prompt of a memory with an end user changed ({family}): every measure of the "
        "product was taken on these prompts")


# The same, for a memory with neither an end user nor an agent (end_user_id
# None, no agent_id), taken on 6.3.0 the same day. On 6.3.0 they equal the
# handle "user"'s, one by one.
FINGERPRINTS_6_3_0_NO_IDS = {
    "extract": "c55da85e1837a1ef6736a5754e457a23bcee97803c5e38a68020d64771a97653",
    "observation": "21d1487675c74a19514352ce9bd91ddc7f77ace8fecd3922c11d6e05aaea3f65",
    "observation_batch": "ad8e49d4c0a5fee0fb697a99b1a93690240e8c021439d3b07038e8cfd751d87a",
    "observation_windows": "851da2baf84434f2e928b0d49a6008faeeb1e3f8e67b69fab8b6ff124c6787d2",
}


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_the_prompts_of_a_memory_of_no_ids_are_those_of_6_3_0(family):
    assert fingerprint(family, handles=(None,)) == FINGERPRINTS_6_3_0_NO_IDS[family], (
        f"a prompt of a memory with neither an end user nor an agent changed ({family}): "
        "a person using the product with no ids would lose the facts of their first person")
    assert fingerprint(family, handles=("user",)) == FINGERPRINTS_6_3_0_NO_IDS[family]


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_a_blank_agent_is_no_agent(family):
    assert fingerprint(family, handles=(None,), agent_id="  ") == FINGERPRINTS_6_3_0_NO_IDS[family]


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_an_agent_changes_nothing_when_there_is_an_end_user(family):
    """6.4.0 reads `agent_id` only for a memory of no end user."""
    assert fingerprint(family, agent_id="build-agent") == FINGERPRINTS_6_3_0[family]


def test_the_combinations_are_all_asked():
    """The fingerprint is worth what it covers: 5 handles, every switch."""
    assert len(prompts("extract")) == 5 * 16
    assert len(prompts("observation")) == 5 * 64
    assert len(prompts("observation_batch")) == 5 * 64
    assert len(prompts("observation_windows")) == 5 * 3 * len(est.windows(LONG))
    assert len(est.windows(LONG)) == 3
