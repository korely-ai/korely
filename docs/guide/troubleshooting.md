# When something is wrong

`GET /health` reports what the provider last actually did, not whether a key is
set:

```json
{"status": "degraded",
 "extraction": {"state": "failing", "seconds_ago": 12,
                "detail": "429 RESOURCE_EXHAUSTED: prepayment credits are depleted"}}
```

On a fresh install, before you have pasted a model key, the same endpoint says
something that reads much better than it is:

```json
{"status": "ok",
 "extraction": {"state": "not configured"}}
```

`ok` there means the service is up, and nothing is being learned. That is the
reason for the next line.

There are four states, and only one of them is trouble:

    working          the provider answered, `seconds_ago` says when. After a
                     restart or a restore, before this process has called the
                     model, it is read off the store: the last extraction
                     saved there, by whichever process made it, so
                     `seconds_ago` can be days on an install that is idle
    unknown          a key is set and no extraction has ever answered: what a
                     fresh install says. Also what it says when the database
                     cannot be read (`database` is then `failing`): the
                     writer chosen on the Models page is kept there
    not configured   no key
    failing          the last call was refused, with the provider's own words;
                     or memories have waited more than five minutes and no
                     extraction has answered in as long, whatever the last
                     call said.
                     A failure belongs to the writer that made it: save
                     another key or model in its place on the Models page
                     (in place of the writer of `.env` too, whichever
                     provider `.env` sets), or a new Google key on the key
                     screen when the writer is the Gemini one of `.env`,
                     and the failure is forgotten, `extraction` reading
                     `unknown` until the new writer is called (`working`
                     off the store, as above, when it holds an extraction;
                     `not configured` when you removed the writer without a
                     replacement). While the database cannot be read, the
                     failure stands

Put `extraction.state` on your monitoring rather than the top-level `status`.

The `provider` field next to it (`"provider": "gemini"` on a fresh install) is
the writer the install is set to use, and its default is `gemini`. It is not a
sign that a key is set or that Gemini answered: with `"state": "not configured"`
no provider has been called. Read `extraction.state`.

The `detail`s are a provider's words, a database driver's (with its host)
and the paths of the embedder's model, and `/health` needs no key. So an
address on the internet, through the proxy, gets each part's `state` and
`seconds_ago` and nothing about why (`"details": "withheld ..."`); this
machine, its containers and its own network get everything: `curl
127.0.0.1:8000/health` on the server, or a monitor on the same private
network. A monitor elsewhere watches the states, which is all the advice
above asks of it.
Without a working provider the store still accepts writes and answers reads,
and mines no facts, which is the failure most likely to go unnoticed: every
response still looks successful.

If the store itself cannot be read, `database` says so (and `status` turns
`degraded`). While the writers saved on the Models page (or the Google key
of the key screen) cannot be read, the writer of `.env`, if there is one,
writes; once the database answers, the saved writer writes the calls that
follow within ten seconds. A region whose writer is resting after a refusal is listed under
`writers`, with when it began; one that waits for a person turns `status`
`degraded` until a memory goes through: refused for payment (HTTP 402, a
prepaid credit run out) or for its key (HTTP 401 or 403: a key revoked,
expired or without the permission for the model; since 1.0.16). The rest belongs
to the writer that was refused (since 1.0.9): saving another key, model or
provider in its place, or forgetting it, ends the rest and its line under
`writers`, and the drain tries the region at its next pass. Saving the same
writer again keeps the rest. Up to 1.0.8 the new writer waited out the old
one's rest, up to half an hour.

`mcp` says whether the MCP door opened at start: `working`, or `failing`
(and `status` turns `degraded`) when the surface did not mount or its
session manager did not start, the case where REST answers and every MCP
client meets a 404 or no session.

`jobs` says what the worker's other jobs last did, `batch` (the
`/v1/batch` imports), `webhooks` (the deliveries), `purge` (the
retentions) and `keys` (the warnings before a key expires): `working`
with `seconds_ago`, `failing` with the error's class (and `status`
turns `degraded`), or `unknown` until each has run in this process, which
is what it says with `RUN_WORKER=false`.

The dashboard answers half of this in Settings, next to the model key: whether
a key is set, and what the last model call did, in the provider's own words
when it failed. It does not show the queue, the memories still waiting for
their facts, and it shows the one account you signed in to. For the whole
install, from a shell (an SSH session, a cron job, a monitoring check), the
same question has a command:

```
$ docker compose exec api python -m korely_agent.manage status
tenants     3
keys        4 live, 1 revoked
memories    1204  (12 waiting, oldest 2026-09-11 14:02)
facts       891 true now, 143 closed
extraction  last fact 14s ago
embeddings  local, embeddinggemma-300m, 2 threads
```

`live` is every key not revoked, the expired ones included, except a
`kor_live_` key, which an install of your own refuses at every door since
0.2.0: the line counts those as `refused`, as `manage list-keys` and the API
keys page mark them. Lines appear
when they have something to say: the memories that gave up extracting, the
vectors waiting (below), the export when `FACTS_EXPORT_DIR` is set, and the
model calls of the last 30 days, by stage and by agent, once there are any.

The drain runs every ten seconds, so anything waiting longer than five minutes
is not waiting, and the `extraction` line says `STUCK` instead, with a second
line saying where to look. It is read off the store rather than off the
serving process, which means it is true from wherever you run it.

A write whose embedding call fails is kept: the memory or the fact is stored
without its vector, and the worker computes it later, a minute after the
failure and then at doubling intervals, twelve calls over about a day. Until
then a memory is not found by search and a fact is not ranked by meaning, and
`status` says how many are waiting (`vectors     3 waiting for a retry`). After
the last call it gives up and says that too; `reextract` puts them back in
the queue once the provider works again.

## The dashboard says what failed

- **"Your session could not be checked"**: when the dashboard opens, it asks
  the install whether the session this browser keeps is still valid
  (`GET /admin/whoami`). Since 1.0.22 only a 401 ends the session; any other
  failure (a 500, a 429, a 503, the install restarting) keeps it and says
  so, with **Try again**, or with the time to try again when the answer
  named a wait (`Retry-After`). **Sign out of this browser instead** leaves.
  On the Cloud, an account that no longer exists ends the session too, and
  the sign-in page says so.
- **"Try again after 14:32 (in 12 minutes)"**: a page whose read was refused
  with a `Retry-After` (a 429 or a 503) says when on your clock, and offers
  Try again once that moment has come. A locked sign-in after too many
  wrong passwords says it the same way.
- **A grey note with a lock and no Try again**: the install refused who is
  asking (a 403), for example Settings and Setup for an operator who is not
  the install's administrator on an install with several accounts. Trying
  again would not change it; the note says who can.
- **"The answer was a web page instead of data"**: something in front of the
  install (a proxy, a maintenance page) answered in its place with a web
  page. The install itself was not reached.
- **"This page could not be drawn"**: the page stopped while drawing; the
  sidebar and the other pages still work, and Reload tries the page again.
  The Memory Graph needs WebGL 2 and says so when the browser has none (a
  virtual machine, a remote desktop, hardware acceleration switched off);
  the Facts page lists the same facts.

## After a crash or a restart

A write that was answered is in the database: a memory is stored (with its
vector, or waiting for one as above) before `POST /v1/memories` answers, and
its facts are written afterwards by the worker. What a crash leaves half done
is picked up again, without anybody re-sending anything:

- **A memory being written when the process stopped** is written again,
  and so are the later memories of the same person, which wait for it
  (since 1.0.22). At a stop (SIGTERM, as `docker compose` sends at an
  upgrade or a restart) the API stops taking requests, gives the ones
  still running 5 seconds (`--timeout-graceful-shutdown 5` in the image's
  command; an MCP client's open stream never ends by itself), cancels what
  is left, and then the worker gives back every memory it holds: they are
  `pending` at once, and the log says `[agent_facts] N claimed memories
  given back to the queue at the stop`. Docker's stop timeout (10 seconds
  by default) leaves it the other 5. After a kill, a crash, or a stop that
  runs out of time, the next drain of any process (every ten seconds) sees
  that no session holds the key of the process that claimed them and puts
  them back, with `[agent_facts] N memories claimed by a process that has
  ended are back in the queue`. A memory a running process holds is not
  given back while its process is connected to the database; the clock
  still takes back any memory held longer than `DRAIN_STALE_MINUTES`, as
  always. A process that keeps running after its own lock session ended
  (the database restarted, the session ended by an administrator) can lose
  its memories to another process in between, a whole person at a time: it
  stops working them at the next memory, and the one it was writing is
  written again, one more model call. A person's memories are written in
  their order in that case too: a group stops as soon as its next memory,
  or the one it has just extracted, waits to be extracted again (given
  back, or edited). What still waits `DRAIN_STALE_MINUTES` (15 by
  default) before it is written again: the memory of a write that waits
  for its facts (`WRITE_ASYNC=false`) cut by the stop, a memory left by a
  release before 1.0.22, and any memory when the process could not take its
  key (an ERROR `[holder] no key ...` in the log). The key is a lock on the
  process's own database session, so it needs a direct connection to
  Postgres (docs/guide/operations.md, "The database connection").
- **A provider that refuses the install as a whole** (a key it rejects, a
  credit or quota run out, too many requests, a server down, no answer) is
  not the memory's fault: the memory goes back to the queue and the drain
  rests, one minute after the first refusal and up to half an hour, until a
  memory goes through. `/health` says `failing` meanwhile.
- **An answer that cannot be read** is never taken for "no facts": that
  memory ends in `error`, `status` counts it, and `reextract` puts it back in
  the queue.
- **A memory erased while its facts are being extracted** is not a failure:
  the write is dropped, the memory is not queued again, and the log has one
  INFO line with the memory's id, `[memory] memory <id> was erased while its
  facts were extracted: this write is discarded`. A write that waits for the
  extraction (`WRITE_ASYNC=false`, the MCP) of a memory erased meanwhile
  discards its write the same way and does not answer 500. A stale-row error
  while the memory is still there stays an ERROR.
- **A memory done with no fact, though the writer declared some**: the quality
  filter drops a fact whose name is not in the memory's text. When it drops
  every fact the writer declared, the log has one INFO line, `[memory_core] the
  writer declared N fact(s) and the filter kept none`, with the count and no
  word of the text (since 1.0.9, memory_core 6.2.1). Such a
  memory is filed done; `manage reextract --empty` finds it.
- **Vectors and `/v1/batch` imports** are retried by the worker at growing
  intervals; webhook deliveries are retried as [Webhooks](webhooks.md) says.
- **Two processes starting together** on one database take turns on the
  migrations, so neither dies on the other's tables.

So the failures to watch are visible ones: memories waiting longer than they
should (`STUCK`), memories in `error`, `failing` on `/health`. A write is
never answered and then dropped in silence.

The engine's log lines start with `[memory]`, `[memory/F3]`, `[vocabulary]` or
`[memory_core]` (the logger names are under `korely_agent.services.memory_core`).
Before 1.0.1 the prefixes were in another language (the 1.0.1 entry of the
[CHANGELOG](../../CHANGELOG.md) lists the old ones), so an alert or a log filter
that matches them has to change.

`list-keys --tenant <id>` and `revoke-key --tenant <id> --id <id>` are next to
it, for the morning somebody finds a key where it should not be. The API keys
page revokes too, inside the account you signed in to; these reach any account
on the install without a login. Both want the tenant, the same one
`create-tenant` printed; `list-tenants` has it if you did not keep it.
