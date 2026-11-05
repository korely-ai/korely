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
fact by its id, or close it with `close=true`: the old fact is closed and a
new one written, never edited in place) and `korely_forget_memory` (forget a
memory by its id, or erase it with `erase=true`; the answer counts the facts it
closed or removed). They are `PATCH /v1/facts/{id}`, `POST
/v1/facts/{id}/forget` and `DELETE /v1/memories/{id}` for an agent that cannot
leave the conversation. `POST /v1/facts` takes a `tense`, `current` (the
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
wrote it.

`/docs` is served by this machine with its own files (Swagger UI 5.33.1,
Apache 2.0, in `korely_agent/docs_ui`), so it works on a network with no way
out; `/openapi.json` is the same contract as JSON.

`/docs` has two sections. `v1` is the memory API, what your agents call with
their keys. `admin` is the administration API: projects, keys, webhooks, the
requests and the usage of the account, its audit trail and the model
settings. The dashboard calls it with its session, and a script can call it
too, with a key issued with the `admin` scope (`manage issue-key --admin`,
or the scope ticked on the API keys page). An ordinary key gets 403 there,
so one leaked from an agent cannot mint more keys. An admin key reaches
every project of the account, whatever its own project, memories included:
keep it for your own scripts, never in the tools of one project. The spec the site
publishes for the hosted service has both sections
(`scripts/dump_openapi.py --public`).

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
| `erasures-export`, `erasures-replay` | what `restore.sh` runs to keep the erasures made after a dump |
| `credentials-export`, `credentials-replay` | what `restore.sh` runs to keep the revocations, key moves, passwords and webhook secrets changed after a dump, and to end every session |
| `add-webhook`, `list-webhooks`, `delete-webhook`, `rotate-webhook-secret` | webhook endpoints |
| `status` | the whole install on one screen: counts, and whether extraction is running |
| `reextract` | memories and vectors that gave up, queued again |
| `reclassify-relations` | asks again whether each learned relation holds one value, one end user's vocabulary at a time, of the writer of that project's region (the facts of no project: the install's default writer); a dry run unless `--apply` |
| `review-vocabulary` | asks again whether each learned phrase is the relation it was merged into, end user by end user and in each project's region as above; a dry run unless `--apply` |
| `review-objects` | asks whether two objects of an end user's memory are one thing named in two languages or forms ("Italia" and "Italy"), as the same-object question of memory_core 6.1.0 (off) would at the write, in each project's region as above; refused while the switch is off unless `--while-off`; one account (`--tenant`, `--project`, `--user`) or `--all`; the dry run (`--out file --cap amount`, in LLM_RATES' currency, billed as the `operator` stage) writes the proposed links to the file and nothing to the database, `--apply file` writes exactly those, with no model call and one trail row each |
| `recompute-vectors` | every stored vector again, with the embedding model in force, after a change of `EMBEDDING_PROVIDER`; a dry run unless `--apply` |

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
`rate_limit_exceeded`, `search_unavailable`, `internal_error`...). `message` is
for a person and its wording may change. `detail` is unchanged, a sentence, a
`{code, message}` pair or the list of fields that failed validation, so a
client that reads it keeps working; `/docs` declares all three. The hosted
service runs this same code since 9 October 2026 and answers the same three
keys; before, it answered `{code, message}` alone, which is why the clients
read those two. Since 0.1.12; before, only `detail`.

## What every door refuses

One rule for every input, the same on REST, in a batch, on MCP and on the
OAuth forms (`services/limits.py`):

- **A NUL byte (U+0000)** anywhere in a request (a body field at any depth,
  a key of `metadata`, a path, a query parameter, a tool argument, a form
  field of `/authorize`) is refused, naming the place: a 422 whose
  `message` is `memories[1].run_id contains a NUL byte (U+0000)`; on MCP
  the line `error: invalid_request, ...`; on `/authorize`
  `error=invalid_request`. The database cannot hold the byte. A field a
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
  argument a tool does not declare is refused the same way.
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
