<p align="center">
  <img src="dashboard/public/korely-mark.svg" width="72" height="72" alt="Korely logo: a blue pyramid">
</p>

<h1 align="center">Korely Agent</h1>

<p align="center">
  <strong>Memory for AI agents that can answer: why did it say that in March?</strong><br>
  Self-hosted, Postgres-only, Apache 2.0.
</p>

<p align="center">
  <a href="docs/guide/README.md">Documentation</a> •
  <a href="docs/benchmarks.md">Benchmarks</a> •
  <a href="https://korely.ai">Korely Cloud</a> •
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/github/v/tag/korely-ai/korely?label=release" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License: Apache 2.0"></a>
  <a href="https://pypi.org/project/korely-memory/"><img src="https://img.shields.io/pypi/v/korely-memory?label=python%20client" alt="Python client on PyPI"></a>
</p>

Korely Agent gives your AI agents a memory that keeps its history. When
something stops being true, the old fact is closed instead of overwritten: your
agent knows what is true now, and you can see what was true, and what it knew,
at any moment in the past. It runs on your servers, with your Postgres and your
model key.

<p align="center">
  <a href="#one-memory-every-agent">One memory</a> ·
  <a href="#why-korely">Why Korely</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#facts-with-dates-why-did-it-say-that-in-march">Facts with dates</a> ·
  <a href="#benchmarks">Benchmarks</a> ·
  <a href="#connect-your-agent">Connect your agent</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#your-data">Your data</a> ·
  <a href="#honest-limits">Limits</a>
</p>

## One memory, every agent

![One memory, every agent: Claude Code, Cursor, ChatGPT, Gemini CLI, Codex and Hermes connect over MCP, and your app with the Python or JavaScript SDK or REST, to one Korely Agent, one memory with dates in your Postgres. What one agent saves, every other one can read.](docs/assets/shared-memory.svg)

Any agent that speaks MCP, and your own code, reads and writes the same memory.
Here Claude Code saves, and Gemini CLI, which never saw that conversation,
answers from Korely's dated facts:

[![Two terminals. Top, Claude Code: the user says customer 4821 has been on the Priority Support plan since 15 March 2026, then that they moved to the Basic plan on 20 June 2026; Claude Code saves both to Korely. Bottom, Gemini CLI, a different agent: asked what plan customer 4821 is on, since when, and what it was before, it calls korely_get_facts and answers Basic since 20 June 2026, Priority Support from 15 March to 20 June 2026. The wait while Korely writes the facts is shortened, and a badge says by how much.](docs/assets/shared-memory.gif)](docs/assets/shared-memory.mp4)

---

## Why Korely

- **It remembers how things change.** When something stops being true, Korely
  closes the old fact instead of overwriting it. Your agent knows what is true
  now and what was true before, and you can check what it knew at any moment
  in the past.
- **You can see why it said that.** Every fact links back to the memory it came
  from, and every read of a person's data leaves an entry in the audit trail.
- **One database you already know.** Korely runs on Postgres with pgvector, both
  open source. No separate vector database, graph database, queue or cache to
  run.
- **Built for personal data.** Data stays on your servers and you use your own
  model key, with a model hosted in Europe if you want one. An erasure deletes
  the person's data and leaves a record that proves it, and the logs never
  contain what people said.
- **Works with the tools you use.** Claude Code, Cursor, Gemini CLI, Codex,
  ChatGPT and any MCP client, plus Python and JavaScript SDKs and a REST API.
- **Measured in public.** Benchmark scores below, with the scripts to run them
  again.

**Not the right fit if** you want a memory that rewrites how your agent behaves
as it learns: Korely stores facts about people and things, it does not change
your agent's instructions. And for one user with no questions about the past, a
plain file is simpler.

---

## Quick start

![What the installer starts on your machine: in Docker, Korely Agent (the api: dashboard, API and MCP on port 8000) talks SQL to Postgres with pgvector (db), whose data lives on the volume pgdata; facts-dir runs once to prepare the ./facts folder, then exits. Your browser and your agents reach Korely at 127.0.0.1:8000. The model provider, with your key, is called only to write facts; searches never call it, vectors are computed on your machine by default.](docs/assets/what-runs.svg)

[![A real run of the one-line install on a Mac: the line pasted into a terminal, the installer printing the dashboard address, the login, the password, the API key and the Claude Code line, then the browser signing in. Played in real time; the image was already on this machine, so the first-time download of about 2.5 GB (a few minutes) is not shown. The secrets on screen belonged to a throwaway install.](docs/assets/install.gif)](docs/assets/install.mp4)

**What the installer prints, line by line.** Two programs run: Korely and its
database. The other lines are Docker setting them up.

| Name | What it is |
|---|---|
| `korely-api-1` | Korely: the dashboard, the API and the MCP server, on port 8000. |
| `korely-db-1` | The database: Postgres with pgvector. |
| `korely_pgdata` | The disk the database keeps its data on. It stays when Korely is stopped, and goes with `./korely uninstall --yes`. |
| `korely_default` | The private network the two programs talk over. |
| `korely-facts-dir-1` | A one-off helper: it lets Korely write to the `facts` folder, then stops. "Exited" is normal. |

1. **Install [Docker Desktop](https://docs.docker.com/get-docker/)** and open
   it (on Windows, use it inside WSL2 with Docker: tested). Korely needs 4 GB of free memory.
2. **Download and run the installer.** Open a terminal (on a Mac: the Terminal
   app), paste this line and press Enter:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/korely-ai/korely/main/install.sh | sh
   ```

   `curl` downloads the installer and `| sh` runs it. The installer works in a new folder,
   `korely`, and nowhere else: it downloads Korely (about 2.4 GB on disk the
   first time), starts it and creates your account.
3. **Open the address it prints**, [http://127.0.0.1:8000](http://127.0.0.1:8000),
   and sign in with the login and password it prints. It also prints an API key:
   that is what your agents use. The password and the key are shown once.

**On a Mac, without a terminal:** download
[Korely Installer](https://github.com/korely-ai/korely/releases/latest/download/Korely-Installer.dmg),
open it and double-click **Korely Installer**. It checks Docker Desktop, asks
where to install and, if you want, your model key, then installs in a Terminal
window you can watch, and opens your dashboard at the end. It runs the same
installer as the line above.

Running the line again starts what is there and creates nothing new. To read
the installer before running it: `curl -fsSLO …/install.sh`, then
`sh install.sh --help`.

**Your model key.** The one model call is the writer that turns memories into
facts, and it is billed to you. Name your provider and the installer asks for
the key without showing it:

```bash
curl -fsSL https://raw.githubusercontent.com/korely-ai/korely/main/install.sh | KORELY_PROVIDER=gemini sh
```

Gemini, Scaleway, Mistral, OpenAI and OpenRouter work; Scaleway writes in
Europe, in Paris. Without a key the install completes and the dashboard's first
screen asks for one (**Skip for now** works too: memories are stored and
searched, and facts arrive when your agents write them).

To stop it, start it again or remove it, use `./korely` in that folder
([Install](docs/guide/install.md) has the commands).

**Without installing anything:** the same code runs as Korely Cloud, with a free
plan, at [korely.ai](https://korely.ai).
**On a server, with your domain and a certificate:** [Install](docs/guide/install.md).

> **Using Claude Code, Cursor or another coding agent?** Connect it over MCP
> with the key the installer printed:
>
> ```bash
> read -rs KORELY_API_KEY    # paste the key, it is not shown
> claude mcp add --transport http korely http://127.0.0.1:8000/mcp \
>   --header "Authorization: Bearer $KORELY_API_KEY"
> ```
>
> Your agent can then save a memory, search its memories, and read what is
> true about a person, now or on any past day.
> [Connect your agent](#connect-your-agent) has Cursor, ChatGPT and the SDKs.

---

## Facts with dates: why did it say that in March?

A support agent tells customer 4821 they have priority support. That is March.
In June they move to a cheaper plan. Today they are angry, quoting your bot back
at you. Was the bot wrong, or right at the time?

![Customer 4821 on a timeline: the Priority Support plan from 15 March 2026, closed on 20 June 2026 and not deleted; the Basic plan from 20 June 2026, still open. A read as of 1 April 2026 returns Priority Support plan, a read today returns Basic plan. A second clock, known_as_of, reads what the store knew on a date.](docs/assets/facts-with-dates.svg)

Claude Code, in a new session with no memory of its own, asked about today and about April:

[![Claude Code using Korely as its memory over MCP. In a first session the user says customer 4821 has been on the Priority Support plan since 15 March 2026, then that they moved to the Basic plan on 20 June 2026; each time the agent saves it to Korely and confirms. The user starts a new Claude Code session, which has no memory of its own. Asked what plan customer 4821 is on, the agent reads Korely and answers Basic; asked about April, it answers Priority Support. The agent's thinking time is sped up, and a badge says by how much.](docs/assets/agent-memory.gif)](docs/assets/agent-memory.mp4)

The same history in the dashboard, with the clock dragged back to 1 April:

[![A 30-second tour: customer c4821 moved from the Priority Support plan to the Basic plan on 20 June 2026. The facts page shows the old plan ended on 2026-06-20, closed rather than overwritten; on the memory graph the clock is dragged back, and as of 1 April 2026 the store answers Priority Support plan.](docs/assets/demo.gif)](docs/assets/demo.mp4)

<details>
<summary><b>The commands, with curl</b></summary>

[![The customer 4821 story with curl and no model: one fact is written (Priority Support plan from 2026-03-15), closed with /forget on 2026-06-20, and replaced by Basic plan from 2026-06-20. GET /v1/facts with as_of=2026-04-01 returns Priority Support plan; without as_of it returns Basic plan.](docs/assets/story-4821.gif)](docs/assets/story-4821.mp4)

Two writes, months apart:

```bash
curl -X POST https://your-domain/v1/memories \
  -H "Authorization: Bearer kor_self_..." -H "Content-Type: application/json" \
  -d '{"content":"Customer 4821 is on the Priority Support plan.","user_id":"c4821","timestamp":"2026-03-15"}'

curl -X POST https://your-domain/v1/memories \
  -H "Authorization: Bearer kor_self_..." -H "Content-Type: application/json" \
  -d '{"content":"Customer 4821 moved to the Basic plan.","user_id":"c4821","timestamp":"2026-06-20"}'
```

What the store held on the day of the conversation:

```bash
curl "https://your-domain/v1/facts?user_id=c4821&as_of=2026-04-01" \
  -H "Authorization: Bearer kor_self_..."
```

```
c4821 -subscribes_to-> Priority Support plan
```

</details>

The bot was right at the time, and that is a row, not a reconstruction. Nobody
wrote the second fact as an update to the first: a model read the second
sentence, saw it was about the same thing, and the older fact was closed on
20 June. `include_invalidated=true` returns both rows with their dates. (The
relation's name is the word the model chose, so yours may read `is_on`: the
example is about the dates.)

There are two clocks. `as_of` asks what was true on a date; `known_as_of` asks
what the store knew on a date, which differs when a change is told late. And
when a model in the write path is not acceptable, an agent can write and close
facts itself (`POST /v1/facts`, `POST /v1/facts/{id}/forget` with the date it
stopped being true), with no model involved.
[Time: what was true, and what the store knew](docs/guide/time.md) has all of it.

---

## Benchmarks

| Benchmark | Score | Reader and judge |
|---|---|---|
| [LongMemEval](docs/benchmarks.md#longmemeval), oracle split, 500 questions | **87.2%** | gpt-4o, with the benchmark's own prompts |
| [LoCoMo](docs/benchmarks.md#locomo), 1,540 questions, Mem0's judge prompt | **86.9%** | gpt-4o-mini |
| [LoCoMo](docs/benchmarks.md#locomo), the same context read by an open-weight model | 80.8% | gpt-oss-120b |
| [LoCoMo, the whole conversation in the prompt instead](docs/benchmarks.md#full-context-baseline) | 79.9% | gpt-4o-mini |

Measured on version 0.1.36 in October 2026, not yet rerun on this release.
The judge alone moves a LoCoMo score by several points, so compare numbers
only under the same judge; [the benchmarks page](docs/benchmarks.md) has the
protocol, the cost and the command for each row.

---

## Connect your agent

**Python.** Install the client, then three lines:

```bash
pip install korely-memory
```

```python
from korely_memory import Korely

korely = Korely(api_key="kor_self_...", base_url="http://127.0.0.1:8000")
korely.add("I prefer to be called Max.", user_id="max")
print(korely.search("What should I call the user?", user_id="max")[0].snippet)
```

JavaScript works the same way: `npm install korely-memory`.

[![A fresh Python virtual environment and pip install korely-memory, then three lines of Python: connect with Korely(), add "I prefer to be called Max." for user max, and search "What should I call the user?", which prints "I prefer to be called Max." The search is by meaning, with local vectors and no model.](docs/assets/python-sdk.gif)](docs/assets/python-sdk.mp4)

**MCP.** Any MCP client connects to `https://your-domain/mcp` with the same key.
Your agent can then save a memory, search its memories, read the facts about a
person (now or on a past day) and get a ready-made context block for its
prompt.

```bash
# Claude Code
claude mcp add --transport http korely https://your-domain/mcp \
  --header "Authorization: Bearer kor_self_..."
```

<details>
<summary><b>Cursor and other MCP clients</b></summary>

```json
{ "mcpServers": { "korely": {
    "url": "https://your-domain/mcp",
    "headers": { "Authorization": "Bearer kor_self_..." } } } }
```

This is the shape Cursor and most clients read. ChatGPT and
Claude on the web connect as a custom connector at the same address and sign in
through your install's own consent page, choosing the project they may read.

</details>

**REST.** `/docs` on your install is the whole contract, served by the install
itself. [Talking to it](docs/guide/api.md) covers the SDKs, the context block
for prompts, errors and the shell commands.

---

## How it works

![How it works. Write: an agent saves a memory; it is stored and searchable at once; a worker in the background sends the text to the model provider (your key), which reads facts and judges them; the new fact is saved, and an older fact about the same thing is closed, never deleted. No key: memories are still stored and searchable, facts wait for one. Read: an agent asks (search, context); one vector of the question is computed on your server, by default with a local model; everything else is SQL over memories, facts and dates. No model on reads.](docs/assets/how-it-works.svg)

An agent writes a memory; it is stored and searchable at once. A worker sends
its text to the model provider you chose, gets facts back, and asks the model
whether each new fact replaces an older one about the same thing; the older one
is closed, never deleted. No read calls the model: a search and a context
compute one vector of the question, on your server by default, and every other
read is SQL.

---

## Your data

- **What leaves the machine** is text sent to the model provider whose key you
  set, and only to write facts: the memory with the end user's id as your agent
  sent it (prefer an opaque id to an email address or a name), a few of the
  same person's earlier memories for context, and the facts the judge compares.
  [Install](docs/guide/install.md) lists exactly what goes. Nothing is sent to
  Korely, and nothing phones home. Vectors are computed on your server by
  default (EmbeddingGemma-300m).
- **Europe or Global** is chosen per project. Europe writes with gpt-oss-120b on
  Scaleway, in Paris; a region with no provider keeps its memories waiting and
  never falls back to the other one.
- **One install, many projects.** Give each app or customer its own project:
  its key reads and writes that project only, so the shop's agent never sees
  the clinic's memories. A project key cannot manage keys or webhooks or read
  the audit trail; an admin key, and you signed in to the dashboard, reach
  every project of the account.

  ![One install, many projects: a key of project Shop reads and writes Shop's memories and facts only, a key of project Clinic reads and writes Clinic's only, and neither crosses the wall; an admin key, or you signed in to the dashboard, reaches every project, and the keys, the webhooks and the audit trail.](docs/assets/who-reads-what.svg)

- **An audit trail that stores no content**: who read or changed which person's
  data, when, with what result, kept 400 days by default.
- **Erasure that erases**: `DELETE /v1/users/{id}/memories` removes the person's
  memories and facts in the key's project, history included, and leaves a
  record with counts and a keyed pseudonym instead of their id; the account's
  administrator can erase them in every project at once.
- **Logs carry no end-user words**: errors and access lines hold ids and route
  shapes, never the text of memories, facts or questions.

[What is in the box](docs/guide/data-and-privacy.md) details the facts, the
trail and every form of erasure.

---

## Status

Version 1.0.3, the first public release, and
actively maintained: releases come out every few days, each with upgrade notes in the [CHANGELOG](CHANGELOG.md).
The same code runs Korely Cloud, the hosted service at
[korely.ai](https://korely.ai). The issues this version ships with are listed in
[Honest limits and known issues](docs/guide/limits.md#known-issues-in-this-version);
none of them shows one account's data to another, loses data or keeps an erased
person's data. Upgrades, backups and restores are in
[Keeping it running](docs/guide/operations.md).

---

## Honest limits

- **Facts are mined by a model.** It misses things, invents relation names, and
  sometimes closes a fact that was still true. A closed fact stays readable, and
  an agent can close or correct one itself.
- **Whether a relation holds one value at a time is a model's answer.** A change
  of plan closes the old plan; a change of employer may leave both employers
  open.
- **More than one process needs sticky MCP sessions.** Two processes on one
  Postgres took 2,000 memories at once: each memory reached the model once and
  sent its webhook once, with no error
  ([scripts/two_processes.sh](scripts/two_processes.sh), no key needed). Thirty
  agents of one account, 15 on REST and 15 on MCP, wrote 600 memories and
  read 600 times in 26 seconds on the same two processes, with no client
  retry: none lost, none stored twice, no read refused
  ([scripts/concurrent_agents.sh](scripts/concurrent_agents.sh)). Twelve hours of load on one
  process (36,027 memories, a restart at the twelfth hour, writer faked) kept the API's memory flat
  at about 590 MiB, with no error in its log
  ([scripts/soak.sh](scripts/soak.sh)). An MCP session lives in the process
  that opened it, so a load balancer in front must keep each MCP client on the
  same process; REST calls can go to any of them.

[Honest limits and known issues](docs/guide/limits.md) has each one with what
was measured.

---

## Documentation

[docs/guide](docs/guide/README.md): install, the two clocks, the API and MCP,
webhooks, keys and limits, data and privacy, operations, troubleshooting.

---

## Support

Bugs and ideas: [GitHub issues](https://github.com/korely-ai/korely/issues),
where everybody can read them; the form asks for the version and whether it is
an install of your own or the Cloud, and real people's data stays out of it.
Vulnerabilities: security@korely.ai, never an issue ([SECURITY.md](.github/SECURITY.md)).
Commercial support and setup for your domain: info@korely.ai. The Cloud's plans
are at [korely.ai/pricing](https://korely.ai/pricing).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). In plain words,
and the file is what governs: you can use it, change it, pass it on and sell it,
run it for yourself or host it for other people. A copy you pass on carries
LICENSE and NOTICE, and a file you changed says so. Whoever contributes code
also licenses the patents that code needs (section 3), and it comes with no
warranty. The licence gives no right to the name and logo "Korely" (section 6):
saying your work is built on Korely Agent is fine, calling it Korely is not. A
contribution you send is taken under the same licence (section 5);
[CONTRIBUTING.md](.github/CONTRIBUTING.md) says how. The `korely-memory` clients on PyPI
and npm are MIT. Every third-party licence in the tree is listed in
[docs/DEPENDENCIES.md](docs/DEPENDENCIES.md).
