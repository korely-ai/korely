# Time: what was true, and what the store knew

## The question

A support agent tells customer 4821 they have priority support. That is March.
In June they move to a cheaper plan. Today they are angry, quoting your bot back
at you. Was the bot wrong, or right at the time?

Two writes, months apart:

```bash
curl -X POST https://your-domain/v1/memories \
  -H "Authorization: Bearer kor_self_..." -H "Content-Type: application/json" \
  -d '{"content":"Customer 4821 is on the Priority Support plan.","user_id":"c4821","timestamp":"2026-03-15"}'

curl -X POST https://your-domain/v1/memories \
  -H "Authorization: Bearer kor_self_..." -H "Content-Type: application/json" \
  -d '{"content":"Customer 4821 moved to the Basic plan.","user_id":"c4821","timestamp":"2026-06-20"}'
```

What the store holds now, closed facts included:

```bash
curl "https://your-domain/v1/facts?user_id=c4821&include_invalidated=true" \
  -H "Authorization: Bearer kor_self_..."
```

```
c4821 -subscribes_to-> Basic plan             from 2026-06-20
c4821 -subscribes_to-> Priority Support plan  from 2026-03-15, invalidated 2026-06-20
```

What it held on the day of the conversation:

```bash
curl "https://your-domain/v1/facts?user_id=c4821&as_of=2026-04-01" \
  -H "Authorization: Bearer kor_self_..."
```

```
c4821 -subscribes_to-> Priority Support plan
```

The bot was right at the time, and that is a row rather than a reconstruction.
Nobody had to write the second fact as an update to the first: the two were
recognised as being about the same thing, and the older one was closed rather
than deleted.

Two memories can carry the same moment: a `timestamp` that is only a date, or
the same one on several memories of a dated history imported in one go. They
are read in the order they were written, which is the moment of the event, then
the moment the store received the memory, then its id. So "moved to Paris",
written after "lives in Rome", reaches the judge after it, and the later fact is
the one that stays open. One limit: the moment received is the one of the
transaction, so memories saved in one transaction tie again and the id decides.
The import writes one memory per transaction, so it does not meet this.

Two things in that output are the model's and not the store's. The relation's
name is the word the model chose, so yours may read `is_on` or `plan`: the
example is about the dates, not the vocabulary. And recognising that a new
plan closes the old one is a model's judgement, which is where this can go
wrong: [Honest limits](limits.md#honest-limits) says how often, measured, and the next
section is the path that does not depend on it.

### Or say it yourself, and leave the model out of the write path

That example relies on a model reading the second sentence and noticing it
contradicts the first. An agent that already knows a fact is finished does not
need anything noticed:

```bash
# assert it
curl -X POST https://your-domain/v1/facts \
  -H "Authorization: Bearer kor_self_..." -H "Content-Type: application/json" \
  -d '{"user_id":"c4821","subject":"c4821","predicate":"is_on","object":"Priority Support plan","valid_from":"2026-03-15"}'

# and retract it, on the date it stopped being true
curl -X POST https://your-domain/v1/facts/fct_.../forget \
  -H "Authorization: Bearer kor_self_..." -H "Content-Type: application/json" \
  -d '{"at":"2026-06-20"}'

# or replace it, keeping both readable
curl -X PATCH https://your-domain/v1/facts/fct_... \
  -H "Authorization: Bearer kor_self_..." -H "Content-Type: application/json" \
  -d '{"object":"Basic plan"}'
```

`at` is the date the world changed, not the date you noticed: `as_of=2026-04-01`
still returns the fact afterwards, because the row keeps its dates instead of
being edited. `PATCH` has no such date: the replacement starts now and the
old value ends now, or, for a fact dated ahead (a plan that begins next
month), both happen where the old value was to begin, so the new value is
not true before the old one would have been. When the change happened on a
past date, retract with `forget` and `at`, then assert the new value with its
own `valid_from`.

A fact sent with `tense` `past` and no `valid_from` is history from the moment
it was written: it begins and ends there, an empty interval, so no `as_of`
returns it as true. Send `valid_from` when you know when it began, and the fact
is true from that date until it was said.

This is the configuration to reach for when a model in the write path is the
thing you cannot have: an approval to get, a regulator to satisfy, a cost to
predict. Writes become deterministic, and what the store says about a person is
what somebody put there.

One thing is still the store's, and it is the relation's name. The predicate
you send is matched against the relations this end user already has (the
vocabulary is one per end user of a project, learned from their facts alone), so
`is_on` can come back as `subscribes_to` when that is the name on record, and
a fact identical to one already open from the same agent is that fact seen
again (`observation_count` goes up), not a second row. The same fact from two
different `agent_id`s is two rows, one per source; a contradiction closes
both, because what closes a fact is the end user's story, not who told it.
With no key the matching is a
lookup and a string normalisation, and deterministic. With a key, a relation
name this end user has never used costs one model call, once for that end
user, to place it.

To run it that way, leave the provider key unset. There is no flag to turn
extraction off, because there is nothing to turn off: with no key the service
stores and serves memories and mines no facts, `/health` says
`"state": "not configured"` rather than pretending, and `POST /v1/facts` keeps
working because it never needed a model. Facts arrive only when you write them.
The memories wait: set a key one day and the worker mines them, billed to it.

### What was true then, and what the store knew then

Those are two different questions, and a change told late tells them apart.
An agent learns on 5 October that a customer moved on 1 September. The new
address starts on 1 September, because that is when it became true; and the
store only knew it from 5 October. So there are two clocks: when a fact was
true in the world (`valid_from`, `invalid_at`), and when the store knew it
(`created_at`, when it learned the fact, and `closed_known_at`, when it learned
the fact had ended).

```bash
# what was true on 20 September, as the store knows it now: the new address
curl "https://your-domain/v1/facts?user_id=c4821&as_of=2026-09-20" -H "Authorization: Bearer kor_self_..."

# what the store knew on 20 September: the old one, the move was not told yet
curl "https://your-domain/v1/facts?user_id=c4821&known_as_of=2026-09-20" -H "Authorization: Bearer kor_self_..."
```

`known_as_of` answers what an agent could have known when it decided. It is on
`GET /v1/facts`, `GET /v1/profile` and `GET /v1/context` (the block as the
store could have built it then: the facts it knew, the memories it had
received), and on the MCP tools `korely_get_facts` and `korely_get_context`.
On the facts and the profile (and `korely_get_facts`) it goes together with
`as_of`: `known_as_of=2026-10-10&as_of=2026-08-15` is what the store knew on
10 October about 15 August. The context takes no `as_of`. A fact read that way
shows the end the store knew of on that date, and none when the end was
learned later. The dashboard's graph has both clocks on its time slider, on
an install of one's own and on the Cloud.

An edit is a memory saying something new. "I work at Acme" told by three
memories is one fact with three sources; edit one of them to "now I work at
Beta" and Beta is weighed against the Acme the other two still state, as it
would be for a new memory: Acme closes where Beta begins, still stated by the
two (their words stay, and the context says what they state changed later),
and the agents' own copies close with it. An edit that still says Acme, in
other words or beside Beta, leaves the fact as it was, apart from its
`last_confirmed_at`, which moves as for any restatement.

A deletion gives back what its memory had closed. "I live in Paris", then
"now I live in Zanzibar": the second memory's fact closes Paris. Forget or
erase that second memory, or edit its text so it no longer says Zanzibar, and
Paris is true again, as a new fact stated by the memories still saying it.
It begins where Zanzibar stopped standing: at the move when the memory is
erased (Zanzibar never was, so Paris reads true throughout), at the forget or
at the edit when the memory is forgotten or edited (until then Zanzibar is
true alone, as the store believed it). One place at a time, on every date.
The closed Paris row is not touched: `known_as_of` a moment before the
deletion still answers Paris closed by Zanzibar. Not when something else
still closes it: Zanzibar said by another memory too, another value of the
same relation open ("now I live in Rome"), no memory left that says Paris (a
fact posted with `POST /v1/facts` needs none: your statement stands), a
memory that said Paris and was edited since, or one told since that says it
is over ("I used to live in Paris"), or a later Paris row already in the
history (given back once, and ended since). Only a fact closed by a newer
value is given back: one closed in place, by a statement in the past tense or
by `POST /v1/facts/{id}/forget`, names no closer. No model is asked on a
deletion, so in doubt Paris stays closed. The answer to the deletion counts
them (`facts_reopened`), and no webhook is sent for them. The closed row
stays history: `PATCH /v1/facts/{id}` on it answers 409 `fact_not_current`
with no successor named, so correct the new open row, which
`GET /v1/facts?user_id=...` lists.

The limits. `closed_known_at` is written by the database since version 0.1.26;
for facts closed before that it is estimated once from what was stored (when
the fact that closed it was learned; else the audit row of the forget that
closed it; else the earlier of its end date and its last change, never before
the fact itself was learned), so read the older history as approximate. A fact
read with `known_as_of` has the `source_memory_ids`, `observation_count` and
`last_confirmed_at` of then, rebuilt from the memories written by then: a
source that a later forget took off the fact is not shown, and restatements
posted with no memory are counted as they are today. The block of
`/v1/context` is rebuilt with today's ranking from what is stored today: a
memory edited since shows today's text, and one deleted since stays out. A
fact given back after a deletion is a row learned at the deletion (above),
and `manage reextract` gives the facts it mines again a new
`created_at`.
