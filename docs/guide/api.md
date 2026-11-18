# Talking to it: REST, MCP, SDKs and the shell

The REST API is the contract; `/docs` on your install has all of it. The clients
published for the hosted service work against your own server unchanged, by
pointing them at it:

```bash
pip install korely-memory
```

```python
from korely_memory import Korely

korely = Korely(api_key="kor_self_...", base_url="https://your-domain")

korely.add("Customer 4821 moved to the Basic plan.", user_id="c4821")
korely.get_facts(user_id="c4821", as_of="2026-03-20")
```

There is a JavaScript client too (`npm install korely-memory`, the same name
on npm as on PyPI), same idea, with the address as `baseUrl`:

```javascript
import { Korely } from "korely-memory";

const korely = new Korely({ apiKey: "kor_self_...", baseUrl: "https://your-domain" });
await korely.add("Customer 4821 moved to the Basic plan.", { user_id: "c4821" });
await korely.getFacts({ user_id: "c4821", as_of: "2026-03-20" });
```

Neither is required: nothing in them is more than an HTTP call you can make
yourself. [`examples/vscode-demo`](../../examples/vscode-demo/) is the story at
[the top of the Time page](time.md) as a VS Code project: the install, a key, one agent that
writes, another that reads, and the dashboard's graph with the clock put back.

An agent that speaks MCP connects to `https://your-domain/mcp` (Streamable
HTTP) with the same key, `Authorization: Bearer kor_self_...`, and gets six
tools: `korely_add`, `korely_search`, `korely_get_facts`, `korely_get_context`,
and, for the key's `memories:write` scope, `korely_correct_fact` (correct a
fact by its `fct_` id, as `korely_get_facts` and `korely_search` print it, or close it with `close=true`: the old fact is closed and a
new one written, never edited in place) and `korely_forget_memory` (forget a
memory by its id, or erase it with `erase=true`; the answer counts the facts it
closed or removed). They are `PATCH /v1/facts/{id}`, `POST
/v1/facts/{id}/forget` and `DELETE /v1/memories/{id}` for an agent that cannot
leave the conversation. `PATCH /v1/memories/{id}` replaces a memory's text and
extracts it again; a fact of it you forgot, corrected or posted in the past
tense stays over, also when the new text says it again, and the answer lists
those facts in `kept_forgotten` (since 1.0.27). Post one again (`POST
/v1/facts`) to make it true. `POST /v1/facts` takes a `tense`, `current` (the
default), `past` or `planned`: the writer says it, the store does not guess it
from the verb, and a `past` fact closes the open fact it restates. The
dashboard's Install page has the snippet for the
common clients. An account runs four tool calls at once and the rest wait
their turn, and every account together runs at most eight, a freed place
going to the account with the fewest running: one account's burst, or a few
accounts' at once, does not take the threads every other account needs.

`GET /v1/context` (and `korely_get_context`) is the block to put in the
prompt. Its `token_budget` defaults to 800 tokens; for an agent that answers
questions about the past we recommend 4000: at 4,000 tokens per person, 15
more of 282 LoCoMo multi-hop questions were answered than with the earlier
block ([Benchmarks](../benchmarks.md#writers-and-context-budget)). A larger
budget is a larger prompt, so an agent that needs a fast answer, as a voice
agent does, can stay lower.

A memory enters the block whole, or cut after a complete sentence, never
inside its first sentence (since 1.0.30). One whose first sentence does not
fit the room left is left out, the block goes on with the next ones, and the
answer names every memory it chose and does not show in `memories_left_out`
(`mem_` ids): raise `token_budget`, or read one with `GET /v1/memories/{id}`.
`korely_get_context` says how many after the block. Sentences end where
Unicode's default sentence boundary puts them (UAX #29), in every script; a
memory with no sentence end in its first 1,200 characters is cut after a word
and marked "…".

A memory says what kind of text it is: `kind: "note"` (the default), what
somebody said or decided, or `kind: "session"`, the log of a work session (what
a coding assistant's hooks save, `korely init --tool`). `GET /v1/context`,
`POST /v1/memories/search`, `GET /v1/memories`, `GET /v1/facts` and
`GET /v1/profile`, and the MCP's `korely_get_context`, `korely_search` and
`korely_get_facts`, take `exclude_sessions=true` to leave the sessions out,
and the facts only sessions state with them: on our session bench, read by
hand, 60 to 71% of a session's facts were working artefacts (commands, paths,
quoted values). A fact a note states too stays.

A search can leave out what is barely related: `min_score`, 0 to 1, on
`POST /v1/memories/search` and the MCP's `korely_search`, drops the memories
whose `score` is below it, so a question with nothing close in the store gets
an empty list instead of the nearest unrelated memories. Absent, there is no
floor. 0.20 is the value measured on LongMemEval with the local
EmbeddingGemma: no best evidence lost, and more than half of the searches
whose answer was not in the store came back empty. A higher floor drops more
weak matches and, past some value, true ones too; the right value depends on
your texts and on the embedding model. `GET /v1/context` has no floor.

With `MEMORY_WINDOWS` on (since 1.0.33, off by default:
[operations](operations.md#windows-of-long-memories-memory_windows)), a long
memory is also found by the windows of its text: its `score` is the better of
its own vector's and its best window's, and `min_score` reads that one. The
memory comes back whole, once.

A search reads the memories unless it is asked for more. `types` on
`POST /v1/memories/search` is the list of the kinds it reads, each once:
`["memories"]` by default, `["facts"]` for the facts true now, the ones
`GET /v1/facts` lists, or `["memories", "facts"]` for both. A fact written
with `POST /v1/facts` is among them: it has no memory, so a search of the
memories never finds it. A fact still waiting for its vector is listed by
`GET /v1/facts` and found by a search once the vector is computed. With
both kinds each is ranked by its own score and the two are merged by rank,
the first memory, the first fact, the second memory, and so on, because a
fact's cosine and a memory's are not on one measured scale: read each
result's `score` against the others of its `type` only. At equal rank a
memory comes first, whatever order the list names them in, so with
`limit: 1` the best memory is the answer whenever there is one. A fact's
result has its `fct_` id, its line (`subject · predicate · object`, then
its dates) as `snippet`, and the whole fact as `GET /v1/facts` shows it in
`fact`. `run_id`, `metadata` and `min_score` narrow the memories only, and
with the facts in the list each of them is a 422: the facts have no floor
of their own, so a search with a floor stays one that gives an empty list
when nothing is close. `korely_search` takes the same kinds as one text,
`types="facts"` or `types="memories,facts"`.

A result shows the first 280 characters of the memory (`snippet`);
`full_text: true` adds the whole text as `content`, so an agent reads a
long memory without a `GET /v1/memories/{id}` for each result, at the
price of more tokens in its prompt (a memory can hold 16,000 characters).
With `full_text=true`, the whole texts of a `korely_search` answer take at
most 8,000 tokens as printed, the size of `korely_get_context`'s largest
block: past them a result shows what is left, and never less than its 280
characters, its line says `[cut]` and a NOTE at the end counts them and
says how to read them whole (a memory with `GET /v1/memories/{id}`; for a
fact, a smaller `limit`), so an answer can pass the 8,000 tokens by what the
answer without `full_text` would hold. In the answers of `korely_search`, `korely_get_facts` and
`korely_add`, every line of a memory's or a fact's text after its first is
indented by four spaces, so a line at the margin is always a result of its
own: a text cannot add one. A search result's id is the last one on the
line that begins with its score. From 1.0.29 every fact `korely_get_facts`
and `korely_add` print ends its first line the same way, with its `fct_` id,
the one `korely_correct_fact` takes: up to 1.0.28 only `korely_search` with
`types=facts` gave a fact's id. Without `types` and
`full_text` a search answers as it did before 1.0.13, except that
indentation for a text with a line break. Since 1.0.16 the context block
(`GET /v1/context`, `korely_get_context`) follows the same rule for its
memories and facts. The SDKs do not send `types` or
`full_text` yet. For an agent that answers questions, the context block
is still the read to start from: it reads the facts and the memories
together, within a budget.

### What to save, so the memory stays clean

The facts are only as good as what the agent sends. A memory per thing that
was said or decided, in the conversation's own words, with the date it
happened (`timestamp`), gives the writer what it needs. Some things are better
left out:

- **What Korely gave the agent.** The context block and search results are
  already in the store; written back, each becomes one more memory to search
  through. Facts restated are reconfirmed rather than duplicated, but the
  memories pile up.
- **The agent's own instructions, configuration files and tool output.** They
  are not about the person, and saving them every session fills the store
  with the same text.
- **Work logs as notes.** Write them with `kind: "session"`, and read with
  `exclude_sessions=true` when you want what was said and decided.

Give every person their own `user_id`, an opaque one rather than an email
address, and every agent its `agent_id`, so a fact can be traced to who
wrote it. The `user_id` also keeps a conversation in order: a person's turns
written one after the other are printed in the context block as they were
said, at the place of the most relevant of them. Memories with no `user_id`
(an agent's notes, a team's memory, an assistant's through MCP with no ids)
keep the search's order, the most relevant first (since 1.0.22).

**Every write says whose memory it is (since 1.0.19).** `POST /v1/memories`,
each row of `POST /v1/batch` and `POST /v1/facts` need a `user_id` or an
`agent_id`; with neither the answer is a 422 with the code
`identity_required`, nothing is written, and the message names the two
fields (in a batch, at the row's place:
`memories[3].user_id or memories[3].agent_id is required: ...`). An id made
only of whitespace, format characters (U+200B, U+FEFF...) or combining marks
names nobody and counts as neither; `run_id` alone is not enough. A key
confined to one end user gives the write that end user, as before. The
refusal comes before the plan's quota on the Cloud: it counts against
nothing. Without an identity the store read every writer that sent none as
one person and compared their facts as that person's, so one writer's new
fact could close another writer's. Memories stored without ids before
1.0.19 stay as they are: they are read, edited and forgotten as before, and
nothing rewrites them. A program that wrote without ids and now sends a
`user_id` starts a second memory: its new facts are not compared with the
old ones, so a change written after the upgrade does not close an old fact
that had no id (correct or forget that one with `PATCH /v1/facts/{id}` or
`POST /v1/facts/{id}/forget`).

A memory written with an `agent_id` and no `user_id` is the agent's own
(since 1.0.12): the memory of a team of agents, or an agent's own notes. The
writer is asked for facts about what the text names, a statement in the
first person is about that agent, and a procedure or a rule in force is
asked as a current fact. A memory with neither id (one stored before
1.0.19, or what an assistant writes over MCP, below) is read as the memory
of one person, an end user named `user`: "I prefer the dark theme" is a
preference of `user`. A memory with a `user_id` is read with the same
instructions.

Over MCP the assistant's connection says who writes: the name the client
gave when it connected (`clientInfo.name`, which the protocol requires) is
kept on each memory as `client` and in the audit trail with the key. So a
`korely_add` that names neither a `user_id` nor an `agent_id` is written
where it was before 1.0.19, in the project's memory with no end user: reads,
the walls of confined keys and the judge see it as before, and a person's
facts written before the upgrade and after it are one memory. A session
whose client gave no name it can be told by is held to the ids, as REST
is. The tool's own `user_id` or `agent_id`, when the assistant passes one, is
used. Several agents that share one memory through MCP should each pass
their own `agent_id`: without it they are one writer.

**One memory for a team of agents.** Each agent writes with its own
`agent_id` and reads without one, or with `reader` set to its own name,
which filters nothing (below). The id does two different things on the two
sides. A write is weighed against the facts of every agent of the same end
user (the same `user_id`, or none, as in a team's memory), so one agent's
change closes another agent's fact: agent-b's "the Cloud runs 1.0.8" closes
agent-a's "the Cloud runs 1.0.7". A read with an `agent_id` keeps that
agent's rows alone: agent-a, reading with its own id afterwards, gets
neither value, its own because it is closed and the new one because it is
agent-b's. Read without the id, the block has 1.0.8; which agent wrote a
fact is in `GET /v1/facts` (each fact's `agent_id`), not in the block.
`agent_id` on a read is the way to ask what one agent wrote, not the way for
an agent to read the team's memory.

An agent can say who is reading (since 1.0.15): `reader`, its own name, on
`GET /v1/context`, `GET /v1/facts` and the MCP's `korely_get_context` and
`korely_get_facts`, by the rule of an `agent_id` a write gives (any line
break, `..` or a `/` is refused). It filters nothing: `GET /v1/facts` and
`korely_get_facts` answer the same with it and without it, and the context
block keeps the same known facts and the same `stable`, opening with the
changes described below. With it, Korely keeps which facts the read handed over as
true, by id, with the fact's end user and the time of the first and of the
last read, for 90 days after the last read (`READ_MARKS_RETENTION_DAYS`).
These read marks are what agent notices read: the next context block of
that reader (`GET /v1/context`, `korely_get_context`) opens with a section
"Changed since you last read" when a fact it was handed has ended since,
"was: ... -> now: ..." or "no longer true: ...", the day the store learned
it and where it came from (`origin`: corrected, forgotten, stated by a
caller, read from a memory); at most 3 lines, inside the budget, each change
told at most once, even to two reads at the same moment (the block reserves
what it shows before it is written; an answer that fails after that loses
it), and REST's answer carries
them as `changed` with the count of the rest, `changed_more` (null when the
changes could not be read this time, under a lock on their records: they
wait for the next read). `origin` is the action that ended the fact:
`corrected` (`PATCH /v1/facts/{id}`), `forgotten` (`POST
/v1/facts/{id}/forget`), `stated` (a fact written with `POST /v1/facts`
replaced it) and `read_from_memory` for every other closure, the store's
reading of a memory and every end in place: a past statement ends a fact in
place, whether a memory said it or a caller (`POST /v1/facts` with `tense:
past`, whose record keeps a deletion from giving the fact back and is no
forget), and writes on it only its end. "now" is a
fact true now, begun and not ended, down the chain of what replaced the
fact, through facts the key may read only: a successor the key may not
read gives "no longer true", and nothing of it is told. A fact whose
memory was forgotten is told without its words (`was` null, "a fact whose
memory was forgotten" in the block). In an agent's memory with no end user
only the explicit changes are told unless the read passes `changes=all`.
A read of the past (`as_of`, `known_as_of`) or of the history
(`include_invalidated=true`) keeps nothing, and neither do the dashboard's
reads, `GET /v1/profile`, `GET /v1/memories/{id}`, its `/history` and the
memory search. The name follows the rule of `agent_id` and is not checked
against the key: any caller of the project can give any name. Name the
agent, never an end user: an end user's id given as the reader's name is
kept in the marks of every fact that reader read (if you do, that end
user's erasure takes those marks too, as an agent namespace's deletion
takes the marks read under its name).

`/docs` is served by this machine with its own files (Swagger UI 5.33.1,
Apache 2.0, in `korely_agent/docs_ui`), so it works on a network with no way
out; `/openapi.json` is the same contract as JSON. The hosted service
serves neither (since 1.0.22): its API is documented at
https://korely.ai/docs/api-reference/.

`/docs` has two sections. `v1` is the memory API, what your agents call with
their keys. `admin` is the administration API: projects, keys, webhooks, the
requests and the usage of the account, its audit trail and the model
settings. The dashboard calls it with its session, and a script can call it
too, with a key issued with the `admin` scope (`manage issue-key --admin`,
or the scope ticked on the API keys page). An ordinary key gets 403 there,
so one leaked from an agent cannot mint more keys. Through `/admin`, an
admin key reaches every project of the account, whatever its own project,
memories included: keep it for your own scripts, never in the tools of one
project. On `/v1` and the MCP it reads and writes its own project only, as
every key does. The reads
of memories, facts, agents, requests and the audit trail take `project_id`
to read one project as its keys read it (404 for a project that is not the
account's, 422 for an empty one), and read the whole account without it;
the dashboard sends the project chosen at its top, and none for **All
projects**. A body that names a project (a new key, a key's move, an alias,
a webhook, the OAuth consent) refuses an empty one the same way, with a
422; where the project may be left out (all but the consent), leave
`project_id` out or send `null`. The spec the site
publishes for the hosted service has both sections
(`scripts/dump_openapi.py --public`).

### Retrying a write: the Idempotency-Key

A write that timed out may have been made. Send it again with the same
`Idempotency-Key` header and it is made once (since 1.0.27), as the IETF
draft "The Idempotency-Key HTTP Header Field"
(draft-ietf-httpapi-idempotency-key-header-08) describes:

```bash
KEY=$(uuidgen)   # one new key per request; send the same one again only to retry it
curl -X POST http://localhost:8000/v1/memories \
  -H "Authorization: Bearer $KORELY_API_KEY" \
  -H "Idempotency-Key: \"$KEY\"" \
  -H 'Content-Type: application/json' \
  -d '{"content": "Ada prefers tea.", "user_id": "ada"}'
```

- **Where**: `POST /v1/memories` and `POST /v1/batch`. Every other route
  ignores the header, as before; `PATCH` and the deletes give the same
  result when sent twice, and `POST /v1/facts` does not honour it yet.
- **The same key with the same request** answers what the first request
  made, with the header `Idempotent-Replayed: true`: the memory (`201`) as
  that request wrote it, with its id, the text and ids you sent, its
  `status` and times now, and `facts: []`; or the job (`202`), with its id,
  status and count now. Nothing is written again, and on Korely Cloud no
  write is counted again. A retry never shows what was written since, an
  edit by another key or the facts extracted since: read them with `GET
  /v1/memories/{id}`, which needs `memories:read` and counts a read. "The
  same request" is the request as checked: the text stripped, the end user
  placed for the key, the client and the time read. If what it made is not
  there for this key (the memory deleted since, or a job another key
  confined to an end user queued, which only that key reads), the answer
  is a 404 `idempotent_target_gone`, naming no id, and nothing is written
  again.
- **The same key with another request** (another text, another end user,
  the other route) is a 422 `idempotency_key_reused`, and nothing is
  written. Use a new key for every new request.
- **A retry while the first is still being made** is a 409
  `idempotency_key_in_use`: retry it as it is, a moment later.
- **A key holds 24 hours** from its write. After that it is a new key, and
  the same request is a new write. The worker's purge deletes the expired
  keys.
- **A key belongs to the project**: another account, or another project of
  yours, with the same key makes its own write; another API key of the same
  project with the same request gets the first answer.
- **A refused write keeps no key** (a 422 of the body, a 403, a quota's
  429): the key is written in the transaction that makes the write, so a
  write that does not commit takes its key with it, and the same request
  with the same key, retried, is written.
- **An erasure takes the key with it**: when an end user's memories are
  erased, the keys of the writes that made them go too, and so does the key
  of every batch with a row of theirs, since its fingerprint is of the rows
  as sent. The same request sent again is written again: a batch is queued
  again whole, the rows of the other end users in it too.
- **The value**: 1 to 255 printable ASCII characters, as a quoted string
  (`"..."`, the draft's form) or bare (`8e03978e-...`, as Stripe's clients
  send it); the two forms are the same key. Anything else is a 400
  `idempotency_key_invalid`. A random UUID is a good key; do not put an
  email address or a person's id in it. The store keeps only its SHA-256
  and the SHA-256 of the request, never the text.

Without the header nothing changes: two identical requests are two writes,
as with Mem0's `infer=False` and Supermemory without an `id`.

## From a shell

Everything an operator does without the dashboard is
`docker compose exec api python -m korely_agent.manage <command>`, and
`--help` after any of them says what it takes:

| | |
|---|---|
| `create-tenant`, `list-tenants` | an account, and its first key |
| `issue-key`, `list-keys`, `revoke-key` | more keys, and taking one out of service |
| `create-operator`, `list-operators`, `reset-password` | dashboard logins |
| `grant-install-admin` | on an install with several accounts, the one person who sets the model provider, its key and the models for all of them (the first login of an install is that person) |
| `erasures-export`, `erasures-replay`, `erasures-superseded` | what `backup.sh` and `restore.sh` run to keep the erasures made after a dump |
| `credentials-export`, `credentials-replay` | what `restore.sh` runs to keep the revocations, key moves and rotations, scopes taken away, passwords and webhook secrets changed after a dump, and to end every session; the replay refuses, with nothing changed, a file older than the data (exit 2) or not signed under `SECRET_KEY` (exit 3); `--unsigned-from-1.0.31` reads a file 1.0.31 wrote |
| `reseal-secrets` | after a rotation of `SECRET_KEY`, seals again under it what a fallback sealed (the screen's provider, key and models, an OAuth client's secret); exit 2 when a value opens with no key |
| `add-webhook`, `list-webhooks`, `delete-webhook`, `rotate-webhook-secret` | webhook endpoints |
| `status` | the whole install on one screen: counts, and whether extraction is running |
| `reextract` | memories and vectors that gave up, queued again |
| `reclassify-relations` | asks again whether each learned relation holds one value, one end user's vocabulary at a time, of the writer of that project's region (the facts of no project: the install's default writer); a dry run unless `--apply` |
| `review-vocabulary` | asks again whether each learned phrase is the relation it was merged into, end user by end user and in each project's region as above; a dry run unless `--apply` |
| `review-objects` | asks whether two objects of an end user's memory are one thing named in two languages or forms ("Italia" and "Italy"), as the same-object question of memory_core 6.1.0 (off) would at the write, in each project's region as above; refused while the switch is off unless `--while-off`; one account (`--tenant`, `--project`, `--user`) or `--all`; the dry run (`--out file --cap amount`, in LLM_RATES' currency, billed as the `operator` stage) writes the proposed links to the file and nothing to the database, `--apply file` writes exactly those, with no model call and one trail row each |
| `recompute-vectors` | every stored vector again, with the embedding model in force, after a change of `EMBEDDING_PROVIDER`; a dry run unless `--apply` |
| `memory-windows` | with `MEMORY_WINDOWS` on (1.0.33, off by default), the windows of the memories stored before, a batch per transaction, resumable; their vectors are computed by the worker; a dry run unless `--apply` ([operations](operations.md#windows-of-long-memories-memory_windows)) |

## Errors

Every error body carries `code` and `message`, next to the `detail` this
install has always sent:

```json
{"detail": "Memory not found",
 "code": "not_found",
 "message": "Memory not found"}
```

```json
{"detail": [{"type": "missing", "loc": ["body", "content"], "msg": "Field required"}],
 "code": "invalid_request",
 "message": "content: Field required"}
```

Branch on `code`: it is stable, and it is the name the hosted service uses
(`invalid_key`, `forbidden`, `not_found`, `stale_write`, `invalid_request`,
`identity_required`, `rate_limit_exceeded`, `search_unavailable`,
`internal_error`...). `message` is
for a person and its wording may change. `detail` is unchanged, a sentence, a
`{code, message}` pair or the list of fields that failed validation, so a
client that reads it keeps working; `/docs` declares all three. The hosted
service runs this same code since 9 October 2026 and answers the same three
keys; before, it answered `{code, message}` alone, which is why the clients
read those two. Since 0.1.12; before, only `detail`.

## What every door refuses

One rule for every input, the same on REST, in a batch, on MCP and on the
OAuth forms (`services/limits.py`):

- **A write with no identity** (since 1.0.19): a memory, a batch row or a
  fact with neither `user_id` nor `agent_id`, or with ids made only of
  whitespace, format characters or marks, is a 422 `identity_required`
  whose `message` says which two fields to add (`user_id or agent_id is
  required: ...`). Over MCP the client's declared name says who writes, and
  only a session whose client gave none gets the same refusal.

- **A NUL byte (U+0000)** anywhere in a request (a body field at any depth,
  a key of `metadata`, a path, a query parameter, a tool argument, a form
  field of `/authorize`) is refused, naming the place: a 422 whose
  `message` is `memories[1].run_id contains a NUL byte (U+0000)`; on MCP
  the line `error: invalid_request, ...`; on `/authorize` a 400 whose
  JSON `error` is `invalid_request` (`/authorize` answers every error of a
  request itself and redirects only to the consent page, never to the
  app's address: RFC 9700 section 4.11.2). The database cannot hold the byte. A field a
  body does not take is not read: most bodies refuse it by its name, and
  the few that ignore unknown fields (the Cloud's sign-up, `/register`)
  ignore its value too, a NUL in it included.
- **A lone UTF-16 surrogate** (`"\ud800"` with no partner, which JSON can
  spell and which is no character) is refused the same way, in the same
  places: `user_id contains a lone UTF-16 surrogate (U+D800), which is not
  text and cannot be stored`. A pair that makes one character (an emoji
  written as two escapes) is that character, and is taken.
- **A moment** (a memory's `timestamp`, a fact's `valid_from`, `as_of`,
  `known_as_of`, `expected_updated_at`) from 0001-01-02 to 9999-12-30, in
  UTC; anything else is a 422 that names the field and those two dates.
  `0001-01-01T00:00:00+01:00` (.NET's `DateTime.MinValue` written in a
  European zone) is the year 0 in UTC, and is refused.
- **An unknown field** of a body is one 422 error per object, naming the
  first (`userId is not a field here. Did you mean user_id?`); an
  argument a tool does not declare is refused the same way. So is a query
  parameter a `GET` of `/v1` does not take (since 1.0.23), one error per
  parameter, with the names the route takes: `userId` on `GET /v1/facts`
  answered with every end user of the key, and `as_of`, the clock of
  `GET /v1/facts` and `GET /v1/profile`, was ignored on `GET /v1/context`,
  whose block answered with what is true today. For the block as the store
  knew it on a date, `known_as_of`; for what was true on a date,
  `GET /v1/facts?as_of=`. One name is taken and not read
  ([Honest limits](limits.md#honest-limits)).
- **`metadata`** holds at most 8192 bytes of JSON, nested at most 32
  levels, with no NaN or infinity.
- **A 422 lists at most 20 errors** in `detail`; `message` names the
  first and counts them all.
- **A body** has its route's ceiling (2 MiB; the batch's, with a key that
  may write, about 78 MB; the MCP's 4 MiB), is counted before it is
  decoded, is JSON in UTF-8, and has 30 s to arrive whole (a batch past 2
  MiB, 120 s once let in), else 408.
- **The bodies under 2 MiB (4 MiB on MCP)**, the MCP's messages too and
  whatever their `Content-Type`, cost together at most 64 MiB, their bytes and what decoding them takes, and
  one network (an address, or its /64) at most 8 MiB of it; past either,
  503 with `Retry-After`. A body that would take more than 8 MiB on its own
  is a write key's batch decoded in turn with the batches past 2 MiB, or
  else a 413. A batch past 2 MiB, or one sent chunked that grows past it,
  waits while its account's large upload, or the server's two, are under
  way, until its 30 s are up, then 503 with `Retry-After`.
- **An MCP session keeps of its `initialize`** the protocol's version, the
  client's name and version and which capabilities it named, by name only;
  every other member is dropped, not refused. A client name, version or
  protocol version past 255 characters is a 422 `invalid_request`.
