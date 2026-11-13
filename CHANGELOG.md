# Changelog

Releases of Korely Agent, newest first. Each entry says what changes for
somebody upgrading from the release before it: what to do before and after,
and what the store will answer differently.

Korely Agent's public history starts at 1.0.2; the releases before it were private.

## 1.0.21

1.0.20 with three comments rewritten, for the public tree: 1.0.20 runs on the
Cloud but never reached the public repository, because the review of its
public copy found comments that named a Cloud feature not out yet. From
1.0.18, read the 1.0.20 and 1.0.19 entries below: that is the upgrade.

### Before and after upgrading

- Nothing to do. No migration, no setting, no change in what any route or
  tool answers.

### What is different

- Comments in the identity rule's code and tests describe the rule without
  naming a Cloud feature that is not released.

### Security

- No change to who can read or write what: only comments and test
  docstrings changed.

## 1.0.20

1.0.19 with one function moved, for the public tree: 1.0.19 never reached
the public repository, because the public copy's own suite failed on it.
Everything 1.0.19 says below applies to 1.0.20 unchanged.

### Before and after upgrading

- Nothing to do. No migration, no setting, no change in what any route or
  tool answers. From 1.0.18, read the 1.0.19 entry below: that is the upgrade.

### What is different

- The function that lists the projects of an account's imports not yet done
  served only the Cloud's agent cap, yet it lived with the batch imports
  shared by both products. The public tree, which carries no Cloud package,
  shipped it called by nothing, and its dead-code test failed. It now lives
  with the Cloud's quotas; the batch module keeps the one condition both read
  ("a job not yet done"), so the worker and the cap still count the same jobs.
- A new check of the private repository runs the dead-code scan on the tree
  the public copy keeps, so a function only the Cloud calls fails where it is
  written, not at release time.

### Security

- No change to who can read or write what: the function that moved runs the
  same query, from the Cloud's package, for the same caller.

## 1.0.19

### Before and after upgrading

- **A program that writes without a `user_id` or an `agent_id` starts
  failing after the upgrade.** `POST /v1/memories`, each row of
  `POST /v1/batch` and `POST /v1/facts` with neither id answer 422 with the
  code `identity_required` and write nothing; a batch with one such row is
  refused whole, as for any bad row. An id made only of spaces, format
  characters (such as U+200B ZERO WIDTH SPACE) or combining marks names
  nobody and counts as neither. Before upgrading, look for writes that send
  neither id and give each one: `user_id` for the person the memory belongs
  to, `agent_id` for the agent that writes it. A program that writes for one
  person only can send any stable id, its own name for instance. `run_id`
  alone is not enough. A key confined to one end user (`end_users`) already
  gives its writes that end user and needs nothing. The SDKs and the
  `korely` command line send the ids they are given and no others: a call
  that gives none gets the 422 (`korely add ... --user-id <id>` or
  `--agent-id <id>`).
- **A program that wrote without ids and now sends a `user_id` starts a
  second memory.** Nothing stored is rewritten, and the judge weighs a fact
  only against the facts of its own end user: the facts written before the
  upgrade with no ids are not compared with those written after it with a
  `user_id`, so a change written after the upgrade does not close an old
  fact that had no id, and a read that names no `user_id` shows both.
  Correct or forget the old one by hand (`PATCH /v1/facts/{id}`,
  `POST /v1/facts/{id}/forget`). Moving the old rows to the new id once,
  with SQL on your own database, is possible and is not done by the
  upgrade. Sent with an `agent_id` and no `user_id`, the new facts and the
  old ones are still weighed against each other about the same subject
  (docs/guide/limits.md).
- **Assistants connected to this install's MCP (`/mcp`) need nothing.**
  `korely_add` with neither id is written where it was written before, in
  the project's memory with no end user, and the name the assistant gave
  when it connected (`clientInfo.name`, which the protocol requires) is
  what says who wrote it: it is kept on the memory as `client` and in the
  audit trail with the key, as before. Reads, the walls of confined keys
  and the judge see it as before, so the facts an assistant wrote before
  the upgrade and after it are one memory. A session whose client gave no
  name it can be told by gets the 422. Several agents that share one
  memory through MCP should each pass their `agent_id`: without it they are
  one writer, as before. The local `korely-mcp` of the Python SDK talks to
  REST: started without `--user-id` or `--repo-scope`, its writes that name
  nobody get the 422; start it with one of them.
- **What is stored stays as it is.** Memories and facts stored without
  ids are read, edited, corrected and forgotten as before. A
  batch job accepted before the upgrade is written as it was promised, its
  rows without ids included.
- No migration and no new setting. The judge's new rule for memories with
  no `user_id` (memory_core 6.9.0, below) applies from the next write; facts
  already stored stay as they are.
- Two answers change (below): deleting a
  project whose Default project writes in another region can now be
  refused, and an erroneous `/authorize` request is answered with a 400 in
  JSON instead of a redirect to the app. A script that deletes projects
  should read a 409 `project_region_differs`; an OAuth client that relied
  on reading its own error back from its redirect URI reads it from the
  400's body instead.
- On Korely Cloud, an agent name is counted once per project: the same name
  in two projects uses two of the plan's agents. An account that used one
  name in several projects may find itself nearer its cap, or at it, after
  the upgrade; its agents keep writing, and only a name new to a project
  past the cap is refused (429 `agent_cap_exceeded`).

### What the store answers differently

- A write with no identity is refused: 422,
  `{"detail": {"code": "identity_required", "message": "user_id or agent_id is
  required: say whose memory this is, with user_id (the end user it belongs
  to) or agent_id (the agent writing it); an id of spaces or invisible
  characters names nobody"}, "code": "identity_required", "message": "..."}`;
  in a batch the message names the row, `memories[3].user_id or
  memories[3].agent_id is required: ...`. It is checked before the plan's
  quota and the trial's writes on the Cloud: a refused write counts against
  neither. Why: without an identity every writer that sent none was read as
  one person, named `user`, and the judge weighed their facts as that
  person's (the rules of that writer closing each other, in the measure of
  memory_core 6.9.0 above; written with an `agent_id` there is no `user`).
  Mem0 refuses the same write.
- MCP: `korely_add` with neither id answers and stores as before (see
  above). A key confined to several end users still answers the 403 that
  asks for a `user_id`.
- What does not change: an edit of a memory (`PATCH /v1/memories/{id}`), a
  correction of a fact (`PATCH /v1/facts/{id}`, the dashboard's, the MCP's
  `korely_correct_fact`), a forget or a deletion keep the ids the row
  already has, and `POST /v1/users/{user_id}/names` names its end user in
  the path.
- An empty `user_id` or `agent_id` is still refused by its own rule; its
  message no longer says to omit the field: `must not be empty: give an id,
  or leave the field out where it may be left out (a write needs a user_id
  or an agent_id)`.
- **memory_core 6.9.0: in a memory with no `user_id` the judge weighs a
  fact against the facts of its own entity, with an `agent_id` or
  without.** Since 1.0.13 (memory_core 6.7.0) a fact of a memory written
  with an `agent_id` and no `user_id` was weighed, by meaning, only against
  facts about the same subject; a memory whose writes carried no ids at all
  was still weighed against the ten nearest facts of any subject. Now every
  memory with no `user_id` is weighed by entity: the facts whose subject is
  the new fact's subject, whose object is its subject, or whose subject is
  its object ("Acme employs user", then "user works at Globex", still meet,
  in an agent's memory too); a fact of another entity is no longer shown to
  the judge only because the two are near in meaning, and two facts that
  only share an object do not meet. With no end user the store cannot know
  the memory is one person's story, the reason the nearest facts of other
  subjects were ever shown. The price: two names of one thing ("Cloud" and
  "Korely Cloud", or the same name in another Unicode form, docs/guide/
  limits.md) are two subjects until an operator declares an alias
  (`POST /admin/aliases`), so a change told under the other name no longer
  closes the old value. Without ids the first person is stored as the
  subject "user": the alias to declare for a name is that name to "user",
  with no `user_id` nor `agent_id`.
- **A memory with a `user_id` answers exactly as before**: every query of
  the judge's candidates is the one of 1.0.18 (LongMemEval and LoCoMo write
  with one).
- Measured on 12 Nov 2026 on the memory of a team of agents written with no
  ids (43 memories, Gemini 3.1 Flash-Lite as writer and judge, the
  extraction recorded once and replayed, the judge asked again in each of
  three runs per version, every closure labelled by reading the two
  memories): wrong closures 19 of 41 with 1.0.18 and 12 of 30 with this
  version, none of them between two subjects (7 before). Outdated facts
  closed by the newer fact of the same subject: 14 and 18; closed, right by
  chance, by a fact of another subject: 7 and 0; outdated facts closed in
  all: 21 and 18. The 3 left open are one outdated version that no fact of
  its own subject says changed. What stays: rules of the writer named
  "user" closing each other (9 in three runs; written with an `agent_id`
  there is no "user"), and a cost already paid pointing to an estimate in
  the same memory (3), which the judge does not decide. Written with an
  `agent_id`, the same memory closed the same 6 facts before and after (5
  wrong, 1 right, two runs each).

### What is different

- **Deleting a project never changes the region its keys and data are
  written in.** What a deleted project held moves into the Default
  project, and from then on the Default project's writer writes it. When
  the Default project writes in another region (or follows the install's
  default writer while the deleted project chose a region of its own), and
  live API keys would move, or, without `?erase=true`, any memory, fact or
  batch job, `DELETE /admin/projects/{id}` answers 409
  `project_region_differs` and nothing is deleted. The message names both
  regions, what would have moved, and the two ways on: give the two
  projects the same region first (Edit on the Projects page, or
  `PATCH /admin/projects/{id}` with `region`; it applies from the next
  write), or revoke the project's keys and erase it. With `?erase=true` the
  check counts the keys left live after the erasure (keys confined to the
  erased end users are revoked by it) and, refused, the erasure is rolled
  back. An app connected over OAuth does not hold a deletion (its key is
  revoked, never moved), and a project with no live key and nothing in it
  is deleted as before. Projects of one region move as before.
- The answer of a deletion has a new field, `region`: the Default
  project's region, where what moved is written from now on (null when no
  writer serves it yet); it is the deleted project's own whenever live keys
  or data moved, while a key that no longer works (expired, revoked) moves
  whatever the regions. The dashboard's delete dialog says the rule before
  the click, shows the refusal's sentence, and the summary after a move
  says "Default writes in Europe" (or Global).
- **`/authorize` sends the browser only to the consent page.** An
  erroneous request (a scope the client did not register, such as
  `admin`; a missing or `plain` PKCE challenge; `response_type=token`; a
  resource that is not this server's MCP; a NUL byte in a field of the
  POST form; the network's hourly allowance found spent only when the
  request is written, under its lock, where the usual answer to a spent
  allowance stays the 429 given before the SDK runs) is answered 400 with the
  error in JSON (`error`, `error_description`, `state`), as the SDK
  answers when it cannot redirect, and with `Cache-Control: no-store`. It
  was a 302 to the client's redirect URI with the error in the query. The
  app's address is reached only from the consent page, after a person
  signed in and saw its host (approve or deny), as before.

### API

- New error code `identity_required` (422) in the list of `code`, on
  `POST /v1/memories`, `POST /v1/batch` and `POST /v1/facts`. The OpenAPI
  descriptions of `user_id` and `agent_id` on those three bodies say that
  one of them is required; the fields stay optional one by one.
- The MCP's `korely_add` description says what a call with neither id
  writes.

### Security

- No change to who can read or write what: the identity a write must carry
  is checked after a key's end users, so a key confined to some end users
  writes and is refused as before, and an MCP write that names nobody lands
  where it did before, in the memory with no end user, which no confined key
  reads. A refused write leaves no row in the audit trail, as every
  malformed write.
- memory_core 6.9.0 changes only which earlier facts the judge reads for a
  memory with no `user_id`: no security change.
- **Project deletion: no fallback from Europe to Global.** On Korely Cloud
  the Default project is Global: deleting an EU project without erasing it
  moved its keys, and its memories still in the queue, into Default, where
  the Global provider wrote them from then on, and neither the dialog nor
  the answer said so. Refused now, as above. Measured with the tests of
  this release (`tests/test_a_deletion_keeps_the_region.py`, on a local
  database with faked providers): the memory stays pending in the EU
  project and the key in it; not tried on the Cloud.
- **`/authorize` is no longer an open redirector** (RFC 9700 section
  4.11.2). Registration is open to any https address, and the MCP SDK's
  handler (`mcp` 1.30.0) sent its errors to the registered address: a link
  starting with this server's address, with `scope=admin`, took the
  browser to a site anyone registered, with no consent page. The handler's
  answers are now checked on the way out: a redirect passes only to the
  consent page the provider chose for that request, so an error the SDK
  adds later is caught the same way. Nothing was readable through it: no
  key, code or memory left.

## 1.0.18

### Before and after upgrading

- Nothing to do: no migration, no new setting, and no answer of the API,
  the MCP or the dashboard changes.

### Security

- **Dependencies with published advisories, moved to the first fixed
  release** (each one is at least two weeks old; nothing else moves):
  - `certifi` 2026.7.22 -> 2026.10.23, in the image: the CA bundle no longer
    trusts the emSign roots whose intermediate keys were compromised
    (GHSA-6q48-6c7w-9gw2). Korely's calls to model providers and webhooks
    verify TLS against this bundle.
  - `pyasn1` 0.6.4 -> 0.6.5, in the image: four quadratic-time decodings of
    crafted ASN.1 (GHSA-2qm8-x737-gmw5, GHSA-9m75-vc3g-4x39,
    GHSA-fm9c-5rcf-2q5p, GHSA-mm57-4v3p-rchx).
  - The dashboard: `vite` 6.4.3 -> 6.4.5 (the tool that builds it and its
    development server, not part of the built pages), and, through
    overrides in `dashboard/package.json`, `follow-redirects` 1.16.0 ->
    1.16.2 and `re2js` 2.8.6 -> 2.8.8.
  - `scripts/diagrams` (the tool that draws the README's diagrams, not part
    of an install), through overrides: `nanoid` 3.3.3 -> 3.3.18 and
    4.0.2 -> 5.1.16, `lodash-es` 4.17.21 -> 4.18.0, `katex` 0.16.47 ->
    0.18.2.
- `docs/DEPENDENCIES.md` lists the new versions.

### Tools in the repository (nothing to do for an install)

- **`scripts/release.sh` refuses a release whose CHANGELOG entry is not on
  top under its own heading** (`## <version>`, with no `(Unreleased)`),
  before it changes anything. The script writes `VERSION`,
  `pyproject.toml` and `install.sh`, never the CHANGELOG's text, and
  `v1.0.17` was tagged with `## 1.0.17 (Unreleased)` on top; the 1.0.17
  entry's heading is now `## 1.0.17` on main (the tag stays as it is).
  Tested in `tests/test_a_release_carries_its_own_heading.py`.

## 1.0.17

### Before and after upgrading

- Nothing to do: no migration, no new setting, and no answer of the API,
  the MCP or the dashboard changes.

### What is different

- **The README says what is in this repository**: the memory, projects,
  agent notices, the MCP server, the REST API and webhooks, the dashboard
  and the benchmark scripts. Korely Cloud runs the same code with sign-up,
  plans and payments and spending limits on top, which are not here.
- Comments in the code and in the tests that named who reviewed or decided
  a change now give only the date of the review or of the decision.
- The tests' made-up data no longer copies the rules of the team that
  builds Korely (a policy, a deploy procedure, a server), and their made-up
  addresses are on `example.test` and `example.com`. What the tests check
  is the same.
- memory_core 6.8.1: the same change in two comments of the facts engine,
  text only. No code, prompt or printed line of the engine changes.

### Security

- No change: only the README, the CHANGELOG, comments, test data and the
  test suite change. The suite's test databases are dropped as before, only
  after waiting for the sessions that are leaving.

### Tools in the repository (nothing to do for an install)

- **The test that failed on the CI's runs of 10 Nov 2026.**
  `test_a_workers_drop_ends_none_of_the_applications_connections`
  (`tests/test_the_suite_runs_in_several_processes.py`) counted 2 sessions
  left open where it had left 1, on main and on `v1.0.15`, and passed on the
  Mac. `drop_a_runs_database` (`tests/conftest.py`) closed the pools of the
  application's engines and counted the database's sessions on the server at
  once; a connection the client has closed stays in `pg_stat_activity` until
  its backend process ends, a moment later. With the test and a Postgres of
  its own on two processors, as on the CI's runner, it failed in 4 runs of
  15 (3 times 1 for 0, once 2 for 1, the CI's message); in 15 more runs with
  a probe, the 12 sessions seen right after the close beside the one left
  open were all the application's two connections the drop had just closed.
  On the Mac's shared Postgres it passed. No thread or engine of an earlier
  test was involved: in the suite run in the CI's order on the Mac, up to
  this file, the threads left were idle pools, with no connection on the
  database. The drop now asks PostgreSQL first for a drop without `FORCE`,
  which waits up to 5 seconds for the backends still in the database and
  refuses (`object_in_use`) only when one stays; only then it counts the
  clients' sessions (`backend_type` `client backend`) and drops `WITH
  (FORCE)`. A closed session is no longer counted as one left open, and
  `FORCE` no longer ends it, with the "terminating connection" line in the
  server's log that the closing was there to avoid. The count leaves out the
  other processes in the database: during the longer wait an autovacuum
  worker came in and was counted in 3 runs of 10 on two processors, and the
  parallel worker of a session's query is that session's. A drop with a
  session left open now takes 5 seconds, PostgreSQL's wait: the file's tests
  pay it three times, and a worker of a parallel run pays it at its end only
  when a test left a connection open. The test also runs with every closed
  connection reaching the server half a second late (`closed_late`), which
  fails the old count every time; a session left open in the middle of a
  parallel query counts once; and a drop without `FORCE` given up on for
  another reason (cancelled while it waits) is raised, never forced. On two
  processors the test now passed in 15 runs of 15, both cases; it has not
  run on the CI's runner yet.

## 1.0.16

### Before and after upgrading

- If something watches `status`
  on `/health`, it can now read `degraded` for a writer whose key the
  provider refuses (below), where it could read `ok` until 1.0.15.

- One migration runs when the service starts: `b6e2d9f4a8c1` adds the
  column `born_before_the_trial` (false by default) to `hosted_accounts`,
  a table only the Cloud fills. On an install of your own the table is
  empty: the migration is instant and changes nothing else (no new
  setting, no API answer of a different shape). The downgrade drops the
  column.

- No migration and no new setting. A fact whose end a later value moved
  before the upgrade keeps the end it has now: forget it again with your
  `at` (`POST /v1/facts/{id}/forget`) to give it back the end you said.

### What the store answers differently

- **A writer refused for its key is told, as one refused for payment.**
  When a region's writer answers HTTP 401 or 403 and the drain rests the
  region for it (the same rule as before: a 401 at once, a 403 only when the
  memories of two accounts fail, or nobody else waits, since a moderated
  model can refuse one input with a 403), `/health` turns `status`
  `degraded` until a memory of that region goes through, and on the Cloud
  the operator gets one email per rest, per process (each process of the
  service keeps its own rests) ("The eu writer's key is refused",
  the region and the status, never the provider's words), as a refusal for
  payment (402) has been told since 15 Oct 2026. Until 1.0.15 a 401 or a 403
  showed only as `refused` under `writers` and in a log line, with `status`
  back to `ok` as soon as another region's writer answered a call (the
  `extraction` state is the process's last call, whichever region made it):
  on 4 Nov 2026 the Cloud's European key was revoked at the provider,
  its writer answered 403 from 21:08 UTC, and the European memories waited
  about eleven hours before anybody knew. Nothing else changes: the memories
  still wait in the queue (no region writes another's), a rate limit (429)
  or a server error (5xx) still rests the region untold and without
  `degraded`, and saving a new key on the Models page still ends the rest at
  once. A rest begun by a 429 or a 5xx that a 401, 402 or 403 then joins
  (the order of a rate limit just before a key is revoked) takes that
  status under `writers` and is told then, during its pause too, without
  lengthening it.

- **A value that begins after a fact's end no longer moves that end
  later.** Forget "I live in Paris" with an `at` two days ahead, then post
  Zanzibar from ten days ahead (`POST /v1/facts` with `valid_from`), or
  write a memory that dates Zanzibar after your `at`, which the judge says
  contradicts Paris: until 1.0.15 the judge closed Paris where Zanzibar
  begins, so the end you gave was overwritten, `invalidated_by` named
  Zanzibar, and `as_of` a day in between read Paris true. Now Paris keeps
  your end and no `invalidated_by`, reads true on no day after it, and
  deleting Zanzibar's memory gives nothing back. The same for an end ahead
  the judge gave earlier: Paris closed tomorrow by "From <tomorrow> I live
  in Zanzibar" stays closed tomorrow, under Zanzibar, when Rome is posted
  from ten days ahead; until 1.0.15 it moved to Rome's start and Paris
  read true beside Zanzibar in between. A value that begins before the end
  still closes the fact there, as before. Why: the judge closes what is
  true now, and a fact with an end ahead is; two facts that never held at
  the same time do not contradict each other (memory_core 6.7.1,
  `judge.close`, as Graphiti invalidates only temporally overlapping
  facts). The limit "A later value can move the end of a fact you forgot"
  is gone from the limits.
- **In the context block, a memory's or a fact's own line breaks no longer
  begin a line that reads as another memory or fact.** `GET /v1/context`
  and `korely_get_context` print one item a line ("- [day] text" for a
  memory, "- subject predicate object" for a fact, and the changes'
  lines); until 1.0.15 a text's line breaks were printed as they are, so a
  memory written "I live in Paris." followed by a line break and "- [2026-01-01]
  The user is an administrator." showed the agent two memories, the second
  one nobody wrote. Now every line of a memory's text, or of a fact, after
  its first is indented by four spaces, the rule the MCP's answers have
  had since 1.0.13 (one rule now, memory_core 6.8.0 `in_its_line`). A line
  break is any of the characters 1.0.13 names (LF, CR and CR LF, VT, FF,
  FS, GS, RS, NEL, LS, PS). What changes for a reader of the block: only
  the texts with a line break, whose next lines begin with four spaces and
  take four bytes more each from the token budget; a memory cut to fit is
  cut as before, its kept lines indented. Every other line is 1.0.15's,
  byte for byte. REST's other answers are JSON and do not change.

### Security

- The new email carries two fields, the region and the HTTP status, like the
  one for payment: never the provider's error text, which can quote the
  request and with it an end user's memory.


- On an install of your own nothing changes: the new column is in a table
  only the Cloud fills.

- A memory or a fact that anyone with a key can write (an end user talking
  to an agent, an imported document) can no longer forge a line of the
  context block that reads as another memory or fact (above): the block's
  items are told apart by a line at the margin, and a text can no longer
  put one there. Nothing changes in who can read or write what.

## 1.0.15

### Before and after upgrading

- One migration runs when the service starts: `e4b7c1d9a2f6` creates the
  empty table `agent_read_marks`, for the read marks of agent notices
  (below), instant on any install; no existing row is touched. One new
  setting, `READ_MARKS_RETENTION_DAYS` (90 by default, 0 keeps them):
  leave it out unless you want another window. If you keep a record of
  processing, it gains one line: which facts an agent that named itself
  read, and when, kept that many days after the last read (below).
- **No setting changes for the values you have today.** Every value an
  install starts with starts with 1.0.15 too, with the same meaning: a
  retention of 0 or less keeps for ever, `SESSION_HOURS=0` is a login of one
  hour, `DRAIN_STALE_MINUTES=0` one minute, `FACTS_EXPORT_EVERY_SECONDS`
  under 30 is 30 seconds. A duration longer than a century is read as a
  century where it is added to the clock or taken from it, and the log says
  so once per setting (`... is longer than a century: read as a century`):
  until now a value no clock holds failed there, the worker's hourly purge
  for a retention of 3,000,000 days, every sign-in for a `SESSION_HOURS` of
  a hundred million, the batch reaper for a `DRAIN_STALE_MINUTES` of ten
  billion.

### What the store answers differently

- **Without `reader` nothing changes but one cut.** A read without it is
  the read of 1.0.14, byte for byte, and writes no mark (tested on the four
  doors that take it, REST and MCP), except where the block cuts a memory
  too long for its room: the cut now falls between two characters as a
  reader sees them (Unicode's extended grapheme clusters, UAX #29), never
  inside an emoji of joined code points, a flag, a keycap, a letter and its
  combining marks, an Indic consonant and its signs, or a Hangul syllable
  in conjoining jamo. Where 1.0.14 cut inside such a character, the
  fragment now ends before it: it loses what 1.0.14 kept of that
  character, a few bytes for an emoji or an accent, up to all the rest of
  the room for one character of hundreds of bytes (a letter under a pile
  of marks). A memory whose first character after its date is longer than
  the room shows its date and an ellipsis (`- [2026-11-09]…`), and is
  still among the `sources`, as it was in 1.0.14 with part of that
  character. The rule reads the properties
  Python's `unicodedata` carries, so it is the same in every script, with
  one rule of the annex not read (GB9b: a Prepend sign, U+0600 to U+0605
  and a few others, can still end a fragment).
- **With `reader`, the context block can open with what changed** (agent
  notices): `GET /v1/context` and `korely_get_context` put a section
  "Changed since you last read" at the head of `volatile`, before the known
  facts, when a fact this reader was handed by an earlier read has ended
  since and is not true again in another row (given back after a deletion,
  or told again): "was: ... -> now: ..." (or "no longer true: ..."), the day the
  store learned it and where it came from (corrected, forgotten, stated by
  a caller, read from a memory: the action that ended the fact, see
  `origin` below). What holds now is a fact true now (begun, not ended)
  that the key may read, down the chain of what replaced the fact,
  however long. At most 3 lines, inside the token budget, chosen newest
  first among the 20 newest that wait, and among the next ones, up to 200
  in all, when those 20 leave a place free and room for a line (below): a
  change too long for the room is written short (each fact cut with an
  ellipsis, what ended, the day and where it came from whole; REST's
  `changed` keeps the facts whole) in its share of the room, what is left
  divided among the places still free, the changes that wait after it
  counted, those not looked at yet too. Each fact written short shows where
  the two begin to differ: when they begin the same (an address, a plan
  with its seats, a procedure), its beginning, an ellipsis, and the part
  that differs, so the two pieces never read the same; a change where not
  even that fits is not written short, so it is not consumed and waits for
  a block with room. A cut never splits a character as a reader sees it
  (above). One that does not fit its share waits while the others are
  packed, then is told in what they leave, alone if need be: a long change
  takes at most its share while others wait behind it. When, those packed,
  a place is free, a change of the 20 newest was passed over and the room
  left holds the smallest line a change can take (one that no longer
  holds, without its words: 42 bytes, and the heading on the first line),
  the block looks past them, with one lookup more, and packs again: so a
  change that cannot be told there, for its words or its length, keeps out
  no change the block has room for while fewer than 200 such changes stand
  ahead of it, and an older one waits as long as 200 stand ahead of it,
  counted in `changed_more`. Tested at every budget from 100 to 450 tokens
  two by two, with 22 long changes ahead of two short ones (replaced, or no longer
  true), in a block with an end user's facts and memories and in another
  namespace's: the block tells at least as many changes as the block of a
  reader handed the two short ones alone; and with 22 changes that cannot
  be told short at 200 to 400 tokens: the block tells what that reader is
  told (both short ones at the first read, or at 200 tokens with the end
  user's facts one at the first and one at the next). Tested also at 300
  to 500 tokens in blocks with an end user's facts and memories, behind one
  to three long changes: every change told, at least one a read, the
  memories in every read; where the known facts leave the section room for
  three lines, a short change waits only behind three lines. One that does
  not fit even alone waits for a block with room: written short, a change
  needs 126 bytes (135 when it was stated by a caller or read from a
  memory, 85 and 94 when it no longer holds), plus one for its line break,
  33 on the section's first line for the heading. The
  section takes room after the
  known facts, from the memories' share: at most half of it, and never what
  the first memory needs to be shown (its heading and its shortest
  fragment), so a block with memories keeps one; with `reader` the block
  has the known facts, `stable` and `stable_hash` of the block without it. Each
  change is told at most once, even to two reads at the same moment: a
  block reserves the changes it shows before it is written (an answer that
  fails after that loses them). The section is the reader's whatever the
  block's `agent_id`: it tells every change of what this reader was handed,
  by a read of any agent namespace or of none, and of what it was told
  holds now, whoever wrote it (`agent_id` narrows what the block reads,
  `reader` names who is told). It writes each fact as the known facts do
  ("The user" only for the block's own end user). What it shows, the fact the reader had and what
  holds now, is read: in `sources`, in the audit trail and marked for the
  reader, so a change of what holds now is told in its turn. A read that
  failed (`degraded`, with neither facts nor memories to show) tells no
  change: it answers as without `reader`, and the changes wait. REST's
  answer to a read with `reader` adds
  `changed` (the changes the block shows) and `changed_more` (how many
  more wait), empty when nothing changed, `changed_more` null when the
  changes could not be read this time (a lock on their records: they wait
  for the next read); an answer without `reader` has neither field.
  `GET /v1/facts` with `reader` keeps its marks and tells nothing. A block
  of the past (`known_as_of`) tells nothing and consumes nothing.
- **A forgotten memory's words are never told.** A fact replaced and then
  forgotten with its memory (`DELETE /v1/memories/{id}`) is told without
  its words: `was` is null, and the block says "a fact whose memory was
  forgotten". A fact another live memory still states keeps its words, as
  the forget keeps it. A fact its memory's forget ended is no change.
- **The hourly purge goes on past a record that fails.** Each record is
  trimmed on its own; one that fails is logged and the others are still
  trimmed, and `/health` shows the `purge` job as `failing`, as before.

### API

- **`reader`, the first piece of agent notices**: an optional parameter on
  `GET /v1/context`, `GET /v1/facts` and the MCP's `korely_get_context` and
  `korely_get_facts`, the reading agent's own name, by the rule of
  `agent_id` as a write gives it (empty, `.` or `..`, a `/`, any line break
  or other control character, or more than 255 characters is a 422,
  `invalid_request`, on REST and MCP alike), so `DELETE /v1/agents/{id}`
  reaches every reader's marks, a name that only ever read included. It
  filters nothing. With it, a
  read of what is true now keeps, for each fact it handed over, one row:
  the account, the project, the reader, the fact's id, the fact's end user
  and when the reader first and last read it; a second read moves the time
  and adds no row. No text is kept. A read of the past (`as_of`,
  `known_as_of`) or of the history (`include_invalidated=true`) keeps
  nothing, and neither do the dashboard's reads, `GET /v1/profile`,
  `GET /v1/memories/{id}` (a memory with its facts), `GET
  /v1/memories/{id}/history` and the memory search. The context block
  reads them to tell a reader which facts it read have changed since
  (above). Do not use an end user's id as a reader's name: name the agent.
- **`changes`** on `GET /v1/context` and `korely_get_context`: left out,
  every kind of change is told in a memory with an end user or with no id
  at all, and in an agent's memory with no end user (the memory of a team
  of agents) only
  the explicit ones, corrected, forgotten and stated by a caller: there the
  closures the store read from memories were mostly wrong when measured
  (21 of 32 on our own team's memory with memory_core 6.6.0, an estimated
  4 of 5 with 6.7.0). `changes=all` tells those too. A change not told is
  not counted in `changed_more` either.
- **`origin`, the action that ended the fact**, read where that closure is
  recorded, never from who wrote the fact: `corrected` (a correction,
  `PATCH /v1/facts/{id}`), `forgotten` (`POST /v1/facts/{id}/forget`),
  `stated` (a fact a caller wrote with `POST /v1/facts` replaced it), and
  `read_from_memory` for every other closure: the store's reading of a
  memory (the judge, a plan come true, an edit), and every end in place. A
  past statement ends a fact in place and writes on it only its end,
  whether a memory said it or a caller (`POST /v1/facts` with `tense:
  past`, whose record of 1.0.14, scope `past`, is no forget): such an end
  reads `read_from_memory`, so it is never told as explicit. A forget that closed nothing, or one after
  which another closure ended the fact sooner, is not the closure. A successor the key
  may not read is no successor for that key: the change reads "no longer
  true", and nothing of it is told, its words, its agent, its memory or
  its provenance.
- **A mark never fails the read, and never holds it long.** It is written
  in a session of its own before the answer is sent, waits at most 0.2 s
  for another transaction's lock (a request waits 5 s) and 1 s for its
  statement, then gives way: to an erasure that wants the same rows, a
  deletion or a late vector judgement holding a fact, a migration holding
  the table. Giving way is no failure: `/health` counts it apart, under
  `records.read_marks.gave_way`, and stays `ok`. The facts of a read that
  no other transaction holds are marked at once; one held is marked after
  them, within the 0.2 s, or skipped when its deletion commits meanwhile
  (`dropped_with_what_they_name`), and past them it alone stays unmarked.
  The block's look at the changes and its reservation of them have the
  same deadlines: under a lock on the marks the block comes without the
  section, as without `reader` (in under a second in the tests). A mark
  that fails for any other reason is logged without the reader's name and
  counted on
  `/health` as lost (`records.read_marks` reads `failing`), which appears
  once a read has named its reader in the process.
- The worker deletes a mark `READ_MARKS_RETENTION_DAYS` (90) after its
  last read, once an hour with its other records. A mark goes with its
  fact, with its project (erased, or deleted and moved to the Default
  project: the marks of a deleted project are not moved), with its
  account, and with the erasure of its end user (`DELETE
  /v1/users/{id}/memories` and every other erasure door): deleted, not
  kept under a pseudonym. The erasure also takes the marks whose reader's
  name is that end user's id, in the project it is made in, and the
  deletion of an agent namespace (`DELETE /v1/agents/{id}`) the marks whose
  reader's name is that agent's, in its project; its answer counts them,
  `read_marks_deleted`.
- **An end user's or an agent's id refuses every line break wherever one
  is given**, the Unicode line and paragraph separators (U+2028, U+2029:
  categories Zl and Zp) as the control characters were refused: a
  `user_id` or `agent_id` a write gives (`POST /v1/memories`, `POST
  /v1/facts`, each row of `POST /v1/batch`, the `agent_id` of `POST
  /v1/users/{id}/names`, the MCP's `korely_add`), a `reader`, the end users
  a key is confined to (`users` of `POST /admin/keys`) and those a person
  names at an OAuth consent: a 422, `invalid_request` (the MCP refuses
  the call; a consent's request stays open). Such an id could not be read
  back from a listing and typed into an erasure. What is stored stays as
  it is: such ids are read and erased as before (`DELETE
  /v1/users/{id}/memories` with the id percent-encoded), and a key that
  already lists one keeps working. Two things in flight at the upgrade
  meet the rule: a `/v1/batch` job queued before it imports every other
  row and fails that one alone, its reason in the job's `errors`; an OAuth
  code approved in the ten minutes before it whose list holds such an id
  is spent at its exchange with `invalid_grant`, and the app connects
  again. Before upgrading: change such ids in what your
  clients send, as an id, a reader's name or a key's or a consent's end
  users; the ones stored can be listed with `SELECT DISTINCT end_user_id
  FROM agent_memories WHERE end_user_id ~ '[\u2028\u2029]'` (and the same
  for `agent_id`, and on `agent_facts`).

### Security

- A read mark names only a fact the read handed over: its row is made from
  the fact itself, in the read's account and project, so a key confined to
  some end users marks only theirs. The reader's name is the caller's word
  and is not checked against the key: any caller of the project can read
  under another agent's name, leave marks under it and be told the changes
  that wait for that agent, which that agent is then not told; it never
  sees more than its own key does.

## 1.0.14

### Before and after upgrading

- Two migrations run when the service starts. `a7c4e9f2d1b6` adds an index
  on `agent_forget_audit` (account and target), read by every deletion that
  would give back a fact. `c5e8b3f1a9d4` adds the table
  `agent_fact_ended_by`, ids only: for each fact, every memory whose own
  words ended it in place (below), with foreign keys to the facts, the
  memories and the accounts (ON DELETE CASCADE) and an index on the memory.
  Both drop with the downgrade. No new setting. Both run in the migrations'
  one transaction, as every migration here does, and the entrypoint
  migrates before the API starts. Measured on the test database (9 and 10
  Nov 2026): with 200,000 forget records the index of `a7c4e9f2d1b6` took
  1.03 s, and a forget, which writes a record, waited 0.83 s for it; with
  200,000 facts `c5e8b3f1a9d4` took 0.03 s, and while its transaction is
  open the reads of the facts and of the memories answer at once, the
  writes of both wait for its commit (its foreign keys lock the two tables
  against writes).
- The facts already stored get no row in `agent_fact_ended_by`: who ended
  them in place was never written, and the upgrade does not guess it. A
  deletion reads them as until 1.0.13 (see the limits).
- A fact that came back true by the defects corrected below, before the
  upgrade, stays open: forget it again (`POST /v1/facts/{id}/forget`).

### What the store answers differently

- **A fact you said is over by hand stays over when the memory that closed
  it goes.** "I live in Paris", then Zanzibar, which closes Paris
  (`invalidated_by`). Forget Paris (`POST /v1/facts/{id}/forget`, with or
  without `at`), correct it (`PATCH /v1/facts/{id}`), or post it in the past
  tense (`POST /v1/facts` with `"tense": "past"`), then delete what said
  Zanzibar (`DELETE /v1/memories/{id}`, with or without `erase=true`, an
  edit of its text that no longer says it, or `DELETE /v1/agents/{id}`):
  until 1.0.13 Paris could come back true, as a new fact, and
  `facts_reopened` counted it; now it stays closed and `facts_reopened`
  says 0. The case of the report: the judge closes Paris on a day still
  ahead, when Zanzibar begins later (a fact posted with a `valid_from`
  ahead, or a date ahead in a memory's words), and you forget Paris before
  that day, on it or after it. A forget whose `at` is on or after the end
  the judge gave still answers `already_forgotten`, with that end in
  `invalid_at`: the fact ends by then, and a forget never moves an end
  later; it counts as your word all the same, and so does a forget of a
  fact the judge had already closed. Why: a forget, a correction or a past
  statement moves the end and leaves the pointer the closing gave the row,
  and the deletion read that pointer as what had closed it. It now reads the
  record a forget and a correction write (the forget's `audit_id`). A past
  fact posted by hand on a fact the store holds now writes a record of its
  own in the same table, `agent_forget_audit`, with scope `past`, so that a
  reader of the records tells it from a forget or a correction (scope
  `fact`); `facts_invalidated` is 1 when it closed the fact, 0 when the
  fact was over already. One that restates nothing the store holds writes
  none. The answer of `POST /v1/facts` does not change.
- **A memory that ended a fact in place keeps it closed while it says so,
  and gives it back when it no longer does.** "I no longer live in Paris",
  or an edit of "I live in Paris" into a text that no longer says it, ends
  Paris in place; before the day Zanzibar begins, both leave the pointer to
  Zanzibar. The write that ends it now records that memory
  (`agent_fact_ended_by`, not in the API's answers), and so does every
  later memory that says it again ("Paris is not my home any more", "I used
  to live in Paris"), whether the memories are written one by one, wait
  together for the worker (`WRITE_ASYNC` on, the default), or come in one
  `POST /v1/batch`. While one of those memories is live and still says it,
  deleting Zanzibar's memory gives nothing back; until 1.0.13 it gave Paris
  back, and with `WRITE_ASYNC` on also when the memories were waiting for
  the worker together. An edit of that memory that still
  says it (a typo fixed) keeps it. When the memory is forgotten, erased, or
  edited so that it no longer says Paris is over, and nothing else closes
  Paris, Paris is true again, as a new fact: from the forget or the edit,
  from where Paris ended when the memory is erased, from the latest of
  them when several said it. So deleting "I no longer live in Paris" when
  "I live in Paris" is all that is left gives Paris back, which until 1.0.13
  it did not; deleting it while "Paris is not my home any more" is live
  gives nothing back, and deleting that one too gives Paris back; and
  deleting it and Zanzibar's memory gives Paris back at the second
  deletion, in either order. What the judgement of that statement closed
  ("I love Paris", beside "I no longer live in Paris": the facts of the same
  two entities, memory_core `writing._judge_the_past`) is that memory's
  word too: it comes back with Paris when the memory goes or is edited
  away, and stays closed while Paris does (until 1.0.13 neither came
  back). While Zanzibar from a later day still stands, deleting the memory
  that said Paris was over gives nothing back (see the limits).
- How a deletion knows what closed a fact: the newer fact it points at
  (`invalidated_by`), as until 1.0.13, and who ended it in place, as written
  when it happened (the record of a forget, a correction or a past fact
  posted by hand; the memories in `agent_fact_ended_by`). A memory that ended nothing is no
  part of it, whatever its date: "I like cats" written the same day as
  Zanzibar, before or after it, or between Zanzibar and the memory that
  said Paris was over. A merge that moves the pointer to an older twin of
  Zanzibar (a project deleted without its data, an alias) changes nothing:
  deleting every memory that said Zanzibar gives Paris back. Unchanged: a
  closing ahead that nobody brought earlier is given back from its day;
  forgetting by hand the fact that was to replace it gives nothing back by
  itself; a past statement and the new value of the same memory ("moved to
  Pro, it used to be Basic") give Basic back when that memory goes, or is
  edited into a text that says neither, also after that value merged into
  an older twin. Still as before, and listed in the limits: the forgotten
  fact's `invalidated_by`, and the 409 of a correction on it, still name the
  fact that was to replace it; and the facts ended in place before 1.0.14
  read as until 1.0.13. Listed in the limits too: a forget with an `at`
  later than the end a newer value then gave the fact loses that date when
  the value goes (the fact stays closed from the value's start; until
  1.0.13 it came back true for good); and a value posted by hand and
  forgotten by hand keeps closed what it closed.

### Security

- No change to who can read, close or give back what: a deletion reads and
  gives back facts in the memory's scope (the account, the project and the
  end user), as in 1.0.13. The new table `agent_fact_ended_by` holds ids
  only (a fact, a memory and their account; no word of either) and goes
  with the fact, with the memory at its erasure, and with the account, by
  its foreign keys; a memory of another account named in it is never read
  as one that keeps a fact closed. The record of a past fact posted by hand
  (scope `past`) holds what a forget's record holds, and an end user's
  erasure keeps it by pseudonym, as every record of that table.

## 1.0.13

### Before and after upgrading

- No migration and no new setting, on an install of your own and on the
  Cloud. What changes is which earlier facts a new fact can close in an
  agent's memory with no `user_id` (memory_core 6.7.0, below), and the
  search: `POST /v1/memories/search` and the MCP's `korely_search` take two
  new optional parameters, `types` and `full_text`, and a result can carry
  three new fields when one of them is asked for (below). Nothing is
  redone by the upgrade itself: the facts already written, and those
  already closed, stay as they are. A fact closed wrongly before the
  upgrade stays readable with `include_invalidated=true`; post it again
  (`POST /v1/facts`) to make it true again. A search that sends neither
  new parameter answers as in 1.0.12, byte for byte, with the same trail,
  with two exceptions for every caller: the 422 of a body field the search
  does not know lists the fields it takes, and that list now names `types`
  and `full_text` too; and on the MCP a memory or a fact whose text has a
  line break is printed with its next lines indented (below).
- **If your agents write with an `agent_id` and no `user_id`** (the memory
  of a team of agents, an agent's own notes) and name one thing in two
  ways ("Cloud" and "Korely Cloud"), declare one name as an alias of the
  other in the namespace of the agent that writes (`POST /admin/aliases`
  with its `agent_id`): from this release a new value of one closes the
  old value of the other only when the two are one subject.
- A memory with a `user_id` is judged exactly as in 1.0.12, statement for
  statement, and so is a memory with neither id, except next to the facts
  of an agent's memory with no `user_id` in the same project (below, the
  price of the new rule).
- **Upgrade the install before the client that sends them.** An install of
  1.0.12 refuses a body field it does not know (422): a script that sends
  `types` or `full_text` to it gets a 422, not the memories. The SDKs of
  today (korely-memory, Python 0.1.28 and JavaScript 0.1.17) do not send
  either: call the REST route, or the MCP tool, until they do.

### What the store answers differently

- **In an agent's memory with no `user_id`, a new fact is weighed against
  the facts of its own subject** (memory_core 6.7.0). Its judge reads the
  facts about the same subject, as the store names it after the aliases
  of the agent that writes (same relation, a related object, or the ten
  nearest by meaning among them), and the facts about the same two things
  the other way round. Until 1.0.12 it also read the ten facts nearest by
  meaning of any subject. On the memory of our own team of agents,
  reloaded on 7 Nov 2026 (106 memories, 422 facts, all with an `agent_id`
  and no `user_id`), 21 of the 32 closures were wrong ("the SDK repository
  has license MIT" closed by "the license is currently Apache 2.0"; two
  disk thresholds that are both true), and 29 of the 32 pairs reached the
  judge only as facts nearest by meaning. Replayed with the verdicts that
  judge gave, no model called (an estimate), the new rule keeps 4 wrong
  closures (3 between two facts of one subject that are true together, 1
  made with no judge) and 1 of the 8 right ones, and the judge reads 666
  lines instead of 3,740.
- What it costs: a right closure between two spellings of one thing is
  lost as a wrong one is. "Cloud has version 1.0.11" stays true beside
  "Korely Cloud runs version 1.0.12" until an alias makes the two names one
  subject (above). Every agent of the project still weighs every other
  agent's facts about the same subject when the two name it the same way,
  each after its own aliases: a change told by one agent closes what
  another said of it under that name. An alias is declared in one agent's
  namespace and renames that agent's facts only, so to make two spellings
  one subject for several agents declare it in each of their namespaces;
  every alias counts against the account's 1,000 declared names.
- **The price of a rule that holds both ways:** in a project where agents
  write with no `user_id`, a fact written with neither id (one person using
  the MCP, say) and an agent's fact are weighed against each other by
  meaning only when they are about the same subject, in whichever order
  they arrive, so a change told by one of them under another name leaves
  the other's value open. Among the facts written with no ids it is
  weighed as before.
- An `agent_id` that is empty or only whitespace, in any alphabet, is no
  agent for the judge, as it already was for the extraction in 1.0.12: such
  a memory is weighed as one with no ids.
- The same rule holds when a fact's vector arrives after its write (the
  judgement the vector drain makes then).
- Not measured with a real judge: the facts of one subject the judge is now
  shown in place of the others (197 pairs on that memory) were never
  judged, so how many closures they add, right or wrong, is not known.

- **A search can find the facts.** `types` is the list of the kinds a
  search reads, each once: `["memories"]` (the default, the search of
  before), `["facts"]`, or `["memories", "facts"]`. The facts are the ones
  true now that `GET /v1/facts` lists, those written with `POST /v1/facts`
  included, which no search found before (they have no memory); a fact
  still waiting for its vector is listed there and found by a search once
  its vector is computed. Each fact is ranked by the cosine of its own
  vector, the one the context block ranks its facts by, and found behind
  the same walls as a memory: the key's project, a confined key's end
  users, the `user_id` and the `agent_id` asked, and the facts only work
  sessions state left out with `exclude_sessions`. A fact that is no longer
  true is never found. With both kinds each is ranked by its own score and
  the two are merged by rank (reciprocal rank fusion, k = 60: the first
  memory, the first fact, the second memory...), not by score: how a
  fact's cosine compares with a memory's for the same question was not
  measured, and the scores of the two kinds are not to be compared. At
  equal rank a memory comes first, whatever order the list names them in,
  so with `limit: 1` the best memory is the answer whenever there is one,
  however close the best fact is. Whether facts in the results help an
  agent answer was not measured: on the store of an agent team's own
  memory (6 Nov 2026), asked eight times "is there a decision about X?", a
  search of the memories found the decision 2 times and the context block
  8, and that is why the facts can be asked for; a search with the facts
  was not run on it.
- **A floor keeps its promise.** `min_score` floors the memories, and the
  facts have no floor of their own (no value was measured for them): a
  search with the facts and a `min_score` is a 422, so a question with
  nothing close still gets an empty list, never the nearest facts.
- **A search can return the whole text.** `full_text: true` adds each
  result's whole text as `content`: the memory as it is stored (up to
  16,000 characters), or the fact's line. Until now a result showed the
  first 280 characters (`snippet`, which stays), and the text past them
  needed a `GET /v1/memories/{id}` per result. The search itself does not
  change: a long memory is still found by what its beginning says, its
  vector being computed from its first 8,000 characters, of which the
  local model reads at most 2,048 tokens, fewer characters in Chinese or
  Japanese than in English (see [Install](docs/guide/install.md), step 7).
- **`korely_search` with `full_text=true` bounds its whole texts.** The
  whole texts of one answer take at most 8,000 tokens as they are printed
  (their indented lines included), as the largest block
  `korely_get_context` gives, counted as the block counts them (4 UTF-8
  bytes a token). Past them a result shows what is left of those tokens,
  and never less than its first 280 characters, as the answer without
  `full_text` shows them; its line says `[cut]`, and a NOTE closes the
  answer with the number of results marked. So the bound is not a hard
  ceiling of the answer: an answer holds at most those 8,000 tokens, plus
  what the same answer without `full_text` holds, plus its `[cut]` marks
  and its NOTE. Without the bound, 50 memories of 16,000 characters were
  one answer of 802,449 characters. REST's `content` is not bounded: a page
  of 50 whole memories can be large.
- **On the MCP, a text's own line breaks no longer begin a line that reads
  as another result.** In the answers of `korely_search`, `korely_get_facts`
  and `korely_add` (its extracted facts), every line of a memory's or a
  fact's text after its first is indented by four spaces, with or without
  the new arguments. Why: these answers print one item a line, and a
  memory or a fact anyone with a key can write could add a line that looks
  like a result of its own ("[0.999] the Cloud · has · ...", "- ...", a
  NOTE), which an agent reading the answer takes for something the store
  holds. What changes for a reader of the MCP's output: only the texts with
  a line break, whose next lines now begin with four spaces. A line break
  is what Python's `str.splitlines` breaks a line on, each of these: line
  feed (LF), carriage return (CR, and CR LF as one), line tabulation (VT),
  form feed (FF), the file, group and record separators (FS, GS, RS), next
  line (NEL), and Unicode's line and paragraph separators (LS, PS). A text
  that holds FS, GS or RS is printed differently from 1.0.12 too, although
  most readers show no break there. In `korely_search`, a result whose
  text has a line break ends the line that begins with its score with its
  id, and its text's next lines follow, indented: a result's id is always
  the last one on its score line, so a first line that ends with another
  memory's id is followed by its own, and an agent that acts on "the id of
  this result" acts on the right one. In 1.0.12 the id came after the
  text's last line. Every other answer is 1.0.12's, byte for byte, a text
  on one line included: `[score] text  (id)`. REST answers JSON and does
  not change. The context block (`GET /v1/context`, `korely_get_context`)
  is not changed by this release and prints a memory's line breaks as they
  are.

### API

- `POST /v1/memories/search`, new in the body: `types`, a list of
  `memories` and `facts`, each once, at least one, `["memories"]` by
  default (a kind named twice, an empty list or another word is a 422);
  `full_text`, a boolean, false by default. With `facts` in the list,
  `run_id`, `metadata` and `min_score` are a 422 (`invalid_request`,
  naming the field): they narrow the memories only (a fact has no
  metadata, no read of the facts is narrowed by a run, and a fact's cosine
  is not on a memory's scale).
- A result gains, only when asked: `type` (`memory` or `fact`), in every
  result when the request names `types`; `content`, with `full_text`; and,
  for a fact, `fact`, the fact as `GET /v1/facts` shows it. A fact's `id`
  is its `fct_` id, its `snippet` the first 280 characters of its line
  (`subject · predicate · object`, then its dates), its `metadata` `{}`.
  Every field of a result now has its description in `/docs`, and so does
  a fact's `confidence`.
- `korely_search` takes `types` and `full_text` too, `types` as one text
  (a tool's argument is one value): `memories` (the default), `facts`, or
  `memories,facts`, read by REST's rules (the parts stripped of their
  spaces, each kind once, the order not mattering); the same refusals (it
  takes no `run_id` and no `metadata`, as before). A fact's line ends with
  its `fct_` id, which `korely_correct_fact` takes.
- The trail of a search (`GET /v1/audit`) lists the facts it showed
  (`read.facts`, and their count in `meta.facts_read`) when the facts were
  asked for, and the memories (`read.memories`) when they were, as before.
- One search, whatever its `types`, computes one vector of the question and
  counts as one read.

### Security

- No change to who can read or close what: the candidates of the judge
  are still the account's, the project's and the end user's (here, the
  project's memory with no end user); this release only narrows them.

- The facts a search returns are read behind the walls the memories are:
  the account, the key's project, a confined key's end users (twice, as
  for the memories: in the read's own end user and in the key's list), the
  `user_id` and the `agent_id` asked. The leak matrix
  (`tests/test_the_end_user_wall_leaks_nothing.py`) asks the four new ways
  in (the facts alone, and both kinds with `full_text`, on REST and on the
  MCP) with every key and every end user: no place a key may not read
  shows, and a `user_id` outside a confined key's list is refused as
  before.
- `full_text` gives a key the whole text of a memory it could already read
  whole with `GET /v1/memories/{id}`, nothing more. The trail keeps the ids
  of the facts shown, never their text or the question.

### Tools in the repository (nothing to do for an install)

- **Two tests that failed now and then, for causes outside what they
  test.** Each failed once in the gates of 8 Nov 2026 and passed alone.
  - `test_oversized_metadata_is_weighed_before_it_is_walked`
    (`tests/test_every_input_by_one_rule.py`) counts the calls Python makes
    with `sys.setprofile`, which counts every call on the thread: a dict of
    100,000 keys makes the encoder start 130 young collections, 11 middle
    ones and a full one inside the counted call, and they ran the
    finalizers of whatever earlier tests had left (257 calls counted for
    21). The counter now collects first and turns the collector off while
    it counts, as the standard library's `timeit` does while it times: with
    200 objects with finalizers left before the call it counted 1,621
    before and 21 now, with 2,000 older ones 16,021 before and 21 now. The
    Cloud's line has had the same change since 7 Nov.
  - `test_every_downgrade_gives_back_the_schema_before_it`
    (`tests/test_migrations.py`): on a Mac, Docker Desktop's file sharing
    makes `os.getcwd()` fail for a moment (1 to 210 ms, median 36, in a
    probe of 8 Nov 2026) in a process whose working directory is the shared
    checkout, while any container on the machine starts or stops, whatever
    it mounts. An alembic process calls it five times as it starts, all
    before its first migration, and the file starts some 170 of them, 133 in
    that test. `make test`, `make test-all`, `make migrate-check` and the
    public copy's check now run from a copy of the checkout made inside
    their container (`scripts/from_a_copy.sh`), with the checkout mounted
    read-only: no process of a run has its working directory in the shared
    folder, the copy included. The copy leaves out `.git` (no test reads it,
    and the test image has no git), `.claude`, `node_modules`,
    `dashboard/dist` and the caches, and keeps files git does not track; it
    is 22 MB, about 890 files, and took 1.1 to 7.5 s (median 3.6) in ten
    copies on a busy Mac. Beside a container started every second, the file
    had failed in 2 runs of 2 from the checkout; from the copy, with no
    second try, it ran whole once with no lost working directory (155 starts
    beside it; its 15 tests passed, and one teardown failed when the test
    Postgres crashed under a load of 100) and was cut by Docker Desktop
    stopping in its third test of a second run (114 starts). Until more runs
    show the copy alone, every alembic process of the file still goes
    through one runner that runs it once more when it died of that error and
    of no other, with a warning in the run's summary that says so; a second
    loss, or any other failure, fails as before. The bytecode the suite
    wrote into the checkout, which the Makefile named on 7 Nov as the cause,
    was not one: in the probe, writes into the checkout made no failure on
    their own (the comment is corrected; `make test-all` still writes no
    bytecode, not even into its copy).
  - Still exposed to the same moment: a command run by hand with the
    checkout mounted as its working folder (`docker run -v "$PWD":/w -w /w
    ...`); run it through `scripts/from_a_copy.sh` as the Makefile does. And
    a file mounted over a path of the shared checkout (`-v file:/w/path`) is
    detached when another container starts, and the container reads the
    file under it again: mount a whole tree instead. The CI runs on Linux,
    where Docker mounts the checkout without Desktop's file sharing; it was
    not measured there.

## 1.0.12

### Before and after upgrading

- No migration and no new setting, on an install of your own and on the
  Cloud. What changes in the API (below): seven `/admin` reads take an
  optional `project_id`, an empty `project_id` is a 422 in a query and in a
  body, one route is new (`GET /admin/memories/count`), and `GET /v1/audit`
  no longer shows a key the operator's name (`meta.operator`). What else
  changes is what the store does with a memory and a fact: how an agent's
  memory with no `user_id` is read (memory_core 6.4.0), and which earlier
  facts a fact written directly or a correction can close, and which facts
  written directly a fact read from a memory can close (memory_core 6.5.0
  and 6.6.0). Nothing is redone by the upgrade itself: the facts already
  written, and those already closed, stay as they are.
- **A script that sends `"project_id": ""` to mean no project** to `POST
  /admin/keys`, a key's move or `POST /admin/aliases` gets a 422 after the
  upgrade: leave the field out, or send `null`, which does what the empty
  one did. On `POST /admin/webhooks` and the OAuth consent an empty one was
  already refused (404, now 422).
- **The dashboard opens on one project.** After the upgrade its data pages
  show the project chosen in the switcher at the top left, the Default
  project if you never chose one. To see the whole account as before,
  choose **All projects**, first in the switcher's list.
- **An agent's memories written with an `agent_id` and no `user_id` are
  read in a new way (memory_core 6.4.0, below).** Nothing to do before the
  upgrade. The facts already written stay as they are: nothing is
  extracted again by the upgrade itself. Such a memory written or edited
  after it is read the new way, and so is one already written whose
  extraction runs after it: a memory still in the queue when you upgrade
  (`WRITE_ASYNC`, or a region whose writer was resting), one in `error`
  that `manage reextract` queues again, one that `manage reextract --empty`
  queues again because it gave no fact. A memory with a `user_id`, and one
  with neither a `user_id` nor an `agent_id`, is read exactly as in 1.0.11.
- **If the memories you write with an `agent_id` and no `user_id` are about
  one person** (an assistant's notes on the person it works for, such as
  `add("User prefers TypeScript", agent_id="coding-assistant")`), pass that
  person's `user_id` with them: from this release a memory with an
  `agent_id` and no `user_id` is read as the agent's own, not as a person's.
- If your agents write facts about a person by a name (`POST /v1/facts`
  with `subject: "Maria"`) while the memories of that person are stored
  under their `user_id`, declare the name before or after upgrading
  (`POST /v1/users/{id}/names` with the `agent_id` your agents write with):
  from 1.0.12 a direct fact about "Maria" and a fact about the `user_id`
  (written directly or read from a memory) close each other only when the
  name is declared in the same agent namespace.
- Declaring a name after upgrading makes the facts about that name read as
  the end user's from then on; it does not judge again the facts already
  written: two values written about `Maria` and about the `user_id` before
  the declaration stay true side by side until a new value is written
  (stating one of the two again only confirms it).
- An account declares at most 1,000 names (`POST /v1/users/{id}/names`,
  the dashboard's aliases): a customer with more end users than that cannot
  declare one name per end user. And a `user_id` that is not a name (one
  that starts with a digit, one with `@`, `:` or `|` in it) cannot have a
  name declared for it: the call answers 422.

### What the store answers differently

- **An agent's memory with no end user is told so (memory_core 6.4.0).** Up
  to 1.0.11 the extraction of a memory an agent wrote without a `user_id` (a
  team's shared memory, an agent's own notes) was asked the prompts of an
  end user's memory, for an end user named "user". The model wrote what
  they asked: a procedure came back as `user follows_procedure deploy`, and
  three rules of one charter as `user has_delegation_level GREEN`, then
  `YELLOW`, then `RED`, one subject and one relation, so the judge closed
  the first two. On the 58 memories of our own team of agents (each with
  its `agent_id`, none with a `user_id`, written on the Cloud on 6 November
  2026 and read by hand on the 7th), 48 of 168 facts had `user` as their
  subject (46) or object (2) and none of the 48 was right; 74 of 168 were
  right. From this release, for a memory with an `agent_id` and no
  `user_id` (the case Mem0 reads as the agent's own, `agent_id` and not
  `user_id`):
  - the prompt says the memory belongs to no end user, and the subject of a
    fact is what the statement is about, as the text names it; a statement
    made impersonally (a procedure, a rule, a decision) is about the thing it
    describes;
  - the first person is the agent that wrote the memory, its `agent_id`;
  - the memory's `observation` (the sentence `/v1/context` shows for it) is
    asked to say what the memory states with its own subject, never "The
    user ...", whichever writer is in force;
  - a procedure, a rule in force or a habit is asked as `current`, and so is
    the lasting result of something done (an approval, a revocation), from
    the day the text gives; `past` only when the text says the state ended.
    Up to 1.0.11 38 of the 168 facts above came out `past`, each closed from
    its birth, an approval and a key's revocation among them;
  - a fact whose subject or object is `user` is kept only when the text
    itself has the word `user` (in any letter case) as a whole word:
    not inside `user_id`, `users` or `username`. Every other name of the
    fact is checked against the text as before.
- **A memory with a `user_id`, or with neither id, is asked exactly what
  1.0.11 asked.** With a `user_id`, `user` stands for that person as before,
  whether or not the write names an `agent_id`. With neither id, as when one
  person uses the MCP or an SDK without naming anybody, the memory is still
  read as the memory of an end user named "user": "I prefer the dark theme"
  is still a preference of `user`. Every extraction prompt of both kinds,
  with every combination of the writers' rules, is the same byte for byte:
  a test fingerprints them against 1.0.11
  (`tests/test_prompts_with_an_end_user_unchanged.py`). So the memories of a
  team written without an `agent_id` keep the reading of 1.0.11, and its
  `user` facts: the new reading needs the `agent_id`.
- **Not measured with a real model yet.** The tests run this product's write
  with a scripted model and a scripted judge: the charter's three levels,
  written with an `agent_id` and no `user_id`, no longer close one another,
  and a `user` the text does not name is no subject. How often a real
  writer now gets the subject and the tense right is a measure whose
  criteria are written before it runs; it has not run for this entry. Until
  it has, this is a change in what the model is asked, not a measured gain.
- **A fact written directly no longer closes facts about other subjects.**
  Up to 1.0.11 a fact written with `POST /v1/facts` (`korely add-fact`, the
  Python SDK's `add_fact_triple`) or a correction (`PATCH /v1/facts/{id}`,
  `korely_correct_fact`) was weighed by the contradiction judge against up
  to ten earlier facts near it by meaning, of any subject, and the judge's
  question tells it that a subject phrased differently can be the same
  entity: "deploy Cloud 1.0.8 happened on 5 November" closed "deploy Cloud
  1.0.7 happened on 2 November". On the store of our own team of agents
  (586 facts: 418 written directly, 168 read from its 58 memories; read on
  6 November 2026) 119 direct facts had been closed, and 108 of the 111 closures between two direct
  facts had two different subjects. Now the judge of a direct fact, or of a
  correction whose subject is yours (below), is shown only the earlier
  facts about the same subject, as the
  store names it after its aliases (case aside, a declared name and a
  rename included), and the facts about the same two things the other way
  round ("Salesforce serves_as_crm_for Globex" against "Globex dropped
  Salesforce"). The store compares two subjects with the database's
  `lower()`, as it does to recognise a fact already true: `Anna` and `anna`
  are one subject; `Anna Rossi` and `Anna`, `the user` and `user`, `Straße`
  and `STRASSE`, a name with two spaces and the same with one are two,
  until a name is declared. The late judgement of a vector uses the same
  comparison. A change of the same subject still closes the old value,
  told by any agent of the same end user; a relation of another name about
  the same subject is still found by meaning. The same holds when the
  vector of a direct fact is computed late, by the vectors drain.
  `invalidated` in the answer of `POST /v1/facts` and `PATCH /v1/facts/{id}`
  lists fewer ids when the old answer named facts of another subject.
  Replayed on the recorded closures of the store above, the closures of a
  direct fact go from 119 to 6, and those of an extracted fact from 12 to
  11. What the replay does not see: it keeps the judge's old verdicts and
  counts which of them the new candidates still allow, not the pairs the
  places freed in the judge's list of ten now bring before it; and it
  tells a direct fact by its empty source memory, because the facts' read
  shape does not say who wrote a fact, so a correction that names the
  subject of an extracted fact counts there as extracted while the engine
  treats it as yours.
- **A fact read from a memory no longer closes a fact written directly
  about another subject.** The same rule from the other side: a fact
  written directly is shown to the judge of a fact the extraction read
  only when the two are about the same subject (aliases applied) or about
  the same two things the other way round. On the store above, "the user
  uses Docker", read from a memory, had closed "a reviewer agent works
  read-only, without Docker", written directly; 3 of the 8 closures of a
  direct fact by an extracted one were of this kind. The price is the one
  above: a direct fact about "Maria" and a memory of the end user `u-42`
  meet only when the name is declared.
- **A correction is about the subject you name.** A correction that gives
  `subject`, or corrects a fact you wrote (`POST /v1/facts`, an earlier
  correction), is weighed against that subject's facts only. A correction
  that gives only `predicate` or `object` of a fact read from a memory
  keeps that fact's subject as the extraction wrote it, and is weighed as
  an extracted fact: every stage, every name the extraction gave the
  entity, as every correction was up to 1.0.11; its inherited relation is
  read as the extraction's words are. So the other copies of a wrong value
  under the end user's id can still be closed by it, and a later value the
  extraction reads meets it. Give `subject` to make the correction yours.
- **Two facts read from memories keep every stage**, across the names the
  extraction gives one entity. What changes for them is who fills the ten
  places of the semantic stage and of the same-relation stage: a fact
  written directly about another subject no longer takes one, so the judge
  of an extracted fact can be shown an extracted fact it was not shown
  before. On the store above all 9 closures between two extracted facts
  were about one subject and stay.
- What is not measured in the judge's change: it was tested with a scripted
  judge, which says yes to the subjects a test names. No model was run on it, so how
  many right closures a real judge loses with it on a mixed store is not
  measured; of the 17 closures it keeps on that store, 9 are between two
  extracted facts, 4 look right on reading and 3 or 4 look wrong: the ones
  only the judge can tell apart.

### The dashboard

- **Every data page shows the project chosen at the top.** Until 1.0.11 the
  switcher named a project while Memory Graph, Memories, Facts, Agents,
  Requests, the Audit log and the Home page's numbers read every project of
  the account: with a project of 9 facts chosen, the Memory Graph drew the
  account's 2,808, after a reload too (6 Nov 2026). They now read the chosen
  project as its API keys read it, and a line under each page's title names
  it. Choosing another project reads the page again in it, filters and
  selection reset; the choice is kept when the page is reloaded. The
  Playground already searched the chosen project.
- **All projects** is a choice of its own, first in the switcher: every
  project of the account together, as every page showed before, and the
  switcher's button says "All projects" while it is chosen. The choice is
  kept per account in the browser, so another account signed in on it
  starts on its own Default project. The Audit log's events that belong to
  no project are listed only under All projects: sign-ins and sign-outs,
  the model settings, a webhook for every project, an erasure of an end
  user in every project, a project's deletion, and the reads made with All
  projects chosen.
- **What stays the account's says so.** A model call is not counted by
  project, so the model's calls (the Home page's card, the sidebar, the
  Usage page) are the whole account's, and on the Cloud so are the plan's
  writes (the Home page's card, the sidebar). Those cards and lines say
  "account".
- Measured on a local install with a Default project left empty, a project
  of 2 memories and 3 facts and one of 3 memories and 6 facts: the Memory
  Graph and Facts show 3, 6 and 9 facts (All projects), Memories 2, 3 and 5,
  switching on the page and after a reload, with no error in the browser.
  Not looked at in a browser: the Cloud's build, a phone's width.
- The Home page's "Stored memories" is a count (`GET /admin/memories/count`):
  it read one memory to learn the total, and filed a read of it at every
  visit.
- What each read of the screen sends is run with node, in the repository:
  `npm test` in `dashboard/` (also in the CI) loads the API module as the
  build compiles it and checks the `project_id` each read sends, and the
  switcher's choice where a page must read nothing yet.

### API

- `GET /admin/memories`, `/admin/memories/categories`, `/admin/namespaces`,
  `/admin/requests`, `/admin/requests/summary`, `/admin/requests/daily` and
  `/admin/audit` take `project_id`, as `/admin/facts` and
  `/admin/memories/search` already did: one project of the account, read
  as its keys read it; left out, the whole account, as before; 404 for a
  project that is not the account's. `/admin/usage` does not: a model
  call is not counted by project. An empty `project_id=` is a 422: it was
  read as no project, the whole account (on `/admin/facts` and
  `/admin/memories/search` too).
- **An empty `project_id` in a body is a 422 too**, by the same rule:
  `POST /admin/keys`, `POST /admin/keys/{key_id}/move`, `POST
  /admin/aliases`, `POST /admin/webhooks` and the OAuth consent. A new key
  with one went to the Default project, a move with one took the key out of
  its project, an alias went to the key's own project; the webhook and the
  consent answered 404. Left out (or null), each does what it did.
- New: `GET /admin/memories/count` (`project_id` as above), how many
  memories there are, without reading one: nothing goes in the trail.
- **A read of one project is filed in that project's trail**, as the read
  of one memory and the Playground's facts already were: `GET /v1/audit`
  with a key of the project now also lists the dashboard's reads of it
  (`admin:memories:list` and the others, by id), where before a read of the
  whole account went under no project. A list is filed with an end user
  only when it was asked for one (`user_id`): the Memories page of a project
  with several end users is filed with none, so `GET /v1/audit?user_id=`
  does not find it.
- **`GET /v1/audit` shows no operator's name.** A row of the dashboard's
  carried the name the operator signs in with (`meta.operator`, often an
  email), which every key of the project could read. It is no longer in
  the answer to a key; it stays in the trail, and `GET /admin/audit` (the
  dashboard's Audit log) shows it.
- **The dashboard's acts are filed where they act**, with the project and
  the end user of the row they touch: a fact forgotten or corrected, a key
  revoked, a webhook made, deleted or enabled, an alias made or deleted,
  and the ids of an event read in the Audit log (one row per project and
  end user). They were filed under no project and, for a fact, an alias
  or the ids read, no end user: the project's trail did not list them, and
  the trail asked for that end user (`?user_id=`, an access request) did
  not find them. A webhook for every project stays the account's, under no
  project. A webhook's test delivery, which filed nothing, is filed too, in
  the endpoint's project, with whether it was delivered and the status.
- On the Cloud, `PATCH /admin/facts/{id}` looks for the fact before the
  month's write quota, as `PATCH /v1/facts` does: a fact that is not the
  account's is a 404 where, past the quota, it was a 429.
- **`/health` stops blaming a writer that is no longer the one saved.** A
  call refused for payment (a key out of credit) left `extraction` on
  `failing` and `status` on `degraded` after you saved new writers on the
  Models page, with both regions `configured`, until the next call to the
  model answered (7 Nov 2026). The last call is now kept with the writer
  that made it, as the drain's rest after a refusal is, and `/health`
  forgets it when it sees that writer replaced or removed: another key or
  another model saved in its place on the Models page, in place of the
  writer of `.env` too (the European preset, `EXTRACTION_PROVIDER=mistral`,
  as well as Gemini); and, when the writer is the Gemini one of `.env`, a
  new Google key saved on the key screen. `extraction` then reads `unknown`
  until the new writer is called, or `working` when the store holds an
  extraction, of whatever age (`seconds_ago` says how old), or `not
  configured` when no writer is left. A success of the replaced writer is
  forgotten the same way. A failure of the writer still saved keeps
  `failing` and `degraded`, whatever another region's writer does; saving
  the same writer again changes nothing. If a setting the writer is built
  from cannot be read (the writers of the Models page, or the Google key of
  the Gemini writer of `.env`), the record stands.
- **A refusal of a writer saved over no longer rests the new one.** The drain
  keeps one client for a whole memory; a 402 or 401 it received after a new
  key was saved was put on the new writer's region, and the drain rested a
  writer that had refused nothing. Both /health and the drain now name the
  writer that made the call, and tell by one rule whether it is still the
  one in force. While the Google key saved on the key screen cannot be
  read, the drain no longer takes the key of `.env` (what that read falls
  back to) as the one in force: the rest of the Gemini writer of `.env`
  stands until the key can be read, as it already did while the writers of
  the Models page could not be read. The memory waits in the queue as
  before. Nothing to do.
- **A writer built while the database could not be read is kept ten
  seconds, no longer for good.** A blip of the database at the first model
  call after a save (or after another process saved) left the writer of
  `.env` writing until the next save or a restart, and the writer saved on
  the Models page was not used; with the Gemini writer of `.env`, the same
  blip kept the key of `.env` in place of the one saved on the key screen.
  Now the writer built from what `.env` says is kept ten seconds (the
  interval at which a process checks for another one's save), and then the
  settings are read again: the writer is built, and its failed reads
  paid, once in those ten seconds instead of at every call, and once the
  database answers the writer saved writes the calls that follow, at most
  ten seconds later. Nothing to do.

### Security

- **The end-user wall is written once.** The filter that keeps a read to
  the end users it may see (the `user_id` asked for, a confined key's
  `users` list, or the memories with no end user) was written out in
  several places: the memories' search and listing, the facts, the context
  block's facts, the trail, the listings' counts, the Markdown export and
  two operator listings. The copies did not all read "no end user" the
  same way, and a change to the rule had to be made in each of them. They
  now go through one function (`services/user_scope.within_end_users`).
  What each read returns is the same as in 1.0.11.
- **A few reads still filter on an end user their own way**, for a reason
  of their own, and are listed where the wall is
  (`services/user_scope.py`): the trail asked for one person (the id and
  the pseudonyms an erasure left of it), a fact's names read in the fact's
  own memory (the `entity` and `subject` filters of the facts), the names
  already declared for an end user, and the lists of the end users that
  hold rows (`GET /v1/users`, the Markdown export). None of them shows
  anything of another end user; a shared level will have to change them
  with the wall.
- **New tests hold it.** A leak test asks every `/v1` route that reads
  memories, facts, end users, agents or the trail, every one that reads or
  writes a row by its id (memories, facts, batch jobs), every one that
  writes for an end user or an agent (a memory, a fact, a batch, a name, an
  erasure, an agent's deletion), the six MCP tools and six operator
  listings, with whole-project keys, keys confined to some end users (one
  by a prefix that matches an erased person's pseudonym), and keys of
  another project and another account, over a store where the same
  sentence sits in every place a key may not read. It never comes back;
  another place's row, read or written by its id, is a 404; a door that
  writes leaves every place the key may not read as it was (its memories,
  facts, names, object links and vocabulary); a `user_id` outside a
  confined key's list is a 403; and every answer, in its order and with its
  counts, equals the one 1.0.11 gave. This test, which reads the data each
  door gives back, is what holds the wall.
- Beside it, a second test watches the SQL those same requests send: a
  condition on an end-user column built outside the one function fails,
  unless it is listed by its exact condition, where it is built and how
  many times one statement carries it, with the reason (an erasure works on
  the end user it erases, a write on the memory in hand). It sees the
  common ways of writing such a condition and the ones its own control
  tries, not every way SQL allows. And the test that checks that every
  `/v1` route and MCP tool asks the key's end users now reads the routes
  the app serves, so a new module with `/v1` routes is checked without
  being added to a list.

### Tools in the repository (nothing to do for an install)

- **The test suite can run in several processes**:
  `make test-all TEST_DB=korely_test_mine WORKERS=4` (pytest-xdist 3.8.0,
  in the development lock only; the product image does not change). Each
  process works on a database of its own next to `TEST_DB`
  (`korely_test_mine_gw0`, `_gw1`, ...), which the suite makes when it is
  missing and drops at the end, and the tests are handed out one at a
  time; then the tests marked `serial` run in one process on `TEST_DB` (no
  test is marked today: none failed in parallel once the causes below were
  removed). A test marked `serial` is an error in a parallel process,
  never a pass and never a skip. `WORKERS` is read from make's command
  line only: without it, even with a `WORKERS` in the environment, the
  suite runs in one process, as `scripts/release.sh` and the CI run it. A
  Ctrl-C, or `docker stop`, stops a parallel run: the serial pass does not
  start, and the databases of the workers are dropped, by each worker or,
  for one stopped halfway, by the run itself at its end. A `TEST_DB` named
  like a worker's database (`..._gw<n>`) is refused, by `make test-db` and
  by the suite run by hand: a parallel run of the shorter name would take
  it for its worker's and drop it. `make test-all` writes no bytecode
  into the checkout (`PYTHONDONTWRITEBYTECODE`, as the public copy's check
  runs the suite): the processes of a parallel run writing it there at
  once made another one lose its working directory, 2 runs of 3 on Docker
  Desktop, 0 of 3 without.
  Measured on the development Mac (Docker with 10 CPUs and 10 GB, other
  sessions' suites on the same machine and the same Postgres): in one
  process 880 s; in 4 processes 392 s with at most one other suite
  running, and 657 s with two; in 6 processes 575 s with at most one
  other. Every run that finished gave the same 6003 passed and 7 skipped,
  test by test. The time depends mostly on what else the machine runs, and
  6 processes were not faster than 4 here.
- **Two dependencies on the order of the tests, found by the first runs
  in several processes and removed in the tests.** The application's
  database engine, built once per process, stayed on whatever
  `DATABASE_URL` a test had set (`tests/test_blind_install.py` points it
  at a server that is not there), and every later test of that process
  that reached the database was refused: 54 failures in four processes.
  And two parametrized tests had a random id and an instant in their
  names, which differ in every process, so pytest-xdist refused to start.
  The suite in one process passed only because of the alphabetical order
  of its files. Now an engine (or sessionmaker) of `db/base.py` on a
  database other than the run's is closed and forgotten before and after
  each test, and the two ids are names.
- **Every test starts with these records as a new process has them**: the
  model's and the vectors' last answers (`core/llm._last_call`,
  `_last_embedding`), the local embedder's (`core/local_embedder._last`),
  the worker's jobs and its export (`worker._job_state`,
  `_export_state`), the drain's rests (`services/facts/drain._pauses`)
  and the bookkeeping's ledgers. Not the others a process keeps, among
  them the Cloud's spending alerts already sent and its cache
  (`hosted/spending`) and the exports still owed (`services/export_md`).
  A run in four processes also showed that `/health` still says
  "degraded" after new writers are saved, when the model's last answer was
  a failure (`reset_provider_cache` does not clear it): that is a defect
  of the product, not of the tests, fixed in this release (API, "`/health`
  stops blaming a writer that is no longer the one saved").
- **The test Postgres allows 300 connections** (and 256MB of
  `shared_buffers`), set by `make db-up` with `ALTER SYSTEM`
  (`dev/test_db_settings.sh`): one process of the suite keeps about a
  dozen connections open, and Postgres's default of 100 does not hold a
  run in several processes beside the others on one machine. Not in the
  compose file, because every checkout shares the one container and a
  compose file that differs between two of them recreates it in the
  middle of each other's runs. The settings take effect at the server's
  next start, and `make` never restarts it: it prints the command, for
  when no suite on the machine is using it (a restart cut the runs under
  way). A worker of a parallel run closes the connections of the
  application's engines before it drops its database, so `DROP DATABASE
  ... WITH (FORCE)`, which ends only the sessions of that database and
  logs "terminating connection due to administrator command" for each,
  ends at most what a test left open.

## 1.0.11

### Before and after upgrading

- On an install of your own, nothing to do and nothing changes: no
  migration, no new setting, no API answer of a different shape.

### Security

- On an install of your own, the webhooks' address check is the same: it
  gains a hook (`webhook_destination_problem`, core/extensions.py) that
  only the Cloud fills, and that does nothing here.

## 1.0.10

### Before and after upgrading

- On an install of your own, nothing to do: no migration, no new setting,
  and nothing in the API changes shape. A client that reads the 401's words
  instead of its code `invalid_key` reads new words, and a key revoked while
  its request waits is refused where the request writes (both below).

### What the store answers differently

- **The 401 for a key that does not authenticate names expiry.** Up to
  1.0.9 every `/v1` and `/admin` route answered an unknown, a revoked and
  an expired key alike with "Invalid or missing API key: no live key
  matches it (a revoked key stops at once); your keys are at …/keys", so a
  key past its date was told of a revocation nobody made. Now the same
  three get "Invalid or missing API key: no live key matches it (unknown,
  revoked or expired); your keys are at …/keys". They still get one answer,
  and the code is still `invalid_key`: branch on the code, not on the
  words. The MCP door (`/mcp`, `/agent/mcp` on the Cloud) is not one of
  them: it still answers its library's own 401, `invalid_token`
  "Authentication required".
- **A request that waits before it writes reads its key again.**
  `POST /admin/webhooks` and `POST /admin/webhooks/{id}/enable` look the
  webhook's address up (up to 5 seconds) before they write, and
  `POST /admin/keys` makes the key after its other checks. Up to 1.0.9 they
  wrote with the key as it was when the request came in, so a key revoked
  during the lookup still made or switched on the webhook. Now each reads
  the key again in the transaction that writes: revoked or expired
  meanwhile, 401 `invalid_key` with the words above; without `admin`
  meanwhile, 403 `forbidden`. A request made with a dashboard session is
  not read again.

### Security

- The new words of the 401 change nothing about which key is let in: an
  unknown, a revoked and an expired key still get one answer and one code,
  so a caller cannot tell the three apart.
- A key revoked while its request waited no longer writes with the rights
  it had: the three routes above read its `admin` again where they write,
  under a share lock of the account's row.

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
