# What is in the box: facts, audit trail, erasure

- **Typed bi-temporal facts.** Subject, predicate, object, plus `valid_from` and
  `invalid_at`. Point-in-time queries via `as_of`, and what the store knew on a
  date via `known_as_of`. Every superseded fact keeps a pointer to the fact that
  superseded it.
- **Two-stage contradiction detection.** A SQL prefilter, then a model asked to
  judge. That a relation of this end user's vocabulary holds one value at a
  time is marked so for the judge, next to both values; nothing is closed on the shape of the
  triple alone (see [Honest limits](limits.md#honest-limits) for what that learning gets
  wrong).
- **An audit trail that never stores content.** Who touched which end user's
  data, when, with what result and what counts. This is the shape of an access
  request. Each event names the key that made the call (`api_key_id`, the
  id the dashboard's Keys page lists, never the key itself; none for an
  operator signed in to the dashboard) and what was done: a memory
  forgotten (out of every read, its text kept until an erasure) is
  `forget`, a memory or a person erased is `erase`, a fact closed with
  `/forget` is `fact_invalidate` (since 1.0.23; before, all three were
  `erase`: the forgets among those rows are the `memories:delete` ones
  without `meta.erase`, the `facts:forget` ones, and the dashboard's
  deletes of a memory, which have a `mem_` target and neither
  `meta.endpoint` nor `meta.erase`; a refused `memories:delete` without
  `meta.erase` may have been either). The personal data it keeps: the end user's id, which an erasure
  replaces with a pseudonym; the username of the operator behind each action
  taken from the dashboard, the reads of memories and facts included (by
  id, never the text), which the account reads (the dashboard's Audit log,
  `GET /admin/audit`) and a key of a project does not: `GET /v1/audit` lists
  the dashboard's acts and reads in the key's project, by id, without the
  operator's name (a list of memories or facts is filed once per project
  and end user whose rows it showed, since 1.0.23; a search carries an end
  user only when it was asked for one); and the client address of each dashboard sign-in
  (the real one: Caddy writes it; a proxy of yours must too, see
  [Your own proxy in front](operations.md#your-own-proxy-in-front)). All of it for `AUDIT_RETENTION_DAYS`,
  400 days unless you change it, which belongs in your record of processing
  and in what you tell your staff.
- **Which facts an agent read, when it says who it is** (since 1.0.15). A
  read that passes `reader` keeps one row per fact it handed over as true
  (`agent_read_marks`): the fact's id, the reader's name, the end user the
  fact is about and when it was first and last read, never any text. One
  more place where an end user's id is kept: for
  `READ_MARKS_RETENTION_DAYS` after the last read, 90 days unless you
  change it (0 keeps them), which belongs in your record of processing
  next to the audit trail. The rows go with the end user's erasure (every
  erasure door, deleted, not kept under a pseudonym), and so do the rows
  whose reader's name is the end user's id, in the project the erasure is
  made in; they go with their fact, with their project (erased or deleted),
  with the account, and with the deletion of the agent namespace whose name
  is the reader's. A read without `reader` keeps none. A change told to a
  reader never repeats the words of a memory that was forgotten.
- **Erasure that erases.** `DELETE /v1/users/{id}/memories` removes the rows
  in the key's project; the same id in another project of the account stays,
  since there it may be another customer's person, and
  `DELETE /admin/users/{id}/memories` (the account's administrator) erases
  it in every project at once.
  Read back with `include_invalidated=true` and count: the answer is zero.
  One memory alone: `DELETE /v1/memories/{id}` forgets it (it drops out of
  every read and its facts stop being true, the row stays, text and vector
  included), and `DELETE /v1/memories/{id}?erase=true` erases it, text,
  vector and the facts it alone stated, with its item in a `/v1/batch`
  payload, the aliases and vocabulary examples those facts alone taught,
  and every object link it took part in (two names judged one thing, below).
  A correction of one of its facts (`PATCH /v1/facts/{id}`) is made of
  that fact's words and goes too, also when it was corrected again; one
  another memory states since stays, as every fact other memories still
  assert. Since 1.0.27 a forget or an edit of the memory leaves such a
  correction true (up to 1.0.26 they closed it), and only the erasure
  takes it.
  A memory forgotten earlier can be erased the same way, and the
  dashboard's memory drawer has both, Delete (the forget) and Erase.
  With `MEMORY_WINDOWS` on (1.0.33, off by default) a long memory also has
  a vector for each window of its text, derived from its words: an edit
  replaces them, a forget deletes them (the row's own vector stays, as
  above), and every erasure of the memory, of its end user, of an agent's
  namespace, of a project or of the account deletes them with the memory,
  the setting turned off since or not. No copy of the text is kept with
  them.
  A fact you forget by hand (`POST /v1/facts/{id}/forget`), correct or post
  in the past tense stays over when its memory's text is edited, even when
  the new text says it again (since 1.0.27): an edit of the text does not
  undo a forget, and `PATCH /v1/memories/{id}` lists such facts in
  `kept_forgotten`. To make one true again, post it (`POST /v1/facts`). Either
  answer, and that of `DELETE /v1/agents/{id}`, counts in `facts_reopened`
  the facts the deleted memories had closed that are true again, each a new
  row stated by the memories still saying it ([Time](time.md)). What was
  deleted is not read again: a test (`tests/test_deletion_does_not_come_back.py`,
  six ways to delete times eight phrasings, 48 cases) records every prompt a
  scripted writer receives and asserts that, after a deletion, no later prompt
  holds the deleted words and no fact derived from them is left; the writer in
  it is scripted, not a real model. The
  audit row survives, carrying counts and no content. What stays names the end
  user by a keyed pseudonym, never by their id (since 0.1.27): `erased:` and
  32 hex digits, an HMAC of the account and the id under a key derived from
  `SECRET_KEY`. The erasure's record and the rest of the access trail keep
  it, so `GET /v1/audit?user_id=<the id>` still finds them while the database
  alone does not say who it was (an erasure in one project renames that
  project's trail, and the dashboard's account-wide reads of the id once no
  other project holds it: while one does, those reads may be of that person); the cost rows never name an end user at all; a webhook
  already delivered takes the pseudonym, and one not delivered yet goes out
  as it was enqueued, with the id its receiver knows, is marked by the
  erasure, and takes the pseudonym (without `SECRET_KEY`, loses the id)
  once it is delivered, spent or dropped. A pseudonym is still
  personal data, not anonymous: whoever holds the id and the key links them.
  Without `SECRET_KEY` the id goes and nothing replaces it, and the proof is
  the `audit_id` the erasure answered with. The reply counts what went
  as `memories_deleted` and `facts_deleted`. The end user's items in `/v1/batch` payloads, queued or
  finished, are scrubbed in the same transaction, so a queued import cannot
  bring them back. The id is one path segment: a `user_id` or `agent_id` holding a
  `/` is refused on write since 0.1.12, and one stored before that is erased by
  sending it percent-encoded, `acme%2F42`. The same goes for a whole
  project: `DELETE /admin/projects/{id}?erase=true` deletes every memory and
  fact it held, history included, and counts as erasing each of its end
  users: the project's audit row, whose id is the `audit_id` it answers
  with, plus one row per end user under their pseudonym (with no counts of
  its own, so nothing is counted twice), so what stays of them, and what
  the trail writes about them afterwards, names them by that pseudonym
  only; it says `"erasure": "permanent"`. Its API keys move to Default and
  keep working, except keys confined to end users, which are revoked
  (`keys_revoked`): every end user they name is erased with the project, or
  only named there. Without `erase` the project's data moves to the
  default project instead, entity aliases, learned relations and the
  webhook deliveries about it included.
  That is the default because it erases no memory and no fact (the
  project's webhook endpoints go either way, see [Webhooks](webhooks.md)):
  what moved can still be erased in Default, by
  end user, by agent or one memory at a time. It cannot be moved out again,
  or erased as the project it was: nothing marks where it came from. A fact
  the default project already holds, from
  the same end user and agent, merges into that one (`facts_merged`).
  A deletion never changes the region what moves is written in: when the
  Default project writes in another region than the deleted one (or
  follows the install's default writer while the deleted one chose a
  region), and live API keys would move, or, without `erase`, any memory,
  fact or batch job, the deletion is refused with a 409
  `project_region_differs` that names both regions, and nothing is
  deleted. Give the two projects the same region first, or revoke the
  project's keys and erase it. A deletion that goes answers `region`, the
  Default project's, where what moved is written from then on. The dashboard's Projects page offers both, the
  erasure as a second step that names what goes and wants the project's name
  typed. A dump taken before an erasure still holds what it erased:
  [Backups](operations.md#backups) says what to do about a restore.
- **Postgres and pgvector. That is the entire data layer.** No vector database
  to operate, no graph database, no message queue, no Redis.
- **REST and MCP over the same store.** Same keys, same isolation.
