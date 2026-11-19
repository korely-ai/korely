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
own `valid_from`. A date with no time is midnight UTC of that day: when the
store may already hold a fact read from a memory later that day, give the
time too (`"2026-06-20T14:30:00Z"`), or, when the two disagree, that later
fact stays true and yours is stored as history
([Honest limits](limits.md#honest-limits)).

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
For a fact you write (`POST /v1/facts`, a correction that names its
subject) the contradiction is looked for among the facts about that subject,
under the names the store holds for it (a declared name, a rename, compared
with the database's `lower()`), and among the facts about the same two
things the other way round (`Globex uses Salesforce` against `Salesforce
serves_as_crm_for Globex`): no other fact is put in front of its judge, and
it is put in front of no other subject's judge, whether that fact was
written or read from a memory, at the write and when a vector arrives late
(since 1.0.12). A fact read from a memory, and a correction that keeps the
extraction's subject, are still weighed across the names the extraction
gives one person. In an agent's memory with no `user_id` (since 1.0.13)
there is no one person: a fact read from a memory there is weighed against
the facts about its own subject, under the names the store holds for it in
the namespace of the agent that writes, and about the same two things the
other way round, of every agent of the project; "Cloud" and "Korely Cloud"
are two subjects there until an alias unites them. So a person you write about by name, whose memories are
stored under their `user_id`, is two subjects until the name is declared
(`POST /v1/users/{id}/names`, with the `agent_id` you write with).
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
10 October about 15 August. The context takes no `as_of` (a 422 since 1.0.23;
it was ignored before). A fact read that way
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

A `PATCH` with the text the memory already has is not an edit (since 1.0.9):
the memory is not read again, no model is called and nothing closes. A text that changed is read again, and a
value taken out of it closes where the edit arrived. If the model answers no
fact at that second reading of a changed text, the facts the old text stated
close all the same: the store cannot tell it from an edit that took them out.

A fact a past statement closes points to the fact that replaced it (since
1.0.9, memory_core 6.3.0; up to 1.0.8 its `invalidated_by` was empty). "Northwind
costs 55 now, it used to cost 50" closes the 50 where it was said, and
`invalidated_by` on it is the 55, as when the judge closes a fact; when the 50
was an open fact before, its `fact.invalidated` event names the 55 too
([Webhooks](webhooks.md)).
It is the one open fact of the same write about the same subject and relation
with another value. With none, or with two, there is nothing to point to and
`invalidated_by` stays empty. Forgetting the memory that said it gives the old
value back if another live memory still states it, as for any closing, as a
new open fact of tense `current`.

A deletion gives back what its memory had closed. "I live in Paris", then
"now I live in Zanzibar": the second memory's fact closes Paris. Forget or
erase that second memory, or edit its text so it no longer says Zanzibar, and
Paris is true again, as a new fact stated by the memories still saying it.
It begins where Zanzibar stopped standing: at the move when the memory is
erased (Zanzibar never was, so Paris reads true throughout), at the forget or
at the edit when the memory is forgotten or edited (until then Zanzibar is
true alone, as the store believed it). One place at a time, on every date.
The closed Paris row is not touched: `known_as_of` a moment before the
deletion still answers Paris closed by Zanzibar. Paris told to two agents of
the end user is a fact in each agent's namespace, and Zanzibar closes both:
the deletion gives each agent its Paris back, and `facts_reopened` counts
both. A Paris open in another agent is the same value, not another one, and
keeps nothing closed. Not when something else still closes it: Zanzibar said
by another memory too, another value open in the same relation or in the
relation Zanzibar was told in ("now I live in Rome"), no memory left that
says Paris (a fact posted with `POST /v1/facts` needs none: your statement
stands), a memory that said Paris and was edited since, or one told since
that says it is over ("I used to live in Paris"), or a later Paris row of
the same agent already in the
history (given back once, and ended since). What closed a fact is read
from what was written when it was closed, never guessed from the dates: the
newer value it points at (`invalidated_by`), and, when it was ended in place,
who did it. A memory that ends a fact in place, a statement in the past
tense ("I no longer live in Paris") or an edit of the fact's own memory into
a text that no longer says it, is recorded by its write, and so is every
later memory that says it again (since 1.0.14). While one of them stands
and still says it, the fact stays closed,
also when the value an earlier judge had named for it goes (Zanzibar from a
later day: the row keeps pointing at it); once every one of them is
forgotten, erased or edited so that it no longer says it, and nothing else
closes the fact, it is given back, from the last forget or edit, or from
where it ended when they are erased. A fact you said is over by hand is never given
back, whatever end the forget moved, even none (`already_forgotten`): a
forget, a correction, or the same fact posted in the past tense with `POST
/v1/facts`, each
with its record. A closing ahead that
nobody brought earlier is given back from its day. No model is asked on a
deletion, so in doubt Paris stays closed. The answer to the deletion counts
them (`facts_reopened`), and no webhook is sent for them. The closed row
stays history: `PATCH /v1/facts/{id}` on it answers 409 `fact_not_current`
with no successor named, so correct the new open row, which
`GET /v1/facts?user_id=...` lists.

In the context block (`GET /v1/context`, `korely_get_context`) a memory whose
fact you changed by hand, by a correction, a forget of the fact or a fact
written with `POST /v1/facts` that ended it, is shown as history, "(true when
said, changed later)" after its day, as a memory whose facts were all
superseded is (since 1.0.29): your word has no memory of its own in the
block, and up to 1.0.28 the memory with the old value stood there unmarked
beside the corrected fact. The mark is on the whole memory, also on what it
says that still holds; those facts stay among the facts above it. The same
fact posted with `"tense": "past"` ends it in place and marks nothing: the
store counts a fact ended only by its tense as still standing, so correct
the fact or forget it when the memory must read as history. A change counts
against the text it was made on: edit the memory afterwards (`PATCH
/v1/memories/{id}`) and the block reads it by its new text, unless another
memory that states the same fact still carries it. A fact a later memory
closed leaves its memory as before: that memory is in the block too, dated
after it. Read with `known_as_of` before the correction, the block is the
one of then.

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
