# Changelog

Releases of Korely Agent, newest first. Each entry says what changes for
somebody upgrading from the release before it: what to do before and after,
and what the store will answer differently.

Korely Agent's public history starts at 1.0.2; the releases before it were private.

## 1.0.9

### Before and after upgrading

- **The public repository is now `korely-ai/korely`** (it was
  `korely-ai/korely-agent`), and its image with it: the image its CI publishes
  is named after the repository, so the releases from this one on are
  `ghcr.io/korely-ai/korely:<version>`, and only there. install.sh, the
  compose files, `.env.example`, the README and the guides name the new
  addresses; install.sh holds the name once, as `PUBLIC_REPO`.
- **The old addresses still open, through GitHub.** `github.com/korely-ai/korely-agent`
  sends you to `github.com/korely-ai/korely`, and the old installer line,
  `curl -fsSL https://raw.githubusercontent.com/korely-ai/korely-agent/main/install.sh | sh`,
  downloads the install.sh on the renamed repository's main, the same file
  as the new line,
  `curl -fsSL https://raw.githubusercontent.com/korely-ai/korely/main/install.sh | sh`.
  This lasts as long as GitHub keeps the old address pointing at the
  renamed repository: GitHub drops it if a repository is created again under
  the old name. Links and scripts of yours are better on the new address.
- **An install already made from the image keeps working as it is.** Its
  `.env` names the image it runs,
  `KORELY_IMAGE=ghcr.io/korely-ai/korely-agent:<version>` (with the digest,
  when install.sh had one): that image is on your machine, and the releases
  up to 1.0.7 are still published under the old name. No later release is
  published there, so that line never brings you a newer one. Nothing to do
  now. It is the one line of such an install that names the repository: to
  run a release published under the new name, it must name that release
  there (put the release in place of `X.Y.Z`, in the install's folder):

  ```bash
  sed -i.bak 's#^KORELY_IMAGE=.*#KORELY_IMAGE=ghcr.io/korely-ai/korely:X.Y.Z#' .env
  ```

  The old line stays in `.env.bak`.
- **A git checkout** needs nothing: `git pull` follows GitHub's redirect, and
  the image is built from the checkout. To name the new address yourself:
  `git remote set-url origin https://github.com/korely-ai/korely.git`.
  `docs/guide/install.md` now clones into a folder named `korely-agent`, the
  name it had before, so that a checkout keeps its Compose project and is
  never the same project as an install.sh install, which is named `korely`.
- **No migration and no new setting.** The changes to the writers and to the
  model settings below need nothing before or after the upgrade.
- **Nothing to do for memory_core 6.3.0**: no migration, no setting. A reader
  of the `fact.invalidated` webhook that matched on `invalidated_by` being null
  for a past statement now receives the replacing fact's id there.
- **Nothing to do for memory_core 6.2.1**: no migration, no setting; one more
  INFO log line, with counts only.
- **Nothing to do for the PATCH with the same text**: no migration, no
  setting. Facts closed that way before this release stay closed.

### What the store answers differently

Two words used below. A region is where a model reads the memories of a
project to write its facts: `eu` (a provider in Europe) or `global` (a
provider anywhere). The drain is the process that takes the stored memories
from the queue and writes their facts; when it rests a region it does not
try that region again for that time.

- **`GET /admin/models` and `PUT /admin/models/{provider}` carry
  `default_region`**, the region a project that never chose one writes in:
  the region of the writer in force, which is the provider saved last on the
  screen, else the one `.env` sets (`null` if neither, or if `.env` points to
  an address no known provider uses). It says what `active` already implied.
  The default region is read from the same writer as before; nothing is
  re-routed.
- **`by_region` of `GET /admin/models` now names the writer of `.env` too.**
  Each entry has a new field `source`: `env` for a writer that comes from
  `.env` or from the Google key pasted in Settings, and `screen` for a
  provider chosen on the Models page. Up to 1.0.8 a region written by such
  a writer was `null` there, and Settings said "No provider: their memories
  wait" for the projects in it while they were written. For example, with
  Scaleway chosen on the Models page and `GOOGLE_API_KEY` in `.env`:

  ```json
  "by_region": {
    "eu": {"provider": "scaleway", "label": "Scaleway", "model": "gpt-oss-120b", "source": "screen"},
    "global": {"provider": "google", "label": "Gemini", "model": "gemini-3.1-flash-lite", "source": "env"}
  }
  ```

  A writer of `.env` at an address no known provider uses belongs to no
  region, and no entry names it.
- **A writer saved after a refusal is tried at the drain's next pass.**
  After a provider refuses, the drain rests that region (one minute, then
  longer, up to half an hour). Up to 1.0.8 a writer saved in place of the
  refused one (a new key, another model or provider) waited out the old
  one's rest, and `/health` went on listing the region under `writers` as
  refused. The rest now belongs to the writer that refused: saving another
  one, or forgetting it, ends the rest and the line on `/health`. Saving the
  same writer again keeps it.
- **Two saves of the model settings at the same moment no longer fail.**
  Saving two providers at once, or saving while forgetting, could answer
  500 (a duplicate or stale settings row) or keep only one of the two
  choices. Each change of the provider key and of the writers now waits for
  the one before it to commit.
- **A fact a past statement closes now says which fact replaced it**
  (memory_core 6.3.0). "Northwind costs 55 now, it used to cost 50": the 50 is
  closed where it was said, and before it had no `invalidated_by`, though the
  judge's closings always had one. Now `invalidated_by` on the 50 is the 55,
  `GET /v1/facts` shows it, and when the 50 was an open fact before, the
  `fact.invalidated` webhook carries it as `invalidated_by` where it was null
  (a fact born closed was never announced, and still is not). It is set only when the same write holds
  exactly one open fact about the same subject and relation with another value;
  with none or with several it stays empty. Nothing else closes and nothing is
  judged. Facts closed this way before this release keep an empty pointer.
  Forgetting the memory that said the past statement now gives the old value
  back, as it already did for a closing by the judge: "Northwind is on the
  Basic plan", then "moved to the Pro plan, it used to be on the Basic plan";
  forget the second, and the first still says Basic, so Basic is true again,
  as a new open fact. Before this release nothing was left open in that case.
  The fact given back is of tense `current` (a fact closed by a past statement
  carries `past`, and the given-back copy kept it while open; that was
  corrected in the same change).
- **A memory whose every declared fact the quality filter dropped now says
  so in the log** (memory_core 6.2.1). The filter drops a fact whose name is
  not in the memory's text. Before, that was a DEBUG line per fact, so such a
  memory ended done with no fact and nothing above DEBUG to tell it from a
  text that holds none. Now one INFO line gives the count, never a word of the
  text or of the fact. Which facts are kept is unchanged, and so is what the
  store answers; memories already written with no fact are not re-read (the
  command `manage reextract --empty` finds them). Limit: the memory is still
  filed done, and a sentence with no subject ("She now lives in Lisbon") whose
  name is in an earlier memory is still dropped; measuring that needs the
  model and is not done.
- **`PATCH /v1/memories/{id}` with the text the memory already has no longer
  reads it again.** Before, it queued the extraction once more, and if the
  model answered no fact that second time, every fact the memory stated was
  closed and the memory was marked done, though nothing had changed:
  "Anna works in Milan" and "Anna has two children" were gone after a PATCH
  that repeated the text. Now the reply is the memory as it is, with
  `facts: []` and no model is called, so no cost either. A text that changed
  is read again as ever and retires the facts it no longer states: editing a
  value out of a memory still removes it. Limit: a text that changed by a
  comma and whose second read answered no fact still closes the facts, because
  the store cannot tell it from an edit that took the value away. Measured on
  the tests of this change, not on LoCoMo or LongMemEval.

### Security

- No change in what the installer trusts: it downloads only the release
  files of the repository it names (now `korely-ai/korely`), over HTTPS,
  checks them against the sha256 it carries, and stops when the answer comes
  from another address.
- The changes to the writers and to the model settings change nothing about
  who can read or change them.
- A PATCH with the same text changes nothing about who can read or write
  what; its one log line carries the memory id and no text.
- The INFO line of memory_core 6.2.1 carries a count and never a word of the
  text or of a fact.
- The pointer of memory_core 6.3.0 is an id of a fact of the same memory, same
  account and same project: nothing crosses a tenant.

## 1.0.8

### Before and after upgrading

- **Two migrations, on every install, at boot as every migration does.**
  `c5e9a3f7b1d4` is on data only: no table or column changes, it gives back
  the `key.expiring` notices the release before marked as sent with nothing
  sent (below). `d8a4f2c6b1e9` adds the column `applied` to
  `hosted_polar_subscriptions`, a table that belongs to the Cloud's billing:
  your install has it too, empty, and the migration changes nothing there.
  No new setting, nothing else to do, and nothing in the API changes shape.
- **Restart every worker of the release before, so none of them runs after
  the upgrade.** The worker is the part that runs the hourly round. Do the
  step that fits your install:
  - One API process, `RUN_WORKER` not set (what the shipped compose and
    `.env.example` do): the worker runs inside the API container and is
    replaced with it. Nothing more to do.
  - A worker in its own process (`RUN_WORKER=false` on the API and
    `python -m korely_agent.worker` beside it): stop it before you upgrade,
    and start it from the new release after.
  - A second API process on the same Postgres: it runs its own worker unless
    `RUN_WORKER=false`. Replace it too, not only the first.

  Why: the repair of `key.expiring` runs once, at the upgrade. A worker of the
  release before that is still running can mark again, in its hourly round, a
  key nobody was told about, and that key loses its notice as before.
- **Check your `EXTRACTION_PROVIDER` line if it is not `gemini` or
  `mistral`.** What it does is the same as up to 1.0.7: only the exact word
  `mistral`, in lower case, writes through `MISTRAL_API_BASE`. Any other line
  (`Mistral`, `openai`, an empty `EXTRACTION_PROVIDER=`) or no line **writes
  with Gemini, without any error**, and the service still starts. If you
  meant your own provider and wrote it another way, you were writing with
  Gemini then and still are: write `mistral` in lower case. What changes is
  only what the service says about it (below).
- **Downgrading** leaves the cleared `key.expiring` marks cleared (the release
  before reads such a key as never told), so those keys can be told again.
  The other migration only adds a column the release before ignores.

### What the store answers differently

- **`GET /v1/context` keeps its facts at a small budget.** (The token budget
  of the block is split in two parts, called shares here: the facts' share
  and the memories' share. Each is a part of the budget the block may use for
  that kind of content.) From 400 tokens up
  the block opens with the reader note (723 bytes), and up to 1.0.7 the note
  came out of the facts' share alone: at 400 tokens it left the facts 75
  bytes, an ordinary fact did not fit, and the block came back without
  "## Known facts" while a current fact existed, with no error and no
  `degraded`. Up to about 550 tokens a longer fact was left out the same way.
  The same happens through `korely_get_context` over MCP.
- **At every budget of 400 tokens or more the split between facts and
  memories moves.** The reader note is now paid by both shares: the facts
  may take half of what the note leaves, about 360 bytes (90 tokens) more
  than up to 1.0.7, and the memories that much less. It shows only when the
  facts the block holds (ten at most) are longer than 1.0.7's share; then the
  block holds more facts and fewer memories. Measured with ten facts of 131
  bytes and twelve long memories (facts and memories in the block, 1.0.7 and
  now): 400 tokens 0 and 2, 3 and 1; 600 tokens 3 and 3, 6 and 2; 800 tokens,
  the default, 6 and 4, 9 and 3; 1,000 tokens 9 and 6, 10 and 5; 2,000 tokens
  10 and 12, unchanged. At the default 800 tokens the block can hold fewer
  memories (in this example 4 became 3).
- **With no memory to show, the facts take the whole room.** When the memory
  search is down (`degraded_parts: ["memories"]`), found nothing, or found
  only memories the block leaves out (a memory whose facts are all
  superseded, a text already shown, one with no text), the facts are no
  longer held to a share kept for memories that do not come. Measured with
  the same ten facts and the search down: 400 tokens 0 facts (the block was
  empty) and now 4, 600 tokens 3 and 10, 800 tokens 6 and 10. Their room is
  never smaller than up to 1.0.7.
- **One case gives the facts fewer bytes.** When only the facts are degraded
  (`degraded_parts: ["facts"]`: they could not be ranked against the
  question) while memories follow, and the budget is under 400 tokens, the
  NOTE about it is now paid by both shares as well, so the facts may take
  half of its bytes less than up to 1.0.7: about 150 from 190 tokens, about
  30 below, and the memories that much more. Measured with ten short facts
  and twelve memories: at 250 tokens 459 bytes of facts and no memory, now
  309 and a memory, cut to fit; at 300 tokens 507 and 410 bytes of
  facts, with memories in both.
- **LongMemEval and LoCoMo were not measured again.** Their scripts ask for
  blocks of 4,000 tokens (LongMemEval) and 2,000 (2,000 for each of the two
  LoCoMo speakers, 4,000 in all), and there the ten facts fit in 1.0.7's share unless their lines
  average more than about 730 and 330 bytes, so for facts of an ordinary
  length the block should be the same. This comes from arithmetic on those
  budgets, not from a rerun. A store whose facts are that
  long, or a client that asks for a smaller budget, gets the split above,
  which no benchmark has measured.
- **What the new split does to the quality of the answers is not measured.**
  The numbers above are bytes and counts of facts and memories in the block;
  no run has read the new blocks and been graded. More facts and fewer
  memories at a small budget could help a question the facts answer and hurt
  one only a memory answers: it is one of this release's open limits, and a
  client that depends on the old split should keep its `token_budget` and
  read the block.
- **`key.expiring` waits for a webhook that takes it.** Up to 1.0.7 the
  hourly round marked a key as warned when it entered its last two weeks
  even when no endpoint of the account took `key.expiring` (none yet, one
  switched off, one subscribed to other events or bound to another
  project): nothing was sent, and a webhook added in those two weeks heard
  only `key.expired`. Now a key is marked only when a delivery was queued.
  Until then it waits, and a webhook added within the two weeks, active,
  subscribed to `key.expiring` (or `*`) and bound to the key's project or
  to none, gets it at a later hourly round
  ([Keys and limits](docs/guide/keys-and-limits.md)). `key.expired` is as
  before: sent once, on the day, heard or not.
- **After the upgrade a key can be told `key.expiring` a second time.** The
  migration clears the mark of each key not yet expired, not revoked and
  not of an OAuth client that has no `key.expiring` delivery about it on
  record, so the keys marked with nothing sent get their notice. A delivery
  is the only record that a notice went: a key whose notice went to an
  endpoint deleted since (its deliveries go with it), or was purged under a
  `WEBHOOK_DELIVERY_RETENTION_DAYS` below 14, is told again. A key already
  expired is not touched, and gets `key.expired` if it has not had it, as
  before.
- **A deletion gives every agent its copy back.** A fact told to two or more
  agents of one end user is a row in each agent's namespace, and one newer
  memory can close them all ("I live in Paris" to agents a and b, then "now
  I live in Zanzibar"). Until 1.0.7, forgetting or erasing the Zanzibar
  memory gave Paris back to the first agent only: that open copy counted as
  another value and kept the others closed, and `facts_reopened` answered 1.
  Now each agent gets its Paris back and `facts_reopened` counts each copy
  (the answer of `DELETE /v1/memories/{id}` and of `DELETE /v1/agents/{id}`;
  `korely_forget_memory` over MCP says the same number).
  A Paris open in another agent, told to it again since, no longer keeps a
  copy closed either. One open copy per agent, never two
  ([Time](docs/guide/time.md)). A row whose canonical name is NULL (the
  column allows it) is compared by its label, as the fact reads do, on a
  deletion too. Three limits are written down in
  [Limits](docs/guide/limits.md): a value filed under a third relation is
  not seen as a contradiction; one place under two names in two agents
  (an alias of one agent's own) is two values on a deletion, so the other
  copy stays closed; and a new value closes only the copies its judge
  reads (ten newest and ten nearest), so with more agents than that some
  copies stay open and are not given back, since they never closed.
- **The extraction rules measured on gpt-oss-120b go to a writer whose
  model name names gpt-oss-120b.** Since 0.1.20 two rules of the extraction
  prompt (a change of state written as the new state, the faithful
  observation) are on for the gpt-oss writer. Up to 1.0.7 they went to any
  writer with `gpt-oss` in its model name. Now they go to a name that says
  `gpt-oss-120b`, under any host's spelling and letter case:
  `gpt-oss-120b` (Scaleway, OVHcloud), `openai/gpt-oss-120b:nitro`
  (OpenRouter), `gpt-oss:120b` (Ollama), a GGUF's or Bedrock's name. The
  name is all the product sees of the model, so a fine-tune named after
  those weights (`...-gpt-oss-120b-finetuned`) gets them too, while a model
  whose name puts a word between `gpt-oss` and the size
  (`gpt-oss-safeguard-120b`) does not. A gpt-oss-20b writer, or a name that does not say the size (a model
  served as `gpt-oss`), writes without them, as every writer they were not
  measured on. To keep them for gpt-oss-120b served under a name of your
  own, put the size in the name (vLLM's `--served-model-name gpt-oss-120b`).
  It holds from the next write; no fact is re-extracted.
- **`GET /health`, `GET /admin/provider` and `GET /admin/models` name the
  writer an `EXTRACTION_PROVIDER` line means.** Up to 1.0.7 a line other
  than `gemini` or `mistral` was answered as written (`provider`,
  `env_provider`: `openai`, or whatever was pasted there) while the install
  wrote with Gemini, and `GET /admin/provider` said in `uses.writes` that
  the Google key wrote nothing. They now answer `gemini`, the writer in use.
- **gpt-oss's least reasoning ("low") goes to every gpt-oss, whatever the
  letter case of its name.** Up to 1.0.7 a name written with capitals
  (`GPT-OSS-120B`) did not get it and thought at gpt-oss's default,
  "medium": slower and dearer on every write call. Two different name checks
  exist, so read them apart:
  - The "low" reasoning needs `gpt-oss` to start a word of the name. What
    follows does not matter: `gpt-oss120b` gets it. A name with a letter or
    digit right before it (`xgpt-oss-120b`) got it up to 1.0.7 and no longer
    does.
  - The extraction rules above need the size too: `gpt-oss`, then `-` or `:`,
    then `120b`. So `gpt-oss120b` gets "low" but not the rules.

### Documentation

- **A fact's family is described with the real families.** The reference
  gave `location` and `role` as examples of `predicate_family`, and neither
  is a family: a filter `GET /v1/facts?predicate_family=location` answers an
  empty page, with no error. The descriptions of `predicate_family` (on a
  fact read, on the answer of `POST` and `PATCH /v1/facts`) and of the
  `predicate_family` filter of `GET /v1/facts`, `GET /admin/facts` and
  `korely_get_facts` now list the engine's families, read from the engine,
  and say how the filter reads a name: case and surrounding spaces are
  ignored, a name that is not a family answers nothing (not an error), an
  empty value is no filter while one made only of spaces answers nothing,
  and a name over 200 characters or with a NUL byte is refused as an
  invalid request, as before.
- **`subject_type` says what it takes.** On a fact read and on the body of
  `POST /v1/facts` it says the value is the one the write that stored the
  fact gave: any type of up to 20 characters, stored as sent on a new fact
  (an empty one as `unknown`). A write that restates a fact already true
  keeps the type the fact was first stored with, and `POST` answers that
  fact's id: a `self` sent then is not stored. It names the types
  extraction gives (`concept`, `event`, `organization`, `person`, `place`,
  `product`, `unknown`) and `self`, which extraction never gives but a
  writer may send: `GET /v1/profile` lists a fact typed `self` first, and
  only in lower case. The context block of `GET /v1/context` does not read
  the type, and the description says so: it shows a fact typed `self` as
  "The user" only when its subject is the end user. The body field had no
  description.

### Security

- **A key pasted on the `EXTRACTION_PROVIDER` line by mistake is no longer
  answered by `GET /health`**, which the shipped Caddyfile serves to anybody
  with no key: its `provider` is now `gemini` or `mistral`, never the line
  as written.

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
