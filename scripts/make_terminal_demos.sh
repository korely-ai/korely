#!/usr/bin/env bash
# Render the short terminal recordings again, each into docs/assets/<name>.gif
# and docs/assets/<name>.mp4:
#
#   bash scripts/make_terminal_demos.sh                 # all of them, before the public release
#   bash scripts/make_terminal_demos.sh --public        # once the repo and the image are public
#   bash scripts/make_terminal_demos.sh story-4821 ...  # only these
#
#   korely-commands      ./korely status, stop, start --wait (back when healthy),
#                        logs -f (Ctrl-C), and uninstall refusing without --yes
#   connect-claude-code  the key read without being shown, claude mcp add, claude mcp list
#   story-4821           the README's customer 4821 with curl and jq, no model:
#                        two facts written, the old one closed on its date, read
#                        as_of the day of the conversation and today
#   python-sdk           a fresh venv, pip install korely-memory (from PyPI), and
#                        three lines of Python: a sentence remembered, then found
#                        by what it means (local vectors, no model)
#   agent-memory         Claude Code with Korely over MCP, two sessions: the
#                        customer's plan told in the first, asked in the second,
#                        a new session that knows it only from Korely (now, and
#                        in April). A recording with model calls: skipped
#                        unless a Gemini key and a Claude login are at hand (below)
#   shared-memory        two different agents, one Korely: Claude Code (top pane)
#                        told the customer's plan and its change, then Gemini
#                        CLI (bottom pane) asked for it, answering from
#                        Korely's dated facts. Model calls as agent-memory, and
#                        a Gemini CLI login (below)
#
# One command, no model key and no paid call (agent-memory and shared-memory
# apart, below). One
# real install serves every recording: the README's line run here, unrecorded,
# with the stand-ins of scripts/make_install_demo.sh before the release
# (demo_standins in scripts/terminal_demo_lib.sh) or for real with --public;
# then each session is typed into zsh on this machine
# (scripts/record_terminal.py --steps), rendered with xterm.js in Playwright
# (scripts/make_install_demo.mjs) and converted with ffmpeg; and the install
# goes at the end with its own `./korely uninstall --yes`, which also removes
# the image.
#
# What is on screen is what the commands printed. The clock is the only thing
# changed, and the frame says so when it is (a badge for a pause shortened or
# output sped up). The login and the API key are the throwaway install's.
# connect-claude-code runs claude with HOME and CLAUDE_CONFIG_DIR in a
# throwaway folder: `claude mcp add` writes there, never in ~/.claude.json, and
# `claude mcp list` only asks the server whether it answers (no model call).
#
# Environment, all optional:
#   KORELY_DEMO_PORT     the port on 127.0.0.1 (default 8126)
#   KORELY_DEMO_PROJECT  the Compose project (default korely-tour)
#   KORELY_DEMO_STILLS   a folder to copy each recording's last frame into (<name>.png)
#   KORELY_DEMO_KEEP=1   leave the install running (its folder is printed)
#   KORELY_DEMO_WORK     that folder: record against the install left running there
#                        instead of making one (with KORELY_DEMO_KEEP=1 it stays)
#   KORELY_DEMO_INSTALL_ONLY=1  make the install and stop, to record against it later
#   KORELY_DEMO_GEMINI_KEY  agent-memory's writer key (Gemini), from the environment,
#                        never a command line: e.g.
#                          KORELY_DEMO_GEMINI_KEY="$(sed -n 's/^GEMINI_API_KEY=//p' some/.env)" \
#                            bash scripts/make_terminal_demos.sh agent-memory
#   CLAUDE_CODE_OAUTH_TOKEN  agent-memory's Claude login (`claude setup-token`); on a Mac
#                        without it, the login Claude Code keeps in the keychain is read
#   PLAYWRIGHT_IMAGE, FFMPEG_IMAGE   the two images used (scripts/make_demo.sh's)
#
# agent-memory has an install of its own (KORELY_DEMO_PORT default 8127, project
# korely-agent-memory), made with the writer key in the installer's environment
# (KORELY_PROVIDER=gemini, KORELY_PROVIDER_KEY) and gone at the end with its
# uninstall, the key with it. Asked with the others, it runs after them, once
# their install is gone. Claude Code runs as connect-claude-code does, with HOME
# and CLAUDE_CONFIG_DIR in a throwaway folder (~/.claude.json and ~/.claude are
# never read or written; no auto-memory, so session 2 has only Korely), the MCP
# server given for the session alone (--mcp-config, --strict-mcp-config,
# --allowedTools), hooks that tell the recorder when a turn ends, and the
# project's CLAUDE.md naming the customers' user_id. Its model is Claude Code's
# default. The recording is checked for every key before it is kept.
#
# shared-memory shares agent-memory's install (both asked: one install), its
# Claude Code and its keys. Two recordings, one per agent, in the same
# throwaway folder (a git repository, whose CLAUDE.md Gemini CLI reads too),
# rendered apart and stacked by demo_stack: the top pane plays while the bottom
# one waits at its prompt, then the bottom one plays under the top's last
# screen. Gemini CLI gets Korely from that folder only, with the install's key,
# and is started once before the recording (not recorded); what it keeps of the
# folder outside it goes at the end (gemini_forget).
#
# Gemini CLI (gemini, logged in: its own login is used, never read): Korely
# (its korely_get_facts tool, see gemini_folder), CLAUDE.md as its context file (with ~/.gemini/GEMINI.md, which it always
# reads), no skills, no tips and the recorder's hooks (SessionStart "ready",
# AfterAgent "agent", by .gemini/recorder.sh) from the folder's
# .gemini/settings.json; the folder trusted for the session by
# GEMINI_CLI_TRUST_WORKSPACE. Its first start notes the folder's hooks as seen
# (~/.gemini/trusted_hooks.json): the recording shows no warning about them. Its model is
# KORELY_DEMO_GEMINI_MODEL (default gemini-3.5-flash), one prompt per run.
# ~/.gemini/projects.json, state.json, state.json.bak and trusted_hooks.json are
# saved before and put back as they were after, and the folder's history/ and
# tmp/ entries go; settings.json and the login are never written.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/terminal_demo_lib.sh"
ALL="korely-commands connect-claude-code story-4821 python-sdk agent-memory shared-memory"
PUBLIC=""
NAMES=""
for a in "$@"; do
  case "$a" in
    --public) PUBLIC=1 ;;
    korely-commands|connect-claude-code|story-4821|python-sdk|agent-memory|shared-memory) NAMES="$NAMES $a" ;;
    *) echo "usage: bash scripts/make_terminal_demos.sh [--public] [$(echo $ALL | sed 's/ /|/g') ...]" >&2; exit 2 ;;
  esac
done
NAMES="${NAMES:-$ALL}"
# The recordings with model calls (agent-memory, shared-memory), asked with
# others, come after them with their own install.
THEN_AGENT=""
AGENT=""
MODELS=""
OTHERS=""
for a in $NAMES; do
  case "$a" in agent-memory|shared-memory) MODELS="$MODELS $a" ;; *) OTHERS="$OTHERS $a" ;; esac
done
if [ -n "$MODELS" ]; then
  if [ -n "$OTHERS" ]; then THEN_AGENT="$MODELS"; NAMES="$OTHERS"; else AGENT=1; NAMES="$MODELS"; fi
fi
if [ -n "$AGENT" ]; then
  PORT="${KORELY_DEMO_PORT:-8127}"
  PROJECT="${KORELY_DEMO_PROJECT:-korely-agent-memory}"
else
  PORT="${KORELY_DEMO_PORT:-8126}"
  PROJECT="${KORELY_DEMO_PROJECT:-korely-tour}"
fi
STILLS="${KORELY_DEMO_STILLS:-}"
KEEP="${KORELY_DEMO_KEEP:-}"
PLAYWRIGHT_IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright:v1.56.0-noble}"
FFMPEG_IMAGE="${FFMPEG_IMAGE:-jrottenberg/ffmpeg:7.1-alpine320}"
ASSETS="$ROOT/docs/assets"
PUBLIC_REPO="$(demo_public_repo)"   # scripts/terminal_demo_lib.sh, from install.sh
RAW="https://raw.githubusercontent.com/$PUBLIC_REPO"
REAL_CURL="$(command -v curl)"

# A short folder: the install's path is printed in the recordings.
REUSE="${KORELY_DEMO_WORK:-}"
if [ -n "$REUSE" ]; then
  WORK="$(cd -P "$REUSE" && pwd -P)"
  [ -f "$WORK/korely/credentials.txt" ] || { echo "make_terminal_demos.sh: no install made by this script in $REUSE" >&2; exit 1; }
  PORT="$(sed -n 's/^PORT=//p' "$WORK/korely/.env" | tail -n 1)"
else
  WORK="$(mktemp -d /tmp/korely.XXXXXX)"
  WORK="$(cd -P "$WORK" && pwd -P)"
fi
DEMO="$WORK/.demo"           # everything but the install and the sessions' folders
DIR="$WORK/korely"           # the install, ./korely of $WORK as the README's line makes it
mkdir -p "$DEMO" "$ASSETS"
rm -f "$DEMO/summary.txt"
BUILT=""

# Gemini CLI keeps, per folder it has opened, an entry in
# ~/.gemini/projects.json (the folder's short name) with ~/.gemini/history/<it>/
# and ~/.gemini/tmp/<it>/ (the chat and its logs), the hooks it has seen in
# trusted_hooks.json, and counters in state.json (and its .bak). gemini_save
# copies those four files before Gemini CLI runs; gemini_forget puts them back
# byte for byte (or removes the ones that were not there) and removes the
# history/ and tmp/ entries made since that belong to the folder.
GEMINI_DESK=""
GEMINI_FILES="projects.json state.json state.json.bak trusted_hooks.json"
gemini_save() {
  local f
  rm -rf "$DEMO/gemini-before" && mkdir -p "$DEMO/gemini-before"
  for f in $GEMINI_FILES; do
    if [ -f "$HOME/.gemini/$f" ]; then cp -p "$HOME/.gemini/$f" "$DEMO/gemini-before/$f"; fi
  done
  (cd "$HOME/.gemini" && ls -1 history tmp 2>/dev/null) > "$DEMO/gemini-before/.entries" || true
}
gemini_forget() {
  local f
  [ -n "$GEMINI_DESK" ] && [ -d "$DEMO/gemini-before" ] || return 0
  python3 - "$HOME/.gemini" "$GEMINI_DESK" "$DEMO/gemini-before/.entries" <<'PY'
import os, shutil, sys
home, desk, entries = sys.argv[1:4]
before = set(open(entries).read().split())
for top in ("history", "tmp"):
    folder = os.path.join(home, top)
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        root = os.path.join(folder, name, ".project_root")
        if name not in before and os.path.exists(root) and open(root).read().strip() == desk:
            shutil.rmtree(os.path.join(folder, name))
PY
  for f in $GEMINI_FILES; do
    if [ -f "$DEMO/gemini-before/$f" ]; then
      cp -p "$DEMO/gemini-before/$f" "$HOME/.gemini/$f"
    else
      rm -f "$HOME/.gemini/$f"
    fi
  done
  rm -rf "$DEMO/gemini-before"
}

cleanup() {
  # agent-memory's and shared-memory's keys, whatever happened.
  rm -f "$DEMO/writer.key" "$DEMO/session.env" "$DEMO/agent.env" "$DEMO/second.env"
  # What Gemini CLI keeps of shared-memory's folder.
  gemini_forget
  if [ -n "$KEEP" ]; then
    echo "The install is still running in $DIR (KORELY_DEMO_KEEP=1)." >&2
    echo "Remove it: \"$DIR/korely\" uninstall --yes && rm -rf \"$WORK\"" >&2
    return
  fi
  if [ -x "$DIR/korely" ]; then
    "$DIR/korely" uninstall --yes >/dev/null 2>&1 || echo "make_terminal_demos.sh: the uninstall failed, see $DIR" >&2
  fi
  if [ -n "$BUILT" ] && docker image inspect "$BUILT" >/dev/null 2>&1; then
    docker image rm "$BUILT" >/dev/null 2>&1 || echo "make_terminal_demos.sh: docker image rm $BUILT failed" >&2
  fi
  [ -d "$DIR" ] || rm -rf "$WORK"
}
trap cleanup EXIT

case " $NAMES " in
  *" connect-claude-code "*)
    command -v claude >/dev/null 2>&1 || { echo "make_terminal_demos.sh: connect-claude-code needs claude on the PATH" >&2; exit 1; } ;;
esac

# agent-memory's two keys, or the recording is skipped: nothing is installed.
# Held in variables and files of $DEMO (mode 600, gone with it), never on a
# command line.
if [ -n "$AGENT" ]; then
  skip() { echo "make_terminal_demos.sh:$NAMES skipped: $1" >&2; KEEP=""; trap - EXIT; rm -f "$DEMO/writer.key" "$DEMO/session.env"; [ -n "$REUSE" ] || rm -rf "$WORK"; exit 0; }
  [ -n "${KORELY_DEMO_GEMINI_KEY:-}" ] || skip "no KORELY_DEMO_GEMINI_KEY (the writer's key)"
  command -v claude >/dev/null 2>&1 || skip "no claude on the PATH"
  CLAUDE_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}"
  if [ -z "$CLAUDE_TOKEN" ] && command -v security >/dev/null 2>&1; then
    # The login Claude Code keeps on a Mac, read and never written.
    CLAUDE_TOKEN="$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null \
      | python3 -c 'import json, sys, time
o = json.load(sys.stdin).get("claudeAiOauth") or {}
print(o.get("accessToken", "") if o.get("expiresAt", 0) / 1000 > time.time() + 1800 else "")' 2>/dev/null)" || CLAUDE_TOKEN=""
  fi
  [ -n "$CLAUDE_TOKEN" ] || skip "no Claude login (CLAUDE_CODE_OAUTH_TOKEN, or a Mac keychain login valid for 30 minutes more)"
  case " $NAMES " in
    *" shared-memory "*)
      GEMINI="$(command -v gemini)" || skip "no gemini (Gemini CLI) on the PATH" ;;
  esac
  (umask 077 && printf '%s' "$KORELY_DEMO_GEMINI_KEY" > "$DEMO/writer.key" \
    && printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$CLAUDE_TOKEN" > "$DEMO/session.env")
fi

# The install, unless KORELY_DEMO_WORK named one.
if [ -z "$REUSE" ]; then
  if [ -n "$PUBLIC" ]; then
    RELEASE="$("$REAL_CURL" -fsSL "$RAW/main/install.sh" | sed -n 's/^THIS_RELEASE="\(.*\)"$/\1/p')"
  else
    RELEASE="$(sed -n 's/^THIS_RELEASE="\(.*\)"$/\1/p' "$ROOT/install.sh")"
  fi
  [ -n "$RELEASE" ] || { echo "make_terminal_demos.sh: no THIS_RELEASE in install.sh" >&2; exit 1; }
  IMAGE="ghcr.io/$PUBLIC_REPO:$RELEASE"
  # The uninstall at the end removes the image: one that was here before is
  # somebody's, and is not this script's to remove.
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "make_terminal_demos.sh: $IMAGE is already on this machine, and the uninstall would remove it." >&2
    echo "Remove it first (docker image rm $IMAGE) if it is not in use." >&2
    exit 1
  fi

  if [ -z "$PUBLIC" ]; then
    demo_standins   # sets BUILT and RUN_PATH
  else
    RUN_PATH="$PATH"
  fi

  echo "==> Installing (not recorded) on 127.0.0.1:$PORT, project $PROJECT" >&2
  # A clean environment, as for the install recording: no model key, but for
  # agent-memory's writer, whose key the installer reads from its environment
  # (KORELY_PROVIDER_KEY), from a file, so that no command line holds it.
  (cd "$WORK" && env -i HOME="$HOME" USER="${USER:-}" PATH="$RUN_PATH" LANG=en_US.UTF-8 \
    ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} ${DOCKER_CONTEXT:+DOCKER_CONTEXT="$DOCKER_CONTEXT"} \
    KORELY_PORT="$PORT" KORELY_PROJECT="$PROJECT" KORELY_SAVE_CREDENTIALS=1 \
    ${AGENT:+KORELY_PROVIDER=gemini WRITER_KEY_FILE="$DEMO/writer.key"} \
    sh -c 'if [ -n "${WRITER_KEY_FILE:-}" ]; then
             KORELY_PROVIDER_KEY="$(cat "$WRITER_KEY_FILE")"; export KORELY_PROVIDER_KEY
             rm -f "$WRITER_KEY_FILE"; unset WRITER_KEY_FILE
           fi
           curl -fsSL "$0/main/install.sh" | sh' "$RAW" > "$DEMO/install.log" 2>&1) \
    || { echo "make_terminal_demos.sh: the install failed, see $DEMO/install.log" >&2; KEEP=1; exit 1; }
  if [ -f "$DEMO/standin.log" ]; then sed 's/^/    stand-in: /' "$DEMO/standin.log" >&2; fi
fi
rm -f "$DEMO/writer.key"
if [ -n "${KORELY_DEMO_INSTALL_ONLY:-}" ]; then
  rm -f "$DEMO/session.env"
  echo "Installed in $WORK: record against it with KORELY_DEMO_WORK=$WORK" >&2
  KEEP=1; exit 0
fi
field() { awk -v k="$1" 'index($0, k) == 1 { print $NF }' "$DIR/credentials.txt"; }
KEY="$(field 'api key')"
case "$KEY" in kor_*) ;; *) echo "make_terminal_demos.sh: no API key in $DIR/credentials.txt" >&2; exit 1 ;; esac
BASE="http://127.0.0.1:$PORT"
# The Claude Code line exactly as the install printed it.
CLAUDE_LINE="$(sed -n 's/^ *\(claude mcp add .*\)$/\1/p' "$DEMO/install.log" | head -n 1)"
[ -n "$CLAUDE_LINE" ] || { echo "make_terminal_demos.sh: no claude line in the install's printout" >&2; exit 1; }

# The sessions, as record_terminal.py --steps reads them. The key is sent into
# `read -rs`, which does not echo it: it is in these files, never on screen.
APP="$WORK/my-agent"         # the folder the curl, claude and Python sessions run in
mkdir -p "$APP"
GEMINI_MODEL="${KORELY_DEMO_GEMINI_MODEL:-gemini-3.5-flash}"
export KEY BASE CLAUDE_LINE GEMINI_MODEL
python3 - "$DEMO" <<'PY'
import json, os, sys
key, base, claude = os.environ["KEY"], os.environ["BASE"], os.environ["CLAUDE_LINE"]
# agent-memory: Claude Code with the MCP server of korely.json for the session
# alone. Its hooks write "agent" for the recorder when it is ready and when a
# turn ends; the shell's prompt, when it has exited.
agent = 'claude --mcp-config korely.json --strict-mcp-config --allowedTools "mcp__korely__*"'
gemini = "gemini -m " + os.environ["GEMINI_MODEL"]
question = "Using Korely's facts, what plan is customer 4821 on, since when, and what was it before?"
# Until the writer has turned every memory into facts (GET /v1/events counts
# the ones in flight), checked outside the terminal.
written = ("python3 -c 'import json, os, urllib.request as u; r = u.Request(os.environ[\"DEMO_BASE\"]"
           " + \"/v1/events?limit=1\", headers={\"Authorization\": \"Bearer \" + os.environ[\"DEMO_KEY\"]});"
           " raise SystemExit(json.load(u.urlopen(r, timeout=10))[\"processing\"] != 0)'")
paste_key = [[1.0, key + "\r"]]
# Each fact on one line: what, from, to.
fmt = '.facts[] | "\\(.object): \\(.valid_from[:10]) to \\(.invalid_at[:10] // "now")"'
sessions = {
    "korely-commands": [
        {"type": "./korely status"},
        {"type": "./korely stop"},
        # --wait reaches `docker compose up`: back when Docker says healthy.
        {"type": "./korely start --wait"},
        {"type": "./korely logs -f --tail 4", "send": [[3.0, "\x03"]], "expect": 130},
        {"type": "./korely uninstall", "expect": 1},
    ],
    "connect-claude-code": [
        {"type": "read -rs KORELY_API_KEY", "send": paste_key},
        {"type": claude},
        {"type": "claude mcp list"},
    ],
    "story-4821": [
        {"type": "read -rs KORELY_API_KEY", "send": paste_key},
        {"type": f'export K={base} H="Authorization: Bearer $KORELY_API_KEY"'},
        {"type": f"F='{fmt}'"},
        {"type": "OLD=$(curl -s $K/v1/facts -H \"$H\" --json '{\"user_id\":\"c4821\",\"subject\":\"c4821\",\n"
                 "  \"predicate\":\"is_on\",\"object\":\"Priority Support plan\",\"valid_from\":\"2026-03-15\"}' | jq -r .id)"},
        {"type": "curl -s $K/v1/facts/$OLD/forget -H \"$H\" --json '{\"at\":\"2026-06-20\"}' | jq -r .status"},
        {"type": "curl -s $K/v1/facts -H \"$H\" --json '{\"user_id\":\"c4821\",\"subject\":\"c4821\",\n"
                 "  \"predicate\":\"is_on\",\"object\":\"Basic plan\",\"valid_from\":\"2026-06-20\"}' | jq -r .id"},
        {"type": 'curl -s "$K/v1/facts?user_id=c4821&as_of=2026-04-01" -H "$H" | jq -r "$F"'},
        {"type": 'curl -s "$K/v1/facts?user_id=c4821" -H "$H" | jq -r "$F"'},
    ],
    # Three lines of Python a person can follow, typed at a person's pace:
    # remember a sentence, then find it by what it means (local vectors, no model).
    "python-sdk": [
        {"type": "python3 -m venv .venv && . .venv/bin/activate"},
        {"type": "pip install korely-memory"},
        {"type": f"read -rs KORELY_API_KEY && export KORELY_API_KEY KORELY_BASE_URL={base}", "send": paste_key},
        {"type": "python -q"},
        {"type": "from korely_memory import Korely", "typing": 0.05, "pause": 0.8},
        {"type": "korely = Korely()   # the key and address set above", "typing": 0.05, "pause": 0.8},
        {"type": 'memory = korely.add("I prefer to be called Max.", user_id="max")', "typing": 0.05,
         "pause": 0.8},
        {"type": 'print(korely.search("What should I call the user?", user_id="max")[0].snippet)',
         "typing": 0.05, "pause": 1.5},
    ],
    "agent-memory": [
        {"type": agent, "at": "agent"},
        {"type": "Remember that customer 4821 has been on the Priority Support plan since 15 March 2026.",
         "pause": 2.5, "at": "agent"},
        {"type": "Remember that customer 4821 moved from the Priority Support plan to the Basic plan "
                 "on 20 June 2026.", "pause": 3.0, "at": "agent"},
        {"type": "/exit", "pause": 3.0, "at": "zsh"},
        # A new session: nothing of the first but what Korely holds.
        {"type": agent, "at": "agent", "until": written, "why": "the writer turning the memories into facts"},
        {"type": "What plan is customer 4821 on?", "pause": 2.5, "at": "agent"},
        {"type": "And what plan were they on in April?", "pause": 3.0, "at": "agent"},
    ],
    # shared-memory: what Claude Code is told (top pane)...
    "shared-claude": [
        {"type": agent, "at": "agent"},
        {"type": "Remember that customer 4821 has been on the Priority Support plan since 15 March 2026.",
         "pause": 2.0, "at": "agent"},
        # The new plan only: "moved from the Priority Support plan to..." also
        # gives the writer a Priority Support fact of its own, from 20 June to
        # 20 June, beside the one it closes.
        {"type": "Remember that customer 4821 moved to the Basic plan on 20 June 2026.",
         "pause": 2.0, "at": "agent"},
    ],
    # ...and what Gemini CLI (bottom pane) answers, once the writer is done.
    # Its hooks write "ready" when it has started, "agent" when a turn ends.
    "shared-gemini": [
        {"type": gemini, "at": "ready", "until": written, "why": "Korely turning the memory into facts"},
        {"type": question, "pause": 2.0, "at": "agent"},
    ],
    # Not recorded: Gemini CLI's first start in the folder, which notes the
    # folder's hooks as seen, left with Ctrl-C twice (which also clears
    # whatever is typed: nothing is ever sent to the model here).
    "gemini-first": [
        {"type": gemini, "send": [[8.0, "\x03"], [1.0, "\x03"]], "at": "zsh", "expect": None},
    ],
}
for name, steps in sessions.items():
    for step in steps:
        step.setdefault("pause", 0.5)    # before each command
        step.setdefault("enter", 0.35)   # between its last key and Enter
    with open(os.path.join(sys.argv[1], name + ".json"), "w") as f:
        json.dump(steps, f, indent=1)
PY

# record NAME CWD ROWS TYPING [VAR=value...]: one session, in a clean
# environment (no model key, nothing of this shell's but what is named, and
# the NAME=value lines of $ENVFILE when it is set). RENDER, when set, is the
# renderer's VAR=value pairs instead of FINAL=4 BUSY=4, and TITLE its title bar.
ENVFILE=""
RENDER=""
TITLE=""
record_cast() {
  local name="$1" cwd="$2" rows="$3" typing="$4"; shift 4
  echo "==> Recording $name" >&2
  env -i HOME="$HOME" USER="${USER:-}" PATH="$PATH" TERM=xterm-256color LANG=en_US.UTF-8 \
    ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} ${DOCKER_CONTEXT:+DOCKER_CONTEXT="$DOCKER_CONTEXT"} \
    "$@" python3 "$HERE/record_terminal.py" --out "$DEMO/$name.cast" --cwd "$cwd" \
      --steps "$DEMO/$name.json" --cols 100 --rows "$rows" --typing "$typing" --limit 600 \
      ${ENVFILE:+--env-file "$ENVFILE"} \
    || { echo "make_terminal_demos.sh: $name did not go as written; the recording is $DEMO/$name.cast" >&2; KEEP=1; exit 1; }
  # No key reaches the screen (read -rs does not echo one): the install's, and
  # agent-memory's writer key and Claude login, whole or their last 8
  # characters. The values from the environment, never on grep's command line.
  if ! KORELY_KEY="$KEY" WRITER="${KORELY_DEMO_GEMINI_KEY:-}" LOGIN="${CLAUDE_TOKEN:-}" \
      python3 - "$DEMO/$name.cast" <<'PY'
import os, sys
cast = open(sys.argv[1], encoding="utf-8").read()
for label in ("KORELY_KEY", "WRITER", "LOGIN"):
    secret = os.environ.get(label, "")
    if secret and (secret in cast or secret[-8:] in cast):
        sys.exit(f"make_terminal_demos.sh: a key ({label}) is on screen in {sys.argv[1]}")
PY
  then KEEP=1; exit 1
  fi
}
record() {
  local name="$1"
  record_cast "$@"
  echo "==> Rendering $name" >&2
  rm -rf "${DEMO:?}/${name:?}.frames"
  # shellcheck disable=SC2086
  demo_render "$DEMO/$name.cast" "$DEMO/$name.frames" ${RENDER:-FINAL=4 BUSY=4} ${TITLE:+"TITLE=$TITLE"}
  echo "==> Converting $name" >&2
  local length
  length="$(demo_convert "$DEMO/$name.frames" "$name")"
  if [ -n "$STILLS" ]; then
    mkdir -p "$STILLS"
    cp "$DEMO/$name.frames/still-2.png" "$STILLS/$name.png"
  fi
  echo "$name: docs/assets/$name.gif $(demo_size "$ASSETS/$name.gif"), $name.mp4 $(demo_size "$ASSETS/$name.mp4"), ${length} s" >> "$DEMO/summary.txt"
}

# erase USER: what a session wrote for that end user, so that a session
# recorded again (KORELY_DEMO_WORK) starts from the same empty store.
erase() {
  python3 - "$1" <<'PY'
import os, sys, urllib.parse, urllib.request
url = os.environ["BASE"] + "/v1/users/" + urllib.parse.quote(sys.argv[1], safe="") + "/memories"
req = urllib.request.Request(url, method="DELETE", headers={"Authorization": "Bearer " + os.environ["KEY"]})
urllib.request.urlopen(req, timeout=30).read()
PY
}

# claude_folder DIR: Claude Code ready to run in DIR, for agent-memory and
# shared-memory. Its own settings in a throwaway home, as for
# connect-claude-code: onboarding done, DIR trusted, the dark theme, the manual
# permission mode (asks before any tool the command line does not allow), no
# auto-memory, and the hooks. In DIR, the MCP server for the session alone,
# with the install's key, and the project's convention for a customer's
# user_id. Then the sessions' environment file ($DEMO/agent.env: the login,
# and the install's address and key for the writer's wait). CLAUDE_ENV is what
# record_cast adds to the environment for Claude Code.
claude_folder() {
  rm -rf "${WORK:?}/home" && mkdir -p "$WORK/home/.claude"
  python3 - "$WORK/home/.claude" "$1" <<'PY'
import json, os, sys
conf, app = sys.argv[1], sys.argv[2]
mark = {"hooks": [{"type": "command", "command": 'echo agent >> "$RECORD_MARK"'}]}
# The one-off notices and offers a first start shows answered already: the
# offer to make auto mode the default took the first prompt's keys.
json.dump({"hasCompletedOnboarding": True, "hasSeenAutoDefaultNotice": True,
           "hasSeenAutoDefaultNudge": True, "hasSeenAutoModeEntryWarning": True, "theme": "dark",
           "autoUpdates": False,
           "projects": {app: {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True,
                              "projectOnboardingSeenCount": 1}}},
          open(os.path.join(conf, ".claude.json"), "w"), indent=1)
json.dump({"autoMemoryEnabled": False,
           "permissions": {"defaultMode": "manual", "allow": ["mcp__korely"]},
           "hooks": {"SessionStart": [mark], "Stop": [mark]}},
          open(os.path.join(conf, "settings.json"), "w"), indent=1)
PY
  (umask 077 && KEY="$KEY" python3 -c 'import json, os, sys
json.dump({"mcpServers": {"korely": {"type": "http", "url": sys.argv[1],
           "headers": {"Authorization": "Bearer " + os.environ["KEY"]}}}}, open(sys.argv[2], "w"), indent=1)' \
    "$BASE/mcp" "$1/korely.json")
  printf '%s\n' "# Support desk" "" \
    "Korely (the korely MCP server) is this project's long-term memory. A customer is an end" \
    "user: their user_id is c and their number (customer 4821 is c4821)." > "$1/CLAUDE.md"
  (umask 077 && { cat "$DEMO/session.env"; printf 'DEMO_BASE=%s\nDEMO_KEY=%s\n' "$BASE" "$KEY"; } \
    > "$DEMO/agent.env")
  CLAUDE_ENV=(PATH="$(dirname "$(command -v claude)"):$PATH" HOME="$WORK/home"
    CLAUDE_CONFIG_DIR="$WORK/home/.claude" DISABLE_AUTOUPDATER=1
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 CLAUDE_CODE_DISABLE_AUTO_MEMORY=1)
}

# first_start AGENT DESK: AGENT's unrecorded first start in DESK
# ($DEMO/AGENT-first.json), with SECOND_ENV.
first_start() {
  env -i HOME="$HOME" USER="${USER:-}" PATH="$PATH" TERM=xterm-256color LANG=en_US.UTF-8 \
    "${SECOND_ENV[@]}" python3 "$HERE/record_terminal.py" --out "$DEMO/$1-first.cast" \
      --cwd "$2" --steps "$DEMO/$1-first.json" --cols 100 --rows 24 --limit 60 \
    || { echo "make_terminal_demos.sh: $1 did not start in $2" >&2; KEEP=1; exit 1; }
  rm -f "$DEMO/$1-first.cast"
}

# gemini_folder DESK: Gemini CLI ready to run in DESK (see the header).
# SECOND_ENV is what record_cast adds to the environment for it.
gemini_folder() {
  mkdir -p "$1/.gemini"
  # The hooks' command has to stay the same from the first start to the
  # recording (Gemini CLI knows a hook by its name and its command, with the
  # environment's $VARIABLES in it written out): a script, which reads the
  # recorder's RECORD_MARK when it runs. The mark is written 4 s later, apart:
  # the hook ends at once, and the "Executing Hook" line Gemini CLI shows for
  # some 3 s is gone before the recorder's last frame.
  printf '%s\n' '#!/bin/sh' '# The recorder'"'"'s marks (scripts/record_terminal.py): started, a turn ended.' \
    'cat > /dev/null' '(sleep 4; echo "$1" >> "$RECORD_MARK") </dev/null >/dev/null 2>&1 &' \
    > "$1/.gemini/recorder.sh"
  (umask 077 && KEY="$KEY" python3 - "$BASE/mcp" "$1" <<'PY'
import json, os, sys
url, desk = sys.argv[1:3]
mark = lambda word: [{"hooks": [{"name": "recorder", "type": "command",
                                 "command": f"sh {desk}/.gemini/recorder.sh {word}"}]}]
# ~/.gemini/GEMINI.md is read whatever the context file's name: the project's
# CLAUDE.md is read too; the summary that counts them is not shown. Of
# Korely's tools, korely_get_facts: the facts with their dates, one line each,
# what the recording is about. Gemini CLI shows a tool's whole answer, and
# korely_get_context's (a prompt-ready block of some thirty lines) would push
# the call and the question out of a 20-row pane. The dark theme (the recording's terminal is dark,
# whatever the user's own theme) and no background colours (the input box's
# would be a light bar).
json.dump({"mcpServers": {"korely": {"httpUrl": url, "trust": True, "includeTools": ["korely_get_facts"],
                                     "headers": {"Authorization": "Bearer " + os.environ["KEY"]}}},
           "context": {"fileName": "CLAUDE.md"}, "skills": {"enabled": False},
           "ui": {"theme": "Default", "hideTips": True, "hideContextSummary": True,
                  "useBackgroundColor": False},
           "hooksConfig": {"notifications": False},
           "hooks": {"SessionStart": mark("ready"), "AfterAgent": mark("agent")}},
          open(os.path.join(desk, ".gemini", "settings.json"), "w"), indent=1)
PY
  )
  GEMINI_DESK="$1"
  gemini_save
  SECOND_ENV=(PATH="$(dirname "$GEMINI"):$PATH" GEMINI_CLI_TRUST_WORKSPACE=true COLORTERM=truecolor)
  first_start gemini "$1"
  grep -qF "\"$1\"" "$HOME/.gemini/trusted_hooks.json" 2>/dev/null \
    || { echo "make_terminal_demos.sh: Gemini CLI did not note the hooks of $1" >&2; KEEP=1; exit 1; }
}

# shared_memory NAME AGENT TITLE: Claude Code, then AGENT (gemini, the one
# for now; its pane titled TITLE), one memory: docs/assets/NAME.gif and
# NAME.mp4.
shared_memory() {
  local name="$1" second="$2" title="$3" desk="$WORK/support-desk" rows="${KORELY_DEMO_ROWS:-20}" length
  # One folder for both agents: a git repository, for the second one's rules.
  rm -rf "${desk:?}" && mkdir -p "$desk" && git -C "$desk" init -q
  claude_folder "$desk"
  "${second}_folder" "$desk"
  (umask 077 && printf 'DEMO_BASE=%s\nDEMO_KEY=%s\n' "$BASE" "$KEY" > "$DEMO/second.env")
  erase c4821
  # The two recordings, one after the other, in the same folder.
  cp "$DEMO/shared-claude.json" "$DEMO/$name-top.json"
  cp "$DEMO/shared-$second.json" "$DEMO/$name-bottom.json"
  ENVFILE="$DEMO/agent.env"
  record_cast "$name-top" "$desk" "$rows" 0.022 "${CLAUDE_ENV[@]}"
  ENVFILE="$DEMO/second.env"
  record_cast "$name-bottom" "$desk" "$rows" 0.03 "${SECOND_ENV[@]}"
  rm -f "$DEMO/agent.env" "$DEMO/second.env"
  ENVFILE=""
  "${second}_forget"
  echo "==> Rendering $name" >&2
  rm -rf "${DEMO:?}/$name-top.frames" "${DEMO:?}/$name-bottom.frames"
  # The top pane's last screen is held by demo_stack while the bottom plays.
  demo_render "$DEMO/$name-top.cast" "$DEMO/$name-top.frames" \
    FINAL=1 BUSY="${KORELY_DEMO_BUSY:-1.5}" "TITLE=Claude Code"
  demo_render "$DEMO/$name-bottom.cast" "$DEMO/$name-bottom.frames" \
    FINAL=4 BUSY="${KORELY_DEMO_BUSY:-1.5}" "TITLE=$title"
  echo "==> Converting $name" >&2
  length="$(demo_stack "$DEMO/$name-top.frames" "$DEMO/$name-bottom.frames" "$name")"
  if [ -n "$STILLS" ]; then
    mkdir -p "$STILLS"
    cp "$DEMO/$name.still.png" "$STILLS/$name.png"
  fi
  echo "$name: docs/assets/$name.gif $(demo_size "$ASSETS/$name.gif"), $name.mp4 $(demo_size "$ASSETS/$name.mp4"), ${length} s" >> "$DEMO/summary.txt"
}

for name in $NAMES; do
  case "$name" in
    korely-commands)
      record korely-commands "$DIR" 30 0.035 ;;
    connect-claude-code)
      # claude's own settings in a throwaway home: ~/.claude.json is never read
      # or written. No update check and no telemetry; no prompt is sent.
      rm -rf "$WORK/home" && mkdir -p "$WORK/home/.claude"
      record connect-claude-code "$APP" 15 0.025 \
        PATH="$(dirname "$(command -v claude)"):$PATH" HOME="$WORK/home" \
        CLAUDE_CONFIG_DIR="$WORK/home/.claude" DISABLE_AUTOUPDATER=1 \
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 ;;
    story-4821)
      erase c4821
      record story-4821 "$APP" 16 0.018 ;;
    python-sdk)
      # A real download from PyPI (no cache), and no pip version nag.
      erase max
      rm -rf "${APP:?}/.venv"
      RENDER="FINAL=4 BUSY=4"
      record python-sdk "$APP" 16 0.03 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
      RENDER="" ;;
    agent-memory)
      claude_folder "$APP"
      erase c4821
      ENVFILE="$DEMO/agent.env"
      RENDER="FINAL=4 BUSY=${KORELY_DEMO_BUSY:-1.5}"
      TITLE="Claude Code with Korely over MCP: two sessions, one memory"
      record agent-memory "$APP" 28 0.022 "${CLAUDE_ENV[@]}"
      rm -f "$DEMO/agent.env"
      ENVFILE=""; RENDER=""; TITLE="" ;;
    shared-memory)
      shared_memory shared-memory gemini "Gemini CLI" ;;
  esac
done
rm -f "$DEMO/session.env"

cat "$DEMO/summary.txt"
[ -n "$STILLS" ] && echo "stills in $STILLS"
if [ -n "$THEN_AGENT" ]; then
  # Its own install, once this one (and the image both use) is gone.
  cleanup; trap - EXIT
  # shellcheck disable=SC2086
  exec bash "$0" ${PUBLIC:+--public} $THEN_AGENT
fi
exit 0
