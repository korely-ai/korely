"""memory_core: the PURE logic of the facts engine.

Why it exists (2026-09-27). The facts engine lived in three copies: an
earlier personal-memory product, hosted Agents and self-hosted Agents (the
`korely-agent` repository). From September every fix was written twice by
hand, and the copies went opposite ways: the self-hosted one had learned the
predicate vocabulary, the cloud one the aliases and the fact's time, and
neither had what the other had.

This folder is the part that CAN be one: rules, questions to the model and the
reading of its answers, weights, and the database steps that apply them. No
model client, no application models or settings, no import from outside the
folder except the standard library and SQLAlchemy 2: that is how the same
folder ran inside another application and inside `korely_agent` without
changing a byte. Since 11 October 2026 it lives only here.

Where these comments name code of the engine's earlier homes
(`agent_facts/...`, one of their commits), it is the history of a rule, kept
for why the rule exists.

Who uses the database (FIX-DOC 2.11.0: this said "no database" here, and it
had not been true since September; an audit of 2.10.0): `store`,
`candidates`, `twins`, `judge`, `worker`, `writing`, (2.12.0)
`vector_search` and (6.7.0) the SQL form of `owner` work with
SQLAlchemy 2 (korely-agent pins `sqlalchemy>=2,<3`) on the tables the engine
passes (`store.Tables`, `Memory`, the class of the memories), never on models
imported from the application. The other modules are pure logic, standard
library only.

Maintenance rule (since 11 October 2026):
  * it is changed ONLY here, in korely-agent, after review, and every
    rule cites its paper (CONTRIBUTING.md);
  * the same change bumps VERSION (below) and regenerates
    `MANIFEST.json` with `python scripts/memory_core_manifest.py`;
  * `tests/test_memory_core_shared_rules.py` recomputes the hashes and fails if
    a file does not match the manifest: a change that skips that step shows on
    the first test run.
Until 10 October it was changed only in its earlier home, and a script copied
it here byte for byte. The structural step (a real package instead of a copied
folder) was decided on 11 October 2026.
"""

# VERSION follows SemVer 2.0.0 (semver.org, §6-8), on the public surface of the
# folder (the functions korely_agent imports; until 10 October 2026 another
# application too):
#   PATCH  a fix that changes neither signatures nor what is printed;
#   MINOR  something new and compatible (an optional parameter, a line printed
#          differently for a case that was wrong before);
#   MAJOR  an existing caller has to change.
# It is bumped in the same change as the file, together with MANIFEST.json
# (scripts/memory_core_manifest.py).
#   1.1.0 (2026-09-28): presentation.mcp_line(f, now=None) prints
#         "[until DATE]" for a future end instead of "superseded"; the dates
#         of mcp_line and context_line are the UTC day of the instant, not the
#         day in the time zone of the database session.
#   1.2.0 (2026-09-28): a failed call is no longer an answer.
#         New `fact_extractor.extract_batch_answers`: a memory the model left
#         out of the batch (or without its `facts` list) is absent from the
#         result, and the caller asks for it alone instead of marking it done
#         with zero facts; `extract_batch` stays as it was (the missing one
#         counts as []). store.resolve_predicate: an `embed` that RAISES on
#         Postgres is a failed resolution (strict: ResolutionUnavailable;
#         otherwise the surface form, nothing cached); an `embed` that returns
#         None stays "no embedding".
#   1.3.0 (2026-09-28): an unusable answer from the vocabulary is no longer a
#         decision, if the caller asks for that. New optional parameter
#         `severe` of store.resolve_predicate and of the four vocabulary
#         readers (read_same_relation, read_single_value, read_family,
#         read_phrase_tense); new classes vocabulary.UnreadableAnswer and
#         store.ResolutionUnusable (NOT a subclass of ResolutionUnavailable:
#         failed call = retry, unusable answer = no). With `severe` an empty
#         answer, one that is not JSON or one outside the options does not
#         become "new relation", "undecided", "other" or "not past" in the
#         cache: it raises (strict) or returns the surface form without
#         caching. Default unchanged: MINOR. The Cloud uses it in both engines;
#         korely-agent keeps the old behaviour until it passes `severe=True`
#         (and updates the tests that pin the lenient reading).
#   1.4.0 (2026-09-28): new module `candidates` (piece 2 of
#         the shared-code plan): `for_the_judge` gathers the facts the
#         contradiction judge reads, the same stages the two Agents engines
#         had written twice. The caller passes `scope` (who may come in) and
#         `is_open`; the filter on objects is the `filtro_oggetti` option (the
#         self-hosted one turns it on with `OGGETTI_MAX_DIST`, the cloud one
#         not until it is measured). Nothing changes for whoever does not
#         import it: MINOR.
#   1.5.0 (2026-09-28): new module `judge` (piece 3, first half): the call to
#         the contradiction judge with `call(prompt, max_tokens)`, the reading
#         of the verdict (JudgementMissing and JudgementUnreadable: a judge
#         that does not answer is not a verdict), the batch and the bitemporal
#         close. The batch uses the same ceiling as the single call,
#         MAX_TOKENS 8192 (in the cloud it was min(2048, 256 + 96 per group)).
#         Nothing changes for whoever does not import it: MINOR.
#   1.6.0 (2026-09-28): new module `writing` (piece 3, second half):
#         `write_fact` and `ToJudge`, the steps of writing a fact and their
#         order (canonical names, reconfirmation, past, insert with a
#         savepoint, alias from identity, vector before the judge). The engine
#         passes columns, the resolved relation, `is_open`, `embed` and
#         `judge`. Nothing changes for whoever does not import it: MINOR.
#   1.7.0 (2026-09-28): new module `worker` (piece 4): the event time and
#         the drain order, the claim with the recovery of stuck rows and FOR
#         UPDATE SKIP LOCKED (the cloud claimed without a lock: two drains at
#         once extracted the same memory twice), the writing of a memory (fact
#         by fact or with the batched judge) and the drain of one memory.
#         MINOR.
#   1.8.0 (2026-09-28): `judge.after_the_vector`: a fact
#         whose vector arrives late is judged against the older facts that
#         only the semantic stage finds, and the facts written after it, which
#         could not find it, against it; a single call. `judge.close` writes
#         the closes at once (autoflush off in both engines: a second `close`
#         of the same fact closed it again) and `judge.batch` closes in order
#         of valid time (the first change closes the old fact, not the last
#         one in the model's answer). MINOR.
#   2.0.0 (2026-09-28): the filter on objects removed (`filtro_oggetti` of
#         `candidates.for_the_judge` and `OGGETTI_MAX_DIST`), measured and
#         dropped: a real replacement ("uses Jira" -> "uses Linear") is at
#         distance 0.217 and would not have reached the judge, and on the
#         corpus it saved no question. MAJOR by the rule (a parameter
#         removed), even though nobody used it: introduced in 1.4.0 tonight,
#         never released. The self-hosted one, which had the filter in its own
#         code, loses it by adopting `for_the_judge`.
#   2.1.0 (2026-09-28): blind installs of 0.1.13: 4 new installs
#         out of 21 left two plans open. The question "one value at a time?"
#         is TYPED when the relation has been seen with a value (Lin et al.
#         2010 §2): for "subscribes to ... plan" from 3 errors out of 10 to 0
#         out of 115.
#         New: `vocabulary.decide_single_value` (majority of
#         SINGLE_VALUE_VOTES answers, self-consistency, Wang et al. 2023), the
#         optional parameter `single_value_votes` of
#         `store.resolve_predicate` (default 1 = as before; the Cloud's engines
#         pass 3) and `store.reclassify_single_value` for the memories already
#         poisoned (only the relations saved with an example: without one, the
#         general question, the wrong one, would remain).
#         Wider measurement: 0 errors out of 160, and works_on, plans,
#         has_skill, requires, attended stay many-valued. MINOR: the question
#         changes its text for a case that was wrong before; the signatures
#         stay.
#         NB (29 Sep): the measurement covered only plans among the
#         "one value" cases; "works_at Globex" comes back "many" 11 times out
#         of 20. Open, measurement ready.
#   2.2.0 (2026-09-29): an unreadable answer no longer makes the write fail at
#         the first try. With `severe`, a vocabulary answer that could not be
#         read dropped the facts of the whole memory (3 writes out of ~450 in
#         the LongMemEval measurement of 29 Sep), and the three votes made it
#         three times as likely. New: `vocabulary.ask_and_read` (asks again,
#         thinking, up to RETRIES_UNREADABLE times, then raises as before),
#         used by `decide_single_value` and by the three readings of
#         `store.resolve_predicate`; `_json` reads an object repeated with text
#         around it if every copy says the same thing. MINOR: no signature
#         changes; some answers that were unreadable before are read now.
#   2.3.0 (2026-09-29): three promises of the public documentation that the
#         conformance acceptance test in production found broken. (1) An
#         edited memory left the facts of the old version open:
#         `store.retire_the_previous_version` closes them when the new one
#         arrives (or removes this source, if others state them), called by
#         `worker.write_memory(retire=)` in the same transaction.
#         (2) `valid_from` was the time of the extraction: `worker.arrival(m)`
#         and `writing.write_fact(arrival=)`, the last choice before now (the
#         date in the text still wins). (3) The context repeated the same
#         memory and showed the superseded ones:
#         `presentation.memories_for_the_context` (duplicates on the
#         normalized text; out goes a memory whose facts are all no longer
#         true; in stays one with no facts) and `store.state_of_the_source`.
#         MINOR: new optional parameters, new functions; the behaviour changes
#         on purpose on the three points (it touches the context
#         measurements).
#   2.4.0 (2026-09-29): the distance thresholds belong to the model that makes
#         the vectors (the decision: EmbeddingGemma-300m instead of
#         Gemini). New `thresholds`:
#         `use_model(name or function)` once at startup, `current()` at every
#         use, `for_model`, `family`, `FAMILIES` and
#         `ModelWithoutThresholds` (a model never measured stops at startup,
#         it does not take another model's numbers). Gemini 0.45 and 0.65 as
#         before; EmbeddingGemma-300m at full precision 0.67 and 0.67,
#         measured on production data. `candidates.for_the_judge` (stage 1b),
#         `judge.after_the_vector` and `store._candidates` read
#         `thresholds.current()`; SEMANTICA_MAX_DIST and SHORTLIST_MAX_DIST
#         stay, with Gemini's values, for whoever imports them. MINOR: without
#         `use_model` the model is gemini-embedding-2 and nothing changes.
#   2.5.0 (2026-10-01): the judge reads the words the fact comes from, and the
#         question "one value at a time?" is asked the way the judge uses it.
#         (1) Every extractor asks for `evidence`, the words of the text that
#         state the fact, copied (`extraction.RULES_EVIDENCE`, JSON_FIELDS);
#         `extraction.read_evidence` keeps them only if they appear in the
#         text (`comparable`: words of every script, capitals and punctuation
#         do not count), otherwise None and the judge reads the triple as
#         before. `ExtractedFact.evidence`, `writing.write_fact(evidence=)`
#         and `ToJudge.evidence`, `judge.judge(evidence=)`, the "evidence" key
#         of the facts of `judge.batch` (from
#         `worker._new_for_the_batch(..., extracted)`). The words are not
#         saved on the fact: a forgotten memory leaves no copy of them.
#         (2) `contradiction.new_fact_for_the_judge(evidence=)` prints them
#         under the new fact ("said as"), and RULE_WORDS, in the three judges,
#         says they decide, above the tag and the examples (rule 4, Ritter et
#         al. EMNLP 2008; BUG-200, "also works part-time at Google" closed
#         "works at Apple"). (3) `vocabulary.single_value_question` with a
#         value asks whether a new value of that kind USUALLY replaces the old
#         one (30 Sep: 34 wrong answers out of 40 on job and manager with the
#         2.1.0 question, 0 with this one; plans, residence and spouse stay
#         right; "requires a responsible person" becomes "one value" 8 times
#         out of 10, and the judge sees the exception with the words). MINOR:
#         new optional parameters, a new field in the extraction schema; the
#         behaviour changes on purpose (to be measured with the memory
#         acceptance test before syncing). The relations already classified
#         with the old question stay as they are until they are reclassified
#         (`reclassify_single_value`).
#         (4) In the examples of the personal-memory judge "Gordon" and
#         "Korely" become made-up entities (Acme, Novaro and Lumio; "Gordon"
#         disappears from the texts given to the model, 30 Sep), chosen outside
#         the cases of the acceptance tests.
#   2.5.1 (2026-10-01 evening): `store.state_of_the_source` counts "still
#         standing" also a fact stated in the past and not superseded by a
#         newer fact (`tense == "past"` and `invalidated_by_id` empty): it is
#         history, born closed by `writing`, not something contradicted.
#         Before, rule (b) of `presentation.memories_for_the_context` took the
#         memories of things that happened out of the context (found and
#         measured on LongMemEval run A: evidence in the context
#         from 74% to 80% at 2,000 tokens, from 85% to 94% at 4,000). PATCH:
#         no signature changes; which memories enter the context changes on
#         purpose.
#   2.6.0 (2026-10-02 night): the better write, to be turned on engine by
#         engine (the decision: measure it tonight on LongMemEval
#         run A). New: `fact_extractor.extract_with_observation` and
#         `extract_batch_with_observation` (the same facts and the same rules,
#         plus, in the same call, an "observation": a short sentence about the
#         person, with the names, numbers and dates of the text; the model
#         reads the previous memories for the pronouns and the memory's date
#         for relative times; no general-knowledge facts explained by the
#         assistant if the person has nothing to do with them), `Extraction`,
#         `RULES_OBSERVATION`, and `presentation.memory_line` (the dated
#         sentence instead of the whole turn, the old text if the memory has
#         no sentence). Mem0 (Chhikara et al. 2025, sec. 2.1) and LongMemEval
#         (Wu et al., ICLR 2025, sec. 5). MINOR: the earlier functions and
#         their prompts stay identical byte for byte.
#   2.6.1 (2026-10-02 night): the previous memories as in Mem0, 10 of 300
#         characters (`storage.get_last_messages(limit=10)`,
#         `PAST_MESSAGE_TRUNCATION_LIMIT = 300`), instead of 5 of 500. PATCH:
#         2.6.0 is not in use anywhere yet.
#   2.7.0 (2026-10-02 evening): `worker.claim_pending(..., filter_condition=...)`,
#         one more condition on the memories to claim, from the calling
#         engine. The cloud leaves out the accounts that have used up the
#         month's AI budget (10% of the price): their
#         memories wait for the next month without taking the turn from the
#         others. MINOR: without `filter_condition` the behaviour is
#         identical.
#   2.8.0 (2026-10-05 evening): the work items of 5 October. On:
#         - `store.states_of_the_sources(db, tables, sources, *, is_open, scope)`:
#           the state of the context's memories in one query (BUG-367);
#         - `vocabulary.same_relation_question`: a plan and the thing done, a
#           relation that lasts and an event, are different relations
#           (BUG-365, TimeML);
#         - the hybrid block: `presentation.WHOLE` (5), `POOL` (40),
#           `block_line`; `memory_line` puts the day in front of every line,
#           whole text included (LoCoMo conv-26: 107 -> 121 out of 152);
#         - `writing.write_fact`: the last confirmation is when the fact was
#           stated (Graphiti 2.2.3, the T timeline), and `store.reconfirm`
#           never goes back;
#         - the `instructions` family (predicate `instructs`): structural in
#           `resolve_predicate`, never chosen by the vocabulary (with
#           `identity`).
#         Off, to be turned on after a measurement (whoever does not pass them
#         gets the earlier prompts and writes, byte for byte):
#         - `fact_extractor.extract_with_observation` / `_batch_`:
#           `istruzioni` (the standing rules, CoALA 4.1), `eventi` (the
#           events mentioned in passing, TimeML OCCURRENCE), `absolute_dates`
#           (relative times written as dates, TimeML TIMEX3);
#         - `write_fact(twins=...)` and the new `twins.py`: the merging of
#           near-duplicates (Graphiti 2.2.2, Broder 1997), with the thresholds
#           in `thresholds.Twin`, measured on EmbeddingGemma only; before
#           turning it on we need the map of the phrasings per source
#           (forgetting).
#         MINOR: new signatures with defaults, lines printed differently where
#         they were wrong before (the whole memory without a day).
#   2.9.0 (2026-10-05 night): the two proposals of 5 October, approved
#         ("yes to everything"). Everything OFF by default (a
#         new rule is turned on only after it has been measured): whoever
#         does not pass the new parameters gets the prompts and writes of
#         2.8.0 byte for byte (the sha256 fingerprints of the 2.8.0 prompts
#         are in the tests), and the old block for every line without
#         relative times.
#         - `relative_time.py`, new: relative dates computed by the code. The
#           model describes every relative time with an object (direction,
#           unit, quantity, day of the week, anchor), never with the date; the
#           code computes the TIMEX3 value from the memory's day. SCATE (arXiv
#           2507.06450, table 2: 0.49 with code against 0.38 with dates
#           written by the model), TReMu (arXiv 2502.01630, table 5), PREMem
#           (arXiv 2509.10852, 3.1). The prompt paragraph, the JSON schema and
#           the reader are in the same file; `times_of_the_day` re-reads a
#           saved column from the day printed in the block, without a model.
#         - `fact_extractor.extract_with_observation` and `_batch_`: `times`
#           asks for "relative_times" in the same call and returns them dated
#           in `Extraction.times` (the column's value); with `absolute_dates`
#           it is a ValueError. The facts' valid_from does not take the
#           annotated day (v1: the `fact_time.date_from_text` guard stays).
#         - `presentation.memory_line` and `block_line`: a line with
#           `relative_times` has the date next to the words, "yesterday
#           [2023-05-07]", in the text and in the observation (at the end,
#           "(ieri: 2023-05-07)", if the observation is in another language).
#         - `turni.py`, new: who said what, read from the SDKs' line format
#           ("<role>: <content>"), never from words (LongMemEval 38146c39: an
#           assistant's suggestion closed the user's taste). `turni` in the
#           five extractions: the user's facts from the user's turns (Mem0
#           USER_MEMORY_EXTRACTION_PROMPT; LoCoMo, appendix A.2.1).
#           `writing.write_fact(dall_assistente=)` and
#           `worker.write_memory(assistant_does_not_close=)`: a fact stated only
#           by the assistant closes nothing (neither with the judge, nor in
#           the past, nor by promoting a plan, nor by merging).
#         Outside v1: the southern hemisphere, intervals ("since 2019"),
#         start/middle/end of a period, the user's time zone (the day is the
#         UTC one, as in the block). In the cloud: AGENTS_RELATIVE_DATES (with
#         AGENTS_OBSERVATION; agent_memories.relative_times),
#         AGENTS_FACTS_FROM_USER_TURNS and AGENTS_ASSISTANT_CLOSES_NOTHING,
#         all off. MINOR: new modules and new parameters with defaults.
#   2.10.0 (2026-10-06 morning): the fair claim across people, from a
#         proposal (korely-agent 6e795ce, which did it with a
#         `filter_condition`). `worker.claim_pending(person=, per_person=)`:
#         at most N memories of each person per claim, each in its own order
#         (the fair queue of Demers, Keshav and Shenker, SIGCOMM 1989). On the
#         whole of LoCoMo the 20 oldest all belonged to two people and the
#         others waited. Off without the two parameters: the claim is the one
#         of 2.9.0. MINOR: new parameters with defaults.
#   2.11.0 (2026-10-06 evening): the defects from the code audit of 6 October
#         and from an audit of 2.10.0.
#         - `worker`, the claim token (Kung and Robinson, ACM TODS 1981,
#           optimistic concurrency control): `Claim`, `claim(db, m)`,
#           `still_its_own`, `close_the_claim`, `release`, `requeue`,
#           `claim_this_one`, `inline`, `FORGOTTEN`. `write_memory(claim=)`
#           writes 'done' only if the row is still the one claimed (being
#           processed, same claim time, same text, not forgotten), otherwise
#           it rolls back the whole write (`claim.outcome == "lost"`).
#           `drain_one` skips a memory that is no longer claimed and writes
#           'error' with the same check. `claim_pending` does not claim
#           forgotten memories and puts to rest (`FORGOTTEN`) the forgotten
#           ones still in the queue. The inline write claims the memory as
#           the drain does. Errors in the log with the stack.
#         - `store.examples_after_the_erasure` (GDPR Art. 17): after an
#           erasure a relation's example stays only if a fact still states
#           it, otherwise it becomes the object of the relation's oldest fact,
#           or nothing, and the centroid is recomputed (or removed: Morris et
#           al., EMNLP 2023).
#         - `store.find_open`, the recovery after the race in
#           `writing.write_fact`, the history in the past, the candidates and
#           the twins: the database's lower() on both sides; the
#           recovery looks for the winner with the index's predicate,
#           `invalid_at IS NULL`.
#         - `model_json` (new): the rule for a model's JSON, read by the
#           extractor and
#           the judge before the engine's reader, which can now return the
#           value as well as the text. An answer without the list of
#           facts raises `ExtractionFailed` (it is not "no facts"); the
#           declared confidence no longer drops a fact (Xiong et al., ICLR
#           2024).
#         - The semantic stage of the candidates and the vocabulary's
#           shortlist in a savepoint; an error is not "no candidates": it
#           raises (`candidates.CandidatesUnread`, `ResolutionUnavailable`
#           with the strict vocabulary). `cosine` swallows only TypeError
#           and ValueError. The package docstring says who uses the
#           database.
#         MINOR: new parameters and functions with defaults; the cases that
#         were wrong change on purpose (a write on a memory changed in the
#         meantime, a fact with low or no confidence, an answer without a
#         list, a failed candidates stage, a name that Python and the
#         database lowercase differently). The Self-hosted adopts the claim by
#         passing `claim=` (see worker).
#   2.12.0 (2026-10-06 evening): more defects from the code audit of 6
#         October and two decisions of that day.
#         - `judge.close`: a fact is never closed before its
#           valid_from; a new fact older than the open one it contradicts is
#           history, closed where the other begins (Graphiti, reference
#           implementation, `resolve_extracted_edge`); on equal dates the new
#           information wins (§2.2.3). The judge reads the new fact's date
#           (`judge(since=)`, the batch, the late vectors;
#           `contradiction.RULE_DATES`, only in the Agents prompts);
#           `after_the_vector` weighs the later facts in their start order and
#           returns the closes in both directions. `worker.claim_pending`
#           claims in event order (`event_order`, coalesce(event_time,
#           created_at)).
#         - `writing.write_fact`: a sentence in the past ends when it is
#           said (`stated`), not on the day of the write, nor at the date in
#           the text (which is the start); the open twin that began after the
#           sentence stays true and the sentence is its history; a plan stated
#           again as a fact goes through the judge (also in a batched write,
#           on its own) and begins when it comes true.
#           `worker.write_memory` in batches also keeps in the answer the
#           closes made fact by fact.
#         - `vector_search` (new; BUG-320): `hnsw_depth(db, pool)`,
#           the rule of the earlier hybrid search (SET LOCAL
#           hnsw.ef_search to the pool, iterative_scan = strict_order from
#           pgvector 0.8.0; pgvector's README), and `nearest(db, q, column,
#           vector, limit)`, the one road of the engine's searches by meaning:
#           the semantic stage of the candidates (inside its savepoint), the
#           neighbours of a late vector and the twins. Outside PostgreSQL the
#           distances are computed in Python. `candidates.cosine` stays (it is
#           `vector_search.cosine`).
#         - `store.retire_the_previous_version(scope=)` and
#           `source_condition(scope=)`: the reconfirmation branch, a
#           LIKE on the JSON text of the sources, limited to the memory's
#           scope; without it, every write read the whole facts table
#           (Parallel Seq Scan, 41 ms on 121,379 facts; with the scope a
#           BitmapOr, 1.0 ms). Without `scope`, as before.
#         - `identity.alias_rule_from_identity` (the decision of 6
#           Oct): no threshold on the declared confidence (Xiong et al., ICLR
#           2024), the categorical criteria stay; `confidence` is accepted and
#           not read. `SOGLIA_CONFIDENZA_ALIAS_AUTOMATICO` goes: the only one
#           that imported it was the earlier product's re-export
#           (changed here); korely-agent does not
#           import it. The identity prompt stays byte for byte the one
#           measured (it still asks for "confidence 0.9+", which no longer
#           decides anything).
#         - `fact_extractor` (the decision of 6 Oct): a memory
#           over MAX_CONTENT_CHARS (6,000) is read in windows (`windows`, at
#           the text's boundaries, 500 characters carried over), one call per
#           window, the facts merged (the same triple once), MAX_FACTS per
#           window; a window that fails makes the memory fail. Up to 6,000
#           characters the same prompt as before. In batches a long memory
#           stays out (the caller asks for it alone); `extract_batch` reads it
#           alone.
#         Written as MINOR (new parameters and modules with defaults; the
#         wrong cases change on purpose: a row closed before it begins, a past
#         true until today, a plan come true that closes nothing, fewer
#         candidates than asked). But it removed a public constant, so it was
#         MAJOR by the rule above: never released nor synced, it ships as
#         3.0.0.
#   3.0.0 (2026-10-06 evening): 2.12.0 with the decisions of 6
#         October (16:45). MAJOR: an existing caller has to change.
#         - Public surface removed: `identity.AUTOMATIC_ALIAS_CONFIDENCE_THRESHOLD`
#           (see 2.12.0). Whoever imports it no longer starts: the earlier
#           product imported it in one place, changed;
#           korely-agent does not import it. It is the reason for the MAJOR.
#         - A plan that comes true (`writing.write_fact`): the plan's row is
#           CLOSED when it comes true (`invalid_at` = the start of the fact,
#           never before its `valid_from`, never after an end it already had;
#           `invalidated_by_id` = the new fact) and the fact that came true is
#           a NEW row, true from then on, with its vector, the identity alias
#           and the judge like every new fact (in the batch, in a batched
#           write). Zep/Graphiti (arXiv:2501.13956, 2.2.3): only t_invalid is
#           written, and the history of the relation stays. `as_of` before it
#           comes true returns the plan, after it the fact. Up to 2.11.0 a
#           single row, promoted in place with the plan's date; 2.12.0 moved
#           the date and lost the plan. What comes back changes: the returned
#           fact is the new row, and the plan's id is among those closed by
#           the write (answer and webhook). A plan stated in other words that
#           comes true (`twins`, off) is closed the same way, without merging.
#           `store.still_standing` (new): a plan that came true counts as
#           "still standing" for the context, like the history in the past, so
#           the plan's memory stays in the block as before;
#           `presentation.mcp_line` writes "[planned, superseded DATE]" for a
#           closed plan (before only "[superseded DATE]").
#         - The end users' words out of the logs. New `log_safe`:
#           an error by its classes, SQLSTATE, constraint, table and column
#           names, HTTP status and message length (`error`), the stack with
#           file, line and function without messages (`stack`), a model
#           answer by its shape (`answer`), a text by its length. Never again
#           `exc_info=True` in the engine: the formatter prints the message,
#           and a database error's message carries the statement's parameters
#           and PostgreSQL's DETAIL, the row's values. What changes: the
#           extractor (dropped triple, unreadable answer, dropped relative
#           times), the judge (unreadable answer), the vocabulary (object of
#           the votes, answers in the exceptions), the store (names in the
#           aliases, errors), the writing (names in the identity alias), the
#           worker (errors of the write and of the drain), candidates and
#           vector_search (database errors). The messages of the engine's
#           exceptions that the two adapters write in their logs
#           (ExtractionFailed, JudgementMissing, ResolutionUnavailable,
#           CandidatesUnread, UnreadableAnswer) no longer carry text either.
#           The relation names stay: they are words of the vocabulary.
#         - Measured on the evening of 6 Oct, because 2.12.0 changes the
#           judge's prompt (RULE_DATES): against the 2.11.0 judge on the same
#           extracted facts, in time order the same
#           outcomes (batched 54/60 and 54/60, per fact 22/24 and 22/24), with
#           the oldest memory written last 44/60 against 39/60 batched and
#           18/24 against 17/24 per fact. Agents memory acceptance test with
#           the real model (its Agents part): 11/12 twice,
#           "Slack stays true" FAILED. It is not the prompt: from the same
#           memory the extractor also writes "Globex replaces Business plan"
#           in the past, judged first and alone, which closes the plan; the
#           batch of "subscribes_to Starter plan" then sees Slack as the only
#           subscription and closes it (redone with the rebuilt candidates: 7
#           out of 8 with the 2.11.0 judge, 5 out of 8 with the 2.12.0 one;
#           with the plan among the candidates 0 out of 10 for both). A defect
#           in the order of the judgements inside one write, already in
#           2.11.0: to be fixed separately (the batch should weigh the facts
#           as they were before the write).
#   3.1.0 (2026-10-06 evening): two new switches, OFF by default, for the
#         quality defects found by the acceptance tests with the real model
#         and by the whole LoCoMo (the 3.1 plan, pieces 1, 3 and 4).
#         MINOR: new parameters with defaults; off, the prompts are those of
#         3.0.0 byte for byte (the tests' fingerprints do not change). They
#         are turned on after the measurement.
#         - `states` (extract, estrai_lotto, estrai_lotto_risposte,
#           extract_with_observation, extract_batch_with_observation):
#           `extraction.RULES_STATES`, a change of state is the new state in
#           the present, which the judge compares with the old one, and the
#           value never sits in the predicate. The Self-hosted acceptance test
#           with gpt-oss-120b: "Globex moved to the Starter plan" became
#           `moves_to Starter plan` in the past and the Business plan stayed
#           open (TimeML, Pustejovsky et al. 2003; Graphiti 2.2.3).
#         - `faithful` (the two with observation): the observation names the
#           user only for what the memory says about them, by them or to them,
#           keeps the caption of a shared image and writes the referent of a
#           pronoun (Maynez et al., ACL 2020). The Cloud acceptance test ("The
#           user's organization, Globex, ...") and the whole LoCoMo (the photo
#           and the referent lost, about 10% of the reader's errors).
#   3.1.1 (2026-10-06 evening): `states` rewritten after an
#         acceptance test with the Self-hosted and gpt-oss-120b (11/13
#         against 12/13 of 3.0.0): the plan change stayed an event, and the
#         sentence "a relation does not contain its value" pushed the unit
#         out of a count ("12 employees" became 12, dropped). That sentence
#         removed; one made-up example of the output form. It changes only
#         with `states` on (off by default): PATCH.
#   3.1.2 (2026-10-06 evening): `faithful` after a test (40 LoCoMo
#         memories the reader got wrong with the evidence in the block,
#         gpt-oss-120b, about 0.04 euro): the caption helps (3 better, 0
#         worse out of 20 photos), the referent does not (0 better, 1 worse
#         out of 20), and twice "last week" became a vaguer time. The sentence
#         on the referent removed, the time stays as precise as said. It
#         changes only with `faithful` on (off by default): PATCH.
#   3.1.3 (2026-10-06 evening): piece 6 of the 3.1 plan. Every judgement of a
#         write weighs the facts as they were before it
#         (`store.one_write`, `as_before_the_write`, `mark_closed`): a fact
#         that an earlier statement of the same write closed (the twin in the
#         past, its judgement, a plan come true, the judge of an earlier fact)
#         stays a candidate of the later judgements, and naming it closes
#         nothing. It was the "Slack stays true" FAILED of the 3.0.0
#         acceptance test (above), already in 2.11.0, on both paths (one fact
#         at a time and the batch). Zep/Graphiti resolve all the edges of an
#         episode on the graph as it was before (`resolve_extracted_edges`,
#         getzep/graphiti 689de29). Only the candidates widen: `find_open`
#         and `close` do not. No new parameter, no change in the adapters (the
#         write already goes through `worker.write_memory`): PATCH.
#   3.1.4 (2026-10-06 evening): in the context block the end user as the
#         OBJECT of a fact reads "the user", as it already did as the subject
#         (`presentation.context_line`). Found on the 3.1
#         measurement (LoCoMo conv-26): "Melanie supports gb-0-caroline"
#         showed the id, in 142 blocks out of 152 with the rules off and 97
#         with them on, always the user of that same block; the reader does
#         not tie it to "I" (the same fault measured on the subject,
#         LongMemEval). It changes the block's text, not the data: PATCH.
#   3.1.5 (2026-10-07): a memory's `timestamp` later than the moment the write
#         reads it counts as that moment (`worker.read_event_time`). Found by
#         the Cloud's "as a new customer" acceptance test: a client clock
#         three minutes ahead hid the memory from /v1/context (its facts,
#         valid from a future instant, are not true now), and a 2030 hid it
#         until 2030. The decision of 6 Oct: never a 422. A fact that begins
#         later is written with its date. PATCH.
#   3.2.0 (2026-10-07): `attributes` in the extractors (off by default, prompt
#         byte for byte when off): an attribute stated with the verb to be ("I
#         am vegetarian") is written with the attribute's name in the relation
#         (has_diet vegetarian), `extraction.RULES_ATTRIBUTES`. Before, the
#         model answered (user, is, vegetarian) and `read_facts` threw it
#         away (predicates under three letters): diet, role, status,
#         condition were lost in every language (the Cloud's new-customer
#         acceptance test; LoCoMo shows it too, "I'm lactose
#         intolerant"). Source: Wikidata, Help:Statements (a statement is a
#         property with a value). It is turned on after the measurement
#         (a bench of attribute sentences). MINOR. Measured on 7
#         Oct on conv-26 (125 against 129): it stays off.
#   3.3.0 (written on the night of 2026-10-06 on a branch of 3.2.0,
#         merged on 2026-10-07 on top of 3.2.0): piece 5 of the 3.1
#         plan, close a fact only when the new one makes it false. A reading
#         of all the closes of LoCoMo conv-26: 0 real changes out of 20
#         with gpt-oss-120b (writer and judge), 1 out of 27 with Gemini 3.1
#         Flash-Lite; the others were facts still true (12 and 15) or
#         duplicates closed instead of merged (8 and 11), and a memory with
#         all its facts closed leaves the block.
#         - `vocabulary.single_value_question`: one more example among the
#           many-valued ones, a feeling or a reaction of a moment (with
#           gpt-oss `feels` was single-valued, decided on a mood, and 10
#           closes out of 20 were a feeling closed by the next one). TimeML
#           (Pustejovsky et al. 2003; guidelines 1.2.1 §2.1.3): what happens
#           is an OCCURRENCE, what lasts a STATE; a feeling stated in a
#           conversation is tied to its moment. In the 2003 paper "love" is a
#           STATE: an affection that lasts stays many-valued like `likes`.
#         - `vocabulary.ARGUMENT_TYPES` in the question "same relation?": two
#           phrases are the same relation only if their subjects and their
#           values are of the same type (Lin, Mausam, Etzioni, EMNLP 2010 §3,
#           "Type Restrictions"). An acceptance test of 3.1.2:
#           `subscribes_to` (Slack) inside `on_plan`, a company's `based_in`
#           inside `lives_in`. The question shows the value of both sides, the
#           subject of neither (a known relation keeps an example object,
#           never a subject): for the subject the model judges the type the
#           phrase itself takes.
#         - `contradiction.RULE_TRUE_TOGETHER` in the two Agents judges (rule
#           6): what happens later does not make a state, a preference, a
#           feeling or an identity false unless it says it ended or changed; a
#           second value, a second work, another side of the same identity add
#           up; the same fact stated in other words is not a change (de
#           Marneffe, Rafferty, Manning, ACL 2008 §3.1; Ritter et al., EMNLP
#           2008 §1.1; Graphiti, getzep `resolve_edge`). Made-up examples, far
#           from the measured cases and from LoCoMo (CC BY-NC). The definition
#           ("cannot both be true at once") is not written in the prompt:
#           written as the first sentence it made gpt-oss close the first job
#           with a second part-time one (BUG-200, 6 times out of 7 against 0
#           out of 7 for 3.1.3). The measurement asked for the example of the
#           change ("prefers coffee" against "switched to herbal tea"):
#           without it, Gemini's batched judge closed "prefers TypeScript"
#           with "Actually I switched to Rust" less often (14 out of 14 with
#           3.1.3, 7 out of 11 with a habit as the example, 8 out of 8 like
#           this).
#         - `contradiction.RULE_NO_LONGER_TRUE`, in the judge of a single
#           fact: "(no longer true)" says the new fact has ended; an ended
#           state closes what depended on it, an ended event does not. On
#           two writes 15 closes out of 27 (Gemini) and 9 out of 20
#           (gpt-oss) came from a new fact in the past, judged alone also in
#           batched writes. The batch never shows the mark, and does not have
#           the rule.
#         - `contradiction.RULE_SINGLE_VALUE` in the two Agents judges (added
#           on 7 Oct): the same value stated more or less precisely, or a part
#           of it, or the whole that contains it, is not a new value ("Maria
#           lives_in Turin" against "... Piedmont" or "... the old centre of
#           Turin"). Ritter, Downey, Soderland, Etzioni, EMNLP 2008 §4, read
#           on 7 Oct (ACL D08-1002): AuContraire filters the apparent
#           contradictions by synonymy and meronymy BEFORE applying
#           functionality; between values in a meronymy relation real
#           contradictions are "extremely rare" (Salzburg and Austria).
#           Measured cause: LoCoMo conv-26 (7 Oct), `family`
#           single-valued, "husband and kids" closed by "2 younger kids", then
#           by "two children", then by "kids": the tag's sentence beat the
#           "stated in other words" rule of RULE_TRUE_TOGETHER. Made-up
#           examples. NOT measured: three new cases of the judge's bench
#           (3.3.0 against 3.2.0, the earlier judge rebuilt), to be run
#           before a release.
#         - `judge._shown` (added on the evening of 7 Oct, work item 8): every
#           judge call writes in the log which relations it shows, with a star
#           on those with the single-value tag. The vocabulary votes the
#           single value for every new relation and only a judge reads the
#           answer: the line says how many of the voted relations get there
#           (7 Oct: today it cannot be read). Relation names only
#           (3.0.0). Each candidate's tag is read once per call,
#           for the prompt's lines and for the log.
#         - `relative_time` (added on the evening of 7 Oct): one day before or
#           after the memory's day is written only as "last" or "next" with
#           unit day; "before" or "after" with a count of one and no anchor is
#           refused, in the reader, in the schema and in the prompt (with two
#           made-up examples of times not to date). A reading of
#           conv-26 (6 Oct): 8 of Gemini's 12 false
#           dates had that form ("recently", "since our last chat"), none of
#           the true dates of the two writers. TIDES 2005 (Ferro, Gerber,
#           Mani, Sundheim, Wilson, §4.3.2, read on 7 Oct): "lately",
#           "recently", "a while ago" are PAST_REF, not a date. A rule on the
#           form of the answer, not on words. It changes the observations'
#           prompt with AGENTS_RELATIVE_DATES (on in production): it goes in
#           with the measurement of the bundle. In the same paragraph, "in
#           every turn and whoever says it": gpt-oss dated 100% of the times
#           in the user's turn and 46% in the other person's (same reading).
#         - `fact_extractor.the_day_it_happened` and
#           `writing.write_fact(day_of_the_words=)` (added on the evening of 7
#           Oct, a proposal with the limits set the same day):
#           the extractor marks the day that a fact's words date (a single
#           relative time dated by the code to a single day, not after the
#           memory's day) only on a CURRENT fact; the writer makes it begin
#           that day only if it is in the events family (with the 2.8.0 events
#           rule, an occurrence is current), and only if the adapter passes it
#           (Cloud AGENTS_EVENT_DAY, Self-hosted its own constant, off: it is
#           turned on with the events rule, after the measurement). Never a
#           fact in the past: it is an ended state, and the time in its words
#           dates its end ("I worked at Acme until I quit yesterday" began
#           yesterday: the independent review of the evening of 7 Oct). Never
#           a state true now ("yesterday" in "I was just remembering yesterday
#           how much I love volunteering" dates the memory of it), never in
#           the future. The code computes the day, not the model. Graphiti
#           (Rasmussen et al. 2025, §2.2.3). In the block "since" stays: "on"
#           printed as the event's day the day it had been said, on the
#           earlier lines and where no day was found (the same review).
#         - `fact_extractor.fixed_prefix` (added on the evening of 7 Oct, work
#           item 8), off by default: the extraction rules
#           call the user END_USER and their handle is bound only once at the
#           end, before the parts that change at every call (previous
#           memories, day, text). So the provider's prompt cache (Scaleway,
#           Gemini's implicit cache) reuses almost the whole prompt: with
#           observation and relative times the common prefix goes from 1,072
#           characters out of 11,382 to 96%. A subject or an object written
#           END_USER goes back to the handle in `read_facts(user=)`, BEFORE
#           the quality filter (which read "END_USER" as a code identifier:
#           the review of the evening of 7 Oct). Off, the prompts are the
#           earlier ones byte for byte; on, it changes how the rules name the
#           user: it is measured (cached_tokens in the two products) before
#           turning it on. A new parameter in the five public functions.
#         - `store.resolve_predicate(lazy_votes=)`, `store.decide_if_missing`
#           and `ensure` in the three judgements (`judge`, `batch`,
#           `after_the_vector`) (added on the evening of 7 Oct, work item 8,
#           off by default: a new relation is born with
#           the single value undecided (the family is asked as before) and the
#           votes are asked when a new fact meets a fact of the SAME relation,
#           the only place the tag's rule talks about ("a new value OF THAT
#           KIND for the same relation"). Same question, same example as at
#           birth (`example_object`); kept forever; undecided it reads as
#           today (no tag). A reading of the fn arm: the vocabulary
#           costs 1.15 calls per memory and 570 relations out of 1,210 have a
#           single fact. A vote that fails or cannot be read raises as at
#           birth (ResolutionUnavailable, ResolutionUnusable). New parameters,
#           no signature removed; off, identical behaviour.
#         - A finding of the 6 Oct audit, the part that needs no measurement
#           (added on the evening of 7 Oct): `quality.drop_reason` no longer
#           drops a date written day first ("05/12/2024" was a path) and
#           checks that its year is in the text as for the other dates; code
#           literals are compared in the case the languages write them in
#           ("Nan", a name, was a boolean). `predicate_forms.
#           canonicalize_predicate`: a predicate with letters outside ASCII
#           keeps its letters ("lavora_già" was "lavora_gi", a Chinese one was
#           "unknown" and the fact was lost); an ASCII predicate reads exactly
#           as before. It holds for the personal-memory product too (same
#           functions): it stops losing true values, no prompt changes. The
#           part that needs a measurement (the English lists of prefixes and
#           synonyms) was left for later.
#         - BUG-202, `store.resolve_predicate(merge=)` (added on the evening
#           of 7 Oct): a predicate chosen by the caller (a fact written via
#           the API, a correction) is not merged into another relation because
#           of the model's answer to the question "same relation?"; it is
#           reused if a relation with that name or one of its variants exists,
#           otherwise it is born with its own name. `plan developer` came back
#           as `role` and the merge stayed forever. The extractor's facts can
#           still be merged into it. Default True: every earlier call stays
#           the same; the adapters pass False for the direct write and the
#           correction.
#         RULE_WORDS and RULE_DATES stay byte for byte; the personal-memory
#         judge stays the 3.1.3 one (paused, not measured), with the earlier
#         text of RULE_SINGLE_VALUE (`_SINGLE_VALUE_RULE_3_1`). The
#         relations
#         already classified keep their tag
#         and their merges until they are reclassified
#         (`store.reclassify_single_value`); it holds for new writes.
#         FIX-DOC: Lin et al. 2010's typing is in §3 ("Type Restrictions"),
#         not in §2 (Related Work): corrected in the comments of store,
#         contradiction and vocabulary (the notes above stay as they were
#         written).
#         Measured on the evening of 6 Oct (the judge's bench, 3.1.3 and
#         3.2.0, 21 cases of which 9 new; the single-value bench, replacement
#         and occurrences; a bench of the judge alone):
#         * whole measurement, Gemini 3.1 Flash-Lite, batched judge as in
#           production: with the rule's text that opened with the definition,
#           3 rounds, 54/62 for 3.1.3 and 56/62 for 3.2.0 (new cases to keep
#           13/17 and 17/17; regression changes 21/21 and 18/21:
#           typescript_rust 0/3); with the text without the definition, 3
#           rounds, 57/61 and 58/61, no case worse except typescript_rust (3/3
#           and 1/3), which asked for the preference example. The final text
#           (with "prefers coffee") has no whole measurement: two rounds
#           interrupted, Gemini not answering (a read without a timeout,
#           stuck for 16 and 36 minutes).
#         * the judge alone, 20 pairs, new ids at every call, batched: Gemini
#           77/80 with the final text against 69/80 for 3.1.3, no pair worse;
#           gpt-oss-120b (with fixed ids, so fewer independent samples) 51/60
#           with the text without the definition against 48/60, and the final
#           text equal to 3.1.3 on the 5 pairs measured before Scaleway
#           refused (429) the calls.
#         * the single-value question: Gemini from 9 to 6 wrong answers out
#           of 81 (the two feelings from 0/3 to 3/3), but "requires a
#           responsible person" goes from many to one (10 out of 10, already
#           unstable on 30 Sep: one 8 times out of 10 with the 3.1.3
#           question); gpt-oss from 7 to 1 wrong out of 42 (the feelings from
#           1/3 to 3/3; "requires" is one with both).
#         Cost: about $1.03 on Gemini and $0.10 on Scaleway.
#         After the independent review of the evening of 7 Oct, besides the
#         events' day and the prefix: the lazy votes do not touch a fact in
#         the past (judged without a tag, as before) and do not vote a
#         relation without an example; an unreadable vote in the Cloud's
#         judgement after the vector is logged and does not break the work;
#         the predicates keep combining marks (Devanagari, Thai) and a
#         two-character one in a script without capitals stays; the words'
#         day is found also where the words have no spaces
#         (`relative_time._contains`); the judge's log names the canonical
#         relation; SQL's and YAML's TRUE/FALSE/NULL/Null stay literals;
#         `keep_grounded` keeps the user as an object, as it kept it as a
#         subject ("Maria mentors me" was lost).
#         The English word lists leave the code (the second part of that
#         finding; the decision of the evening of 7 Oct, "nothing must be
#         hardcoded", and the rule of 8 Oct, "we must have no word list in the
#         code": no switch). The predicate stays as the model wrote it: out go
#         the 25 prefixes and the ~70 synonyms of `predicate_forms.py`, some of
#         them
#         wrong ("prefers" is not "likes", "works_with" is not "works_at", a
#         company's "based_in" is not "lives_in", "has" a daughter is not
#         "owns"). The family map keeps only our schema (identity,
#         instructions: the names the extraction prompt asks for); the family
#         of every other relation is asked of the model by the vocabulary when
#         it is born (BUG-109), and saved on the relation and on the fact; the
#         Cloud's migration (and the Self-hosted twin) saves it once on
#         the earlier facts, as data. Out also go the five pairs of opposites
#         (`OPPOSING_PREDICATE_FAMILIES`): every pair has subject and object in
#         common, and phase 1c of the candidates brings it (BUG-111), in the
#         Agents and in the personal-memory product. Merging the phrases of a
#         relation is the work of the learned vocabulary (CESI, Vashishth et
#         al. 2018, §7.3.1 and §8.1.1; Lin et al. 2010 §3 for the type of the
#         values). Measured (8 Oct, LoCoMo conversations 0-4, gpt-oss): 610
#         answers without lists, 616 with, 618 the baseline measured the same day, all
#         in the noise; closes from 65 to 20.
#         What stays, only in the personal-memory product: the owner's English
#         pronouns and the prefixes of the test notes (to be redesigned, not
#         just removed).
#         With an inventory of 8 Oct: the relative-time readers read
#         the enumeration's values exactly (out go "weeks", "sat", "sept",
#         "fall"); `contradiction._words` reads the words of every script
#         (letters, marks and digits, NFKC and casefold: "компания Ромашка"
#         and "Ромашка" are compatible); the families in the vocabulary's
#         prompt are defined in words, without relation names.
#         The past mark in the Agents judge (night of 8 Oct, from reading the
#         closes of the 3.3.0 bundle): " (no longer true)" after an event
#         stated in the past, gpt-oss-120b read it as the negation of the
#         event ("did not paint the picture") and closed the facts about the
#         activity, with RULE_NO_LONGER_TRUE in the prompt. Now
#         PAST_MARK, " (said in the past: it was true, and it is over)", and
#         the rule says it never means "it did not happen". Bench of the judge
#         alone ($0.11):
#         wrong verdicts from 18 to 3 out of 33 with gpt-oss, from 3 to 0 out
#         of 33 with Gemini; the four ended states that must close always
#         close. The personal-memory judge keeps " (no longer true)": only
#         `judge.judge` passes `new_fact_for_the_judge(mark=)`; the earlier
#         rule stays for the
#         scripts that rebuild that judge (`_NO_LONGER_TRUE_RULE_3_2`).
#         The present of a past phrase without its object (8 Oct, the
#         judge's cases, only on 3.3.0): the tense question shows the object as
#         context, and gpt-oss answered "leaves Apple", so the relation
#         `leaves_apple` was born (then "Luca leaves_ferrero Apple", "Kenji
#         lives_in_kyoto Kyoto"). The words of the fact's own object, at the
#         end of the answer, are removed (`store.without_the_object`;
#         relation and arguments are separate parts of the sentence, ReVerb,
#         Fader et al. 2011 §3). No list: the comparison is with that fact's
#         object. The prompt does not change.
#         The review of the vocabulary already learned (8 Oct):
#         `store.review_the_variants` asks again, one pair at a time and
#         with the example of each, whether a phrase learned as a variant is
#         that relation; with `apply` it separates the phrases the model says
#         are different (they are born under their own name,
#         `resolve_predicate(merge=False)`, and their facts follow them in
#         every memory that shares the vocabulary, `facts=`). The phrases in
#         the past stay where they are; the closes between the two groups are
#         listed, not reopened. It comes from a bench (gpt-oss:
#         "visited" in `plans_to_visit`, "met" in `networking_with`,
#         "mentors" in `receives_support_from`). It is tried dry, then on a
#         copy, before any real memory.
#         The lazy vote typed by the two conflicting values (8 Oct, the 3.3.0
#         confirmation run): `decide_if_missing(examples=)` asks the vote with
#         the new value and with the earlier one, not with the birth example,
#         and the relation keeps a single value only if it keeps it for both
#         (Lin et al. 2010 §3; the direction that loses nothing). In the run
#         `reads`, born from "reads reading", had been voted single-valued
#         and every book closed the previous one. The judges pass the two
#         values; the adapters' `ensure` receives `examples`.
#         The superseded memories as history (8 Oct, the 3.3.0 confirmation
#         run): rule (b) of `presentation.memories_for_the_context` (2.3.0)
#         takes out of the block a memory whose facts have all been closed,
#         and a WRONG close by the judge also hid the memory with the
#         evidence. `memories_with_their_state(superseded_as_history=True)`
#         keeps it, and `block_line(superseded=True)` puts after the day
#         SUPERSEDED_MEMORY_MARK, "(true when said, changed later) ": true
#         when it was said, never "false" (the lesson of PAST_MARK). Graphiti
#         keeps the closed edge with its time instead of deleting it
#         (§2.2.3). Off in the adapters (Cloud
#         `agent_context._SUPERSEDED_AS_HISTORY`, Self-hosted the twin
#         constant): it is measured with a re-read.
#         Measured and removed (evening of 8 Oct): the past phrase that begins
#         a state (BUG-199, "a move is born closed"; Moens and Steedman 1988
#         §2, with votes). A run (LoCoMo conversations 0-4,
#         gpt-oss): 608 answers against 618 for the baseline and 610 without,
#         multi-hop from 91 to 86, closes from 17 to 46, calls per memory
#         from 2.32 to 2.63. The model marked 95 phrases as a beginning,
#         including "graduated", "passed_away" and "lost_job" (become
#         "is_unemployed", about ten closes on its own): the prompt's sentence
#         on lasting outcomes did not hold, not even with the votes. Never
#         released, so out of the code together with the `begins_state`
#         column (its migration and the twin removed). BUG-199 is open again.
#         Also measured and removed: REGOLE_EVENTI (2.8.0, the events
#         mentioned in passing as facts of their own), never turned on in
#         either product: in the 3.3.0 bundle worse with both writers (gpt-oss
#         from 128 to 125 answers with the closes from 5 to 16; Gemini from
#         135 to 127). Out go the rule, the `eventi` parameter of the
#         extraction with observation and `_observation_rules(eventi=)`.
#         The review by votes (8 Oct, a dry run on LoCoMo's variants:
#         about one separation in ten was two names of the same relation,
#         close synonyms or two forms of the same phrase):
#         `review_the_variants(votes=)` separates only with the majority of
#         "different", with the reasoning after the first answer (Wang et
#         al., ICLR 2023); the first "same" stops.
#         IT BREAKS COMPATIBILITY (8 Oct, the lists and REGOLE_EVENTI out of
#         the code; the `eventi` parameter of `extract_with_observation` and
#         of its batch is gone): removed
#         `predicate_forms._AUX_PREFIXES` and `_SYNONYM_MAP`, `families.
#         OPPOSING_PREDICATE_FAMILIES`, the 49 English entries of
#         `PREDICATE_TO_FAMILY`, the `senza_liste` parameter everywhere, and
#         `schema_family` has a single argument. An adapter that passes them
#         has to be updated together with the copy (Cloud and Self-hosted, 8
#         Oct). The rest is MINOR. New public
#         constants ARGUMENT_TYPES, RULE_TRUE_TOGETHER,
#         RULE_NO_LONGER_TRUE, END_USER_NAME, PAST_MARK; new functions
#         `fact_extractor.the_day_it_happened`, `store.decide_if_missing`,
#         `store.without_the_object`, `store.review_the_variants`,
#         `presentation.memories_with_their_state`; parameter
#         `review_the_variants(votes=)`; new
#         constant SUPERSEDED_MEMORY_MARK and `block_line(superseded=)`; new
#         field `ExtractedFact.day_of_the_words`; new function
#         `store.schema_family`; new parameters, all optional and defaulting
#         to the earlier behaviour: `fixed_prefix` (five functions of the
#         extractor), `read_facts(user=)`, `write_fact(day_of_the_words=)`,
#         `resolve_predicate(lazy_votes=, merge=)`, `relation=` and `ensure=`
#         in the judgements.
#         THEY SHIP WITHOUT A SWITCH (they change the behaviour as soon as
#         3.3.0 is released, so only after the measurement): the prompts of
#         the vocabulary and of the two Agents judges (piece 5 and Ritter's
#         lines), the guard of the day before or after and the turns sentence
#         in the relative times (where AGENTS_RELATIVE_DATES is on), the
#         judge's log, the 6 Oct finding (quality filter, predicates in every
#         script),
#         `keep_grounded` with the user as an object. Behind a switch that is
#         off: the events' day, the fixed prefix, the lazy votes, the
#         superseded memories as history; `merge=False` is passed by the
#         adapters for direct writes and corrections (BUG-202).
#   3.3.1 (2026-10-11): a single home. Comments and descriptions only, no
#         line of code: memory_core lives and is changed only in korely-agent;
#         the sentences in the present about the other copy move to the past
#         (the note at the top of this file).
#   4.0.0 (2026-10-12): out go the engine's leftovers, 12 October 2026.
#         MAJOR: public functions, constants and parameters
#         removed (a caller that imports them does not start, one that passes
#         them raises TypeError). No korely_agent code outside the tests used
#         them, and nothing changes in what the memory answers: the prompts of
#         every combination of the remaining switches are those of 3.3.1 byte
#         for byte (extraction, observation, batch, the two judges).
#         - `turni.py` (2.9.0) and the `turni` parameter of the extractions
#           (`extract`, `extract_with_observation`,
#           `extract_batch_with_observation`, `_core_rules`): the user's facts
#           read only from the user's turns. Measured with Gemini 3.1
#           Flash-Lite as the writer on LoCoMo conversation 26, 127 against
#           129 out of 152 (p 0.80), no gain; the adapter's switch had gone
#           with korely-agent 0679017.
#         - The assistant rules, with the same measurement and the same
#           commit: `writing.write_fact(from_the_assistant=)`,
#           `worker.write_memory(assistant_does_not_close=)` and the `text` of
#           `worker._new_for_the_batch` (a fact stated only by the assistant
#           closed nothing).
#         - `absolute_dates` (2.8.0) in the two extractions with observation and
#           in `_observation_rules`, with `ABSOLUTE_DATES_SENTENCE` and
#           `OBSERVATION_RULES_ABSOLUTE_DATES`: it converted 1 relative time
#           out of 40 (LoCoMo conversation 26, 5 Oct), the dates computed by
#           the code in 2.9.0 replaced it, and turned on it made every write
#           fail (the ValueError together with `times`, on in production;
#           korely-agent 5510f07).
#         - `fact_extractor.extract_batch` and `extract_batch_answers` (1.2.0): only
#           the tests called them, the two products write one memory per
#           call. `extract_batch_with_observation` stays, the lever for a
#           batched write (with PROMPT_BATCH, its base, and the same contract:
#           a memory without an answer is absent from the result).
#         - The code that only the personal-memory product used, without
#           references since it left (11 Oct): `contradiction.HUMAN_JUDGE`
#           with `_SINGLE_VALUE_RULE_3_1`; `_NO_LONGER_TRUE_RULE_3_2` (for
#           its measurement scripts); `_NO_LONGER_TRUE_MARK` and the
#           `mark` parameter of `new_fact_for_the_judge`, which was there to
#           leave it " (no longer true)": a fact in the past always carries
#           PAST_MARK, as already in the Agents judge;
#           `store.detach` (the facts of a trashed note) and
#           `store.functional_relation`; `Memory.read_alias` and
#           `store.alias_map_of(wins=)` (a team's aliases over the personal
#           ones).
#         - `candidates.cosine`, the import pyflakes flagged: the name stayed
#           for whoever imported it from there, and nobody did.
#           `vector_search.cosine` stays.
#         What stays, with no callers outside the tests but outside this
#         change: `contradiction.same_arguments`, `store.state_of_the_source`,
#         `worker.inline`, `candidates.SEMANTIC_MAX_DIST`,
#         `vocabulary.SHORTLIST_MAX_DIST`.
#   5.0.0 (2026-10-12): out go the standing instructions, 12 October 2026.
#         MAJOR: a public parameter, a constant and a family
#         removed (whoever passes `istruzioni` raises TypeError, whoever
#         imports `REGOLE_ISTRUZIONI` does not start). Measured on a
#         procedural bench (26 conversations, 28 questions,
#         gpt-oss-120b as writer, reader and judge): 22 out of 28 with the
#         instructions written by the engine against 21 without (3 won, 2
#         lost), 25 with the right ones put in by hand: a tie in the bench's
#         noise. The prompts of every
#         combination of the remaining switches are those of 4.0.0 byte for
#         byte (extraction, observation, batch, the vocabulary's three
#         questions).
#         - `istruzioni` (2.8.0) in `extract_with_observation`,
#           `extract_batch_with_observation` and `_observation_rules`, with
#           `extraction.INSTRUCTION_RULES`.
#         - The `instructions` family and the `instructs` predicate of the
#           schema map (`families.PREDICATE_TO_FAMILY`, `FAMILY_ORDER`,
#           `FAMILY_LABELS_EN`), with their exceptions in
#           `store._SCHEMA_FAMILIES` and `resolve_predicate`,
#           `twins.FAMILIES_LEFT_OUT` and `vocabulary._CHOOSABLE_FAMILIES`. An
#           `instructs` written by a caller is now a relation like the others:
#           the vocabulary learns it and the model chooses its family.
#   5.2.0 (2026-10-15): a borrowed word does not ground a fact, 15 October
#         2026 (measurement on conversation 26 with Gemini and gpt-oss). MINOR:
#         it changes which facts pass the anchor check,
#         not the public surface (`argument_grounded` takes one more
#         argument, with a default). An argument not written verbatim in the
#         memory is grounded only by one of its words that does not belong to
#         another argument of the same memory, written in the memory, that
#         names something else. The VS Code demo of 15 October: "Customer
#         4821 moved to the Basic plan." also wrote "Priority Support plan"
#         (taken from the March memories), let through by the word "plan" of
#         "Basic plan". Rashkin et al., Computational Linguistics 2023 (AIS).
#         The prompts do not change. (5.1.0 is the number of an experiment,
#         never released.)
#         Measured on 15 October (conversation 26, gpt-oss-120b as reader and
#         judge): Gemini as writer 135 against 134 for gem2; gpt-oss as writer
#         128 and, redone, 134 against 133 for gpt0 (the first in the noise:
#         almost equal contexts, the answer present in the lost cases). Memory
#         acceptance test 12 out of 13 with both writers, and the two failed
#         checks are earlier cases ("moved to the Starter plan" written as
#         ended with gpt-oss, already on 13 October; "based in Milan" written
#         `lives_in` by Gemini, already at 0.1.28).
#         Merged into main on 15 October 2026.
#   5.2.1 (2026-10-18): a predicate's canonical form fits in its column,
#         18 October 2026 (a code review: "canonical predicate over 200
#         characters"). PATCH: no prompt changes, no public
#         signature changes. `canonicalize_predicate` cut nothing, and NFKC
#         plus casefold can lengthen a form ("ß" becomes "ss"): 150 "ß" gave
#         300 characters, the insert into the vocabulary (String(200)) raised
#         DataError, only IntegrityError was caught, and the write answered
#         500 or, in the drain, sent the whole memory into error. Now the form
#         stops at `MAX_PREDICATE` (200, the width of the columns and the cut
#         the fact's row already made in `writing`), at the last "_" that
#         fits, inside a word only if a single word is longer. It is not a
#         rule on meaning but a column's limit: no paper.
#   5.2.2 (2026-10-18): the vocabulary reads the model's JSON like the
#         extractor and the judge, 18 October 2026 (a code review). PATCH: no
#         prompt and no public signature changes. `vocabulary._json` had its
#         own chain of `json.loads` (the
#         whole text, every ``` block, from the first "{" to the last "}")
#         and on the same text it could answer differently from
#         `model_json.read_object`: an object in a ``` block and a different
#         one outside was the block's one here, no answer for the rest of the
#         engine. Now it goes through `read_object`: two different objects
#         are not an answer for any reader, and a value that is not an object
#         is not a vocabulary answer. No new rule: the reading is the one of
#         2.11.0.
#   5.2.3 (2026-10-18): a memory deleted during its extraction does not stop
#         the drain, 18 October 2026 (a code review, and the account closures
#         of 0.1.38 and 0.1.39).
#         PATCH: no prompt and no public signature changes.
#         `worker.drain_one`, after `do`, read `m.graph_sync_status` and, in
#         the error branch, `m.id` for the log: on a row deleted in the
#         meantime (a DELETE /v1/agents/{id}, a closed account) or on a
#         session to roll back, the read reloaded the row and
#         ObjectDeletedError or PendingRollbackError escaped from the drain,
#         with the group's other memories in 'processing' until the reaper.
#         Now it reads only the claim (`close_the_claim` already says "still
#         its own and being processed") and its id; an error of a memory that
#         no longer belongs to the drain is the end of that claim, in the log
#         as info, and the warning with the stack stays for when this call
#         files it as 'error'.
#   5.2.4 (2026-10-18): out go two dead constants, 18 October 2026 (a code
#         review). PATCH: no function
#         that korely_agent imports changes, no prompt changes.
#         `candidates.SEMANTIC_MAX_DIST` and `vocabulary.SHORTLIST_MAX_DIST`,
#         Gemini's values kept since 2.4.0 for whoever imported them: nobody
#         imported them (already listed without callers at 4.x). The
#         thresholds in use stay `thresholds.current()`.
#   5.2.5 (2026-10-18): next to the relation examples in the prompts, why they
#         stay (a code review). PATCH: comments only, the
#         prompts are the same byte for byte. The examples ("likes, works_at,
#         lives_in ...") in the two extraction prompts are a measured
#         exception to the rule "no word list in the prompts": without them
#         (the 5.1.0 experiment) conversation 26 scored 129 against 134 with
#         Gemini and the relations scattered. Those of the single-value
#         question were never measured
#         without: the comment says so.
#   5.2.6 (2026-10-18): the comments of `families.py` and `manifest.py` in
#         English, like those of the other modules (a code review;
#         CONTRIBUTING.md: code comments in English). PATCH: comments and
#         docstrings only. This file stayed in Italian by choice (until 29
#         October 2026, when everything public moved to English): the
#         maintenance rule and the version log were the engine's diary.
#   5.2.7 (2026-10-18): the judge's candidates are the same every time,
#         18 October 2026 (a code review).
#         PATCH: no signature, no prompt. The candidate phases cut with LIMIT
#         ordering only by `created_at`, and the facts of one write have the
#         same `created_at` (the transaction's now()): past the limit, which
#         tied facts the judge saw was decided by the physical order of the
#         rows. On the kept LoCoMo database the cut at 10 fell in a tie for 13
#         subjects out of 40 with more than 10 open facts, on LongMemEval for
#         200 out of 386. Now the order is `created_at` and then `id`: the cut
#         is the same at every read (not better: no paid measurement, it would
#         measure the noise).
#   5.2.8 (2026-10-18): three rules say where they come from, 18 October
#         2026 (a code review). PATCH:
#         comments only. The two-character predicate (`predicate_forms`):
#         ReVerb (EMNLP 2011, §3.1 and Table 2) supports its reason, not the
#         length test, which is ours. The ceiling of 8 facts
#         (`fact_extractor`): ours, never measured; the model is Mem0's
#         (§2.1), the number is not.
#         The forms dropped by `quality.drop_reason`: the principle is
#         TextRunner's (IJCAI 2007, §3.2) and OPIEC's (AKBC 2019, §4.2), the
#         forms are ours (BUG-110). Citations read in the papers' texts.
#   5.4.0 (2026-10-18): a plan and the same fact come true do not become a
#         single row, after the measurements of 18 October 2026 (a code
#         review). MINOR: `merge_into`
#         returns the row that stays open (None before), and
#         `recanonicalize` also counts `plans_closed`. When an alias, a
#         review of the vocabulary or the merge of two projects made a plan
#         and a fact equal, `merge_into` kept the older row and changed its
#         time: the plan became "current" in place and the fact's row
#         disappeared, against the 3.0.0 rule (Graphiti §2.2.3). Now the plan
#         is closed where the fact begins (`close_the_plan`, moved here from
#         `writing` to serve both), points to the fact, and the fact stays
#         the open row. Two plans or two facts merge as before. (5.3.0 is the
#         number of the work-sessions experiment, rejected and never merged.)
#   5.4.1 (2026-10-18): comments only. korely-agent's relation vocabulary is
#         one per end user of a project (every end user is a thing of its
#         own, every project is a thing of its own):
#         `review_the_variants` and `examples_after_the_erasure` say so. The
#         engine already receives the scope from the product (`Memory`
#         .relations / .variants / .new_relation), no behaviour changes.
#   5.5.0 (2026-10-18): without the confidence threshold, OFF (a finding of
#         the audit of 18 October 2026). MINOR: new optional
#         parameter `without_threshold` of `extract`,
#         `extract_with_observation` and `extract_batch_with_observation`.
#         Off, every prompt is the 5.4.1 one byte for byte. On, the confidence
#         line loses "below 0.5 SKIP": the declared confidence is not a
#         threshold (Xiong et al., ICLR 2024; the engine has not used it
#         since 2.11.0, `is_valid`), and the GROUNDING rule decides whether a
#         fact is written. Measured on conversation 26 (18 October): answers
#         135 against 138 (p 0.51), facts 470 against
#         463, no fact under 0.5 in either arm (the sentence removed nothing).
#         (The [another subject] tag for the judge, tried the same day,
#         stayed on a branch: rejected by the criterion written beforehand, it
#         has no number here.)
#   5.6.0 (2026-10-18): the vocabulary's example stays on its line. A
#         behaviour PATCH only for an
#         example with a line break or longer than 160 characters: new
#         `contradiction.example_on_one_line` (line breaks become spaces, cut
#         at `EXAMPLE_MAX`), used by the judge's tag and by the options of
#         "is it the same relation?". An example of one short line is printed
#         as before: on all the bench databases of 18 October no example had
#         a line break and the longest was 120 characters, so the benches'
#         prompts stay identical. Hines et al., arXiv:2403.14720, 2024,
#         section III (spotlighting: separating untrusted text from the
#         instructions).
#   5.6.1 (2026-10-18): `writing.write_fact` split into one function per step
#         (`_the_clocks`, `_already_true`, `_the_past`, `_new_row`,
#         `_insert`, `_alias_from_identity`, `_twin`). No behaviour changes:
#         same signature, same rules in the same order, tested on 400 scripted
#         writes with the earlier function and this one (same rows, same
#         returns, same calls to the judge).
#   5.7.0 (2026-10-18/19): four findings of the audit of 19 October 2026. (1)
#         `presentation.context_line`: a name mapped back
#         to the user by an alias (the declared name, POST
#         /v1/users/{id}/names) reads "The user", not "<id> (then: name)".
#         (2) `store.merge_into`: merging two duplicates,
#         `session_only` stays true only if both were. (3)
#         `store.resolve_predicate`: a variant's relation is looked up in the
#         same vocabulary (`mem.relations`), never by id alone. (4)
#         `vocabulary`: the value printed in the questions "same relation?",
#         "one value at a time?", "which family?" and "which tense?" goes
#         through `example_on_one_line`, like the example since 5.6.0: on
#         76,768 bench facts no value has a line break or goes
#         over 160 characters, so the measured prompts stay identical. The
#         benches declare no names: (1) does not touch them.
#   5.8.0 (2026-10-19): the [another subject] tag for the judge, OFF until it
#         is measured (asked on 19 October: "fix the error" of the judge
#         that closes the user's facts with those of a colleague with the same
#         name). MINOR: new optional parameters
#         (`judge(subject_canonical=, mark_other_subject=)`,
#         `judge_batch(mark_other_subject=)`,
#         `line_for_the_judge(other_subject=)`); the batch's groups carry
#         `new_subject`. Off, every prompt is the 5.7.0 one byte for byte. On,
#         an earlier fact whose subject has a canonical name other than the
#         new fact's (the aliases, declared names included, already applied)
#         carries " [another subject]", and the judge reads one more rule: a
#         name, a nickname or an id can belong to two different people.
#         Ritter, Downey, Soderland, Etzioni, "It's a Contradiction, No, It's
#         Not", EMNLP 2008 §4 (the ambiguity of the arguments). Tried on 18
#         October on a branch (5.5.0 there): it
#         removed the errors on the namesake but blocked the right closes of a
#         user called by the short name that was not declared; since the
#         evening of the 18th the short name is declared (korely init --name,
#         repeatable).
#   5.11.0 (2026-10-20): three fixes from the audit of 20 October 2026
#         (5.9.0 and 5.10.0 belong to the speakers rule, on a branch never
#         merged). MINOR: new optional
#         fields of `Tables` (`vector`, `alias_stated`, `text_changed`), new
#         parameter `add_alias(recanonicalize_now=)`, new functions
#         `open_twins` and `restore_the_chain`. (1) An alias remembers the
#         name as it was stated (`alias_stated`): `remove_alias` restores only
#         the rows its flattening had changed, never an alias decided by hand
#         that the rule did not touch; a fact whose canonical names change
#         loses its vector, which is recomputed on the new text
#         (`text_changed`). (2) `examples_after_the_erasure` locks the
#         relations it cleans before writing them and writes the centroid
#         even when it is NULL: the vectors drain can no longer save the
#         vector of the erased value inside the erasure's window (PostgreSQL,
#         §13.2.1 Read Committed). (3) Moving a deleted project reads only
#         what it moves: `recanonicalize` does not load the vectors
#         (`vector`), the twins are looked up in a single read
#         (`open_twins`), and the moved aliases are recanonicalized once per
#         memory.
#   5.11.1 (2026-10-20): the engine's hot spots split into one function per
#         step, 20 October 2026 ("the engine's hot spots", from the audit of
#         that day). PATCH: no behaviour changes. `store.
#         resolve_predicate` (`_from_the_variant`, `_to_the_present`,
#         `_the_vector`, `_the_shortlist`, `_the_questions`, `_born`,
#         `_count_and_remember`), `store.review_the_variants`
#         (`_the_variants`, `_facts_of_the_relation`, `_are_different`,
#         `_separate`, `_closures_between_the_groups`) and
#         `candidates.for_the_judge` (`_the_wall`, `_newest_first`,
#         `_same_arguments`, `_semantic`): same signatures, same rules, same
#         SQL statements and same questions to the model in the same order.
#         Tested on the real Postgres with the earlier version (5.11.0) and
#         this one on 550 scripted cases with a fake model (200 resolutions,
#         150 reviews, 200 candidate searches): same returns, rows, prompts
#         byte for byte, SQL statements and log lines. `writing.write_fact`
#         was already split (5.6.1).
#   5.12.0 (2026-10-27): two fixes from the audit of 27 October 2026.
#         MINOR: new function
#         `worker.order_key`. (1) Two memories with the same instant (a
#         `timestamp` with the date only, or repeated: the import road of a
#         dated history) are claimed and read in the order they were written,
#         not by the random id: before, the judge could meet "I moved to
#         Paris" before "I live in Rome" and the wrong fact stayed current.
#         The order is (event instant, write, id) in `worker.claim_pending`
#         (the window of the per-person ceiling and the final order), in
#         `drain._each_account_its_share` and in the group's order
#         (`drain._memory_order_key`). Graphiti keeps two times, when it
#         happened (T) and when the system learned it (T'), and the second
#         orders what the first cannot (Rasmussen et al., 2025, section
#         2.2.3). (2) A past fact written with POST /v1/facts without
#         `valid_from` was born closed before it began (the end was the
#         engine's time, the start the column's default value read later, at
#         the flush): now it begins and ends when it was stated, an empty
#         interval by construction (`writing._new_row`; the engine's rule, "a
#         fact is never closed before it begins").
#   5.12.1 (2026-10-27): comments only. Next to the two ordering lines of
#         `worker.claim_pending` now sits the paper's citation (Rasmussen et
#         al., 2025, section 2.2.3), as CONTRIBUTING.md requires. No behaviour
#         change.
#   5.12.2 (2026-10-27): a memory deleted while its facts are being extracted
#         is no longer an ERROR. The flush of the shortlist (`store.
#         _the_shortlist`) found the memory's row gone (StaleDataError) and
#         logged it as a failed query, with the stack, for an expected race.
#         Now the StaleDataError goes up as it is and `worker.
#         write_memory` looks at the database: row gone = an INFO line with
#         the id only, write abandoned as before and not put back in the
#         queue; every other StaleDataError stays an ERROR. It is decided from
#         the state of the database, never from the error's text.
#   5.12.3 (2026-10-27): the other two roads of the same race (a memory
#         deleted during the extraction). A memory that was not edited fails
#         with a foreign key violation, not with a StaleDataError:
#         `worker._gone` decides only from the fact that the row is no longer
#         there (INFO with the id only). And the lost-claim branch read
#         `memory.id` after the rollback (ObjectDeletedError on a deleted
#         memory): the id is now taken before, from the identity map.
#   6.0.0 (2026-10-29): every name in English, nothing the engine does
#         changed (the decision of 29 October 2026: everything public in
#         English). MAJOR: every public name of the folder changed,
#         so every caller changes its imports. The package and its 22
#         modules took English names (store, candidates, contradiction,
#         fact_extractor, extraction, families, twins, judge, identity,
#         model_json, worker, manifest, log_safe, predicate_forms,
#         presentation, quality, writing, thresholds, relative_time,
#         fact_time, vector_search, vocabulary), and so did their functions,
#         classes, constants and parameters. The map from the old names went
#         to the callers of the time; it is not part of this repository.
#         Also in English: the comments, the docstrings and this history; the
#         values that never leave the process except in a log line (the alias
#         outcomes "invalid", "conflict", "same", "created", "updated"; the
#         result keys of `store.recanonicalize`,
#         `store.reclassify_single_value` and `store.review_the_variants`;
#         the claim outcome "lost"; the template placeholders); the keys of
#         MANIFEST.json ("files", "version"). What an operator reads changes:
#         the log prefixes (now [memory], [memory/F3], [vocabulary] and
#         [memory_core]), the logger names and the class paths in log lines
#         (`korely_agent.services.memory_core.worker`,
#         `...memory_core.vocabulary.UnreadableAnswer`), and the words after
#         the prefix where a line prints an alias outcome or the result of
#         `store.recanonicalize`. The same class path, for an engine
#         exception, is what Agents keeps in `agent_batch_jobs.last_error`
#         (old rows keep the old path; nothing reads it back) and shows in
#         the `detail` of `jobs` in `GET /health`. Unchanged, byte for byte:
#         every prompt the engine sends to a model and every line it prints
#         for a reader (961 rendered prompts and lines compared before and
#         after), the JSON keys the model reads and writes, every other value
#         stored in the database or sent on the wire, the SQL apart from two
#         column labels (rank, standing).
#   6.0.1 (2026-10-29): comments and docstrings without internal references
#         (work sessions, audit-run and finding ids, the predecessor project's
#         names and paths, internal documents and scripts) for the public
#         repository; the rules they cite point at CONTRIBUTING.md. The
#         refusal of an embedding model without thresholds no longer names
#         the predecessor's calibration script, and relative_time.resolve's
#         TypeError names its parameter `day` (it named it in Italian). PATCH: no behaviour, prompt or
#         log line changes (961 rendered prompts and lines compared before and
#         after).
#   6.1.0 (2026-10-29, rebuilt 2026-10-30 after an independent review):
#         one thing named in two languages or forms is one object, OFF until
#         it is measured and agreed on ("lives in Italia" and
#         "lives in Italy" were two objects, by the GROUNDING rule of the
#         extraction). MINOR: new module `objects` (`describe_question`,
#         `select_question`, `pair_question`, their readers, `decide`,
#         `unite_new_objects`, `review_objects`, `apply_proposals`, the
#         links `add_link`, `remove_link`, `link_map`, `root_of`), new
#         optional members (`Tables.Link`, `Memory.links`,
#         `worker.write_memory(objects=)`) and `presentation` reading a
#         name a link resolved ("Italia (written: Italy)", only when the row
#         says so: `<side>_link_id`). Without `objects=` and without link
#         rows every write, prompt and line is the 6.0.1 one. On: a write's
#         new objects (never of the identity or people families) are
#         offered the memory's OPEN facts of the same family nearest by the
#         fact's vector (Papadakis et al. 2020, blocking; Botha et al. 2020,
#         the mention in its context; `vector_search.nearest`, BUG-320); the
#         writer DESCRIBES each mention, then selects (Wang et al. 2025) and
#         matches the chosen pair alone, framed the other way round (Peeters
#         et al. 2025), all batched (Fan et al. 2024; Logeswaran et al. 2019
#         for the descriptions); two "same" write a LINK, never an alias: no
#         fact is rewritten, merged or reconfirmed, a link is read only in
#         the relation family it was decided in (`Tables.Link.family`,
#         `link_outcome`), and removing the row restores the memory exactly. The first build wrote an alias, which
#         reconfirmed the new fact away and could not be undone (the
#         review's P1.2). Measured on scripts/bench_same_object.json: see the
#         product's `services/facts/objects.py` and its CHANGELOG entry.
#   6.1.1 (2026-10-30): no Italian left in the engine's texts, 30 October
#         2026 (the founder's check before the public repository). PATCH:
#         comments and docstrings only, nothing the engine does or prints
#         changes. This history named code that was removed before 6.0.0 by
#         its Italian names, and 6.0.0's entry listed every old name: they
#         read as they would in English now (`extract_batch`,
#         `contradiction.HUMAN_JUDGE`, `_NO_LONGER_TRUE_MARK`, ...), and
#         6.0.0 says that the package and its modules took English names.
#         model_json's docstring names the shared test of the old copies
#         without its Italian name.
#   6.2.0 (2026-10-31): an edited memory is judged against the copies it
#         shared (the founder's yes of 31 October 2026 to fix it in the
#         engine). "I work at Acme" stated by three memories is one row with
#         three sources; one of them edited to "now I work at Beta" left
#         Acme open beside Beta, and the judge was never asked: the edited
#         memory was still one of Acme's sources, and a memory's own facts
#         are never its candidates (BUG-123). The retirement took it out of
#         the sources only after the judgement. MINOR: new
#         `store.shared_by_the_previous_version`, `store.judged_apart`,
#         `store.apart_from`, `store.lose_the_source` (the rule of
#         `retire_the_previous_version` for a fact other sources still
#         state, now one function the product's forget uses too); new
#         optional `outside_the_source(apart=)`,
#         `without_the_same_source(apart=)`,
#         `retire_the_previous_version(apart=)` and
#         `worker.write_memory(shared=)`. With `shared=` and `batch=`, once
#         the new text's facts are written, the copies its old text shared
#         with other memories and the new one does not state are read (ids
#         only, nothing written or locked) and the judgement of the whole
#         write weighs them as another memory's facts (Graphiti's
#         `remove_episode` and `add_episode`, read at 72b5c59; Rasmussen et
#         al. 2025, section 2.2.3); `retire_the_previous_version` then takes
#         the memory out of their sources, open or closed by the judge, on
#         the rows locked and read again. What the new text states again is
#         never a candidate of its own statements, in any order, and a
#         judgement made while the text is still being written (a past
#         statement's, or every one without `batch=`) does not see the
#         copies, as before. Nothing is closed without the judge, and no
#         prompt changes: the judge is shown one candidate more, the copy,
#         when the edited memory no longer states it. Without `shared=`
#         every write is the 6.1.1 one.
#   6.2.1 (2026-11-04): a round whose every declared fact the filter drops
#         leaves one INFO line (the founder's yes of 4 November 2026, E1 of
#         the engine list). `keep_grounded` logged each drop at DEBUG, so a
#         memory whose only fact was dropped ended 'done' with none and no
#         trace above DEBUG. PATCH: `keep_grounded` writes, when it was given
#         facts and kept none, "the writer declared N fact(s) and the filter
#         kept none", with the count and no word of the text or of the fact.
#         Which facts it keeps is unchanged: nothing is measured here, and
#         the second reading that would use this case (`feat/past-tense-
#         changes`) is not in this line of the engine.
#   6.3.0 (2026-11-04): a fact a past statement closed points to its
#         successor (the founder's yes of 4 November 2026, E3 of the engine
#         list). The judge's closing sets `invalidated_by_id`; a past
#         statement closes its row in place (`writing._the_past`) or writes
#         it born closed, and the pointer stayed empty. MINOR: new
#         `worker._point_to_the_successor`, called by `write_memory` on the
#         facts it wrote: a past fact, closed, with no pointer, takes the one
#         open fact of the same write about the same subject and relation
#         with another value; with none or with several it keeps none.
#         Graphiti, Rasmussen et al. 2025, section 2.2.3. What it changes
#         downstream: `facts/given_back` reads the pointer to decide whether
#         the deletion of the closer's memory gives the fact back.
#   6.4.0 (2026-11-07): an agent's memory with no end user is told so
#         (the founder's yes of 7 November 2026). Until now the extractor
#         knew only the end user, and a memory an agent wrote with no
#         `user_id` was asked the prompts of an end user's memory with the
#         handle "user": on the 58
#         memories of a team of agents, 48 of 168 facts had "user" as their
#         subject (46) or object (2), none of them right, 74 of 168 right
#         (44%), and three levels of a charter closed one another under
#         "user has_delegation_level". MINOR: new optional `agent_id` of
#         `fact_extractor.extract`, `extract_with_observation` and
#         `extract_batch_with_observation`, new keyword `end_user` of
#         `keep_grounded` (default the behaviour of before), new `writer` of
#         `read_facts`, new prompts `fact_extractor.*_WITHOUT_END_USER` and
#         `extraction.TENSE_WITHOUT_END_USER`. With `end_user_id` None and an
#         `agent_id` (Mem0's test, `is_agent_scoped`, mem0ai/mem0 0724291):
#         the frame says the memory belongs to no end user; the subject is
#         the entity the statement is about, as the text names it; the first
#         person is the agent (Mem0's AGENT_CONTEXT_SUFFIX), bound once after
#         the rules like the end user's handle (`WRITER`); the observation is
#         the faithful one of 3.1.0 whatever `faithful` says; a procedure, a
#         rule in force or a habit is current (Friedrich, Palmer, Pinkal, ACL
#         2016, Figure 1), so is the lasting result of something done, from
#         its day, and past needs the text to say the state ended (Graphiti
#         extract_edges, DATETIME RULES); `keep_grounded` lets "user" through
#         only for an end user's memory, and in an agent's memory keeps it
#         only as a whole word of the text (not inside "user_id", "users",
#         "username"). With an end user (`end_user_id` not None,
#         an id of spaces included, named "user" as 6.3.0 named it) every
#         prompt is the 6.3.0 one byte for byte, `agent_id` included, and
#         with neither an end user nor an agent too: one person using the MCP
#         or an SDK with no ids keeps the facts of their first person
#         (tests/test_prompts_with_an_end_user_unchanged.py). Not measured
#         with a real model yet: the measure and its criteria are written,
#         and it has not run.
#   6.5.0 (2026-11-07): a subject named by its writer is judged against its
#         own facts, both ways (the plan approved on 7 Nov 2026; the finding
#         of 6 Nov: facts written directly closed true facts of other
#         subjects). MINOR: new optional `candidates.for_the_judge(
#         subject_named=, named=)` and `judge.after_the_vector(named=)`.
#         `subject_named`: the candidates of a fact whose subject the engine
#         says its writer chose (Agents: a direct write and a correction whose
#         subject is the caller's) are the facts about the same canonical
#         subject, the memory's aliases applied (stages 1a, 1c, and the
#         semantic stage 1b among those facts only), and about the same two
#         entities the other way round (1d); stage 1a-bis is not run for it.
#         `named`: a condition true for such facts (Agents: their
#         `model_used`; NULL is not named); a named fact enters stages 1a-bis
#         and 1b of a new fact the extraction read only about that fact's
#         subject. Two facts that are not named meet as in 6.4.0, every stage
#         included. The late judgement of a vector follows the same rule both
#         ways (the database reads the condition for the late fact and the
#         later ones near it). Ritter, Downey, Soderland, Etzioni, EMNLP 2008,
#         §2, §4.2 and §4.3; Graphiti, Rasmussen et al. 2025, §2.2.2 (both
#         quoted in `candidates`). Measured cause, on a store of an agent
#         team's facts (6 Nov 2026): 108 of the 111 closures between two
#         direct facts had two different subjects, all reachable by the
#         semantic stage alone, and 3 of the 8 closures of a direct fact by
#         an extracted one were judged on the extracted fact's write between
#         two subjects ("the user uses Docker" closed "a reviewer agent works
#         read-only, without Docker"); the 9 closures between two extracted
#         facts were all about one subject. Without the parameters every
#         query, candidate and prompt is the 6.4.0 one. No prompt changes.
#   6.5.1 (2026-11-07): comments only. The examples of 6.5.0 are told
#         without the names of the store they were measured on. PATCH:
#         nothing the engine does or prints changes.
#   6.6.0 (2026-11-07): one rule of "the same subject", the decision of 7 Nov
#         2026 after an independent review of 6.5.0. MINOR: new public
#         `candidates.about_the_subject`, the condition of 6.5.0 (the
#         canonical name after the memory's aliases, the database's lower()
#         on both sides, as stage 1a and `store.find_open` compare it), now
#         also the filter of the later facts in `judge.after_the_vector`, in
#         SQL. Until 6.5.1 the late judgement compared the two subjects in
#         Python (`contradiction.other_subject`: NFKC and case folding), the
#         write with the database's lower(): "Straße 5" and "STRASSE 5" met in
#         one road and not in the other. `other_subject` stays for the
#         [another subject] tag of 5.8.0 only. Nothing else enters the
#         comparison (no Unicode normalization, no squeezing of spaces, no
#         word list, no name inside another): `about_the_subject` says why.
#         `candidates.for_the_judge` says why its subject rule is a filter
#         on the candidates and not the 5.8.0 tag (Lin, Mausam, Etzioni,
#         EMNLP 2010 §3 for the tag; `identity`, never from similarity).
#         Same prompts; the late judgement may now meet a pair the write
#         meets and no longer meets one the write keeps apart.
#   6.7.0 (2026-11-07): in an agent's memory with no end user, the judge's
#         candidates by meaning are the facts of one subject (approved on
#         the night of 6-7 November 2026). MINOR: new optional `candidates.for_the_judge(
#         agent_scoped=, agent_scoped_facts=)`, `candidates.about_the_subject(
#         agent_scoped=, agent_scoped_facts=)` and `judge.after_the_vector(
#         agent_scoped_facts=)`. `agent_scoped`: the new fact belongs to an
#         agent's memory with no end user (Agents: an `agent_id` and no
#         `user_id`, the test of the extraction's frame of 6.4.0, now one
#         rule for both in the new module `owner`: `agent_of`, the
#         extraction's own, a blank `agent_id` of any alphabet's whitespace
#         being no agent; `agent_scoped`; `agent_scoped_rows`, the same rule
#         on two columns, a `btrim` of every character `str.strip()`
#         removes, read from the Unicode database); its stage
#         1b looks among the facts of its canonical subject only, the
#         memory's aliases applied, as a named subject's does. Stages 1a, 1c
#         and 1d are unchanged, and 1a-bis does not run without an end user,
#         as before. `agent_scoped_facts`: the condition true for such facts;
#         one of them enters stage 1b of another new fact only about that
#         fact's subject, so the rule is the same from either fact of a pair
#         and which memory came first does not decide whether two facts meet.
#         The late judgement of a vector applies the same rule. Mem0
#         (mem0ai/mem0 f1791daa, `add`: the ten nearest memories under a
#         filter of every id passed); Graphiti (Rasmussen et al. 2025,
#         §2.2.1 and §2.2.3: invalidation among related edges of entities
#         already resolved; v0.5.2, 0f50b74: the edges of the new edge's two
#         nodes); Ritter et al., EMNLP 2008, §4.2 (all quoted in
#         `candidates`). Measured cause, replayed for free on the memory of
#         a team of agents reloaded on 7 Nov 2026 (422 facts, every one with
#         an `agent_id` and no `user_id`; the real judge's verdicts taken as
#         given, so the numbers are estimates): 21 of 32 closures wrong, 29
#         of the 32 pairs (19 of the 21 wrong ones) shown to the judge only
#         by stage 1b, whose ten places were full at every write; with this
#         rule 4 wrong (3 by the judge, 1 made with no judge), 1 right of 8,
#         the judge's lines 3,740 -> 666. The price: two spellings of one
#         thing ("Cloud", "Korely Cloud") are two subjects there until an
#         alias of the writing agent's namespace unites them. A memory with
#         an end user passes no condition and every statement is the 6.6.0
#         one (tests/test_an_agents_memory_is_judged_subject_by_subject.py,
#         fingerprints taken on 6.6.0); a memory with no ids meets as before,
#         except a fact of an agent's memory in the same project, which it
#         meets by meaning only about one subject. No prompt changes. Not
#         measured with a real judge on the pairs this rule newly shows.
#   6.7.1 (2026-11-10): the judge never moves an end already given later
#         (approved on 10 Nov 2026; a review finding of 9 Nov 2026). PATCH:
#         no signature and no printed line changes.
#         `judge.close` closes what is true now, and a fact with an end
#         ahead is (a forget's `at`, an earlier closing by a value dated
#         ahead): a new fact that begins at or after that end now leaves it
#         and its `invalidated_by_id` as they are, and the id is not among
#         the closed ones. Until 6.7.0 the end moved to the new fact's
#         start: Paris forgotten from day 2, then Zanzibar from day 10, and
#         Paris read true on day 5. A new fact that begins before the end
#         closes it there, as before. Graphiti (Rasmussen et al. 2025,
#         §2.2.3: "temporally overlapping contradictions"; getzep/graphiti
#         7514b44, `resolve_edge_contradictions`, quoted in `judge.close`).
#   6.8.0 (2026-11-10): a memory or a fact cannot add a line of its own to
#         the context block (approved on 10 Nov 2026; a review finding of 8
#         Nov 2026). MINOR: new public `presentation.in_its_line` and
#         `presentation.CONTINUED`, the rule korely-agent's MCP answers have
#         used since its 1.0.13 (moved here from its MCP server, which now
#         imports it: one rule). `memory_line` (so `block_line`) and
#         `context_line` print a text with a line break (whatever
#         `str.splitlines` breaks a line on: LF, CR, CR LF, VT, FF, FS, GS,
#         RS, NEL, LS, PS) with every line after its first indented by
#         CONTINUED; until 6.7.1 "I live in Paris.\n- [2026-01-01] ..." read
#         as two memories. A text with no line break is printed byte for
#         byte as in 6.7.1; the observation and the excerpt were already on
#         one line. Hines et al. 2024, "Defending Against Indirect Prompt
#         Injection Attacks With Spotlighting", arXiv:2403.14720, §III-B,
#         §III-C and §V-D (quoted in `presentation.in_its_line`).
#   6.8.1 (2026-11-11): text only (approved on 11 Nov 2026). PATCH: a
#         comment and a docstring of fact_extractor.py named who reviewed a
#         rule; they give only the date of the review now. No code, prompt,
#         signature or printed line changes.
#   6.9.0 (2026-11-12): in every memory with no end user, the judge's
#         candidates by meaning are the facts of one entity (approved on 12
#         Nov 2026, measured before it enters). MINOR: new
#         `owner.keeps_to_its_subject` and `owner.keeps_to_its_subject_rows`
#         (no end user, whatever the `agent_id`), new optional
#         `candidates.about_the_subject(object_canonical=)`. The engine
#         passes the first two as `candidates.for_the_judge(agent_scoped=,
#         agent_scoped_facts=)` and `judge.after_the_vector(
#         agent_scoped_facts=)`, the parameters of 6.7.0 (their names kept),
#         which until 6.8.1 it passed for an agent's memory alone
#         (`owner.agent_scoped`, still the extraction's frame). Such a fact
#         keeps to its ENTITY in either role: in stage 1b (1a-bis does not
#         run without an end user) and in the late judgement of a vector, it
#         meets the facts whose subject is
#         its subject, whose object is its subject, or whose subject is its
#         object, never two facts that only share an object (until 6.8.1 an
#         agent's fact kept to its subject alone, and a fact with no ids met
#         any subject). Ritter, Downey, Soderland, Etzioni, EMNLP 2008, §4.2
#         and §4.3; Graphiti, Rasmussen et al. 2025, §2.2.1 and §2.2.3, and
#         getzep/graphiti 0f50b74 (v0.5.2), the edges of the new edge's two
#         nodes in either position (quoted in `owner.keeps_to_its_subject`
#         and `candidates.about_the_subject`). A named fact (6.5.0) keeps to
#         its subject alone, and a memory with an end user passes no
#         condition: every statement is the 6.6.0 one
#         (tests/test_an_agents_memory_is_judged_subject_by_subject.py). No
#         prompt changes. Measured on 12 Nov 2026 (Gemini 3.1 Flash-Lite,
#         the memory of a team of agents written with no ids, 43 memories,
#         the extraction recorded once and replayed, the judge asked in
#         three runs per version, every closure labelled by reading both
#         memories): wrong closures 19 of 41 with 6.8.1 and 12 of 30 with
#         6.9.0, none between two subjects (7 before); outdated facts closed
#         by the newer fact of their subject 14 and 18, by a fact of another
#         subject (right by chance) 7 and 0, in all 21 and 18. What stays:
#         the writer named "user" without ids (9) and the pointer of a past
#         fact inside one memory (3, `worker._point_to_the_successor`, no
#         judge). Written with an `agent_id`, the same 6 closures before and
#         after (5 wrong, 1 right). On the agent memory replayed on 7 Nov
#         (the real judge's verdicts as given, an estimate) the other role
#         brings back 1 right and 2 wrong of its 32 closures. The price:
#         two names of one thing, and one name in two Unicode forms the
#         database's lower() does not unite (a final sigma, a composed and a
#         decomposed accent, full-width letters), are two subjects until an
#         alias unites them.
#   6.14.0 (2026-11-15): a fact the caller said is over by hand stays over
#         when its memory is written again: a forget is the person's will,
#         and an edit of the text does not undo it. No version is numbered
#         6.10.0 to 6.13.0. MINOR: new optional
#         `writing.write_fact(held_by_hand=)`, new
#         `store.rows_of_the_triples_stated_by` (every row of each triple a
#         memory stated, one query, read as columns), `store.mark_held` and
#         `store.held_by_the_write`. A current or planned statement of a
#         memory written again, when nothing of its triple is true now and
#         the engine's `held_by_hand` names the triple (korely-agent: the
#         memory stated a row of it, no row of it is true now, and the last
#         end by hand of a row of it, a forget, a correction or a past
#         statement posted by hand in agent_forget_audit, is later than any
#         row of it posted by hand; rows born of memories count for neither),
#         is not written: the row of that end comes back as it is, not
#         reconfirmed, not judged, and no later step of the write touches it
#         (`worker._point_to_the_successor` skips it). Until
#         6.13.0 an edit that fixed a typo wrote the fact again as a new open
#         row, true again. Mem0's update extracts nothing from the new text,
#         Zep edits no episode's text and undeletes nothing, Supermemory's
#         forget is kept out of recall (quoted in `writing.write_fact`).
#         Without `held_by_hand` a write is the 6.9.0 one, byte for byte. No
#         prompt changes, no model call. Revised the same day: the hold
#         read only the triple's last row of the same memory, then the
#         last row from any source, and its rows from the session.
#   6.16.0 (2026-11-16): a correction of a correction outlives an edit of
#         the memory it started from (6.14.0 did not read the corrections
#         of 1.0.27 as the memory's). No version is numbered 6.15.0.
#         MINOR: new optional `store.rows_of_the_triples_stated_by(
#         also=)`, a condition on the facts for more rows whose triples the
#         source stated, which the caller knows and this folder does not
#         (korely-agent: the rows whose first writer is the memory, its
#         corrections, which since 1.0.27 name no source memory and keep it
#         as their `first_memory_id`). Until now a triple the memory reached
#         only through a correction (Paris corrected to Lyon, Lyon to Nice,
#         then the text edited to say Lyon) was not one of its triples, so
#         the edit wrote Lyon again as a new open row next to Nice. Now the
#         correction's triple is the memory's and the hold of 6.14.0 reads
#         it like any other: Lyon stays closed, Nice true. Without `also`
#         the read is the 6.14.0 one, byte for byte. Same sources as 6.14.0
#         (Mem0's update extracts nothing from the new text, Zep edits no
#         episode's text, quoted in `writing.write_fact`). No prompt
#         changes, no model call.
#   6.17.0 (2026-11-16): a memory in the context block is whole or cut
#         after a complete sentence. Written on 15 Nov 2026 as 6.15.0 and
#         numbered 6.17.0 when it was merged after 6.16.0. MINOR: new module
#         `sentences` (`boundaries`, `ends`, `whole_sentences`,
#         `first_sentence`, `head`), Unicode's default sentence boundary,
#         UAX #29 revision 39 (Unicode 14.0.0) section 5, rules SB1 to SB998
#         on the Sentence_Break property of its Table 4, the same for every
#         alphabet, no list of words and no regular expression, one pass
#         over the text (linear); every case of SentenceBreakTest-14.0.0.txt
#         is a test (tests/data/unicode), and the property is the one of
#         SentenceBreakProperty-14.0.0.txt for every code point (checked
#         outside the repository). A line printed differently:
#         `presentation.memory_line` for a memory with an empty observation
#         shows the whole sentences of its first 1,200 characters
#         (`presentation.START_CHARS`), or, when no sentence ends in them, a
#         cut after a word (with no space, where no character is split),
#         where until 6.16.0 it cut at the 1,200th character, inside a word.
#         The block's packing, which cuts the line again by its budget, is
#         korely-agent's (services/context.py). No prompt changes, no model
#         call. Revised the same day: the first form was quadratic (a
#         terminator and 15,900 spaces took 204 s).
VERSION = "6.17.0"
