# Contributing

Open an issue before you start on a change larger than a small fix, so that
what it should do is agreed before you spend the time on it. A vulnerability
goes to security@korely.ai instead, never to an issue or a pull request
([SECURITY.md](SECURITY.md)).

The code is under the Apache License 2.0 ([LICENSE](../LICENSE)), and so is what
you send: a contribution submitted for inclusion is under the same licence,
with no further terms (section 5 of the licence). There is no contributor
licence agreement to sign. Send only code you have the right to license that
way, and keep the copyright and licence notices of anything you bring in from
elsewhere.

## Running the tests

Everything runs in Docker: the product wants Python 3.11, and a suite that
passes only on one laptop is not a suite.

```bash
make test        # no database, no network
make test-all    # with Postgres and pgvector, started on port 55433
make migrate-check
```

The suite empties the database it runs on. When another checkout may be
running on the same Postgres, give the run a database of its own:
`make test-all TEST_DB=korely_test_mine` (the name must say `test`; it is
created when missing).

The tests are hermetic: `tests/conftest.py` empties the provider keys and
closes the network to anything but this machine, so no test calls a model or
spends money. A test that needs a model's answer fakes it.

## The rules the code keeps

- **No word lists and no regular expressions decide language, intent, tense
  or scope**, in the code or in a prompt, in any language. A model decides, or
  a rule that holds for every alphabet. A refusal is a token or a field, never
  a sentence searched for in a reply.
- **A model's JSON is read with `memory_core.model_json.read_object`**,
  never a bare `json.loads`; a reply that does not parse is never "no facts".
- **Every model call goes through `korely_agent/core/llm.py`** and its gate,
  never a client made by hand. A project's region decides its provider, and a
  region with no writer leaves its memories waiting: no fallback from `eu` to
  `global`, ever.
- **Logs never carry end users' words**: engine errors with `log_safe.error`
  or `log_safe.stack`, never the text of an exception or `exc_info=True`.
- **Every `/v1` route and every MCP tool writes the audit trail**
  (`services.usage.log_v1_request`): ids, counts and labels, never the text of
  a memory, a fact or a question.
- **`korely_agent/services/memory_core/` changes only with the maintainers'
  review**, each rule citing the paper it comes from, with its `VERSION` bumped
  and `MANIFEST.json` regenerated (`python scripts/memory_core_manifest.py`)
  in the same commit. A switch of the engine that is off by default is turned
  on only after a measurement.
- **The confidence a model states is not a threshold**: it runs from 0.90 to
  1.00 even when the model is wrong (Xiong et al., ICLR 2024). A decision is
  taken on a categorical field. A single value is a label for the judge, with
  the value it was measured on, never a rule that closes a fact by itself
  (Lin, Mausam, Etzioni, EMNLP 2010; Ritter et al., EMNLP 2008).
- **Two clocks**: when a fact was true (`valid_from`, `invalid_at`, read by
  `as_of`) and when the store knew it (`created_at`, `closed_known_at`, read
  by `known_as_of`; Graphiti, Rasmussen 2025 §2.2.3). A plan that comes true
  stays in the history: its row closes where the fact begins and the fact is a
  new row, never a promotion in place. A fact never closes before it begins.
- **Memories are searched by meaning** (the cosine on the vectors); the word
  search left on 12 Oct 2026, measured worse (LongMemEval 83.1% against
  85.6%).
- **Every door that calls a model reads the project's region**, never
  `global` by default. Where a model reads the memories (`processing_region`)
  is not where the data is kept (`region`): a text never says "processed in
  the EU" for a `global` project.
- **After an erasure** the end user's id stays only as a keyed pseudonym
  (`services/pseudonyms.py`): still personal data, never "anonymous".
- Comments and messages in English, saying why. A text an operator reads
  (README, `.env.example`, the dashboard) says what the code does, and a change
  to one changes the other.

## Commits and the changelog

One change per commit, its first line `area: what changes`, then a paragraph
on what was wrong and what the change does. A change an operator notices gets
an entry in [CHANGELOG.md](../CHANGELOG.md) saying what to do before and after
upgrading and what the product answers differently.
