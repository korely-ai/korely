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

An endpoint made on the Webhooks page can belong to one project: it then
receives that project's events and nothing from the others, so a staging
receiver never sees production's end users. One with no project receives
every project's, including the projects made later. On the dashboard, with
more than one project, the project is chosen before the endpoint can be added
(since 1.0.5); through the API, leaving `project_id` out still means every
project. Each delivery says the project it is about (`project_id`,
null for none), and deleting a project deletes its endpoints, erased or
not, with their secrets and the deliveries still waiting.

Every delivery is `{"event": ..., "data": ..., "project_id": ...}`. The
page's Send a test posts the same envelope, signed the same way, with
`"event": "ping"` and `"test": true`: an event no endpoint subscribes to,
which a receiver acknowledges like any it does not act on.

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
