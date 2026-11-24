"""Pure rules of contradiction that every engine must apply the same way (memory_core).

Which EXISTING facts a new fact may close is decided by the judge (a model
that reads both facts), never by a rule on the shape of the triple. Graphiti
(Rasmussen 2025, arXiv:2501.13956 §2.2.3) does the same: "an LLM compares new
edges against semantically related existing edges to identify potential
contradictions". This module decides only WHICH existing facts are put in
front of the judge, and how they are shown.

1. Single-valued relations are a HINT for the judge, not a rule (2026-09-27).
   Until today a relation the vocabulary had classified as holding one value
   closed the old value without asking. Removed, for two reasons measured in
   the literature and here:
   * Ritter, Downey, Soderland, Etzioni (EMNLP 2008, "It's a Contradiction,
     No, it's Not"): on pairs with a functional relation and different values,
     only 1.2% were genuine contradictions; synonyms, meronyms and ambiguity
     made the rest look contradictory, and "extensive background knowledge is
     necessary to tease apart seeming contradictions from genuine ones".
   * Lin, Mausam, Etzioni (EMNLP 2010, §3 "Type Restrictions", "typed
     functionality"; until 3.2.0 cited here as §2, which is Related Work):
     the same relation phrase is single-valued for one type of argument and
     multi-valued for another ("published in" a year vs a language). Measured
     on our model the same day: "has Business plan" single-valued 3/3, "has 12
     employees" many-valued 3/3. The vocabulary learns the cardinality once,
     from the first object it sees, so the rule closed "Acme subscribes_to
     Business plan" when "Acme uses Salesforce" arrived (MCP acceptance run).
   The judge sees both objects, so it can apply the cardinality to values of
   the same kind only. Measured in korely-agent after the truncation fix
   (1051c67): 17/18 genuine supersedes caught, 7/7 compatible pairs kept.

2. Same arguments, different relation (BUG-111, 2026-09-27). "Gordon lacks
   Temporal Decay" and "Gordon adopt Temporal Decay" were both open; so were
   "Claude uses Gordon" and "Claude unused_in Gordon". The relations differ,
   so neither the exact-predicate lookup nor the opposing-family table ever
   put them in front of the judge. What they share is the arguments. Same
   subject and same object with a different relation is a cheap, precise
   reason to ASK; the judge decides (has/lacks exclude each other, uses and
   integrates_with do not).

3. A fact never closes a fact stated by the SAME source (BUG-123,
   2026-09-27). A note listing a product's plans "Free, Personal, Business" gave one
   fact per plan, and the judge, reading `plan` as single-valued, closed Free
   with Personal and Personal with Business: the note cancelled itself. In
   Graphiti (Rasmussen et al. 2025, arXiv:2501.13956 §2.2.3) an edge is
   invalidated by a contradicting edge "by setting their t_invalid to the
   t_valid of the invalidating edge", and "Graphiti consistently prioritizes
   new information" along the ingestion timeline T'. Facts extracted from one
   episode have neither order: they share the episode's reference time t_ref
   (§2.1, §2.2.3) and arrive in the same ingestion, so none of them is the
   newer information, and closing one with another would give it an empty
   interval. A change told inside one text ("we were on Free, now on
   Business") is carried by the tense the model reads (`tense=past` is born
   closed, memory_core.fact_time), not by the judge. The source is the memory
   (until 11 Oct 2026 also a note of the personal-memory engine); a fact
   reconfirmed by the source counts as stated by it, because the source says it
   holds now.

4. The new fact comes with its words (2.5.0, 2026-10-01). The judge read
   triples only, and a triple loses what decides the case: "Luca also works
   part-time at Google as a consultant" became `Luca works_at Google`, the
   relation carries [single-valued], and the judge closed `Luca works_at
   Apple` (BUG-200). Ritter et al. (EMNLP 2008, above) is the reason: the
   seeming contradictions on functional relations are told apart from the
   genuine ones by the context, not by the shape of the pair. So the words
   the fact was read from (`extraction.read_evidence`, copied from the text
   or absent) are shown under the new fact, and RULE_WORDS tells the judge
   that the words decide over the tag. The words are described by what they
   assert, never by a list of words that signal it (the rule of
   2026-09-27). The tag can now be asked as "does a new value usually replace
   the old one?" (vocabulary, 2.5.0): the usual case is the tag's, the
   exception is the words'.

5. The new fact comes with its date (2.12.0, code audit of 6 Oct).
   The judge read every earlier fact "(since 2025-06-01)" and the new one
   with no date, so it could only take the new one for the latest. It is
   the last one WRITTEN, not always the latest: a batch exported newest
   first brings the older fact after the newer one. The verdict stays the
   one the judge gave before (does one fact make the other false?), and the
   dates decide which of the two ended, in code (`judge.close`). That is
   the split of Graphiti, whose judge reads the facts without their dates
   (getzep/graphiti, prompts/dedupe_edges.py `resolve_edge`: "Determine
   which facts the NEW FACT contradicts"; edge_operations.py builds the
   context from `edge.fact` alone) and whose code applies the time
   (`resolve_edge_contradictions`, `resolve_extracted_edge`): RULE_DATES
   tells our judge the same.

6. Two facts contradict only when they cannot both be true at once (3.2.0,
   RULE_TRUE_TOGETHER). A reading of every closure of LoCoMo conversation
   26 on 6 Oct: 0 genuine changes in 20 closures with gpt-oss-120b as writer
   and judge, 1 in 27 with Gemini 3.1 Flash-Lite (a pet first said to be one
   animal, corrected to another); the other closures were facts still true
   (12 and 15) or restatements closed instead of merged (8 and 11). With
   Gemini none of the wrong closures came from a [single-valued] tag: the
   judge closed a love of a place with a visit to it, one preference with a
   second one, one artwork with another, an identity with a facet of it.
   * de Marneffe, Rafferty, Manning (ACL 2008, §3.1, read on 6 Oct):
     "contradiction occurs when two sentences are extremely unlikely to be
     true simultaneously", and "For texts to be contradictory, they must
     involve the same event": a visit and a love of the place, or two
     feelings of two moments, are not one event.
   * Graphiti (Rasmussen et al. 2025, arXiv:2501.13956 §2.2.3) invalidates
     only on "temporally overlapping contradictions"; its reference prompt
     (getzep/graphiti, prompts/dedupe_edges.py `resolve_edge`, read on 6 Oct)
     has "Bob ran 5 miles on Tuesday" vs "Bob ran 3 miles on Wednesday":
     "different events on different days - neither duplicate nor
     contradiction". A restatement is a duplicate there, and a contradiction
     only when the new fact updates the old one: "A fact from EXISTING FACTS
     can be both a duplicate AND contradicted (e.g., semantically the same
     but the new fact updates/supersedes it)" (same prompt, read again on
     7 Oct 2026; until then this line said "never a contradiction").
   * TimeML (Pustejovsky et al. 2003; Guidelines 1.2.1 §2.1.3 "Class"): an
     OCCURRENCE is "something that happens or occurs in the world", a STATE
     "circumstances in which something obtains or holds true". What happens
     later does not end a state unless it says so (selling something ends
     owning it, visiting a place does not end loving it).
   * Ritter et al. (EMNLP 2008, §1.1): the apparent contradictions on the Web
     were mostly consistent statements, "due to meronyms ..., synonyms ...,
     hypernyms ..., and reference ambiguity", and "visited" is not functional
     ("Mozart visited Salzburg" and "Mozart visited Vienna" do not
     contradict). A facet of an identity is not a second identity; the same
     fact in other words is not a change.
   The definition itself is NOT in the prompt, on purpose: written as its
   first sentence ("A contradiction means the two facts cannot both be true
   at once"), it made gpt-oss-120b close "Luca works at Apple
   [single-valued]" with "Luca also works part-time at Google" (BUG-200) 6
   times in 7 in the batched judge, against 0 in 7 with 3.1.3 and 0 in 7
   with the rule without it; with Gemini the rule without it is as good
   (6 Oct, a measurement of the judge's words and a judge-only bench, the
   numbers next to the constant). The examples carry the definition.
   A second half, RULE_NO_LONGER_TRUE, in the judge of one fact only: the
   "(no longer true)" of a past statement says that the NEW fact is over,
   and an event that is over ends nothing (more than half of the wrong
   closures came from there; the measurement is next to the constant).
   The examples are invented and kept away from the measured cases
   and from LoCoMo, whose texts are CC
   BY-NC and stay out of this repository. RULE_SINGLE_VALUE, RULE_WORDS
   and RULE_DATES are unchanged, byte for byte, and come after it: the tag
   says what usually holds one value, the words what happened. In the two
   judges of the Agents engine; the personal-memory engine's judge, paused
   and never measured on these rules, left the code with 4.0.0.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

def _n(s: Optional[str]) -> str:
    """A name compared by code: NFKC, case folded, trimmed (3.3.0; until then
    `lower()`, which leaves "Straße" and "STRASSE" apart). Since 6.21.0 the
    one function `identity.name_key`, which keys aliases and links too."""
    from .identity import name_key

    return name_key(s)


def _words(s: str) -> set:
    """The words of a name, in any script (3.3.0, an inventory of 8
    Oct). Until then `[a-z0-9]+`: an object in Cyrillic, Greek, Arabic or
    Chinese had no word at all, so "компания Ромашка" and "Ромашка" were never
    compatible and stages 1c and 1d never asked the judge about them
    (CONTRIBUTING.md: a rule holds for every alphabet). Letters, marks and
    digits of every script make the word, as `predicate_forms._form_in_any_script`
    (the marks keep a
    Devanagari word whole). A one-letter word of a cased script is a function
    word and is left out, as before; in a script without case one character
    carries meaning, so it stays. CJK text has no spaces, so there a name is
    compared whole."""
    import unicodedata as _u

    t = "".join(ch if _u.category(ch)[0] in "LMN" else " " for ch in _n(s))
    return {w for w in t.split() if len(w) >= 2 or w.upper() == w.lower()}


def compatible_objects(a: Optional[str], b: Optional[str]) -> bool:
    """Two objects that may name the same thing: equal, or the words of one
    contained in the other's ("SSO" and "SSO support", "Acme" and "Acme Corp").
    Found by the MCP acceptance run (2026-09-27): "lacks SSO support" and "uses SSO"
    never reached the judge because the objects differed by one word. Only a
    reason to ASK: the judge still decides."""
    na, nb = _n(a), _n(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    pa, pb = _words(na), _words(nb)
    if not pa or not pb:
        return False
    return pa <= pb or pb <= pa


def arguments_swapped(
    new_subject: str,
    new_object: str,
    candidate_subject: str,
    candidate_object: str,
) -> bool:
    """True when the candidate states the relation the other way round:
    "Globex no longer uses Salesforce" against "Salesforce serves_as_crm_for
    Globex". Found by the MCP acceptance run (2026-09-27): the extractor often writes
    both directions, and closing one left the other true forever. Only a
    reason to ASK the judge."""
    return (
        _n(new_object) == _n(candidate_subject)
        and compatible_objects(new_subject, candidate_object)
    )


def same_source(source: object, candidate_sources: Optional[Iterable]) -> bool:
    """True when the candidate was stated by the source (note, memory) that is
    stating the new fact, as its first source or as a reconfirmation: then it
    is never put in front of the judge (rule 3, Graphiti §2.2.3). A write with
    no source (a triple posted directly) excludes nothing."""
    if source is None:
        return False
    f = str(source).strip().lower()
    return any(
        str(s).strip().lower() == f
        for s in (candidate_sources or ())
        if s is not None
    )


def new_fact_for_the_judge(
    subject: str,
    predicate_raw: str,
    obj: str,
    *,
    functional: bool = False,
    past: bool = False,
    example: Optional[str] = None,
    evidence: Optional[str] = None,
    since: Optional[str] = None,
) -> str:
    """The NEW fact as the judge reads it. A past-tense statement says so
    explicitly, because that is exactly what makes the facts that depend on it
    stop being true: PAST_MARK after it (3.3.0; until 4.0.0 a `marca`
    parameter let the personal-memory judge keep " (no longer true)"). `evidence`: the
    words it was read from, on a line of their own (rule 4, RULE_WORDS).
    `since` (2.12.0, rule 5): since when it holds, "(since YYYY-MM-DD)" as on
    the earlier facts."""
    end = PAST_MARK if past else ""
    since_suffix = f" (since {since})" if since else ""
    row = f"{subject} {predicate_raw} {obj}{tag(functional, example)}{since_suffix}{end}"
    words = " ".join((evidence or "").split()).replace('"', "'")
    return f'{row}\n  said as: "{words}"' if words else row


def same_arguments(
    new_subject: str,
    new_predicate: str,
    new_object: str,
    candidate_subject: str,
    candidate_predicate: str,
    candidate_object: str,
) -> bool:
    """True when a candidate has the same subject and a compatible object as
    the new fact but a different relation: a reason to ask the judge (rule 2)."""
    return (
        _n(new_subject) == _n(candidate_subject)
        and compatible_objects(new_object, candidate_object)
        and (new_predicate or "") != (candidate_predicate or "")
    )


# 5.6.0 (18 Oct 2026): a relation's
# example is an end user's value, up to 500 characters, and it was printed in
# the prompt as written, line breaks included, so a value could open a line of
# its own and read as one of the prompt's lines (a candidate, a rule): text to
# be judged taken for the judge's instructions. Spotlighting (Hines, Lopez,
# Hall, Zarfati, Zunger, Kiciman, "Defending Against Indirect Prompt
# Injection Attacks With Spotlighting", arXiv:2403.14720, 2024, section III)
# marks untrusted input so the model can tell it from the instructions; its
# delimiting, datamarking and encoding change every prompt, so the measured
# judge, and are not taken here. This is the step that leaves every prompt of
# the benches the same: the example stays on its line (its line breaks become
# spaces) and is cut at EXAMPLE_MAX characters. On every bench database of 18
# Oct 2026 no example had a line break and the longest had 120 characters.
EXAMPLE_MAX = 160


def example_on_one_line(example: Optional[str]) -> str:
    """A vocabulary example as the prompts print it: one line, at most
    EXAMPLE_MAX characters. Unchanged when it is already one short line."""
    rows = (example or "").strip().splitlines()
    text = " ".join(r.strip() for r in rows if r.strip()) if len(rows) > 1 else (rows[0] if rows else "")
    return text[:EXAMPLE_MAX].rstrip()


def tag(functional: bool, example: Optional[str] = None) -> str:
    """The tag the judge reads next to a relation that holds one value, with
    the value the cardinality was measured on when it is known: typed
    functionality (Lin, Mausam, Etzioni, EMNLP 2010 §3). Measured on the MCP
    acceptance run (2026-09-27): with the bare tag the judge closed "subscribes_to
    Slack" when "subscribes_to Starter plan" arrived."""
    if not functional:
        return ""
    example = example_on_one_line(example)
    return f" [single-valued for values like: {example}]" if example else " [single-valued]"


def line_for_the_judge(
    fact_id: object,
    subject: str,
    subject_canonical: Optional[str],
    predicate_raw: str,
    obj: str,
    since: Optional[str],
    functional: bool = False,
    example: Optional[str] = None,
    other_subject: bool = False,
) -> str:
    """One EXISTING fact as the judge reads it. The canonical name is shown
    with the written one: "Korely (then: Gordon) uses Groq" and "Korely uses
    Deepgram" are about the same entity, and the judge must see that. A
    single-valued relation carries its tag (see RULE_SINGLE_VALUE)."""
    name = subject
    if subject_canonical and _n(subject_canonical) != _n(subject):
        name = f"{subject_canonical} (then: {subject})"
    since_suffix = f" (since {since})" if since else ""
    other = OTHER_SUBJECT_TAG if other_subject else ""
    return f"- id={fact_id} | {name} {predicate_raw} {obj}{tag(functional, example)}{other}{since_suffix}"


# 5.8.0, off (`judge.judge(mark_other_subject=)`, `judge_batch`): an
# EXISTING fact whose subject is named otherwise than the NEW fact's, and is
# not one entity for the store (no alias makes the two canonical names one),
# carries a tag, and the judge reads one rule more. Ritter, Downey, Soderland,
# Etzioni, EMNLP 2008 §4 (read on 7 Oct, quoted above RULE_SINGLE_VALUE):
# AuContraire filters out seeming contradictions "by reasoning about ...
# argument ambiguity" before it applies functionality. Measured cause (the
# identity bench of 17 Oct, qid6 and qid7): the judge closed the user's
# facts with a namesake colleague's ("Marco Bianchi works the day shift"
# closed "marco-n does night shifts"), 2-3 times a run, with the rule above
# telling it that a subject phrased differently is the same entity. A tag for
# the judge to weigh, never a rule that decides on its own (the single-value
# tag's principle, Lin, Mausam, Etzioni, EMNLP 2010 §3).
OTHER_SUBJECT_TAG = " [another subject]"
RULE_OTHER_SUBJECT = (
    "- An EXISTING fact marked [another subject] names its subject differently from the\n"
    "  NEW fact, and nothing known makes the two names one entity. A first name, a\n"
    "  nickname or an id can belong to two different people or things: judge it as\n"
    "  about another entity unless one of the two facts itself says they are the same."
)


def other_subject(subject: str, subject_canonical: Optional[str], new: Optional[str]) -> bool:
    """Whether an EXISTING fact's subject is another name than the NEW fact's
    canonical subject `new` (aliases already applied on both sides)."""
    if not new:
        return False
    return _n(subject_canonical or subject) != _n(new)


# ---------------------------------------------------------------------------
# The judge's questions. One text per engine shape, identical in every copy.
# ---------------------------------------------------------------------------

# From korely-agent (2026-09-14), measured there: without the tag the judge read
# "customer on_plan Business" against "customer on_plan Standard" the way it
# reads "likes fish" against "likes meat", and answered "compatible" for both.
# The second half is typed functionality (Lin, Mausam, Etzioni, EMNLP 2010 §3):
# the tag is learned per relation, one value at a time holds only among values
# of the same kind, and the judge is the one who sees both values.
# 3.3.0 (2026-10-07): before one value can replace another, the two must be
# different values, and the same thing named more or less precisely, or a part
# and the whole that contains it, are not. Ritter, Downey, Soderland, Etzioni,
# EMNLP 2008, read on 7 Oct (ACL Anthology D08-1002): AuContraire first
# "Filter[s] out seeming contradictions ... by reasoning about synonymy,
# meronymy, argument types, and argument ambiguity" (§4) and only then applies
# functionality; "genuine contradictions between y-values sharing a meronym
# relationship are extremely rare" (§4, Meronyms: "Mozart was born in
# Salzburg" does not contradict "... in Austria"). Measured cause: on LoCoMo
# conversation 26 (7 Oct) with a single-valued `family`, "husband
# and kids" was closed by "2 younger kids", then "two children", then "kids":
# the tag sentence above said a new value of that kind IS a contradiction, and
# beat the restatement rule of RULE_TRUE_TOGETHER. Examples invented, far
# from the measured cases and from LoCoMo.
RULE_SINGLE_VALUE = (
    "- A fact tagged [single-valued for values like: X] holds ONE value at a time\n"
    "  among values of the same kind as X. A new value OF THAT KIND for the same\n"
    "  relation replaces it, so it IS a contradiction even when both values look\n"
    "  reasonable: \"customer on_plan Business [single-valued for values like:\n"
    "  Business]\" vs \"customer on_plan Standard\" -> contradiction, because a\n"
    "  customer is on one plan. A value of ANOTHER kind is another sense of the\n"
    "  relation and does not replace it: a customer on one plan can also subscribe\n"
    "  to several services. A value that names the SAME thing, more or less\n"
    "  precisely, or a part of it, or a whole that contains it, is not a new\n"
    "  value: \"Maria lives_in Turin [single-valued for values like: Turin]\" vs\n"
    "  \"Maria lives_in Piedmont\" -> NOT a contradiction (Turin is in Piedmont),\n"
    "  and vs \"Maria lives_in the old centre of Turin\" -> NOT a contradiction\n"
    "  (the same city, said more precisely). A bare [single-valued] tag means the\n"
    "  kind is unknown: judge the kind from the two values. Without a tag, assume\n"
    "  the relation admits many values."
)

# Rule 4 (2.5.0): the words of the new fact decide over the tag and over the
# examples. Described by what the words assert, never by the words that signal
# it (the rule of 2026-09-27); Ritter et al. (EMNLP 2008) for why the context
# and not the shape of the pair separates genuine contradictions.
RULE_WORDS = (
    "- The NEW fact may come with the words it was read from (said as: \"...\").\n"
    "  The triple is a summary of those words, and a summary can lose what decides\n"
    "  the case. Judge what the words assert: when they present the new value as\n"
    "  holding TOGETHER with an existing one (an addition, a second one at the\n"
    "  same time, a part of it), it is NOT a contradiction, even when the relation\n"
    "  is tagged [single-valued] or an example above calls it exclusive; when they\n"
    "  present it as taking the place of the existing one, it is. The tag says\n"
    "  what usually happens, the words say what happened."
)

# Rule 5 (2.12.0): the order of writing is not the order of time. The verdict
# is asked as before, as if the NEW fact were the latest: the question the
# judge was measured on (17/18 and 7/7, korely-agent 1051c67) showed the new
# fact with no date, so the judge read it as the latest. The dates only
# decide which fact ended, in `judge.close` (Graphiti's split, rule 5
# above). Measured on 6 Oct evening against the question without dates
# (the judge of 2.11.0 and of 2.12.0 on the same extracted facts, Gemini 3.1
# Flash-Lite): in the
# order of time the same results (batched 54/60 both, per fact 22/24 both);
# with the older memory written last 44/60 against 39/60 batched, 18/24
# against 17/24 per fact, and no genuine change missed that the question
# without dates caught.
RULE_DATES = (
    "- \"(since YYYY-MM-DD)\" is when a fact began to hold. The NEW fact is the last\n"
    "  one written, not always the latest in time: it may be older than an EXISTING\n"
    "  fact, because memories can arrive in any order. Give the verdict you would\n"
    "  give if the NEW fact were the latest: the dates decide which of the two\n"
    "  facts ended and when, never whether they contradict."
)

# Rule 6 (3.2.0): a contradiction is two facts that cannot both be true at
# once; what happens later, a second value of the kind, another side of an
# identity and the same fact in other words do not close what was true. The
# sources and the measurement are in the module docstring (rule 6). Before
# RULE_SINGLE_VALUE, which it does not change: the tag still makes a new
# value of the same kind a replacement. In the two Agents judges only.
# The example of a switch ("prefers coffee" vs "switched to herbal tea")
# came from the measurement: without it the batched judge (Gemini 3.1
# Flash-Lite) stopped closing a preference when the new fact said only what
# it switched to ("prefers TypeScript" against "uses Rust", said as
# "Actually I switched to Rust", case typescript_rust of the measurement: 14
# times in 14 with 3.1.3, 7 in 11 with a habit as the example, 8 in 8 with
# the preference written here). Measured as written here with the judge
# alone, fresh ids at every call (6 Oct, the batched judge as in
# production, a past statement on its own; 20 pairs: the changes and
# additions of the judge's bench, feelings, works, tags, past
# statements): Gemini 3.1 Flash-Lite 77 right in 80 (3.1.3: 69), and no
# pair worse; gpt-oss-120b in the version notes of the package.
RULE_TRUE_TOGETHER = (
    "- What happened (an event, an action, a feeling or a reaction of one moment)\n"
    "  does not make an earlier state, preference, feeling or identity false,\n"
    "  unless it says that the earlier one ended or changed: \"Maria loves the\n"
    "  Uffizi\" vs \"Maria visited the Uffizi\" -> NOT a contradiction (a visit\n"
    "  does not end a love of the place); \"Maria feels grateful to her\n"
    "  neighbours\" vs \"Maria feels nervous before her exam\" -> NOT a\n"
    "  contradiction (two moments); \"Maria owns a red bicycle\" vs \"Maria sold\n"
    "  her red bicycle\" -> contradiction (selling it ends owning it); \"Maria\n"
    "  prefers coffee\" vs \"Maria switched to herbal tea\" -> contradiction\n"
    "  (switching to another one ends the earlier preference).\n"
    "- A second preference, a second work or another side of the same identity\n"
    "  adds to the first: \"Maria likes jazz\" vs \"Maria likes opera\", \"Maria\n"
    "  wrote a poem about her mother\" vs \"Maria wrote a poem about the sea\"\n"
    "  (two poems), \"Maria is Sicilian\" vs \"Maria celebrates her Sicilian\n"
    "  roots\" -> NOT contradictions.\n"
    "- The same fact said in other words is NOT a contradiction (a restatement,\n"
    "  not a change): \"Maria has two daughters\" vs \"Maria is the mother of two\n"
    "  girls\"."
)

# Rule 6, second half (3.2.0): "(no longer true)" says the NEW fact is over,
# not that what it touches is. The marker exists for a state that ended
# (`new_fact_for_the_judge(past=True)`; MCP acceptance run of 27 Sep:
# "Globex no longer uses Salesforce" must close "Salesforce serves_as_crm_for
# Globex"), and the extractor also writes as past an event that is finished
# (a visit to a place, a picture painted). Read on two writes of
# LoCoMo conversation 26 (6 Oct, the tense of each closing fact): 15 of the 27
# closures with Gemini and 9 of the 20 with gpt-oss came from a past-tense new
# fact, judged on its own (a past statement is judged one by one even in a
# batched write, `worker.write_memory`), and none of them was a change.
# TimeML (above): an OCCURRENCE that is over still happened, a STATE that is
# over has ended. Measured with the judge alone (6 Oct, Gemini 3.1
# Flash-Lite, fresh ids at every call): a love of a place against a past
# visit to it, kept 0 times in 4 by the judge of 3.1.3 and 4 in 4 with this
# rule; a past state ("no longer uses", "replaced the plan") still closes 4
# in 4. With gpt-oss-120b the judge of 3.1.3 already kept it (3 in 3).
# Only in AGENTS_JUDGE: the batched judge never shows the marker
# (`judge.batch` renders no past statement).
#
# 3.3.0 (8 Oct, the closures of the 3.3.0 pack): the marker of the Agents'
# judge is PAST_MARK, not "(no longer true)". With gpt-oss-120b the
# words "no longer true" after an event read as a negation of the event
# itself ("she did not make the painting", "the creation of the bowl is no
# longer true, negating the earlier claim"), and the facts about the activity
# closed: in the gpt-oss arms of the pack every closure by a past fact was
# this, with the rule above in the prompt. A judge-only bench (the 6 groups
# of the pack, an invented visit-vs-love case, and 4 states that ended which
# must still close, among them a twin of the same relation; 3 runs each,
# 0.11 $): wrong verdicts with gpt-oss 18 of 33 -> 3 of 33, with Gemini
# 3 of 33 -> 0 of 33; the four that must close closed every time with every
# wording. The marker says the fact happened or held and is over, and the
# rule says it never means "did not happen" (TimeML, above: an OCCURRENCE
# that is over still happened). A worked example of the failure added to
# the rule did not help: with Gemini it made "went through" close again.
# The old marker and the 3.2.0 rule (`_MARCA_NON_PIU_VERO`,
# `_REGOLA_NON_PIU_VERO_3_2`), kept for an earlier product's judge and its
# measurement scripts, left with 4.0.0.
PAST_MARK = " (said in the past: it was true, and it is over)"
RULE_NO_LONGER_TRUE = (
    "- \"(said in the past: it was true, and it is over)\" after the NEW fact\n"
    "  says that the new fact itself happened or held, and is over. It never\n"
    "  means that the new fact is false or did not happen. A state that is over\n"
    "  can end what depended on it; an event that is over (a visit, a trip,\n"
    "  something made or attended) still happened, and being over ends nothing:\n"
    "  \"Maria loves cooking\" vs \"Maria baked a lemon cake (said in the past: it\n"
    "  was true, and it is over)\" -> NOT a contradiction."
)

# The agent memory: candidates may name one entity several ways, so the judge
# resolves coreference first (Graphiti entity resolution, ConflictBank).
AGENTS_JUDGE = """\
You judge whether a NEW fact contradicts EXISTING facts about the SAME real-world entity.

Output JSON ONLY:
{
  "contradictions": [
    {"existing_id": "<id from the existing list>", "reason": "<short why>"}
  ]
}

Rules:
- FIRST decide if the two facts are about the same real-world entity. The subject
  may be phrased differently for the same entity ("Maria", "the user", "she", "the
  runner", "the customer" can all be one person) — treat those as the same entity.
- Do NOT conflate distinct entities: "Maria" vs "Maria's company", "Maria" vs
  "John", a person vs their pet / project / employer are DIFFERENT entities and
  can never contradict each other.
- Given the same entity, a contradiction exists when the new fact implies the
  existing one is no longer true.
- "Maria likes fish" vs "Maria doesn't like fish anymore" -> contradiction.
- "Maria likes fish" vs "Maria likes pesce" -> NOT a contradiction (synonyms).
- "Maria likes fish" vs "Maria likes meat" -> NOT a contradiction (compatible).
- "the runner's personal best is 27:12" vs "Maria's personal best is 25:50" ->
  contradiction WHEN the runner is Maria (an exclusive single best time).
- "Maria works at Apple" vs "Maria works at Google" -> contradiction (exclusive).
- "Maria lives in Rome" vs "Maria's company is in Milan" -> NOT a contradiction
  (different entities: Maria vs her company).
{RULE_TRUE_TOGETHER}
{RULE_NO_LONGER_TRUE}
{RULE_SINGLE_VALUE}
{RULE_WORDS}
{DATE}
- Return an empty contradictions array when nothing contradicts.

EXISTING facts:
{EXISTING}

NEW fact:
{NEW}
""".replace("{RULE_TRUE_TOGETHER}", RULE_TRUE_TOGETHER).replace("{RULE_NO_LONGER_TRUE}", RULE_NO_LONGER_TRUE).replace(
    "{RULE_SINGLE_VALUE}", RULE_SINGLE_VALUE).replace("{RULE_WORDS}", RULE_WORDS).replace("{DATE}", RULE_DATES)

AGENTS_JUDGE_BATCH = """\
You judge whether NEW facts contradict EXISTING facts about the SAME real-world entity.

Output JSON ONLY:
{
  "results": [
    {"new_id": "<new_id from the list>", "contradictions": [{"existing_id": "<id>", "reason": "<short why>"}]}
  ]
}

Rules (apply to EACH new fact independently):
- FIRST decide if the two facts are about the same real-world entity. The subject
  may be phrased differently for the same entity ("Maria", "the user", "she", "the
  runner", "the customer" can all be one person) — treat those as the same entity.
- Do NOT conflate distinct entities: "Maria" vs "Maria's company", "Maria" vs
  "John", a person vs their pet / project / employer are DIFFERENT entities and
  can never contradict each other.
- Given the same entity, a contradiction exists when the new fact implies the
  existing one is no longer true.
- "likes fish" vs "doesn't like fish anymore" -> contradiction.
- "likes fish" vs "likes pesce" -> NOT a contradiction (synonyms).
- "likes fish" vs "likes meat" -> NOT a contradiction (compatible, multi-value).
- "works at Apple" vs "works at Google" -> contradiction (exclusive).
- "lives in Rome" vs "company is in Milan" -> NOT a contradiction (different entities).
{RULE_TRUE_TOGETHER}
{RULE_SINGLE_VALUE}
{RULE_WORDS}
{DATE}
- Each new fact maps to its OWN candidate list; never cross candidates between new facts.
- Return an empty contradictions array for a new fact when nothing contradicts.
- Include every new_id in results, even with empty contradictions.

NEW facts and their candidate EXISTING facts:
{BLOCKS}
""".replace("{RULE_TRUE_TOGETHER}", RULE_TRUE_TOGETHER).replace("{RULE_SINGLE_VALUE}", RULE_SINGLE_VALUE).replace(
    "{RULE_WORDS}", RULE_WORDS).replace("{DATE}", RULE_DATES)

# The two questions with RULE_OTHER_SUBJECT (5.8.0, off): the rule before
# the last lines, the rest byte for byte.
AGENTS_JUDGE_OTHER_SUBJECT = AGENTS_JUDGE.replace(
    "- Return an empty contradictions array when nothing contradicts.",
    RULE_OTHER_SUBJECT + "\n- Return an empty contradictions array when nothing contradicts.")
AGENTS_JUDGE_BATCH_OTHER_SUBJECT = AGENTS_JUDGE_BATCH.replace(
    "- Each new fact maps to its OWN candidate list;",
    RULE_OTHER_SUBJECT + "\n- Each new fact maps to its OWN candidate list;")


def merge_candidates(*lists: Sequence) -> list:
    """Union of candidate lists, first occurrence wins, order kept (the most
    precise signal first in what the judge reads)."""
    seen = set()
    out = []
    for listed in lists:
        for c in listed:
            k = getattr(c, "id", c)
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
    return out
