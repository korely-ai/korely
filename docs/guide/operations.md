# Keeping it running

```bash
./deploy/backup.sh                          # a dump, keeping the last 14, and the erasures
./deploy/backup.sh erasures                 # the erasures alone: every hour
./deploy/restore.sh backups/korely-....dump # put one back
```

## Upgrading

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
(CHANGELOG, 0.2.0; [Backups](#backups)), unless the old key is in
`SECRET_KEY_FALLBACKS` ([Rotating SECRET_KEY](#rotating-secret_key)).

### An install made by install.sh

`install.sh` installs and starts; it does not upgrade. Run again in its
folder with another `--version`, it stops and points here. Its folder (the
published image, its default) holds no source: the files of the release
(`docker-compose.yml`, `docker-compose.image.yml`, `.env.example`,
`deploy/Caddyfile`, and from 1.0.40 `deploy/backup.sh` and
`deploy/restore.sh`, the [Backups](#backups) below), `.env`, the `./korely`
command and `.korely-install`,
whose `release=` line is the release the folder runs. The image is the
`KORELY_IMAGE` line of `.env`. An upgrade changes those three things, in
that folder. Put the release you go to in place of `<new>` and paste:

```bash
cd korely                                   # the folder install.sh made
NEW=<new> sh -eu <<'EOF'
raw=https://raw.githubusercontent.com/korely-ai/korely
grep -q '^KORELY_IMAGE=' .env || { echo ".env names no KORELY_IMAGE: see below" >&2; exit 1; }
# The release's files and their sha256, from the tag's own install.sh.
curl -fsSL "$raw/v$NEW/install.sh" > install-tag.sh
files=$(sed -n 's/^RELEASE_FILES="\(.*\)"$/\1/p' install-tag.sh)
[ -n "$files" ] || { echo "the tag's install.sh names no files: see below" >&2; exit 1; }
sed -n '/^RELEASE_SHA256="/,/^"$/p' install-tag.sh | grep '  ' > release.sha256
for f in $files; do                         # every file has its sha256, or nothing is done
  awk -v f="$f" '$2 == f { n++ } END { exit n != 1 }' release.sha256 \
    || { echo "the tag's install.sh has no sha256 of $f: see below" >&2; exit 1; }
done
# The image's digest, which exists only once CI has pushed the image: main's
# install.sh carries it, while main is this release.
curl -fsSL "$raw/main/install.sh" > install-main.sh
grep -qx "THIS_RELEASE=\"$NEW\"" install-main.sh || { echo "main is not $NEW: see below" >&2; exit 1; }
digest=$(sed -n 's/^THIS_IMAGE_DIGEST="\(sha256:[0-9a-f]\{64\}\)"$/\1/p' install-main.sh)
[ -n "$digest" ] || { echo "no digest of $NEW's image yet: try again later" >&2; exit 1; }
for f in $files; do curl -fsSL -o "$f.new" "$raw/v$NEW/$f"; done
if command -v shasum >/dev/null; then check="shasum -a 256 -c -"; else check="sha256sum -c -"; fi
sed 's/$/.new/' release.sha256 | $check     # one OK a file; a FAILED stops here, nothing changed
umask 077                                   # the dump, the copy and .env: their owner's alone
./korely compose exec -T db pg_dump -U korely -d korely -Fc > "korely-before-$NEW.dump" </dev/null
there=$(for f in $files; do [ ! -e "$f" ] || echo "$f"; done)    # a file new in the release is not
tar -cf "korely-before-$NEW.tar" $there .env .korely-install    # the way back
for f in $files; do mv "$f.new" "$f"; case "$f" in *.sh) chmod 755 "$f" ;; esac; done
sed "s|^KORELY_IMAGE=.*|KORELY_IMAGE=ghcr.io/korely-ai/korely:$NEW@$digest|" .env > .env.new
sed "s|^release=.*|release=$NEW|" .korely-install > .korely-install.new
mv .env.new .env
mv .korely-install.new .korely-install
rm release.sha256 install-main.sh install-tag.sh
echo "ready for $NEW: ./korely start"
EOF
```

It runs in `sh -e`, so the first command that fails stops it and the rest
does not run: until the release's files are checked and the dump is written,
the folder's files are as they were (the downloads wait beside them, as
`*.new`). It writes `.env`'s line as
`KORELY_IMAGE=ghcr.io/korely-ai/korely:<new>@sha256:...`, the form
install.sh writes: Docker refuses an image whose content is not that
digest, so a tag moved on the registry is not run. The digest is stamped
into main's install.sh after CI has pushed the image, and the tag's own
install.sh never has it; so the steps go only to the release main's
install.sh names, the newest, and stop with "main is not <new>" for any
other, or with "no digest" between a release and the stamp of its digest. A
folder whose `.env` names no `KORELY_IMAGE` builds its image: it is the
checkout of the paragraph after the way back. In `.korely-install` it writes `release=<new>`, so a later run of
install.sh in the folder knows what is there. Then start it:

```bash
./korely start                              # pulls the image, migrations run at boot
curl -s http://127.0.0.1:8000/health        # (your PORT) "version" is <new>
```

`./korely` and not `docker compose`: it is the same compose, with the
folder's `.env` deciding over any `PORT` or `COMPOSE_*` your shell exports.

What the old process was working on when it stopped goes back to the queue
at the new process's first pass: the memories it was extracting (since
1.0.22) and the `/v1/batch` imports it was running (since 1.0.40), each
claim carrying the key its process held in the database, which a process
that has ended no longer holds. A claim with no key (made by a release
before those, or without a free key) waits `DRAIN_STALE_MINUTES` (15) as
before.

`./korely start` starts the API alone. If the install runs Caddy (a
server with a `DOMAIN`), Caddy goes on with the files it started with: the
upgrade replaces `deploy/Caddyfile` with a new file (so does `git pull` in a
checkout), and a running Caddy keeps reading the one it opened, even across
`docker compose up -d`, which makes no container again when only a file
changed. So, when a release changes `deploy/Caddyfile`, `./korely compose
restart caddy`; when it changes Caddy's image in `docker-compose.yml` (1.0.30
does), `./korely compose up -d`, which makes Caddy's container again with
the new image and the new Caddyfile. Either drops the open connections once.
Tried on 1.0.28's image, 16 Nov 2026.
`.env` is kept, and with it `SECRET_KEY`, as above. A line the CHANGELOG
says to add goes into `.env` by hand; the new `.env.example` shows it.

The way back is the dump and `korely-before-<new>.tar`, which holds the
release you left: its files, its `.env` and its `.korely-install` (a file
new in the release, such as 1.0.40's two scripts, is not in it, and stays).
Without a migration (the start printed no `Running upgrade`), putting it
back is the whole of it:
`./korely compose rm -sf api && tar -xf korely-before-<new>.tar && ./korely start`
(the API's container goes, so the start makes it again from the old image).
Across a migration it is the dump, as
[below](#which-version-am-i-running-and-how-to-go-back), by the steps of
[Restoring by hand](#backups), each command through `./korely compose`
(`./korely compose run --rm -T --no-deps api python -m korely_agent.manage
...`, `./korely compose exec -T db pg_restore ...`), never `docker compose`
alone and so never `deploy/restore.sh`, which calls it: step 1 with the new
release still in place, whose image reads the live database, then
`tar -xf korely-before-<new>.tar`, then steps 2 to 5, which the old
release's image runs.

A folder made with `--build` or `--from-local` is a git checkout of the
source: it upgrades as one (above), with `./korely compose` in place of
`docker compose`, and the same `release=` line.

These steps follow what install.sh writes. The upgrade's block runs in a
test, `tests/test_the_upgrade_of_an_install_sh_folder.py`, on a folder like
install.sh's with the downloads and Docker answered by fakes: a file that is
not the release's, a main of another release and a missing digest each stop
it with the folder unchanged. The release's checks run the upgrade of a
checkout (`scripts/acceptance_survives.sh`), not of a folder install.sh
made, and no check runs the way back
([Known issues](limits.md#known-issues-in-this-version)).

## Rotating SECRET_KEY

To change `SECRET_KEY` (it leaked, say), keep the old value as a fallback,
as Django's `SECRET_KEY_FALLBACKS` does: the new one makes and seals
everything from now on, and the old one is only read. In `.env`:

```bash
SECRET_KEY=<the new value, openssl rand -hex 32>
SECRET_KEY_FALLBACKS=<the old value>
```

then `docker compose up -d`. Since 1.0.27 every lookup tries `SECRET_KEY`
and then each fallback, newest first, comma separated: `GET
/v1/audit?user_id=` finds the erasures made under the old key, a restore's
replay of them runs, the trail writes a later call that names an erased
person with the pseudonym their erasure left, and the provider, its key and
models chosen on the screen, and an OAuth client's secret, still open.

Then seal again under the new key what the old one sealed:

```bash
docker compose exec api python -m korely_agent.manage reseal-secrets
```

It prints how many values it sealed again and how many were under the new
key already, and exits 2 when a value opens with no key at all (it read as
unset already: choose the provider on the screen again; such an OAuth client
registers again). A save on the screen seals its value under the new key
too, but an OAuth client's secret is sealed only when the client registers,
so without the command it would need the fallback for as long as the client
lives.

An erasure's pseudonym is never made again, so it needs its old key for as
long as its record is kept. Drop a fallback only when that is fine: from
then on, an erasure made under it is found by its `audit_id` alone, a call
that names such a person is written in the trail with their id again, and
a webhook delivery about them that finishes after the drop keeps their id,
as after a change of `SECRET_KEY` before 1.0.27. A value with a comma in it
cannot be a fallback. What leaked still opens what it sealed: rotating stops
new values being made under it, it does not take back the old ones.

## Your own proxy in front

The API takes the client's address from `X-Forwarded-For` and the scheme
from `X-Forwarded-Proto`, and believes them only from the peers named in
`FORWARDED_ALLOW_IPS`. The image sets it to `caddy`, the name of the proxy
service in the shipped compose: the name is looked up again when that
container comes back at another address, and every other peer that sends
the headers is ignored, so it keeps its own address and scheme. The address
decides the login lock, the address the audit trail keeps for a sign-in,
and the caps per network. The shipped Caddy writes `X-Forwarded-For` itself,
with the address of the connection, and drops whatever the client sent
(`deploy/Caddyfile`).

Up to 1.0.22 the image said `FORWARDED_ALLOW_IPS=*`: every peer was
believed, so a container on the compose network, or port 8000 published
by mistake, could choose its own address.

A proxy of yours in Caddy's place has to be named, with one line in `.env`:

```bash
FORWARDED_ALLOW_IPS=<your proxy's address>
```

then `docker compose up -d`. An entry is a name (a service on the same
compose network), an address or a network (`10.0.0.0/24`); several go
comma-separated. A proxy on the same machine reaches the API from the
compose network's gateway, not from 127.0.0.1: `docker network inspect
<directory>_default` names it. The API then reads `X-Forwarded-For` from
the right and skips only the peers it believes, so a proxy that appends to
the header (nginx with `$proxy_add_x_forwarded_for`) is safe too, and one
that overwrites it (`proxy_set_header X-Forwarded-For $remote_addr;`) is
the simplest. Until the proxy is named, every client appears with the
proxy's own address, sign-ins share one lock, and `/health` keeps its
details from every caller the proxy relays; the API's log says so once
(`[proxy] a request carried X-Forwarded-For ...`).

With Caddy behind another proxy (a CDN), add that proxy to
`trusted_proxies` in the Caddyfile's `servers` block, where
`trusted_proxies_strict` already makes Caddy read its header from the
right.

While the API restarts (an upgrade, a reboot, a `docker restart`: 3 to 11 s
to ready, measured on 1.0.28), the shipped Caddy holds each request that
arrives and sends it once the API listens again, trying every 250 ms for up
to 20 s; past that it answers 502. Caddy sends a request again only when no
connection to the API could be made. One that went out as the API stopped
(still running past the 5 s it is given, or sent in the instant the API
closes its connections: 2 to 4 in some 1,300 requests, with eight clients
writing at once) is not sent again by Caddy, as RFC 9110 section 9.2.2
wants of a proxy. Under Caddy, Go's transport still sends again, once, a
GET, HEAD, OPTIONS or TRACE, or a request with no body and an
`Idempotency-Key`, that failed on a kept-alive connection the API had
closed; a write, with its body, is never sent twice by the proxy. A POST
keeps its body for Caddy's retry only on Caddy 2.11.4 or later, hence the
version and the digest in `docker-compose.yml`.

A client that gives up while Caddy holds its write has not stopped it: over
HTTP/1.1 Caddy notices a client gone only once it has read the body, which
it does when it sends the request (seen: a POST sent 3.9 s after its client
left). So a client whose timeout is under 20 s and that sends a write again
sends it with the same [`Idempotency-Key`](api.md#retrying-a-write-the-idempotency-key)
every time, or the write is made twice (measured with a client that waits
5 s over HTTP/1.1: without a key, both writes it sent twice were stored
twice; with the key, the one it sent again was stored once).

During an outage longer than 20 s (the API down, not restarting) every
request waits 20 s and is answered 502, and each one waiting keeps its
connection in Caddy for those 20 s; how many Caddy can keep waiting at once
was not measured. A proxy of yours in Caddy's place answers 502 during a
restart unless it does the same: try again a connection that could not be
made, never a request the API may have received.
`scripts/caddy_restart_check.sh` counts the 502s and the writes made twice
through a restart, with a client that waits 5 s and sends again, with a key
and without one.

A proxy that serves the install at an address other than `https://DOMAIN`
(another name, a path, another port) names it in `.env`:

```bash
PUBLIC_BASE_URL=https://memory.example.com
```

then `docker compose up -d`. It is the address the OAuth and MCP metadata
advertise, and the one the MCP door's 401 points to: a client that connected
to one address and is told another refuses to go on (RFC 9728 §3.3, RFC 8414
§3.3). Left unset, it is `https://DOMAIN`, with `HTTPS_PORT` when that is not
443.

Its log is yours to keep clean too. The shipped Caddyfile logs no request
address at all, neither the request's path and query, nor the `Location` of
a redirect, nor the `Referer` a page sends with its own address
(`request>uri delete`, `resp_headers>Location delete`,
`request>headers>Referer delete`, in both of its logs; the `Referer` since
1.0.30): `/v1/users/<end user>/memories` carries an end user, and an
erasure never reaches a log. The API's own access line writes each path by
its route (`/v1/users/{end_user}/memories`); read paths there. A proxy of
yours that logs full paths or query strings keeps them, for as long as it
keeps its log.

The API says its own security headers (since 1.0.27), so a proxy of yours
should not overwrite them. Every page of the dashboard sends
`Referrer-Policy: strict-origin-when-cross-origin` (another site learns the
origin, never a path, which can carry an end user) and a
`Content-Security-Policy` that lets the page run only its own scripts and
the graph's `blob:` worker, frame only its own origin, load no plugin, and
be framed by nobody (`script-src 'self'; worker-src 'self' blob:; frame-src
'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'`). Every
other answer sends `Referrer-Policy: no-referrer`. The shipped Caddyfile
adds `X-Frame-Options` and `Referrer-Policy` only where an answer has none
(`?` in its `header` block). The policy restricts scripts and frames, not
styles, images, fonts or connections, and it is a list of origins, not a
policy of nonces.

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

## The database connection

`DATABASE_URL` must reach Postgres directly: not through PgBouncer, or any
pooler, in transaction mode (`pool_mode = transaction`). Each process holds
a lock on its own database session for as long as it runs (since 1.0.22),
and that lock is how another process knows that the memories it claimed are
still being worked, or that it has ended and they can go back to the queue
at once. A pooler in transaction mode runs each transaction on whichever
server session is free, so the lock is not the process's. The process
checks that its own session holds its lock, and when it does not it logs an
ERROR, `[holder] this process's session no longer holds its key`, and takes
one again. No pooler has been tested, in either mode.

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
`GET /v1/audit?user_id=` stops finding those records. A key changed on
purpose goes to `SECRET_KEY_FALLBACKS` instead, and nothing of this happens
([Rotating SECRET_KEY](#rotating-secret_key)).

The erasures go beside the dumps. `backup.sh` writes, after each dump,
`backups/korely-erasures-<time>.json`, every erasure record as `manage
erasures-export` writes it: pseudonyms, ids, counts and times, no memory's
text, and no end user in clear (with `SECRET_KEY`). `backup.sh erasures`
writes that file alone. An older file is removed once the newest holds all
its records (`manage erasures-superseded`), so usually one is left; a file
that holds records the newest lacks stays, and the run says how many it
kept. Run it every hour, beside the nightly dump, and copy the folder off
the machine with the dumps, as often:

```bash
0 3 * * *  cd /srv/korely-agent && ./deploy/backup.sh >> backup.log 2>&1
10 * * * * cd /srv/korely-agent && ./deploy/backup.sh erasures >> backup.log 2>&1
```

It is what a restore needs when the live database is lost with its disk:
the people erased after the dump are made erased again from these files, up
to when the newest was written. An erasure made after the newest file is in
none: with the hourly line that is an hour at most while the machine's disk
is there, and with the disk lost, as long as the copy off the machine is
old (a nightly copy: up to a day). After a loss the hourly export goes on
from whatever database the API finds, an empty one included; the files of
before stay beside it, since it does not hold their records, and a restore
makes them again. The export runs in the API's container: with the API
stopped, or an export a restore could not read, the run fails, says so on
stderr and keeps no file. A file that is no longer one of erasures at all
(damaged on the disk, or a copy cut short) is set aside as
`<name>.unreadable`, and the run fails saying so: no restore makes it again,
so put back a good copy of it, from off the machine, under its name.

`restore.sh` asks you to type `yes`, because it replaces the database; from a
script, `FORCE=1 ./deploy/restore.sh <dump>`. Give it a `korely-<time>.dump`:
it first reads the whole file (`pg_restore -f /dev/null`, in the db
container, which writes to no database), and a file pg_restore cannot read
whole, a file of erasures or a dump cut short, is refused before anything
is stopped or changed. It stops the API first, before
it reads the live database, so nothing written after that read is lost, and
starts it again at the end; `/health` then reads `working` off the last extraction
the dump holds until the next model call, with `seconds_ago` counted from it.

A restore brings back whatever the dump held, including people erased after
it was taken, and the erasure records go back with the database. So
`restore.sh` reads every erasure of the live database first
(`manage erasures-export`), and after the restore makes again the ones the
dump does not hold (`manage erasures-replay`), and then those of every file
of erasures in `BACKUP_DIR` (`./backups`), the oldest first, or of the file
`ERASURES_FILE` names alone (one copied back from off the machine): what the
live database held is in the restored data by then and counts as already
there, and so does what an earlier file made again.
Each is an end user, an agent namespace, a project, one memory. A memory erased with `?erase=true` is
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
again, and so is every account a claim had merged into it before the close,
when the dump holds it apart: its data and its keys go again (since 1.0.40;
a close made by 1.0.39 or earlier left no record of those accounts, and
they come back on their own, on trial, from a dump older than the merge);
when the account it went into was made after the dump,
`credentials-replay` makes that account again from its file, with its
login, and `erasures-replay` alone leaves the merged account on its own
(since 1.0.39). A
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
   (both personal data, the second with password hashes, the keys' hashes
   and webhook secrets in clear: deleted after step 4). `erasures-export`
   exits 3, with nothing written, on an install whose `SECRET_KEY` cannot
   be the one its erasures were made under: put that key back first.
2. Restore the dump (`pg_restore --clean --if-exists --no-owner`).
3. Bring the restored schema to this version: `alembic upgrade head`, then
   each other chain of migrations the image lists
   (`for chain in $(python -m korely_agent.db.chains --list); do alembic -c
   "$chain" upgrade head; done`; the list is empty on an install of your
   own), which the container's entrypoint runs on every start and a bare
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

With the live database gone, step 1 cannot be done: in step 4 make again
each `backups/korely-erasures-<time>.json` instead, the oldest first
(`python -m korely_agent.manage erasures-replay < backups/korely-erasures-<time>.json`),
and the credentials changed after the dump stay as the dump had them.

The credentials go back with the dump too: a key revoked after it would work
again, a key rotated after it would take its old secret again and refuse the
new one, an old password would let its owner in, a webhook would sign with a
secret rotated away. So `restore.sh` also reads the credentials of the live
database first (`manage credentials-export`) and after the restore makes
again what changed since the dump (`manage credentials-replay`). What ended
stays ended: a key revoked there is revoked, a key keeps no scope that was
taken from it there, an operator disabled there is disabled, and a webhook
endpoint deleted after the dump comes back switched off. The rest is as it
was when the file was written: a key moved there to another project is in
that project again (revoked, when that project was made after the dump),
and the password, the webhook secret and a key's secret are the ones in
force then (a rotated key's with its end, and its old secret only for the
overlap the rotation left it, as the old one).

So the file must be the one written just before this restore, never an
older one: replayed onto data that changed after it, an older file would
bring back a key's secret rotated away since, or an old password. Since
1.0.32 the file says when it was written, and `credentials-replay` refuses,
exit 2 and nothing changed, a file older than the newest credential the
data holds (a key made or rotated, an operator made or logged in, a webhook
endpoint made or changed, an account made). It cannot see a password reset
with `manage reset-password` and no login after it: do not replay a file
older than that by hand. The file is also signed with a key derived from
`SECRET_KEY` (or one in `SECRET_KEY_FALLBACKS`): a file changed by hand, or
written by another install, is refused, exit 3, and so is an unsigned file.
If `SECRET_KEY` changed since the file was written, put the old one in
`SECRET_KEY_FALLBACKS` and replay it again. The one unsigned file read is
one Korely 1.0.31 wrote, and only when asked: a dump and its file of
credentials made by 1.0.31, restored right after the upgrade, are replayed
with `python -m korely_agent.manage credentials-replay --unsigned-from-1.0.31
< credentials.json`, which says what it cannot check (when the file was
written, and by whom) and leaves a rotated key, and its scopes, as the dump
has them. With it, whoever wrote the file decides the passwords and webhook
secrets it holds, and an older file brings older ones back: use it only on
the file written just before this restore. On an install with no
`SECRET_KEY` nothing can be signed: the
replay reads the file with a warning
([Honest limits](limits.md#honest-limits)).

Nothing is reopened. And whatever the live database said, every dashboard
session and every OAuth grant ends, with the keys apps such as ChatGPT and
Claude got over OAuth: everybody signs in again and each app connects again,
which is the whole cost. The command then prints, for each account, the keys
that work and the project each reads and writes, the webhook endpoints that
are on and the operators who can log in, with their dates: check them
against what you expect.

If the live database cannot be read the restore stops, with nothing restored
and the API stopped (`docker compose start api` puts it back as it was);
`SKIP_ERASURES=1` is for when it is gone: the erasures are then made again
from the files of erasures, whose number and newest the script names, and
none at all when there is no such file (it says so). The erasures made after
the newest,
and every credential changed after the dump (a key revoked or rotated, a
password reset), are not made again; the sessions and grants still end, and the list
is what to check. If making
the erasures or the credentials again fails, the API stays stopped and the
file is kept in the install's folder (`erasures-<time>.json`, `credentials-<time>.json`,
readable by you alone: the second holds password hashes, the keys' hashes and
webhook secrets, as a dump does), with the command to run printed. A `pg_restore` that reports
errors leaves the API stopped as well, keeps both files and prints how to
finish the restore or start over. So does any stop once `pg_restore` has
begun, an interrupt included (Ctrl-C, a closed terminal or SSH session):
from then on those two files are the only copy of what the live database did
since the dump. If a restore was interrupted, look in the install's folder
for `erasures-<time>.json` and `credentials-<time>.json` and run the two
replays with them before `docker compose start api`, then delete them. While
they are there, `restore.sh` refuses to run, before it stops anything: the
database is no longer the live one, and it would read it as such. To restore
a dump instead (another one, or the same again), name them, and it reads
them in place of the database and removes them once made again:

```bash
KEPT_ERASURES=./erasures-<time>.json KEPT_CREDENTIALS=./credentials-<time>.json \
  ./deploy/restore.sh backups/korely-<time>.dump
```

Name only the ones that are there: a replay that failed keeps its own file
alone. `.gitignore` keeps them out of git.

A file of erasures in `backups/` the replay reads nothing of (damaged since
the last hourly run, or written by a newer Korely) keeps the API stopped
too, and the script names it with what to do: replay a good copy of it from
off the machine, or that version for a newer one; running the same
command again changes nothing. An erasure of an end user or an agent
namespace made again takes what was there when it was made: everything of
theirs written up to that moment goes, in every place an erasure reaches,
and what was written after it, by any key, stays (someone who wrote again
after the erasure, in a database that lost it); the replay counts those
erasures apart. A row the product writes again in place is dated by that
write (a name by when it was last stated, a read mark by its last read),
and a fact is decided by the memories it comes from: a fact a memory of
theirs written after the moment restated stays, without the memories of before
among its sources and with the words and dates of its row of before, and
one a fact that goes had closed comes back open as a new row, unless what
stays still says it is over. What a fact that goes had taught (a name, a
relation of the vocabulary no fact that stays is stored under, with its
phrases) goes with it. A memory of before goes whole, also when edited
since, and a fact posted again by itself, or a name declared again, in the
same words since, is still the row of before, and goes. An agent
namespace's erasure takes every fact of the namespace, also one another
namespace's memory restated since, as when it was made. The moment is when
the erasure ended,
its record's time since 1.0.31; a record of before is dated when its
erasure began.

## What the worker trims

Once an hour the worker deletes its own records past their window, each record
on its own: one that fails is logged and the others are still trimmed. Each
window is a line in `.env`, in days, and `0` (or less) keeps them forever. A
window longer than a century (36,500 days) is read as a century, and the log
says so once: longer than any of these records can be.

| | default | what it is |
|---|---|---|
| `AUDIT_RETENTION_DAYS` | 400 | who touched which end user's data, and who signed in to the dashboard from which address |
| `LLM_USAGE_RETENTION_DAYS` | 400 | every model call, its tokens and the agent that made it, never the end user |
| `WEBHOOK_DELIVERY_RETENTION_DAYS` | 30 | finished webhook deliveries; one still waiting for a retry is never removed |
| `READ_MARKS_RETENTION_DAYS` | 90 | which fact an agent that named itself (`reader`) read, and when, counted from its last read; a mark whose change was shown goes at the next trim whatever this says |

Finished `/v1/batch` jobs go after seven days, and that window is not a line
in `.env`. A job's payload is a copy of every memory it imported, and once the
import is over the only thing anybody reads is the job's counts and errors;
the first three above keep evidence somebody may decide to keep forever (the
read marks only what an agent is to be told), this keeps a second copy of
people's data. A job still queued, running or waiting for a
retry is never removed.

How much each job takes per tick is there too (`DRAIN_LIMIT`,
`BATCH_DRAIN_LIMIT`, `WEBHOOK_DRAIN_LIMIT`, `VECTOR_DRAIN_LIMIT`), with what
each one bounds, in `.env.example`.

## Windows of long memories (MEMORY_WINDOWS)

Off by default. A memory has one vector, computed from its beginning, so a
question about one sentence deep inside a long memory can find other
memories first. With `MEMORY_WINDOWS=true` in `.env` (since 1.0.33):

- a write of a memory longer than one window (100 words; in a script written
  without spaces, 100 pairs of letters) also stores its windows, a new one
  every 50 words: where each begins and ends in the text, never a copy of the
  words; a write of a shorter memory stores one row that says it has none (no
  offsets, no vector), one row more per write;
- the worker's job `windows` computes their vectors, one per window, never
  on a request: at most 20 a pass, and the next pass
  30 seconds after the end of the one before, whatever is left: up to 2,400
  an hour, fewer when the passes themselves take long. With the
  local embedder that is this machine's CPU; with
  `EMBEDDING_PROVIDER=gemini` each window is one call to Google, billed and
  through the same gate as every vector;
- a search ranks a memory by the better of its own vector and its best
  window's (its `score` is that one), and the context block shows a memory
  too long for its room by the whole sentences of that window.

After turning it on, make the windows of the memories stored before:

```bash
docker compose run --rm api python -m korely_agent.manage memory-windows           # what is there
docker compose run --rm api python -m korely_agent.manage memory-windows --apply   # make them
```

`--apply` makes the windows of each live memory that has none, `--batch`
memories (200) per transaction, and computes no vector: the worker's job
does, at its pace, and the command says the least time that takes (the
pace's ceiling; the time of the vectors comes on top). Each memory is
held while its windows are made, so an edit or a forget of it at the same
time waits for that transaction and then wins: usually milliseconds (no
vector is computed there), under a second in the worst case measured, a
batch of 200 memories of 16,000 characters (rows held 0.83 s, a PATCH
waited 0.68 s); a smaller `--batch` holds fewer rows at a time. A memory an
edit holds is skipped, and the next run takes it if it still needs windows.
A memory too short for a window gets one row that says so (no offsets, no
vector): the next run does not read it again until its text changes. Stopped anywhere, it goes on from where it was;
`--limit` stops after that many memories. It refuses while the setting is
off (no search reads the windows, the worker computes none), unless
`--while-off`.

What it costs, in vectors: one window per 50 words beyond the first 100. A
note of 130 words has 2 (on one team's 488 notes, 366 had windows, 866 in
all); the longest memory (16,000 characters) has 39 in Latin words of seven
letters and 159 in ideographs. The pace is fixed, not a setting. In
time: on the Mac where it was built, in a container, a window took 0.08 s
with the machine quiet and 0.6 to 3.3 s with other work on it; the time on a
server is not measured. In space: see the limits ([limits](limits.md)).

Turning it off again: no search reads the windows and the worker computes
none, and the blocks are those of before. The windows stay stored, every
edit, forget and erasure still removes them, and `recompute-vectors` still
recomputes them after a change of the embedding model; `manage
memory-windows` shows how many there are.

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

Going back to a version before 1.0.31, take the hourly `backup.sh
erasures` line out of the crontab first: `backup.sh` before 1.0.31 does not
know the argument, makes a whole dump at every run, and its rotation keeps
the last 14, so within 14 hours the nightly dumps are gone. The
[CHANGELOG](../../CHANGELOG.md) entry of each release names the migration
to undo, and with which image.

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
