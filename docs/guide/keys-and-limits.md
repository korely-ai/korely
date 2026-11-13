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
it.

A key that does not authenticate (unknown, revoked or expired) gets one
answer on every `/v1` and `/admin` route, 401 `invalid_key` with "no live
key matches it (unknown, revoked or expired)" (since 1.0.10; up to 1.0.9
the words named revocation alone): branch on the code, not on the words,
since the three cannot be told apart. The MCP door answers the
same three with its library's own 401, `invalid_token` "Authentication
required".

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
