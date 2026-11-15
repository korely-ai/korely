# Keys and account limits

## API keys expire

You choose when you make one, and the default when you say nothing is 90
days: a credential with no end date is one nobody gets round to rotating. The
dashboard offers 30, 60, 90 or 180 days, or never. The API (`expires_in_days`,
`null` for never) and `manage issue-key --expires-in` (a number of days, or
`never`) take any number up to 3650, and `0` expires a key at once, which is how
you retire one without revoking it. Two weeks before the day, your webhooks get
`key.expiring`, because a key that stops working in silence is worse than one
that never stops at all. A webhook you add within those two weeks, subscribed
to `key.expiring` (or `*`) and to the key's project or to every project, still
gets it at a later hourly round, unless a webhook of the account already got
it. Within the same two weeks the dashboard's API Keys page marks the key
beside its date ("Ends in 3 days", in red with the hours on its last day)
and Home names the keys that end soon, with a link to rotate them (since
1.0.27). The page reads the notice from the server (`warning_days` of
`GET /admin/keys/lifetimes`) and counts the days with the browser's clock.

Where an install caps how long a key may last, `GET /admin/keys/lifetimes`
says so (`max_days`, and no "never" among the `choices`): a longer
`expires_in_days`, or `null`, is then a 422 that names the most, and nothing
is made. The keys made before keep their dates.

Every answer to a request made with a key that expires carries its end, in
`Korely-Key-Expires-At` (ISO 8601, UTC, to the second), so an agent with no
webhook can see the day coming (since 1.0.23), a refusal by the route
included; a 500 of an unexpected failure carries none. A key that never expires, and
the key of an app connected over OAuth, which renews itself, send no such
header.

### Rotating a key

`POST /admin/keys/{id}/rotate` gives the same key (its id, name, project,
scopes and end users) a new secret, in that answer and never again, and lets
the old secret go on working for a while, so the agents that hold it can
take the new one:

```bash
curl -X POST https://your-domain/admin/keys/<key id>/rotate \
  -H "Authorization: Bearer <a key with admin>" \
  -H "Content-Type: application/json" \
  -d '{"old_key_expires_in_hours": 24}'
```

`old_key_expires_in_hours` is 24 when left out, at most 168 (seven days), and
`0` ends the old secret at once; it never outlives the key's own end.
`GET /admin/keys/lifetimes` serves what a screen offers for it
(`old_secret_choices`: now, 1 hour, 24 hours, 3 days, 7 days, and
`old_secret_default_hours`), and `GET /admin/keys` says of each key whether
it rotates (`rotates`: a live key a person made; since 1.0.27).
`old_secret_expires_at`, in the answer and in `GET /admin/keys`, says until
when it works (null once it has ended). `expires_in_days` sets the new
secret's lifetime as at creation; left out, the new secret gets the lifetime
the current one was given, counted from when it was made, never more than a
key made now may last (`max_days` above). Two rotations at once take turns. Rotating again ends an
earlier overlap at once, revoking the key ends both secrets, and a revoked or
expired key, or a connected app's, answers 409 `key_not_rotatable` (since
1.0.23).

During the overlap the old secret holds the key's scopes but `admin`
(since 1.0.26). Every `/admin` route answers it 403
`previous_secret_has_no_admin`, as it answers a key without `admin`, and
changes nothing: with the old secret no key is made, rotated, revoked or
moved, no project deleted, neither the model provider's key nor a model
set, and no webhook made. Webhooks made before the rotation stay, and go on
receiving the account's events. `GET /v1/ping` lists the scopes the secret
it was sent with holds. A call of its other scopes that would revoke a key
as it goes is refused too: erasing an end user to whom a key is confined
alone (`DELETE /v1/users/{id}/memories`) revokes that key, so with the old
secret it answers 403 `previous_secret_cannot_manage_keys` and erases
nothing. The key's current secret, and a person signed in to the
dashboard, do all of it as before.

Up to 1.0.25 the old secret was the key in full: whoever held the old
secret of an admin key could rotate it again, end the new secret and take
the newest. Stripe's rolled key, which this follows, keeps working for API
calls while keys are made, rotated and expired on its Dashboard
(https://docs.stripe.com/keys).

On the dashboard (since 1.0.27), API Keys has a Rotate on the row of each
key that rotates: it asks when the old secret stops, from Now to 7 days, 24
hours chosen first, shows the new secret once, and the row then says until
when the old secret works. A connected app's key has no Rotate: it renews
its own secret every hour. If no answer comes back (the connection drops,
or a proxy answers in the install's place), the key may have been rotated
all the same, and its new secret is then lost: the page reads the key
again and says whether its secret changed. Do not rotate it again while
the old secret still works: a rotation ends an earlier rotation's old
secret at once, and that is the one your agents hold. Make them a new key
instead, before the old secret ends. The same goes for a script: after a
rotation with no answer, read `GET /admin/keys` (the key's `masked` ends
with the current secret's last four) before sending another.

For a key that leaked, do not leave an overlap: rotate with
`"old_key_expires_in_hours": 0` (Now, on the dashboard), which ends the old
secret at once, or revoke the key. Until it ends, the old secret still reads and writes what
its scopes allow, and erases the end users no key is confined to alone.

A key that does not authenticate (unknown, revoked or expired) gets one
answer on every `/v1` and `/admin` route, 401 `invalid_key` with "no live
key matches it (unknown, revoked or expired)" (since 1.0.10; up to 1.0.9
the words named revocation alone): branch on the code, not on the words,
since the three cannot be told apart. The MCP door answers the
same three with its library's own 401, `invalid_token` "Authentication
required".

### One key per agent or machine

A key is not tied to a device: it works from any machine it is copied to,
from several at once, and the dashboard shows only when it was last used,
not where from. So make one key per agent or machine, named after it, and
give it only what it needs: the scopes it uses, and, if it works for some
end users, only those (`users`; a confined key cannot hold `admin`). When
one leaks, rotate it with Now or revoke it, and the others go on working.
Stripe gives the same advice for its restricted keys: make as many as you
want and assign them to different parts of your application
(https://docs.stripe.com/keys, read 15 Nov 2026).

## How many keys, names and projects

An account holds at most 100 keys that are not revoked: an expired key and a
`kor_live_` key (refused on an install of your own) count until you revoke
it; a connected app's key (Claude, ChatGPT) is not counted. The 101st is
refused with a 409 `too_many_keys` (and `manage issue-key` says so): revoke
one first. Revoked keys stay in the list, a page at a time: `GET /admin/keys`
takes `limit` (up to 200) and `offset`, and lists the keys not revoked first,
so the first page holds all of them. A script that lists keys must page.

An account may also declare at most 1,000 names (`POST /v1/users/{id}/names`
and the dashboard's aliases; the names the engine learns are not counted).
The 1,001st is a 409 `too_many_names`: delete one with `DELETE
/admin/aliases/{id}`.

An account holds at most 100 projects, the Default one and archived ones
included (an archive is undone with `/unarchive`, so it still counts). The
101st (`POST /admin/projects`, or the Projects page) is a 409
`too_many_projects`: delete one first (`DELETE /admin/projects/{id}`). The
Default project is never refused. On the Cloud, an account that a key's
account was merged into keeps every project of both, and makes no new one
until it is back under 100.

On the Cloud the plan also caps the account's agents, and an agent name is
counted once per project: the same name in two projects uses two of the
plan's agents. A write under a name new to the key's project, past the
cap, is a 429 `agent_cap_exceeded`; the agents the account already has
keep writing. `cap` and `used` in `GET /v1/agents` are the account's
totals, the same for a key of any project: they say how many agents the
account's projects hold, never which. An install of your own has no such
cap.
