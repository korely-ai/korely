# Keeping it running

```bash
./deploy/backup.sh                          # a dump, keeping the last 14
./deploy/restore.sh backups/korely-....dump # put one back
```

Read [CHANGELOG.md](../../CHANGELOG.md) first: it says what to do before and after
each upgrade, and what the store answers differently.

An upgrade from a git checkout is `git pull && docker compose up -d --build`;
migrations run at boot. From a tarball, keep the old directory, because it is
also the way back:

```bash
(cd korely-agent && ./deploy/backup.sh)             # first
mv korely-agent korely-agent-<old version>
tar xzf korely-agent-<new version>.tar.gz
cp -a korely-agent-<old version>/.env korely-agent-<old version>/backups korely-agent/
cd korely-agent && docker compose up -d --build
```

Copy `facts/` across as well if you export. The directory keeps its name, so
compose finds the same volumes and the same data.

Either way the install keeps its `.env`, and with it the same `SECRET_KEY`:
never make a new one at an upgrade. Under another key the provider chosen on
the screen cannot be read, the pseudonyms earlier erasures left are
unfindable by id, and a restore's replay of the erasures refuses to run
(CHANGELOG, 0.2.0; [Backups](#backups)).

## Your own proxy in front

The API takes the client's address from `X-Forwarded-For`, and trusts
whoever sends it (`FORWARDED_ALLOW_IPS=*`, set in the image): the port is
bound to loopback and only the proxy reaches it. It takes the leftmost
address of that header, and that address decides the login lock, the
address the audit trail keeps for a sign-in, and the caps per network.
The shipped Caddy writes the header itself, with the address of the
connection, and drops whatever the client sent (`deploy/Caddyfile`). A
proxy that appends instead (nginx with `$proxy_add_x_forwarded_for`,
several load balancers) lets every client choose its own address.

So a proxy of yours in Caddy's place does one of two things. It overwrites
the header with the client's address (nginx:
`proxy_set_header X-Forwarded-For $remote_addr;`), or the API is told to
trust that proxy alone, with one line in `.env`:

```bash
FORWARDED_ALLOW_IPS=<your proxy's address>
```

then `docker compose up -d`. uvicorn then reads the header from the right,
skipping only that proxy; several addresses go comma-separated. A proxy
on the same machine reaches the API from the compose network's gateway, not
from 127.0.0.1: `docker network inspect <directory>_default` names it. With
Caddy behind another proxy (a CDN), add that proxy to `trusted_proxies` in
the Caddyfile's `servers` block, where `trusted_proxies_strict` already
makes Caddy read its header from the right.

A proxy that serves the install at an address other than `https://DOMAIN`
(another name, a path, another port) names it in `.env`:

```bash
PUBLIC_BASE_URL=https://memory.example.com
```

It is the address the OAuth and MCP metadata advertise, and the 401s point
to: a client that connected to one address and is told another refuses to
go on (RFC 9728 §3.3, RFC 8414 §3.3).

Its log is yours to keep clean too. The shipped Caddyfile logs no
address at all, neither the request's path and query nor the `Location` of
a redirect (`request>uri delete`, `resp_headers>Location delete`, in both
of its logs): `/v1/users/<end user>/memories` carries an end user, and an
erasure never reaches a log. The API's own access line writes each path by
its route (`/v1/users/{end_user}/memories`); read paths there. A proxy of
yours that logs full paths or query strings keeps them, for as long as it
keeps its log.

## The database's log

Postgres writes, for a failed statement, the statement with its values and
the values of a violated key. The shipped `docker-compose.yml` starts it
with `log_min_error_statement=panic` and `log_error_verbosity=terse`, so a
failure leaves its message and, in general, none of the
data (a failed type cast can still quote the value in the message): the
application fails statements on ordinary paths (restating a fact, a write
that waited past its lock timeout), and an erasure never reaches a log. If
you run Postgres another way, set both, and do not turn on
`log_statement` or `log_min_duration_statement`: they write every value.

## Backups

A dump is the whole store: every memory and fact in clear, and the model key
encrypted with `SECRET_KEY`. `backup.sh` writes it readable by its owner only,
and `SECRET_KEY` is not in it: it lives in `.env`. Two consequences. A dump
kept on the same disk dies with the disk, so copy it somewhere else. And a
dump restored on another machine needs the same `SECRET_KEY` there. Without
it the provider and key chosen on the screen cannot be decrypted: no memory
is mined until they are saved again in Settings, unless `.env` names a
provider. An OAuth client that registered with a secret is unknown and
registers again (ChatGPT and Claude register without one). And the
pseudonyms the erasures left no longer match their end users' ids, so
`GET /v1/audit?user_id=` stops finding those records.

`restore.sh` asks you to type `yes`, because it replaces the database; from a
script, `FORCE=1 ./deploy/restore.sh <dump>`. It stops the API first, before
it reads the live database, so nothing written after that read is lost, and
starts it again at the end; `/health` then reads `working` off the last extraction
the dump holds until the next model call, with `seconds_ago` counted from it.

A restore brings back whatever the dump held, including people erased after
it was taken, and the erasure records go back with the database. So
`restore.sh` reads every erasure of the live database first
(`manage erasures-export`), and after the restore makes again the ones the
dump does not hold (`manage erasures-replay`): an end user, an agent
namespace, a project, one memory. A memory erased with `?erase=true` is
erased again, row, text, vector and facts, and one only forgotten is
forgotten again. An erased end user is found among the
restored ones by their pseudonym, and the original record goes back, so the
`audit_id` an erasure answered with still proves it. An end user or an agent
is erased again in the project the erasure was made in, even when that
project was deleted since without `?erase` and its records were filed under
the default project; a project the dump does not hold has nothing of it to
erase. A record moved that way before 0.2.0 no longer says where it was made
and is repeated in the default project: take a dump after upgrading. A
project deleted without `?erase` after the dump is deleted again, in its
place among the erasures, so its rows are in the default project again
before an erasure made there later, through one of its moved keys, is made
again (projects deleted before this version left no record of that, and a
person erased that way after such a deletion is not found in the restored
project: take a dump after upgrading). On the Cloud, an account merged into
another by a claim is merged again first, and an account closed is closed
again. A
project erased is erased again whole, which reaches each of its end users,
and their own rows of that erasure go back as they were, counted with the
project's. A
fact forgotten or corrected after the dump is a change of what is true, not
an erasure, and the dump's version stands. The pseudonym needs the same `SECRET_KEY`: each
erasure record keeps a check of the key its pseudonym was made under (the
records made before this version, of the key in force when the upgrade ran),
and a replay under another key, or with none, would find nobody, so it
refuses before it erases anything and the API stays stopped; put back the
key the erasures were made under and run the replay again (the script prints
the command). An export on an install whose `SECRET_KEY` was removed, of
records that keep no such check, is refused before anything is restored: put
the key back first. An end user erased while the install had no `SECRET_KEY` is named
by nobody, and the replay says how many it could not find. An end-user or
agent erasure made under 0.1.11 or earlier wrote no project; the upgrade
finds it from the erasure request's own row of the trail, and a record it
cannot place is marked "project not recorded": the replay repeats it
nowhere, since in every project of the account it would erase a namesake,
counts it apart, exits 2 and says to erase those people again by hand, in
their project. A record whose repeat fails (an error of the database on the
restored data) stops no other: it is rolled back and named by its own id,
never the person's, every other record is made again, and the replay exits
4 with the API kept stopped. Run the replay again with the same file
(`restore.sh` keeps it and prints the command): it repeats only what the
restored data does not hold yet. If the same records fail again, keep the
API stopped and report the lines it printed for them, which name each
error's kind and never its values.

**Restoring by hand**, without `restore.sh` (a database restored with your
own tools). The order is what keeps the erasures and the credentials:

1. With the API stopped and the live database still there, keep what it did,
   in files only their owner can read:
   `(umask 077; python -m korely_agent.manage erasures-export > erasures.json;
   python -m korely_agent.manage credentials-export > credentials.json)`
   (both personal data, the second with password hashes and webhook secrets
   in clear: deleted after step 4). `erasures-export`
   exits 3, with nothing written, on an install whose `SECRET_KEY` cannot
   be the one its erasures were made under: put that key back first.
2. Restore the dump (`pg_restore --clean --if-exists --no-owner`).
3. Bring the restored schema to this version: `alembic upgrade head`, which
   the container's entrypoint runs on every start and a bare
   `python -m korely_agent.manage` does not.
4. `python -m korely_agent.manage erasures-replay < erasures.json`, then
   `python -m korely_agent.manage credentials-replay < credentials.json`.
   `erasures-replay` exits 0 when every erasure is made again; 2 when some
   could not be (named by nobody, project not recorded, an account merged
   into one the dump does not hold), with what to do printed; 3, with
   nothing erased, when the `SECRET_KEY` in force is not the one the
   erasures were made under: put it back and run it again; 4 when some
   records failed, each named by its id, every other one made again: run
   it again with the same file.
5. Start the API only after both replays succeeded, and keep `SECRET_KEY`
   as it was throughout.

The credentials go back with the dump too: a key revoked after it would work
again, an old password would let its owner in, a webhook would sign with a
secret rotated away. So `restore.sh` also reads the credentials of the live
database first (`manage credentials-export`) and after the restore makes
again what changed since the dump (`manage credentials-replay`), always
towards the closed side: a key revoked there is revoked, a key moved there
to another project is in that project again (revoked, when that project was
made after the dump), an operator disabled there is disabled, and the
password and the webhook secret are the ones in force just before the
restore. A webhook endpoint deleted after the dump comes back switched off.
Nothing is reopened. And whatever the live database said, every dashboard
session and every OAuth grant ends, with the keys apps such as ChatGPT and
Claude got over OAuth: everybody signs in again and each app connects again,
which is the whole cost. The command then prints, for each account, the keys
that work and the project each reads and writes, the webhook endpoints that
are on and the operators who can log in, with their dates: check them
against what you expect.

If the live database cannot be read the restore stops, with nothing restored
and the API stopped (`docker compose start api` puts it back as it was);
`SKIP_ERASURES=1` is for when it is gone, and then nothing made after the dump can be made again,
the sessions and grants still end, and the list is what to check. If making
the erasures or the credentials again fails, the API stays stopped and the
file is kept in the install's folder (`erasures-<time>.json`, `credentials-<time>.json`,
readable by you alone: the second holds password hashes and webhook secrets,
as a dump does), with the command to run printed. A `pg_restore` that reports
errors leaves the API stopped as well, keeps both files and prints how to
finish the restore or start over. So does any stop once `pg_restore` has
begun, an interrupt included (Ctrl-C, a closed terminal or SSH session):
from then on those two files are the only copy of what the live database did
since the dump. If a restore was interrupted, look in the install's folder
for `erasures-<time>.json` and `credentials-<time>.json` and run the two
replays with them before `docker compose start api`; running `restore.sh`
again instead reads the restored database as the live one. `.gitignore`
keeps them out of git; delete them once the replays succeeded.

## What the worker trims

Once an hour the worker deletes its own records past their window. Each window
is a line in `.env`, in days, and `0` keeps them forever:

| | default | what it is |
|---|---|---|
| `AUDIT_RETENTION_DAYS` | 400 | who touched which end user's data, and who signed in to the dashboard from which address |
| `LLM_USAGE_RETENTION_DAYS` | 400 | every model call, its tokens and the agent that made it, never the end user |
| `WEBHOOK_DELIVERY_RETENTION_DAYS` | 30 | finished webhook deliveries; one still waiting for a retry is never removed |

Finished `/v1/batch` jobs go after seven days, and that window is not a line
in `.env`. A job's payload is a copy of every memory it imported, and once the
import is over the only thing anybody reads is the job's counts and errors;
the three above keep evidence somebody may decide to keep forever, this keeps
a second copy of people's data. A job still queued, running or waiting for a
retry is never removed.

How much each job takes per tick is there too (`DRAIN_LIMIT`,
`BATCH_DRAIN_LIMIT`, `WEBHOOK_DRAIN_LIMIT`, `VECTOR_DRAIN_LIMIT`), with what
each one bounds, in `.env.example`.

## Which version am I running, and how to go back

Every release is a tag, and the number is in `VERSION` at the root. The running
service says it too, so you can ask the machine rather than the directory:

```bash
curl -s https://your-domain/health | grep version   # what is actually serving
cat VERSION                                          # what this directory is
git tag --list                                       # a git checkout: what you could go back to
```

Going back is the half that matters while something is broken, so it is written
down here rather than left to be worked out under pressure:

```bash
./deploy/backup.sh                       # first. Always first.
git checkout v0.1.0                      # the tag you were on
docker compose up -d --build
```

From a tarball, the tag is the directory you kept: `docker compose down` (no
`-v`) in the new one, swap the two names back, `docker compose up -d --build`
in the old one.

One caveat, stated plainly because finding it out afterwards is expensive:
**going back across a migration does not undo the migration.** The schema stays
where the newer version left it, and an older service against a newer schema is
a case nobody has tested. If the upgrade you are undoing printed
`Running upgrade` at boot, restore the dump you took before it instead of
checking out the old tag. That is what the dump is for, and it is why the first
line of the recipe is the backup.

Both are tested, on an install that already holds data rather than an empty
one. `scripts/acceptance_survives.sh` starts an older version (0.1.20 unless
`--from` names another), fills it, upgrades it to this one across every
migration in between and checks that the memories, the facts and
the keys are still there and that `as_of` still answers; then it deletes the
data and brings it back from a backup. It was written by breaking both first:
a migration referencing a column that does not exist, and a restore that
restores nothing.

If a migration fails, the container refuses to start rather than serving on a
half-built schema. A crash loop with the error in the log is a worse morning
than a clean start and a better one than silent corruption.
