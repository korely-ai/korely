# Install

A machine with nothing on it, Debian or Ubuntu, two cores and four gigabytes
of memory: both installs below ran on one (2 vCPU, about 3.8 GB usable), and
nothing has been measured on less. Since 0.1.19 the service carries the model
that computes the vectors and uses about 0.7 GB of memory with it (0.77 GB the
highest measured, under sixteen parallel clients). From 0.2.0 the API runs
under a ceiling of 1.5 GB, twice that peak (`API_MEMORY_LIMIT` in `.env`
changes it).
Measured on a Hetzner CPX22 with Debian 13 on 28 September 2026: 1m 39s from
an empty box to a store that has learned its first fact, scripted, on
0.1.14-dev, with an application image of 627 MB built in 33 s; 3m 41s on the
same kind of machine by an agent that had never seen the code, following only
the public documentation, on 0.1.13. Both are older than
0.1.19, since when the image carries the model that computes the vectors
here, about 1.2 GB the first build downloads (step 7): add that download to
them. [Benchmarks](../benchmarks.md#install-time) has both, and a laptop's
quick start timed on 1.0.1-dev. On a Mac it works the same way with Docker
Desktop, and [On a laptop](#on-a-laptop) has the one thing that differs, and
the one command that does all of this for you.

**1. Docker, if it is not there yet.**

```bash
curl -fsSL https://get.docker.com | sh
```

That script is for Linux. On macOS, install Docker Desktop instead.

**2. The source.**

```bash
git clone https://github.com/korely-ai/korely korely-agent
```

The folder is named `korely-agent` on purpose, as it was before the
repository took the name `korely`: Docker Compose names the containers and
the volumes after the folder, and `korely` is the Compose project install.sh
makes unless told `--project`. Two installs under one project name on the
same machine are the same containers and the same volumes.

If you were handed a tarball instead, unpack it:

```bash
tar xzf korely-agent-<version>.tar.gz
```

**3. Settings.** Copy the example and set three things.

```bash
cd korely-agent
cp .env.example .env
chmod 600 .env     # it will hold passwords and keys: yours to read alone
```

| | |
|---|---|
| `POSTGRES_PASSWORD` | anything long and random. `openssl rand -hex 16` |
| `SECRET_KEY` | likewise, `openssl rand -hex 32`. It encrypts the provider, key and models you choose on the screen (without it that page refuses to save rather than storing a key in clear text) and the secret of an OAuth client that registers with one, and it keys the pseudonym an erasure leaves in place of the end user's id. Keep it: with a new one the screen's choice cannot be read, so the memories wait unless `.env` names a provider, until it is saved again; such an OAuth client is unknown until it registers again; and the earlier pseudonyms are unfindable by id |
| `DOMAIN` | a name already pointing at this machine. No DNS name yet: your IP with dashes plus `.nip.io`, see just below. Leave it empty only on a laptop |

A server with no name yet gets one from nip.io, and it has to be decided now,
before the first start, because it is the name the certificate is asked for:
`203.0.113.7` becomes `DOMAIN=203-0-113-7.nip.io`. That resolves to your IP, so
Caddy gets a real certificate for it and every browser and client is happy.
Without a `DOMAIN` Caddy serves one site called `localhost`, and a browser
pointed at the bare IP fails the TLS handshake before reaching anything: an IP
address carries no name to match.

Write the dashes. The dotted form, `203.0.113.7.nip.io`, resolves to the same
address and works in every browser, and then fails from Python on a Mac with
`TLSV1_ALERT_INTERNAL_ERROR` and no explanation from either side. The reason is
that the Python shipped with macOS is built against LibreSSL 2.8.3, which
decides a name beginning with four numbers is an IP address and, since RFC 6066
forbids sending an IP as the server name, sends no name at all. Caddy then does
not know which certificate you wanted and hangs up. Measured, asking a server
that logs what it receives: the dotted form arrives as nothing, the dashed
form arrives intact, and on a current Python both arrive intact.
It is not your install and not your client. It is four numbers and a dot.

No model key is needed here. The first login asks where your memories are
written and for that provider's key, which is the point: the key is the
customer's, and they should not need an SSH session to change it. Decide now
about `EMBEDDING_PROVIDER` (step 7 says what it is), because the image is
built with it.

If something on the machine already holds a port this wants, `.env` moves it:
`PORT` for the API on loopback (8000), `HTTPS_PORT` and `HTTP_PORT` for Caddy
(443 and 80; a real certificate needs those two on their defaults). The
address the install gives ChatGPT, Claude and every other OAuth or MCP
client to come back to follows: `https://DOMAIN`, with `HTTPS_PORT` when it
is not 443, `https://localhost` with `DOMAIN` empty. Nothing to set, unless
a proxy of yours serves the install at another address (see "Your own proxy
in front"). And the
compose project is named after the directory: two installs on one machine
need two directory names, or `COMPOSE_PROJECT_NAME` in each `.env`, because the
volumes carry that name and `docker compose down -v` in one would delete the
other's data.

**4. Start it.**

```bash
docker compose up -d
```

Three containers: Postgres, the application, and Caddy in front doing TLS. A
fourth runs for a moment at each start, to prepare the export directory
(`./facts`, see [The facts, as files](facts-as-files.md)), and
exits. The first run builds the image on this machine, which is the slow part.

**5. Two credentials, each shown once.**

```bash
docker compose exec api python -m korely_agent.manage create-tenant --name "Acme"
# prints a tenant id and an API key: the key is what your agents use

docker compose exec api python -m korely_agent.manage create-operator \
  --tenant <the tenant id from above> --username you
# prints a password: that is your login to the screen
```

Neither is shown again, and there is no default password to change
afterwards, which is the reason they work this way. The model provider, its
key and the models are the whole install's: with one account its
administrators set them; once the install holds several accounts (an agency
with a tenant per client) only the person named with
`manage grant-install-admin --username <name>` does, signed in to the
screen, never an API key, and each account's trail records the change. The
first login an install ever gets is that person. A lost one is replaced
rather than recovered: `manage reset-password --username you` prints a new
password and ends that login's sessions, and
`manage issue-key --tenant <id> --name "support bot"` mints another key.

**6. Open it.** `https://your-domain` and sign in with the operator username
and password. On a laptop, read the next section first.

## On a laptop

**The short way: one command, or the Mac download.** On a Mac or a Linux
laptop with Docker running (on Windows, inside WSL),

```bash
curl -fsSL https://raw.githubusercontent.com/korely-ai/korely/main/install.sh | sh
```

does the steps above for you in a folder of its own, `./korely` unless
`--dir` says otherwise, and nowhere else on the disk: it checks Docker,
downloads the release's files and checks each against the sha256 it carries,
writes `.env` readable by you alone with two random secrets, starts the
database and the API without Caddy, makes the account and the login, and
prints once the address (`http://127.0.0.1:8000`), the login, the API key and
the `claude mcp add` line. `curl -fsSLO` the same address and `sh install.sh
--help` to read it before it runs. `./korely` in that folder is the one
command for what comes next: `start`, `stop`, `status`, `logs`,
`manage <command>` and `uninstall --yes`. Your model key, optionally:
`curl ... | KORELY_PROVIDER=scaleway sh` (or `gemini`, `mistral`, `openai`,
`openrouter`) asks for it on the terminal without showing it; without one the
dashboard's first screen asks, as below.

Whoever would rather not open a terminal downloads
[Korely-Installer.dmg](https://github.com/korely-ai/korely/releases/latest/download/Korely-Installer.dmg)
from the GitHub release (macOS 13 or later, Apple silicon and Intel, signed
and notarized). Korely Installer checks Docker Desktop, asks where to install
and, optionally, for the model provider and its key, then runs the same
install.sh in Terminal, so every step is seen, and opens the dashboard at the
end.

The rest of this section is for an install made by hand, as above, with
`DOMAIN` empty.

With `DOMAIN` empty, Caddy serves `https://localhost` with a certificate from
its own local authority, made inside the container. Your Mac has never heard of
that authority, so a browser warns, `curl` says `unable to get local issuer
certificate`, and the Python client says `CERTIFICATE_VERIFY_FAILED`. Nothing is
broken; nothing on the Mac has been told to trust it. Two ways through.

**To try it, use the loopback address.** The API serves the dashboard and
everything else on plain HTTP at `http://127.0.0.1:8000` (or the `PORT` you
set), reachable only from this machine. That is what to open in the browser
and what to give a client as `base_url` while you evaluate.

**To use `https://localhost`, trust Caddy's authority.** Take its root out of
the container once:

```bash
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > caddy-root.crt
```

and hand it to whatever connects: `curl --cacert caddy-root.crt`; for the
Python client, `Korely(base_url="https://localhost", ca_file="caddy-root.crt")`
or `KORELY_CA_FILE=caddy-root.crt` (korely-memory 0.1.17 or later), which also
works on the Python that ships with macOS, 3.9 on LibreSSL, where
`SSL_CERT_FILE` does not; `SSL_CERT_FILE=caddy-root.crt` for other Python
programs on 3.10 or later; `NODE_EXTRA_CA_CERTS=caddy-root.crt` for Node and
the JavaScript client; and for the browsers
`sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain caddy-root.crt`
(remove it with `sudo security delete-certificate -c "Caddy Local Authority"`
when you are done). The root lives in the `caddy_data` volume, so
`docker compose down -v` makes a new one and you trust it again.

Caddy's log says `certificate installed properly in linux trusts`. That is the
container's own trust store, not your Mac's.

## If the address does not answer at all

Not a page, not a warning: the browser cannot connect and `curl` does not finish
the handshake. The application is usually fine, and you can check from inside
the machine with `curl 127.0.0.1:8000/health`. What is missing is the
certificate, and the reason is only in one place:

```bash
docker compose logs caddy
```

Two causes account for almost all of it. Either the name does not point at this
machine yet, or you have asked Let's Encrypt for the same name too often:

```
HTTP 429 urn:ietf:params:acme:error:rateLimited - too many certificates (5)
already issued for this exact set of identifiers in the last 168h0m0s
```

Five per name per week. It is easy to reach while evaluating and almost
impossible to reach afterwards, which is the wrong way round: rebuilding the
same machine several times in a week is exactly what trying a product looks
like, and `docker compose down -v` takes the `caddy_data` volume with it, so
the certificate it already had is thrown away and a new one is asked for.

Three ways out, and the one you want depends on why you are rebuilding.

**Keep the volume.** `docker compose down` without `-v` leaves the certificate
in place, and the next start reuses it. This is the answer if you are restarting
the same install.

**Change the name.** `g2.<your-ip-with-dashes>.nip.io` resolves to the same
address and counts as a different name, so it has its own five. This is the
answer if you want a real, trusted certificate on every rebuild.

**Use the staging directory.** Put this in your `.env` and the whole path still
runs, on limits you will not reach:

```
ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory
```

The certificate is untrusted, so the browser objects and `curl` wants `-k`.
That is the right trade while evaluating and the wrong one afterwards: take the
line out and restart, and a real certificate is fetched.

**7. Where your memories are written.** The first time you log in, the
dashboard asks two questions before anything else:

1. **Europe or Global.** Europe writes with gpt-oss-120b on Scaleway, on
   servers in Paris. Global writes with Gemini 3.1 Flash-Lite, by Google. Both
   were measured as the whole writer (facts, judge and vocabulary) on LoCoMo:
   129 right answers out of 152 each.
2. **Your key** for that provider. It is checked with the provider, which also
   returns the models this key can use, and it is stored encrypted.

**Use another provider**, under the key, opens the other choices: in Europe,
Mistral or Scaleway; in Global, OpenAI, Claude, Gemini, Grok or OpenRouter. The
model comes preselected: the writer we measured where we measured one, a fast
model marked as not measured yet on the others. You can pick any other from
the list: OpenRouter's starts with the week's most used. Settings, Writing
model, Change, asks the same questions again.

**One provider per region, one region per project.** The provider you save
last is the install's: every project writes with it unless you say otherwise.
Saving a provider of the other region keeps both, and a project can then
choose its region on the Projects page (Europe or Global), for example to keep
one customer's memories in Paris. Its memories are written by that region's
provider and never by the other one: a region with no provider, or whose
provider refuses (credit run out, too many requests), keeps its memories
waiting while the rest go on. A change of region applies to the next
memories, and no fact is extracted again. What travels with each new write
goes to the new region's provider, wherever it was written: the end user's
earlier memories read next to it and the earlier facts the judge compares
it with (the list below). `/v1/ping` answers `processing_region` for the
project of the key.

An install that names its provider in `.env` (`GOOGLE_API_KEY`, or
`EXTRACTION_PROVIDER=mistral` with `MISTRAL_API_KEY`) never sees these
questions. **Skip for now** opens the dashboard without a model, the mode
described at [the top of the Time page](time.md#or-say-it-yourself-and-leave-the-model-out-of-the-write-path). `python3 scripts/choose_models.py` writes those lines for you from a
terminal.

A memory written before the key waits for it: stored and searchable at once,
and mined by the worker within seconds of the key arriving, billed to that key
like any other.

The facts are the only thing here that calls a model (and the vectors, if you
choose Gemini's), and the call is billed to that key, which is why it is yours
and not ours.

Say the consequence out loud, because it is the question a buyer asks and the
one a careless page leaves them to work out: **text leaves this machine**, to
the provider whose key you pasted and to nobody else. Precisely:

- a memory's text, on the write, to be read for facts, with the end user's id
  as your agent sent it (`user_id`: the facts about that person are written
  under it, so prefer an opaque id to an email address or a name);
- with it, up to ten of the same end user's earlier memories in the same
  project and agent namespace, 300 characters each, read to resolve "she" or
  "that plan" and never mined: so a memory's text goes out again with each of
  that person's next ten writes;
- the facts read from it, to the judge, which reads a new fact next to the
  earlier facts about the same thing, so a fact's words travel again whenever
  a later one might replace it;
- the phrase of a relation the end user has not used before, with a value, to
  place it in that end user's vocabulary. The vocabulary keeps one value per
  relation as its example: it goes again with the same end user's later facts
  of that relation, to the judge (for a relation that holds one value at a
  time) and to the vocabulary's own questions, never with another end user's,
  until an erasure of the facts it came from replaces it;
- with `EMBEDDING_PROVIDER=gemini` only, the texts of the vectors too: each
  memory's and each fact's once more, and the words of each search and context
  query. With the default, `local`, those never leave the machine.

What stays here is everything they become: the memories, the facts, the
vectors, the index. Nothing is sent to us, ever, and there is nothing here that
phones home.

If that is unacceptable, the provider is your choice, not a dependency. Pick
Europe at the first login and the reading, the judge and the vocabulary go to
Scaleway (or Mistral, as another provider), on servers in the EU. To send
them to a server on your own network instead, inside your building, put
`EXTRACTION_PROVIDER=mistral`, `MISTRAL_API_KEY` and `MISTRAL_API_BASE` (the
address of anything that speaks the same API) in `.env`, and then, if a
provider was ever chosen on the screen, press **Use what .env says instead**
in Settings, Writing model: a provider saved on the screen wins over `.env`,
and until that button is pressed every memory still goes to it. `GET
/admin/models` says which one writes: `source` is `env` for a writer that
comes from `.env` or from the Google key pasted in Settings, and `screen` for
a provider chosen on the Models page. Its `default_region` is the region a
project that never chose one writes in: the region of the writer in force,
the provider saved last on the screen, else the one `.env` sets (`null` if
neither, or if `.env` points to an address no known provider uses).

The vectors are a separate choice, `EMBEDDING_PROVIDER` in `.env`:

- `local`, the default since 0.1.19, computes them on this machine with
  EmbeddingGemma-300m. No key is needed, and a memory's text and your queries
  never leave the machine to be searched: with Europe chosen at the first
  login, nothing goes outside the EU. Our LoCoMo and LongMemEval numbers were
  measured with it. The image carries the model, about 1.2 GB, and the service
  then uses about 0.7 GB of memory. It computes one vector at a time, and the
  accounts take turns: an account has four places there per API process
  (two processes, eight), which its searches, its contexts and the writes
  of its requests share, over REST and MCP together, each for the time its
  vector takes. A search or a context past them waits its turn, holding no
  thread another account needs, for at most 20 s, then waits at most 20 s
  more for the embedder itself; a write past them does not wait. Only a
  read that waited that long is answered so: `POST /v1/memories/search`
  with 503 `search_unavailable` and `Retry-After` (retry; it takes no query
  of the month), `GET /v1/context` with 200 and `degraded: true`,
  `degraded_parts: ["memories"]` (the facts, ranked without the question),
  the MCP's `korely_search` with `search_unavailable`. A write past them is
  stored (201) and its vector is computed by the worker a little later.
- `gemini` asks Google with `GOOGLE_API_KEY`, for a machine that should not
  carry the model: the image is built without it. With no Google key,
  memories are stored and facts are mined, and search by meaning is off:
  `/v1/memories/search` answers 503 `search_unavailable`, and the context says
  it is degraded. The first-login screen warns you when that is the case.

Decide before the first `docker compose up`, because the image is built with
it. Changing it later means building again, `docker compose up -d --build`,
and computing every stored vector again, because two models' vectors cannot be
compared:

```bash
docker compose run --rm api python -m korely_agent.manage recompute-vectors --apply
```

The install remembers which model computed its vectors and does not start on
another model's: the log says which one it found, and these two ways out.

A memory has one vector, computed from its beginning: its first 8,000
characters, of which the local model reads at most 2,048 tokens. A search by
meaning finds a longer memory by what it says first; its facts are mined from
the whole text.

What the product cannot do is mine facts out of a sentence without a model
reading the sentence.

**8. Point something at it.**

```bash
pip install korely-memory
```

```python
from korely_memory import Korely

korely = Korely(api_key="kor_self_...", base_url="https://your-domain")
written = korely.add("Customer 1001 prefers to be contacted by email.",
                     user_id="c1001", agent_id="support-bot",
                     timestamp="2026-03-15")
print(written.id, written.status)      # mem_... processing
```

Facts are mined by a background worker a few seconds after a write, and
`written.status` says `processing` until they are there. An empty `facts` on a
reply is not the same as no facts: it usually means not yet.

The client hands you objects, not dictionaries. `written["id"]` raises
`TypeError: 'Memory' object is not subscriptable`, which is worth knowing
before you meet it. Needs `korely-memory` 0.1.12 or later: before that the
field came off the wire and was dropped.

**`base_url` is the whole point of this step.** The same package talks to our
hosted service and to your machine, and without an address it picks ours.

Two things now stand between you and that mistake. Keys minted here begin with
`kor_self_`, not `kor_live_`, so the two cannot be confused by eye, in a log, or
in a screenshot pasted into a chat. And the client refuses the mismatched pair
before it sends anything: a `kor_self_` key with no address is an error at the
constructor, not a 401 after the memory has travelled. Neither replaces setting
the address. Three ways to give it yours, and they win in this order:

```python
Korely(api_key="kor_self_...", base_url="https://your-domain")   # an argument
```

```bash
export KORELY_API_KEY=kor_self_...          # or two environment variables,
export KORELY_BASE_URL=https://your-domain  # and then Korely() with none
```

```bash
korely init --base-url https://your-domain --api-key kor_self_...
# writes ~/.korely/config.json, which the library reads by itself afterwards
```

An argument is a decision, a variable is a setting, the file is what you said
once. Needs `korely-memory` 0.1.11 or later: before that the last two were read
by the command line tool and not by the library, so a self-hosted key was sent
to our service, refused with a 401, and the memory had already travelled.
