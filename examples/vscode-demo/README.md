# The angry customer, in VS Code

The story at the top of the README, run end to end from one VS Code window:
install the store on this machine, mint a key, let one agent write what it
heard in March and June, let a second, separate agent ask what the store
believed on April 1, and see the same answer in the dashboard's graph with the
clock put back.

You need Docker, Python 3.9 or later, VS Code with the Python extension, and a
Google AI Studio key. About two dozen model calls per run of the story
(measured on 15 Oct 2026 with Gemini: 24 calls, about 16,000 tokens): each
memory is read once, and on a store that starts empty each new relation and
value is also put to the learned vocabulary and to the contradiction judge.

## Run it

Open this folder in VS Code (*File > Open Folder…*, `examples/vscode-demo`),
then *Terminal > Run Task…*, in order:

| task | what it does |
|---|---|
| **Korely 0: Python environment** | a `.venv` here with `korely-memory` |
| **Korely 1: the store, on this machine** | `setup_local.sh`: the README's install, two directories up, on port 8765 with no `DOMAIN`, secrets generated. Set `GOOGLE_API_KEY` in the environment first, or paste the key in the dashboard's Settings afterwards |
| **Korely 2: a key** | `create_key.py`: an account, its key (into `.env`) and a dashboard login (into `.demo-login`), both readable by you only |
| **Korely 3: Agent A writes** | the support bot logs three conversations, in plain English, with their dates |
| **Korely 4: Agent B reads** | the complaints desk, a separate program: what the store believes today, what it believed on April 1, everything it kept, and what a model would be handed |
| **Korely 5: the graph on April 1** | opens `/graph?user_id=c4821&as_of=2026-04-01`. Log in once first with `.demo-login`; drag the slider to June and the plan changes, the old one stays dashed. **5b** opens it on today |
| **Korely: reset the story** | the real erasure, `DELETE /v1/users/c4821/memories`, so the story can be told again |

The two agents share nothing but the store and the key. Agent A never says
which sentence replaces which; working that out is the store's job.

## On a server instead

The agents only need an address and a key. For an install on a server:

```bash
.venv/bin/python create_key.py --ssh root@your-server --dir /root/korely-agent \
    --url https://your-domain
```

and then tasks 3 to 5 as above. `--ssh` takes ssh options too
(`--ssh "-i ~/.ssh/key root@host"`).

## Over https on this machine

The tasks use the loopback address and need no certificate. To go through
Caddy at `https://localhost` instead, put `KORELY_BASE_URL=https://localhost`
and `KORELY_CA_FILE=/path/to/caddy-root.crt` in `.env` (the README's "On a
laptop" says how to take the root out of Caddy). The agents hand it to the
client as `ca_file`, which works on the Python that ships with macOS too.

## Afterwards

`docker compose down -v` in the repository root removes the store and its
data. `.env`, `.demo-login` and `.venv` here are yours to delete.
