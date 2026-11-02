# Changelog

Releases of Korely Agent, newest first. Each entry says what changes for
somebody upgrading from the release before it: what to do before and after,
and what the store will answer differently.

Korely Agent's public history starts at 1.0.2; the releases before it were private.

## 1.0.7 (2026-11-02)

### Before and after upgrading

- **Nothing to do.** No migration, no new setting, nothing in the API changes.
  The dashboard is built with one more package (below), so an image you build
  yourself downloads it; the published images carry it.

### What the store answers differently

- Nothing. The store, the API and the engine are those of 1.0.6.

### The dashboard

- **Selecting a node with many connections no longer draws a wall of
  labels.** Until 1.0.6 the page wrote the name of every neighbour of the
  selected node, and at a few hundred neighbours they covered each other
  (screenshot of 2 Nov 2026, 2,249 facts). Now at most twelve neighbours are
  named, the strongest by shared facts, placed so that none overlaps; the
  others stay as dimmed dots. A **Focus** button shows only the selected node
  and its strongest neighbours ("12 of 275 links", "+ more" to widen,
  "Leave focus" to come back).
- **A new look for the graph**, in the visual language of Hindsight's: a dark
  navy surface, nodes coloured by how recently they were mentioned (blue,
  oldest, to coral, newest) and sized by their number of facts, thin curved
  edges coloured by family with a closed fact fainter, a floating card for
  the selected node. The light theme follows the dashboard's.
- **A 3D view**, behind a 2D/3D switch. It is fetched only when you open it
  (three.js is most of the weight), so the 2D page loads as before.
- **Full screen, zoom in, zoom out, reset, and Pause/Resume of the layout.**
- Measured on a local install with 1,000 facts written through
  `POST /v1/facts` (no model) and on synthetic graphs of 30, 960 and 1,000
  nodes in a headless browser. Not measured: an account of 2,000 or more real
  facts, a phone (only a narrow window with touch emulated), a click on a node
  in the 3D view. With no model configured every fact has no family, so the
  colours by family were seen only on synthetic data.
- New package in the dashboard build, MIT: `3d-force-graph` 1.80.1, with the
  packages it brings (`docs/DEPENDENCIES.md`).

### Documentation

- `docs/guide/troubleshooting.md` no longer spells out the log prefixes used
  before 1.0.1; it points to the 1.0.1 entry of this file, where they stay.

### Security

- No change.

## 1.0.6 (2026-11-02)

### Before and after upgrading

- **Nothing to do.** No migration, no new setting, nothing in the API changes.
  The dashboard is built with three new packages (below), so an image you
  build yourself downloads them; the published images carry them.

### What the store answers differently

- Nothing. The store, the API and the engine are those of 1.0.5.

### The dashboard

- **The Memory Graph page is readable at a thousand facts.** It was drawn by a
  simulation of our own, written for a few hundred nodes, and at 959 nodes
  every label was drawn over the others (screenshot of 1 Nov 2026). It now
  draws with Sigma.js on WebGL and lays the nodes out with ForceAtlas2: a
  label for the nodes big enough to carry one and more as you zoom in, the
  neighbours of the node under the pointer (or selected) lit and the rest
  dimmed, a closed fact fainter than a current one. Measured on a local
  install with 997 facts written through `POST /v1/facts` (no model): 374 nodes,
  readable, no error in the browser. Not measured: an account of 2,000 or more
  facts, a phone.
- New packages in the dashboard build, all MIT: `sigma` 4.0.3, `graphology`
  0.26.0 and `graphology-layout-forceatlas2` 0.10.1
  (`docs/DEPENDENCIES.md`).

### Documentation

- Three engine behaviours that were in the product and not in the guide are
  there now: the order of memories that carry the same moment, a past fact
  written with no start (`docs/guide/time.md`, `docs/guide/api.md`), and what
  the log says of a memory erased while its facts are extracted, with the
  prefixes of the engine's log lines (`docs/guide/troubleshooting.md`).

### Security

- No change.

## 1.0.5 (2026-11-01)

### Before and after upgrading

- **Nothing to do for an install of your own.** The dashboard's Webhooks
  page changes how a new endpoint is made (below); nothing is migrated and
  every endpoint you have keeps the project it has.

### What the store answers differently

- **The Webhooks page no longer starts on "Every project".** With more than
  one project in the account the project is chosen before the endpoint can be
  added, and "Every project" is one of the choices, with the sentence that it
  also receives the events of projects made later. An account with one project
  sees no choice, as before. Through the API nothing changes:
  `POST /admin/webhooks` without `project_id` is still an endpoint for every
  project, which a script written against 1.0 relies on.

### Security

- A key that could write memories could delete the whole account it belonged
  to (`DELETE /v1/account`, Cloud, accounts made with `POST /v1/agents/init`
  that nobody signs in to). Found by the documentation review of 1 Nov 2026.
  The `admin` scope is now required, as for the routes that issue keys.

## 1.0.4 (2026-11-01)

### Before and after upgrading

- **Nothing to do.** One sentence of the API description changes and nothing
  the product does. `DELETE /v1/memories/{id}` used to say that a restore of
  a backup taken before an erasure makes the erasure again. That holds for
  `restore.sh` of a Self-hosted install, which exports the erasure records
  before (`manage erasures-export`) and replays them after (`manage
  erasures-replay`); a restore done any other way brings back what the dump
  held ([Operations](docs/guide/operations.md#backups)). The OpenAPI
  document you generate from the API carries the corrected sentence.

### What the store answers differently

- Nothing.

### Security

- No change. The corrected sentence is the one the erasure promise rests on:
  an erasure survives a restore only where the records of erasures are
  replayed.

## 1.0.3 (2026-11-01)

### Before and after upgrading

- **Nothing to do for the extraction rules of a gpt-oss writer chosen on the
  Models page** (below). They apply from the first write after the upgrade;
  facts already written are not rewritten. To read old memories again with
  them, `manage reextract --no-errors --empty` queues the memories that ended
  with no fact, at one model call each. Memories that already hold facts are
  not re-read by any command.

- memory_core 6.2.0, and no migration for the edits (below): a copy an edit
  left open before the upgrade stays open, because the new value was judged
  then and a restatement is not judged again. Close it with
  `PATCH /v1/facts/{id}` (or `POST /v1/facts/{id}/forget` with `at`);
  `GET /v1/facts?user_id=...` shows both values open where it happened.

### What the store answers differently

- **A gpt-oss writer chosen on the Models page gets the extraction rules
  measured on it.** Since 0.1.20 (7 Oct 2026) two rules of the extraction
  prompt are on for a gpt-oss writer alone, measured on it: a change of state
  is written as the new state in the present ("Tom moved to Munich" is "Tom
  lives_in Munich", current, which the judge then weighs against "Tom
  lives_in Berlin"), never as a past event of its own; and the observation
  stays faithful to the memory (a memory about someone or something else
  keeps its own subject, a shared image keeps its caption, a time keeps its
  precision). The product decides "a gpt-oss" from the writer's model name
  (`gpt-oss` in it, whatever the letter case: Scaleway's `gpt-oss-120b`,
  OpenRouter's `openai/gpt-oss-120b:nitro`), the Models page's model first when the page
  chose one. That choice never reached the extraction: the property that
  carries it, `forced_model`, was declared only on the providers' protocol
  (`core.llm.Provider`, a `typing.Protocol`, whose members a class that only
  matches it does not inherit), so on every real provider the extraction read
  the file's model name (`MISTRAL_EXTRACTION_MODEL`, "mistral-small-latest" by
  default). Every release from 0.1.20 to 1.0.2 has it. A writer chosen on the
  Models page with a gpt-oss model, among them the Cloud's European writer
  (Scaleway gpt-oss-120b, chosen there for the region `eu`), wrote without
  the two rules; the calls themselves always went to the chosen model. Now
  both providers state the model the page chose, the European writer gets the
  rules, and a test checks every member of the protocol on every provider the
  product can build. Not affected: a writer named in the file (the rules
  followed its name, as measured), Gemini (recognised by its client), and
  every other model (no rule is tied to them). Measured once with the real
  writer on a bench of ours that is not part of this release (1 Nov 2026), a
  small sample:
  the 14 hardest change cases, three times each, one session; changes that
  ended right 0 of 33 before and 7 of 33 after, "Northwind Hosting costs 50
  euro per month." left with no fact 2 times in 3 before and 0 in 3 after,
  the two guard cases that must not close failing 4 times in 6 before and 3
  after; EUR 0.065 and 0.075 for the two runs. What it costs: on 7 Oct 2026
  the two rules made the writer's prompt about 12% longer and its answer about
  20% richer in facts (78 against 65 on the same memories), so a writer of this
  kind, the Cloud's European one included, spends about 12% more per memory
  and its facts are judged one call more often.

- **Editing one of the memories that state a fact to a new value closes the
  fact.** "I work at Acme" told by three memories is one fact with three
  sources (`observation_count` 3); `PATCH /v1/memories/{id}` on one of them to
  "now I work at Beta" left Acme open beside Beta, in `GET /v1/facts` and in
  the context's known facts, because the judge was never shown it: the edited
  memory was still one of its sources, and a memory's own facts never close
  each other. A new memory saying Beta closed it; the same words as an edit
  did not. Now the judgement of the edit weighs Acme as another memory's
  fact, once every statement of the new text is written, and Acme is
  closed where Beta begins, `invalidated_by` Beta, stated by the two
  memories that still say it (their text stays, and the context says what
  they state changed later). The same for the copies the agents keep in
  their own namespaces. An edit that still says Acme, in other words or
  beside Beta and in either order, leaves the fact as it was (same row,
  same sources, same first source and count; only its `last_confirmed_at`
  moves, as for any restatement); one that says something unrelated only
  takes its own source away, as before. A statement in the past tense of the
  new text ("I no longer like Acme") is weighed as it is written, before the
  rest of the text, and still does not see the copies. Nothing of the copy
  is written while the model is asked: a forget of another memory that
  states it, or an edit of one, does not wait for the edit's judge, and
  each takes only its own source away. Forgetting the edited memory
  afterwards gives Acme back (above). No prompt changes; the judge is shown
  one candidate more, the copy, and only on such an edit.

### Security

- Nothing changes in what an account can reach. The extraction prompt of a
  gpt-oss writer carries the two rules measured on it (the text that was meant
  to be sent since 0.1.20), to the same provider the install already chose;
  nothing new leaves the machine. An edit's withdrawn copies are handled by
  id; the logs keep ids and counts, never what an end user said.

## 1.0.2 (2026-11-01)

### Before and after upgrading

- **Nothing to do for an install.** The image is laid out as before
  (`/app/requirements.lock.txt`, `/app/alembic.ini`, `/app/entrypoint.sh`),
  `docker-compose.yml`, `docker-compose.image.yml`, `.env.example` and
  `install.sh` stay where they were, and the commands of the guide are the
  same.

- **From a clone of the repository, ten files moved out of the root**, so
  that it holds what a visitor reads: the four `requirements*.txt` are in
  `requirements/` (same names; `make lock` and `make image` know),
  `Dockerfile.dev` and `docker-compose.test.yml` in `dev/`, `alembic.ini` in
  `alembic/` (from the root, `alembic -c alembic/alembic.ini upgrade head`;
  in the container, `alembic upgrade head` as before), `entrypoint.sh` in
  `deploy/`, and `CONTRIBUTING.md` and `SECURITY.md` in `.github/`, where
  GitHub still shows them. A script of yours that names one of these paths
  needs the new one.

- The guide says three more things: the `min_score` floor on a search and
  the value measured for it (`docs/guide/api.md`); what an agent should save,
  and leave out, so the memory stays clean (same page); and what happens to a
  write after a crash or a restart, so the failures to watch are the visible
  ones (`docs/guide/troubleshooting.md`). Nothing the product does changes.

- Two scripts that measure what the guide claims, run on your own machine
  with no key (`scripts/concurrent_agents.sh`: 30 agents on two processes;
  `scripts/soak.sh`: hours of load on one process, with the API's memory and
  the database's size sampled every minute). Nothing the product does
  changes; the numbers are in `docs/guide/limits.md`.

- memory_core 6.1.1: comments and docstrings only, no Italian left in them;
  nothing the engine does, sends to a model or prints changes.

- No migration for what a deletion gives back (below): the facts closed by a
  memory forgotten before the upgrade stay closed; erasing that memory after
  the upgrade (`DELETE /v1/memories/{id}?erase=true`) gives them back.

### What the store answers differently

- **Deleting the memory that closed a fact makes the fact true again.**
  "I live in Paris", then "now I live in Zanzibar": the judge closed Paris,
  and forgetting or erasing the Zanzibar memory, deleting its agent
  namespace, or editing its text so it no longer says it, left Paris closed
  although its memory was live and nothing contradicted it any more. Now
  Paris comes back as a NEW open fact: same subject, relation and object,
  `source_memory_ids` the live memories that say it, a new `created_at`, and
  a `valid_from` where Zanzibar stopped standing: the moment Paris was closed
  when Zanzibar is erased (with its memory or its agent namespace: it never
  was), the forget or the edit's arrival when it is forgotten or edited away
  (Zanzibar stays in the history until then). So `as_of` answers one place
  on every date. The closed Paris row is not touched (`invalid_at`,
  `closed_known_at`, `invalidated_by`), so `known_as_of` before the deletion
  answers it as it did; nothing links the new row to it. Only when nothing
  else closes it: Paris stays closed while Zanzibar is said by another live
  memory, while another value of the same subject and relation is open ("now
  I live in Rome", whatever the relation's single-value label), when no
  memory vouches for Paris any more (none live, or the one that said it was
  edited since, or a memory told since says it is over, "I used to live in
  Paris": a source vouches only with a text that arrived before the store
  learned Paris had ended), or when a later Paris row is in the history
  already (given back once and ended since). A fact posted with
  `POST /v1/facts` needs no memory and comes back with none. Only a fact
  closed by a newer one is given back: one closed in place (a past
  statement, `POST /v1/facts/{id}/forget`) names no closer. A closer dated
  ahead and deleted before its date gives Paris back from where it was to
  end. No model is called on a deletion; a fact given back carries the
  judgement of the row it repeats, and one whose vector was still pending
  inherits its late judgement, which the vector drain asks when the vector
  arrives (`services/vectors.py`), as it would have for the closed row.
  The closed row is history: `PATCH /v1/facts/{id}` on it answers 409
  `fact_not_current` with no successor; correct the new row.
  `DELETE /v1/memories/{id}` (forget and erase) and `DELETE /v1/agents/{id}`
  answer a new `facts_reopened` count, and the access trail's row carries it
  in `meta` when it is not zero; no webhook is sent for a fact given back
  (there is no event for a fact that becomes true). The erasure of an end
  user or of a project gives nothing back: all their facts go.

- **A memory forgotten and then erased keeps the start the forget gave.**
  Paris given back at the forget begins at the forget; erasing the memory
  afterwards does not move it earlier (that would edit the row), so `as_of`
  a date between the move and the forget answers no place at all.

- **An agent can now correct a wrong fact and forget a wrong memory over MCP.**
  Before, the MCP door had four tools (`korely_add`, `korely_search`,
  `korely_get_facts`, `korely_get_context`): an agent could add and read, and
  fixing or retracting needed a REST call. Now `tools/list` shows six.
  `korely_correct_fact(fact_id, subject?, predicate?, object?, close?, at?)`
  is `PATCH /v1/facts/{id}` (and, with `close=true`, `POST
  /v1/facts/{id}/forget`) through the same service functions: a corrected
  fact is closed and a new row is written, never edited in place, and
  `as_of` / `include_invalidated` still show the old one; only a current fact
  can be corrected (the 409 `fact_not_current` of REST). `korely_forget_memory(memory_id, erase?)`
  is `DELETE /v1/memories/{id}`: the memory drops out of every read and the
  facts only it stated are closed, and with `erase=true` it is removed for
  good; the answer counts the facts closed or removed. Nothing to do on
  upgrade: a client lists the tools again on its next session.
  A key needs `memories:write` for both, as REST does, and is refused the
  ids of another project (or, for a confined key, of another end user) as
  "not found".

- **The audit trail of a correction, a close and a forget says the same
  thing from either door.** `PATCH /v1/facts/{id}`, `POST
  /v1/facts/{id}/forget` and `DELETE /v1/memories/{id}` now write in `meta`
  the counts the MCP tools write (`facts_closed`; `status`;
  `facts_invalidated`, `facts_deleted` and `erase`), where they wrote none
  or only `erase`. A reader of `GET /v1/audit` that matched on an empty
  `meta` for those actions sees the counts now; ids and counts only, never
  text.

- **A synchronous write erased while its facts were being written answers
  201.** With `WRITE_ASYNC=false`, a `POST /v1/memories` whose memory was
  erased (by an erasure of it, of its end user or of its agent) while the
  model was writing its facts answered 500, although the write had worked:
  it read the erased row again. It now answers with the memory as stored,
  and the erasure stands. Nothing to do.

- **Under a burst from one account, a search waits its turn instead of
  failing.** Before, an account had four places at the local embedder per
  process, and a REST search past them (`POST /v1/memories/search`) was
  answered at once with 503 `search_unavailable`; REST's four and the MCP's
  four also added up there, so `korely_search` and `korely_get_context`
  were refused too. With 30 agents of one account (15 REST, 15 MCP) on two
  processes, 549 searches were refused and passed only because the test
  client retried (`scripts/concurrent_agents.sh`, 31 Oct 2026); without
  retries about half fail. Now `POST /v1/memories/search`, `GET
  /v1/context`, the screen's search (`GET /admin/memories/search`),
  `korely_search` and `korely_get_context` wait for one of the account's
  places, REST's and MCP's together one count of four per API process (two
  processes, eight), and the answer comes when the turn does. A read holds
  its place only while its vector is computed, not through the rest of its
  work, and it waits only once everything it could be refused for has been
  checked: a malformed body or query (422), a key without the scope or a
  confined key naming another end user (403) and the Cloud's monthly limit
  (429) are answered at once, as before. The wait holds no request thread and
  no database connection, so another account's requests are served
  meanwhile, and its searches go ahead of the backlog. Only a read that
  waited 20 s for its place (`WAIT_SECONDS`) is answered as before: the
  search with 503 `search_unavailable`, now with `Retry-After`, the context
  with `degraded: true`, the MCP's tools with `search_unavailable`. A
  burst's latency moves from refusals to waiting: a client that retried on
  503 sees fewer of them and longer answers, and a client timeout under 20 s
  can now fire before the answer under a heavy burst. With
  `EMBEDDING_PROVIDER=gemini` nothing changes: the places are the local
  embedder's, and a Google vector takes none. Writes are unchanged: a write
  past the places is stored (201) and its vector computed by the worker.
  Nothing to do on upgrade.

### Security

- The two new tools are writes behind the same walls as their REST twins: the
  `memories:write` scope, the key's project and end users. Each call, refused
  ones too, is in the audit trail as `facts:correct`, `facts:forget` or
  `memories:delete` with actor `mcp`, the key, the target id and counts, never
  the text. Nothing else changes in what an account can reach or in what the
  logs keep.
