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
  request. The personal data it keeps: the end user's id, which an erasure
  replaces with a pseudonym; the username of the operator behind each action
  taken from the dashboard, the reads of memories and facts included (by
  id, never the text); and the client address of each dashboard sign-in
  (the real one: Caddy writes it; a proxy of yours must too, see
  [Your own proxy in front](operations.md#your-own-proxy-in-front)). All of it for `AUDIT_RETENTION_DAYS`,
  400 days unless you change it, which belongs in your record of processing
  and in what you tell your staff.
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
  A memory forgotten earlier can be erased the same way, and the
  dashboard's memory drawer has both, Delete (the forget) and Erase. Either
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
  the same end user and agent, merges into that one (`facts_merged`). The dashboard's Projects page offers both, the
  erasure as a second step that names what goes and wants the project's name
  typed. A dump taken before an erasure still holds what it erased:
  [Backups](operations.md#backups) says what to do about a restore.
- **Postgres and pgvector. That is the entire data layer.** No vector database
  to operate, no graph database, no message queue, no Redis.
- **REST and MCP over the same store.** Same keys, same isolation.
