# Benchmarks

Every accuracy and install-time number we publish, with how it was measured,
what it cost and how to run it again. The two benchmark harnesses are in this
repository: [`scripts/bench_locomo.py`](../scripts/bench_locomo.py) and
[`scripts/bench_longmemeval.py`](../scripts/bench_longmemeval.py). Both
benchmarks were last run in full on **0.1.36** (15 October 2026). No
release since, 1.0.1 included, has been run on either benchmark in full. This
repository's history starts at 1.0.1: the versions before it are not in it,
so the numbers below cannot be rerun on the code that produced them, only on
the current one with the current scripts.

## Results

| Number | What | Questions | Reader / judge | Version, date |
|---|---|---|---|---|
| **86.9%** | [LoCoMo](#locomo), Mem0's judge prompt, categories 1-4 | 1,338 / 1,540 | gpt-4o-mini / gpt-4o-mini | 0.1.36, 16 Oct 2026 |
| 79.9% | [LoCoMo, the whole conversation instead of the memory](#full-context-baseline) (no Korely in the loop) | 1,231 / 1,540 | gpt-4o-mini / gpt-4o-mini | 7 Oct 2026 |
| 80.8% | [LoCoMo](#locomo), the same blocks as the 86.9% read by an open-weight model | 1,245 / 1,540 | gpt-oss-120b / gpt-oss-120b | 0.1.36, 16 Oct 2026 |
| **87.2%** | [LongMemEval](#longmemeval), oracle split, the benchmark's own reader prompt and judge | 436 / 500 | gpt-4o-2024-08-06 / gpt-4o-2024-08-06 | 0.1.36, 16 Oct 2026 |
| 129 and 129 | [Europe writer against Global writer](#writers-and-context-budget), one LoCoMo conversation | 152 each | gpt-oss-120b / gpt-oss-120b | 0.1.16-dev, 6 Oct 2026 |
| +15 | [Multi-hop questions answered with a 4,000-token block](#writers-and-context-budget) since 0.1.28 | 205 against 190 of 282 | gpt-oss-120b / gpt-oss-120b | 0.1.28-dev, 12 Oct 2026 |
| 3 min 40 s | [Laptop quick start](#install-time), from `git clone` to a running install | - | - | 1.0.1-dev, 29 Oct 2026 |
| 1 min 39 s | [Empty server to first fact](#install-time), scripted | - | - | 0.1.14-dev, 28 Sep 2026 |

The writer (the model that reads each memory and writes the facts) is
gpt-oss-120b on Scaleway, in Paris, for every row except the Global writer
row. Vectors are computed locally with EmbeddingGemma-300m. Percentages are
of the questions judged. The 95% intervals are Wilson intervals.

## LoCoMo

**The benchmark.** LoCoMo (Maharana et al., *Evaluating Very Long-Term
Conversational Memory of LLM Agents*, ACL 2024) is ten long conversations
between two people. We use `locomo10.json` from
[snap-research/locomo](https://github.com/snap-research/locomo) (SHA-256
`79fa87e9…a698ff4`): 272 sessions, 5,882 turns and 1,986 questions. We ask the
1,540 questions of categories 1-4 and skip category 5 (adversarial, 446
questions).

**How the store is filled.** Each person in a conversation is an end user of
their own. Every turn is written once, into its speaker's memory, together
with the turn it answers; the session's date is the write's timestamp. That is
5,882 memories. Once the writer has digested a conversation, each question
calls `GET /v1/context` with 2,000 tokens for each person (4,000 in all). The
reader gets the two blocks under the two names, with "Current date" set to the
conversation's last session day.

**The judge.** Mem0's LoCoMo judge prompt, verbatim (`ACCURACY_PROMPT`,
[mem0ai/mem0 at `aae5989`](https://github.com/mem0ai/mem0/blob/aae5989e78a6188b3b047c104d960c9ad0927e75/evaluation/metrics/llm_judge.py)),
at temperature 0 in JSON mode. The reader prompt is ours, and is in the script.
"Mem0's protocol" on this page means Mem0's judge prompt, gpt-4o-mini as reader
and judge (the model of the Mem0 paper's experiments, Chhikara et al.,
[arXiv:2504.19413](https://arxiv.org/abs/2504.19413)), and categories 1-4.

**The run.** 0.1.36 on a fresh database: write, read and judge with
gpt-oss-120b on Scaleway, 3 h 49 min on a laptop. **80.8%** (95% CI 78.8-82.7).
Then the blocks that run saved were read again by gpt-4o-mini and judged by
gpt-4o-mini through OpenRouter: **86.9%**.

| Category | gpt-4o-mini (86.9%) | gpt-oss-120b (80.8%) | Whole conversation, gpt-4o-mini (79.9%) |
|---|---|---|---|
| 1 multi-hop | 233 / 282 | 181 / 282 | 228 / 282 |
| 2 temporal | 253 / 321 | 237 / 321 | 167 / 321 |
| 3 open-domain | 63 / 96 | 58 / 96 | 61 / 96 |
| 4 single-hop | 789 / 841 | 769 / 841 | 775 / 841 |

**What it cost.** The writer made 13,225 model calls, 29.6 M tokens in and
2.9 M out: 6.20 EUR at Scaleway's 0.15 / 0.60 EUR per million tokens, about
0.001 EUR a memory. The gpt-oss reader and judge cost 1.14 EUR, and the
gpt-4o-mini reread about 1.05 USD (estimated from the tokens at list price).

### Full-context baseline

The 79.9% row is the same 1,540 questions, the same reader prompt, the same
date line and the same judge, with the whole conversation (every session, with
its date) in place of the memory blocks. No Korely in the loop. A conversation
is about 19,000 tokens; a prompt with the memory blocks averaged 3,900. Most
of the gap is temporal questions (253 against 167 of 321). The Mem0 paper
reports 72.9 for full context under its own harness: full-context numbers move
with the harness, so compare ours with ours. This reread cost about 4.89 USD
(`--full-context`, in [How to reproduce](#how-to-reproduce)).

### Caveats

- **The judge alone is worth about 7 points.** The gpt-oss answers of an
  earlier full run (0.1.20-dev, 1,247 of 1,540 = 81.0% by the gpt-oss judge)
  judged again by gpt-4o-mini with the same prompt: 1,361 = 88.4%, **+7.4
  points with no answer changed**. Most of the distance between 80.8% and
  86.9% is the judge, not the reader. Compare only numbers with the same judge.
- **Variance.** Three full runs with gpt-oss as reader and judge, on two
  versions (0.1.20-dev twice, 0.1.36): 81.0%, 81.9%, 80.8%. Their gpt-4o-mini
  rereads: 86.2%, 86.4%, 86.9%. On one conversation (152 questions) about 20
  answers flip between nearly identical blocks, so a one-conversation result
  is not a ranking.
- **The 86.9% was a reread, not a fresh run.** It read the blocks the gpt-oss
  run saved (`--reread`), with one difference from `--run`: a saved block holds
  the reader note and the memory together, so the note went into the user turn
  instead of the system prompt. A fresh `--run` with gpt-4o-mini asks the store
  again and keeps the note in the system prompt, so it is not the same
  measurement (see [How to reproduce](#how-to-reproduce)).
- **The reread needs that run's blocks.** `--reread` sends the very prompts of
  the 86.9% only when given the blocks the 0.1.36 run saved; a new install
  writes its own store and its own blocks, so expect a number within the
  variance above. The full-context prompts depend only on `locomo10.json`.
  Either way the reader is a model at temperature 0, which still varies a
  little from one call to the next.
- **Category 5 is excluded.** A harness that scores the adversarial questions
  reports a different number.

## LongMemEval

**The benchmark.** LongMemEval (Wu et al., *LongMemEval: Benchmarking Chat
Assistants on Long-Term Interactive Memory*, ICLR 2025) is 500 questions of
six types, 30 of them abstentions. We use the **oracle split**,
`longmemeval_oracle.json` from the Hugging Face dataset
`xiaowu0162/longmemeval-cleaned` (SHA-256 `821a2034…5d38620c`): each question's
history holds only the sessions with the evidence. It is the easiest of the
three splits; `_S` buries the evidence in about 50 sessions (about 115,000
tokens) and `_M` in 500.

**The protocol.** The benchmark's own, at commit
[`9e0b455`](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc).
Each question is an end user; every turn of 12 characters or more is a memory
(`role: content`, the session's date as timestamp): 10,949 memories.
`GET /v1/context` returns a 4,000-token block, which goes to the reader in the
benchmark's prompt for retrieved chats plus user facts, step by step
(`prepare_prompt`), with the question's date as "Current Date". The judge is
the benchmark's (`get_anscheck_prompt`, one prompt per question type and one
for abstentions). Reader and judge are gpt-4o-2024-08-06 through OpenRouter.

**The result.** 436 of 500, **87.2%** (95% CI 84.0-89.8; the mean of the six
types is 87.5%).

| Type | Right |
|---|---|
| single-session-user | 69 / 70 |
| single-session-assistant | 55 / 56 |
| single-session-preference | 23 / 30 |
| multi-session | 108 / 133 |
| knowledge-update | 65 / 78 |
| temporal-reasoning | 116 / 133 |

**What it cost.** The writer made 27,146 calls, 55.9 M tokens in and 6.6 M out:
12.32 EUR at Scaleway's prices. The reader cost 4.78 USD and the judge
0.47 USD (at OpenAI's list price, as the script counts them).

### Caveats

- **What the oracle split measures.** The LongMemEval paper gives GPT-4o the
  oracle sessions themselves as context and reports 87.0% (92.4% with
  Chain-of-Note). So on this split the memory matches handing the reader the
  evidence sessions; it does not show how well the store finds evidence among
  distractors. Many published LongMemEval numbers are on `_S`, and ours is
  not comparable with them. We have not run `_S` in full.
- **The judge** is the benchmark's, which its paper reports agrees with human
  experts more than 97% of the time.
- **One run** at 500 questions; there is no repeat to measure its variance.
- **Where the 64 wrong answers were lost**, by tracing each question's evidence
  turns: 49 had every evidence turn in the block (the reader's mistake), 8 lost
  evidence between the search and the block, 1 at the write, 1 at the search,
  and 5 have no evidence turn marked in the dataset.

## Writers and context budget

**Europe or Global, 129 and 129.** The whole writer (facts, judge and
vocabulary) as gpt-oss-120b on Scaleway, then as Gemini 3.1 Flash-Lite, each on
a fresh store with LoCoMo conversation 26 (152 questions), read and judged by
gpt-oss-120b, 4,000 tokens in all, on 0.1.16-dev (6 Oct 2026). Both answered
129 of 152 (84.9%, 95% CI 78.3-89.7), and the two disagree on 18 questions (9
each way). On one conversation that says the two writers are level, and nothing
finer. Gemini wrote at 0.00194 USD a memory at list price.

**A larger block since 0.1.28.** Above 2,000 tokens the block now asks the
search for more memories and orders those past the first ones by maximal
marginal relevance. On the 282 multi-hop questions of LoCoMo, at 4,000 tokens
per person, the same written store read with the old block and the new one: 190 then 205
right, 23 won and 8 lost (McNemar p = 0.011), gpt-oss-120b as reader and judge
(12 Oct 2026). At 2,000 tokens per block and below the block is unchanged byte
for byte.

## Install time

- **3 min 40 s, laptop.** The README's quick start, run as written by a
  script, on a MacBook with an M4 (29 Oct 2026, 1.0.1-dev): from `git clone` to
  `/health` answering and the account created took 219 s, including building
  the image and downloading the embedding model (about 1.2 GB). The dashboard
  login, a memory write (201) and a search (200) followed by 221 s. No model
  key was set, so no fact was mined. Docker's cache was not emptied before the
  run: a machine that has never pulled the base images (Postgres with pgvector,
  Caddy, Python) also downloads those.
- **1 min 39 s, server, scripted.** [`scripts/timed_install.sh`](../scripts/timed_install.sh)
  on a fresh Hetzner CPX22 (2 vCPU, 4 GB, Debian 13), 28 Sep 2026, 0.1.14-dev:
  from a machine with nothing on it (Docker installed by the script) to a store
  that has mined a fact, closed it with a later one and answers `as_of`
  correctly (the code was copied from a local checkout rather than cloned). Older than 0.1.19, so the image did not yet carry the embedding
  model.
- **3 min 41 s, server, by hand.** The same day, on the same kind of machine,
  0.1.13: an agent that had never seen the code, allowed only the public
  documentation and the running service's own pages, from the first command to the first fact.
  About 1 min 30 s of it was the machine's.

## How to reproduce

Python 3.9 or later; both scripts use the standard library alone. They spend
only with `--run` (or `--calibrate`), and secrets come from the environment,
never the command line.

**1. An install with the Europe writer.** Install the current release as
the README says (0.1.36, which produced the numbers above, is not in this
repository: expect your numbers to differ by what changed since, see
[What these numbers do not say](#what-these-numbers-do-not-say)), and give the
writer gpt-oss-120b on Scaleway in `.env`:

```bash
EXTRACTION_PROVIDER=mistral            # the OpenAI-compatible provider
MISTRAL_API_BASE=https://api.scaleway.ai/v1
MISTRAL_EXTRACTION_MODEL=gpt-oss-120b
MISTRAL_API_KEY=...                    # a Scaleway key: paid
EMBEDDING_PROVIDER=local
```

Then one tenant and an admin key (the admin key reads what the writer spent):

```bash
docker compose exec api python -m korely_agent.manage create-tenant --name bench
docker compose exec api python -m korely_agent.manage issue-key --tenant <id> --admin --name bench-admin
export KORELY_API_URL=http://127.0.0.1:8000 KORELY_API_KEY=kor_self_... KORELY_ADMIN_KEY=kor_self_...
```

**2. LoCoMo.** Download `locomo10.json` (CC BY-NC 4.0) from the LoCoMo
repository.

```bash
# free: the estimate, nothing written, no model asked
python3 scripts/bench_locomo.py --data locomo10.json

# a few cents: what one write costs on your writer, 30 turns
python3 scripts/bench_locomo.py --data locomo10.json --calibrate 30

# the 80.8% run: about 6 EUR of writing and 1 EUR of reading on Scaleway
READER_BASE_URL=https://api.scaleway.ai/v1 READER_MODEL=gpt-oss-120b READER_API_KEY=... \
JUDGE_BASE_URL=https://api.scaleway.ai/v1 JUDGE_MODEL=gpt-oss-120b JUDGE_API_KEY=... \
python3 scripts/bench_locomo.py --data locomo10.json --run --speakers each \
  --write-cost <the calibrated figure> --write-cost-speakers each \
  --user-prefix loc- --out loc-gptoss.jsonl

# Mem0's protocol on the same store: same --user-prefix, a new --out.
# No turn is written again; about 1 USD of gpt-4o-mini
READER_BASE_URL=https://openrouter.ai/api/v1 READER_MODEL=openai/gpt-4o-mini READER_API_KEY=... \
JUDGE_BASE_URL=https://openrouter.ai/api/v1 JUDGE_MODEL=openai/gpt-4o-mini JUDGE_API_KEY=... \
python3 scripts/bench_locomo.py --data locomo10.json --run --speakers each \
  --reader-price 0.15 0.60 --judge-price 0.15 0.60 \
  --user-prefix loc- --out loc-gpt4omini.jsonl
```

**The 86.9% and the 79.9%.** Neither asks the install: they need only a reader
and a judge key (here OpenRouter's), and without `--run` they print the
questions and the estimate and stop, for free. The reader may write 1,024
tokens and the judge 300, as in the runs that produced the numbers; the reader
prompt, the date line and Mem0's judge are those of `--run`.

```bash
export READER_BASE_URL=https://openrouter.ai/api/v1 READER_MODEL=openai/gpt-4o-mini READER_API_KEY=...
export JUDGE_BASE_URL=https://openrouter.ai/api/v1 JUDGE_MODEL=openai/gpt-4o-mini JUDGE_API_KEY=...

# the 86.9%: the blocks the gpt-oss run saved in loc-gptoss.jsonl, read again by
# gpt-4o-mini, each block whole in the user turn under its person's name.
# About 1 USD (1,540 questions, about 3,900 tokens a prompt)
python3 scripts/bench_locomo.py --data locomo10.json --reread loc-gptoss.jsonl --run \
  --reader-price 0.15 0.60 --judge-price 0.15 0.60 --out loc-reread-gpt4omini.jsonl

# the 79.9%: the whole conversation (every session under its date) in place of
# the memory. About 5 USD (1,540 prompts of about 19,000 tokens)
python3 scripts/bench_locomo.py --data locomo10.json --full-context --run \
  --reader-price 0.15 0.60 --judge-price 0.15 0.60 --out loc-full-gpt4omini.jsonl
```

The costs are at gpt-4o-mini's list price, 0.15 / 0.60 USD per million tokens
in and out; the free estimate prints a somewhat higher figure (about 1.6 and
5.9), because it counts the out tokens of a reasoning model. `--reread` also
takes a file written by `--blocks` (the blocks of a stored install, for free),
reads only the lines its run judged or `--blocks` wrote, and prints how many
verdicts changed against that run's. Both resume with the same command.

The `--*-price` flags are per million tokens in and out, in EUR (the default is
Scaleway's gpt-oss-120b). `--eur-cap` (default 9, in the unit of the prices
you pass) stops a run before its estimated spend passes the cap;
`--limit-conversations 1 --limit-questions 20` is a cheap first try. The same
command resumes an interrupted run. Each run writes `<out>.summary.json` with
its conditions (install version, writer, reader, judge, budget, data hash).

**3. LongMemEval.** Download `longmemeval_oracle.json` from the Hugging Face
dataset `xiaowu0162/longmemeval-cleaned`.

```bash
# free: the questions, the memories to write, the reader and judge estimate
python3 scripts/bench_longmemeval.py --data longmemeval_oracle.json --estimate

# the 87.2% run: about 12 EUR of writing with gpt-oss on Scaleway,
# about 5-9 USD of gpt-4o (--usd-cap, default 15, caps the reader and judge)
READER_BASE_URL=https://openrouter.ai/api/v1 READER_MODEL=openai/gpt-4o-2024-08-06 READER_API_KEY=... \
JUDGE_BASE_URL=https://openrouter.ai/api/v1 JUDGE_MODEL=openai/gpt-4o-2024-08-06 JUDGE_API_KEY=... \
python3 scripts/bench_longmemeval.py --data longmemeval_oracle.json --run --out lme.jsonl
```

`--limit 20` runs the first 20 questions. The cap covers the reader and the
judge only: the writing is billed by your writer's provider.

**4. Install time.** The laptop figure is the README's quick start with a clock
around it. On a server, as root on a fresh machine:
`GOOGLE_API_KEY=... bash scripts/timed_install.sh --label <provider-and-size>`
(a Gemini key, for a few cents of writing; without one the run stops once the
store answers, and the report says so).

## What these numbers do not say

- **Not a ranking.** Other systems' published numbers use other readers,
  judges, prompts and splits; the judge alone moved ours by 7 points. Only
  numbers measured with the same harness, reader and judge compare.
- **Not 1.0.1.** The full runs are of 0.1.36. Releases since then changed the
  write path and the block; neither benchmark has been rerun in full.
- **Not LongMemEval `_S` or `_M`**, the splits where the evidence has to be
  found among unrelated sessions.
- **Not the time dimension.** Neither benchmark asks what was true on a given
  date or what the store knew then, which is what Korely Agent is for. These
  numbers say that the memory answers ordinary questions about a person; the
  `as_of` and `known_as_of` reads are covered by the test suite, not by a
  public benchmark.
- **Not latency or price.** The costs above are what the benchmark runs paid
  their model providers. No read latency figure is published here.
- **Not other languages.** Both benchmarks are in English.
