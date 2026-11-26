# Webhooks

An endpoint is made on the Webhooks page, or from a shell:

```bash
docker compose exec api python -m korely_agent.manage add-webhook \
  --tenant <tenant id> --url https://hooks.example.com/korely --events "*"
```

Either way the signing secret is shown once. The address must be `https://`
and must not resolve to a private network, because the thing sending is inside
yours; `WEBHOOK_ALLOW_HTTP` and `WEBHOOK_ALLOW_PRIVATE_URLS` in `.env` lift
that for a receiver on this machine while you test, and belong nowhere else.
`list-webhooks` and `delete-webhook` sit next to it.

The address's name is looked up when the endpoint is made or enabled and
again at every delivery attempt, with this machine's resolver (its
/etc/hosts and search domains hold), and each lookup is waited for 5 s. A
name that does not answer in time is a create or enable refused with 400
"host did not resolve within 5 s", and a failed delivery attempt retried on
its schedule. An account has at most 4 lookups under way and the server 64
(since 1.0.40); past the server's, a lookup is refused at once with "host
could not be resolved now", and a delivery that meets it makes no attempt:
it keeps its attempts and is due again 5 s later ([Limits](limits.md)).

An endpoint made on the Webhooks page can belong to one project: it then
receives that project's events and nothing from the others, so a staging
receiver never sees production's end users. One with no project receives
every project's, including the projects made later. On the dashboard, with
more than one project, the project is chosen before the endpoint can be added
(since 1.0.5); through the API, leaving `project_id` out still means every
project. Each delivery says the project it is about (`project_id`,
null for none), and deleting a project deletes its endpoints, erased or
not, with their secrets and the deliveries still waiting. An archived
project takes no new endpoint (409 `project_archived`, since 1.0.39), as it
takes no key: unarchive it first.

Every delivery is `{"event": ..., "data": ..., "project_id": ...}`. The
page's Send a test posts the same envelope, signed the same way, with
`"event": "ping"` and `"test": true`: an event no endpoint subscribes to,
which a receiver acknowledges like any it does not act on.

## fact.invalidated

Sent once for each fact the store closes, in the same transaction as the
closing. Its `data` is `{"fact_id": ..., "invalidated_by": ..., "at": ...}`:
the fact closed, the fact that replaced it, and when it was closed.
`invalidated_by` is null when no fact replaced it: a fact forgotten, or one an
edit of its memory no longer states (that one also carries
`"reason": "memory_updated"` and the `memory_id`).

Since 1.0.9 a fact a past statement closes carries the fact that replaced it.
"Luca works at Acme", then "Luca left Acme and joined Globex": the second
memory closes "works at Acme", and its event's `invalidated_by` is the id of
"works at Globex". Up to 1.0.8 it was null there, while a closing by the
contradiction judge always named the new fact. It is set when the same write
holds exactly one open fact about the same subject and relation with another
value; with none, or with several, it stays null. A receiver that took a null
`invalidated_by` to mean "a past statement closed it" now reads the
replacing fact's id there. Events sent before the upgrade are not sent again.

## Webhook signatures

Deliveries are signed per [Standard Webhooks](https://www.standardwebhooks.com):
`webhook-signature` is HMAC-SHA256 over `{webhook-id}.{webhook-timestamp}.{body}`,
keyed by the base64-decoded part of the endpoint's secret after `whsec_`. So the
official library verifies them as it is:

```python
from standardwebhooks.webhooks import Webhook   # pip install standardwebhooks

Webhook(secret).verify(body, headers)   # raises on a wrong signature or a stale timestamp
```

Endpoints made before 0.1.11 have secrets that are not valid for the spec,
and since 0.2.0 they are not sent (until 0.1.42 they signed with the whole
secret string as the key). `manage list-webhooks` and the Webhooks page say
which ones they are. Their events are still queued: each attempt is refused
with the reason, which the page shows as a failure and its last error, and
after the three days of attempts the endpoint is switched off like any that
accepts nothing. To move one to a spec secret:

```bash
docker compose exec api python -m korely_agent.manage rotate-webhook-secret \
  --tenant <tenant id> --id <endpoint id>
```

It keeps the endpoint, its events and anything still queued, which then goes
out under the new secret (an endpoint already switched off needs Enable too),
and prints the new secret once. There is one secret at a time: until the receiver has the new
one it rejects deliveries, and they are retried with backoff. Recreating the
endpoint on the Webhooks page also works, with a new id and without the queued
deliveries.
