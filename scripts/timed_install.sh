#!/usr/bin/env bash
# How long does this take to install on a machine that has nothing?
#
# Run it on a fresh VM as root. It installs Docker, clones, starts, creates a
# tenant, writes a memory, waits for the fact to be mined, and checks that
# as_of answers. Every phase is timed. It prints a table and writes a report.
#
#   GOOGLE_API_KEY=... bash timed_install.sh --label hetzner-cx22
#
# Nothing here is a simulation: the number it prints is the number a customer
# would live through, on that machine, on that network, that day. Which also
# means it is not portable. A report without --label is worth nothing, so the
# script refuses to run without one.
#
# Flags:
#   --label NAME     provider and size. Required. Goes in the report.
#   --skip-docker    Docker is already installed (a rerun, or a laptop)
#   --repo URL       where to clone from
#   --from-local DIR use this directory instead of cloning (scp'd source)
#   --dir PATH       where to install (default /opt/korely-agent). Replaced
#                    whole, so it must be empty or one this script made:
#                    anything else is refused, see below
#   --keep           do not stop the stack at the end (without it the stack
#                    goes down with its volumes)
#
# Without GOOGLE_API_KEY the run stops after the store answers, and says so:
# with no provider no fact is ever mined, so the two phases that prove the
# product works cannot be measured. That is a real install of a store that
# learns nothing, and the report will say exactly that rather than printing a
# total that looks complete.
set -u

LABEL=""
SKIP_DOCKER=0
# install.sh's PUBLIC_REPO, written here too: this script is copied alone to the
# machine it measures, where install.sh is not
# (tests/test_the_docs_say_what_the_code_does.py holds the two to one name).
REPO="https://github.com/korely-ai/korely"
FROM_LOCAL=""
DIR="/opt/korely-agent"
KEEP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --label)      LABEL="${2:-}"; shift 2 ;;
    --skip-docker) SKIP_DOCKER=1; shift ;;
    --repo)       REPO="${2:-}"; shift 2 ;;
    --from-local) FROM_LOCAL="${2:-}"; shift 2 ;;
    --dir)        DIR="${2:-}"; shift 2 ;;
    --keep)       KEEP=1; shift ;;
    -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$LABEL" ]; then
  echo "--label is required: the number means nothing without the machine it was measured on." >&2
  echo "  e.g. --label hetzner-cx22  --label aws-t3.small  --label gcp-e2-small" >&2
  exit 2
fi

# ------------------------------------------------------ not a used machine --
# The clone phase deletes --dir and the end takes the compose project down
# with its volumes. Run on a machine with a real install in /opt/korely-agent,
# that deleted its .env (SECRET_KEY, POSTGRES_PASSWORD), backups/ and facts/,
# and `down -v` of the project, named after the directory, took the volume
# pgdata of any install with the same directory name (audit of 17 Oct 2026).
# So the directory must be empty or carry the mark this script leaves in the
# ones it makes, and the project must have no volumes unless the directory is
# one of those.
MARK=".made-by-timed-install"
if [ -n "$(ls -A "$DIR" 2>/dev/null)" ] && [ ! -f "$DIR/$MARK" ]; then
  echo "$DIR is not empty and this script did not make it: the clone phase deletes it" >&2
  echo "whole, with any .env, backups/ and facts/ in it. Pick an empty --dir, or move it." >&2
  exit 2
fi
# Compose names the project after the directory, lowercased, keeping only
# a-z, 0-9, - and _.
PROJECT=$(basename "$DIR" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')
if [ ! -f "$DIR/$MARK" ] && command -v docker >/dev/null 2>&1; then
  VOLUMES=$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null)
  if [ -n "$VOLUMES" ]; then
    echo "the compose project '$PROJECT' already has volumes on this machine ($(echo $VOLUMES)):" >&2
    echo "an install named like --dir holds its database there, and this run would share" >&2
    echo "them and, without --keep, remove them. Pick a --dir with another last name." >&2
    exit 2
  fi
fi

# ---------------------------------------------------------------- timing ----
# Milliseconds where the platform has them. GNU date does; BusyBox and macOS
# do not, and there the resolution falls back to a second, which is honest
# rather than wrong.
case "$(date +%3N 2>/dev/null)" in
  [0-9][0-9][0-9])
    # One call, not two. Seconds from one `date` and milliseconds from the
    # next straddle a second boundary now and then, and the run of
    # 2026-09-28 reported `configure: -964ms`.
    _now() { date +%s%3N; }
    RESOLUTION="ms" ;;
  *)
    # BSD date answers "3N" here, which is neither an error nor a number. A
    # dry-run on a Mac caught this: every phase timed 0ms and the report looked
    # like an instant install.
    _now() { echo $(( $(date +%s) * 1000 )); }
    RESOLUTION="s" ;;
esac

PHASE_NAMES=""
PHASE_MS=""
PHASE_NOTE=""
CURRENT=""
CURRENT_START=0
FAILED=""

_phase() {
  CURRENT="$1"
  CURRENT_START=$(_now)
  printf '\n\033[1m== %s\033[0m\n' "$1"
}

_end() {  # _end [note]
  local ms=$(( $(_now) - CURRENT_START ))
  # A note ends up in a markdown cell, and these notes are program output:
  # a pipe or a newline in one would silently break the table the report is for.
  local note
  note=$(printf '%s' "${1:-}" | tr '\n|' '  ' | cut -c1-160)
  PHASE_NAMES="${PHASE_NAMES}${CURRENT}\n"
  PHASE_MS="${PHASE_MS}${ms}\n"
  PHASE_NOTE="${PHASE_NOTE}${note}\n"
  printf '   %s: %s\n' "$CURRENT" "$(_fmt $ms)"
}

_fmt() {
  local ms=$1
  if [ "$ms" -lt 1000 ]; then echo "${ms}ms"
  elif [ "$ms" -lt 60000 ]; then echo "$(( ms / 1000 )).$(( (ms % 1000) / 100 ))s"
  else echo "$(( ms / 60000 ))m $(( (ms % 60000) / 1000 ))s"; fi
}

# ------------------------------------------------------------------ report --
_report() {
  local file="install-report-${LABEL}-$(date -u +%Y%m%d).md"
  {
    echo "# Cold install: $LABEL"
    echo ""
    echo "\`$(date -u '+%Y-%m-%d %H:%M UTC')\` — $CPU vCPU, $RAM RAM, $DISK disk, $OS"
    echo ""
    if [ -n "$FAILED" ]; then
      echo "**The run failed during \`$CURRENT\`: $FAILED**"
      echo ""
    fi
    echo "| phase | time | what it did |"
    echo "|---|---|---|"
    local i=1
    printf '%b' "$PHASE_NAMES" | while IFS= read -r name; do
      [ -z "$name" ] && continue
      local ms note
      ms=$(printf '%b' "$PHASE_MS" | sed -n "${i}p")
      note=$(printf '%b' "$PHASE_NOTE" | sed -n "${i}p")
      echo "| $name | $(_fmt "$ms") | ${note} |"
      i=$(( i + 1 ))
    done
    echo ""
    if [ -z "$FAILED" ]; then
      if [ "$SKIP_DOCKER" = "1" ]; then
        echo "**Total: $(_fmt "${TOTAL_MS:-0}")**, and Docker was already installed"
        echo "(\`--skip-docker\`), so this is not a cold install: it is the time from a"
        echo "machine that can already run containers to a"
      else
        echo "**Total: $(_fmt "${TOTAL_MS:-0}")** from a machine with nothing on it to a"
      fi
      if [ "${PARTIAL:-1}" = "0" ]; then
        echo "store that has mined a fact, superseded it, and answers \`as_of\` correctly."
      else
        echo "store that accepts writes. No provider key, so no fact was mined:"
        echo "this is not a complete install."
      fi
    fi
    echo ""
    echo "Resolution: $RESOLUTION. Network at start: $(_fmt "${NETMS:-0}") to fetch a Debian index."
    echo ""
    echo "Measured by \`scripts/timed_install.sh\`. Rerunning on the same size of"
    echo "machine on a different day will not give the same number: image pulls"
    echo "and apt mirrors move by tens of seconds."
  } > "$file"
  echo ""
  cat "$file"
  echo ""
  echo "written to $(pwd)/$file"
}

# Rewrite one KEY=value line in a file. `sed -i` means two different things on
# GNU and BSD, and this script gets dry-run on a laptop before it is trusted on
# a VM, so it does not use it.
_setconf() {  # _setconf FILE KEY VALUE
  local file="$1" key="$2" value="$3" tmp
  tmp=$(mktemp)
  awk -v k="$key" -v v="$value" '
    index($0, k "=") == 1 { print k "=" v; found=1; next }
    { print }
    END { if (!found) print k "=" v }
  ' "$file" > "$tmp" && mv "$tmp" "$file"
}

_die() {
  FAILED="$1"
  echo "" >&2
  echo "FAILED during '$CURRENT': $1" >&2
  _report
  exit 1
}

# ------------------------------------------------------------- the machine --
echo "korely-agent: timed install on '$LABEL'"
echo "started $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

CPU=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null); [ -z "$CPU" ] && CPU="?"
RAM=$(free -m 2>/dev/null | awk '/^Mem:/{print $2"MB"}'); [ -z "$RAM" ] && RAM="?"
DISK=$(df -h / 2>/dev/null | awk 'NR==2{print $2}'); [ -z "$DISK" ] && DISK="?"
OS=$( (. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") || uname -s )
KERNEL=$(uname -r)
echo "$CPU vCPU, $RAM RAM, $DISK disk, $OS, kernel $KERNEL"

# A number that includes a slow apt mirror is still the customer's number, but
# it should be visible in the report rather than hidden inside a phase.
echo -n "network: "
NETMS=$( { S=$(_now); curl -fsS -o /dev/null --max-time 30 https://deb.debian.org/debian/dists/stable/Release 2>/dev/null; echo $(( $(_now) - S )); } )
echo "$(_fmt ${NETMS:-0}) to fetch a Debian index"

START_ALL=$(_now)

# ------------------------------------------------------------------ docker --
if [ "$SKIP_DOCKER" = "1" ]; then
  command -v docker >/dev/null 2>&1 || { echo "--skip-docker but docker is not installed" >&2; exit 2; }
  echo ""
  echo "== docker  (skipped: already installed, $(docker --version 2>/dev/null))"
else
  _phase "docker"
  export DEBIAN_FRONTEND=noninteractive
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq >/dev/null 2>&1 || _die "apt-get update"
    apt-get install -y -qq ca-certificates curl git >/dev/null 2>&1 || _die "apt-get install prerequisites"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc 2>/dev/null \
      || curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
      || _die "docker gpg key"
    chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    DISTRO="$ID"; [ "$DISTRO" != "debian" ] && DISTRO="ubuntu"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$DISTRO $VERSION_CODENAME stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq >/dev/null 2>&1 || _die "apt-get update after adding docker repo"
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1 \
      || _die "apt-get install docker"
  else
    _die "no apt-get: this script only knows Debian and Ubuntu"
  fi
  docker compose version >/dev/null 2>&1 || _die "docker compose plugin missing after install"
  _end "$(docker --version | sed 's/,.*//')"
fi

command -v git >/dev/null 2>&1 || apt-get install -y -qq git >/dev/null 2>&1

# ------------------------------------------------------------------- clone --
_phase "clone"
rm -rf "$DIR"
mkdir -p "$(dirname "$DIR")"
if [ -n "$FROM_LOCAL" ]; then
  cp -a "$FROM_LOCAL" "$DIR" || _die "copying from $FROM_LOCAL"
  _end "from local copy, not cloned"
else
  git clone --depth 1 "$REPO" "$DIR" >/dev/null 2>&1 || _die "git clone $REPO"
  _end "$(cd "$DIR" && git rev-parse --short HEAD)"
fi
cd "$DIR" || _die "cd $DIR"
# The mark that lets the next run replace this directory (the guard above).
: > "$MARK"

# --------------------------------------------------------------- configure --
_phase "configure"
cp .env.example .env || _die "no .env.example"
PGPASS=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
# Written the way a person would: three settings, nothing generated behind
# their back that they would then not know how to change.
_setconf .env DATABASE_URL "postgresql://korely:${PGPASS}@db:5432/korely"
_setconf .env POSTGRES_PASSWORD "$PGPASS"
# Generated, not left for somebody to remember. It encrypts the provider key
# when it is set from the screen, and an empty one means that page refuses to
# save rather than writing a customer's paid credential in clear text.
_setconf .env SECRET_KEY "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
if [ -n "${GOOGLE_API_KEY:-}" ]; then
  _setconf .env GOOGLE_API_KEY "$GOOGLE_API_KEY"
fi
# 80 and 443 by default, because that is the install being measured. They are
# overridable for the case where the machine already serves something, and for
# dry-running this script next to a stack that is already up.
API_PORT="${PORT:-8000}"
TLS_PORT="${HTTPS_PORT:-443}"
SITE="${DOMAIN:-localhost}"
[ -n "${DOMAIN:-}" ] && _setconf .env DOMAIN "$DOMAIN"
_setconf .env PORT "$API_PORT"
_setconf .env HTTP_PORT "${HTTP_PORT:-80}"
_setconf .env HTTPS_PORT "$TLS_PORT"
_end "3 settings, api on $API_PORT, $SITE on $TLS_PORT"

# -------------------------------------------------------------------- pull --
_phase "pull images"
docker compose pull -q db caddy >/dev/null 2>&1 || _die "docker compose pull"
# `config --images` ignores the service filter, so the locally built api image
# comes back too. It is the one without a registry tag.
# sort -u: the export directory's step runs on the database image, and the
# report listed it twice.
PULLED=$(for img in $(docker compose config --images 2>/dev/null | grep ':' | sort -u); do
  docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' "$img" 2>/dev/null | head -1
done | tr '\n' ' ')
_end "$PULLED"

# ------------------------------------------------------------------- build --
_phase "build api image"
docker compose build -q api >/dev/null 2>&1 || _die "docker compose build api"
IMGSIZE=$(docker images --format '{{.Size}}' "$(docker compose config --images api 2>/dev/null | head -1)" 2>/dev/null | head -1)
_end "${IMGSIZE:-built}"

# ---------------------------------------------------------------------- up --
_phase "start"
docker compose up -d >/dev/null 2>&1 || _die "docker compose up -d"
_end "3 containers, and the export directory step"

# ------------------------------------------------------------------- ready --
# The migrations run inside the api container's entrypoint, so this phase is
# "Postgres came up, the schema was built, and the process is serving".
_phase "schema + first response"
DEADLINE=$(( $(date +%s) + 300 ))
until curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:${API_PORT}/health" 2>/dev/null; do
  [ "$(date +%s)" -gt "$DEADLINE" ] && _die "/health never answered in 5 minutes: $(docker compose logs --tail 20 api 2>&1 | tail -5)"
  sleep 1
done
_end "$(curl -fsS "http://127.0.0.1:${API_PORT}/health" | head -c 120)"

# --------------------------------------------------------------------- tls --
# Caddy serves one site: the DOMAIN, or localhost when there is none. A TLS
# connection to the bare IP carries no SNI, matches no site, and is refused
# with a handshake alert — which reads like a broken install and is not one.
# --resolve keeps the name and still goes to this machine.
_phase "tls"
DEADLINE=$(( $(date +%s) + 180 ))
until curl -fsSk --resolve "${SITE}:${TLS_PORT}:127.0.0.1" -o /dev/null --max-time 5 \
        "https://${SITE}:${TLS_PORT}/health" 2>/dev/null; do
  [ "$(date +%s)" -gt "$DEADLINE" ] && _die "https://${SITE}:${TLS_PORT}/health never answered: $(docker compose logs --tail 10 caddy 2>&1 | tail -4)"
  sleep 1
done
CERT=$(echo | openssl s_client -connect "127.0.0.1:${TLS_PORT}" -servername "$SITE" 2>/dev/null \
        | openssl x509 -noout -issuer 2>/dev/null | sed 's/issuer=//')
_end "$SITE, ${CERT:-answering}"

# ------------------------------------------------------------------ tenant --
_phase "tenant"
OUT=$(docker compose exec -T api python -m korely_agent.manage create-tenant --name "Timed Install" 2>&1) \
  || _die "create-tenant: $OUT"
KEY=$(echo "$OUT" | awk '/^key /{print $2}')
[ -n "$KEY" ] || _die "no key in create-tenant output: $OUT"
TENANT=$(echo "$OUT" | awk '/^tenant /{print $2}')
# The administrative credential is a second key on purpose: the one the agents
# run with cannot reach /admin, so one of them leaking does not hand over the
# ability to issue more. Part of provisioning, so it is timed with it.
ADMIN_KEY=$(docker compose exec -T api python -m korely_agent.manage \
              issue-key --tenant "$TENANT" --name "operator" --admin 2>/dev/null | tr -d '\r')
[ -n "$ADMIN_KEY" ] || _die "no admin key issued"
# And a login for the screen. Generated, printed once, stored hashed: there is
# no default password to change later and forget.
OPOUT=$(docker compose exec -T api python -m korely_agent.manage \
          create-operator --tenant "$TENANT" --username "operator" 2>&1)
OP_PASSWORD=$(echo "$OPOUT" | awk '/^password /{print $2}')
[ -n "$OP_PASSWORD" ] || _die "no operator created: $OPOUT"
_end "one agent key, one operator key, one login"

# Everything another script needs to talk to this install, so the acceptance
# run and the timing run share one installation path instead of two.
cat > install.env <<ENV
API_URL=http://127.0.0.1:${API_PORT}
TLS_URL=https://${SITE}:${TLS_PORT}
SITE=${SITE}
TLS_PORT=${TLS_PORT}
TENANT=${TENANT}
KEY=${KEY}
ADMIN_KEY=${ADMIN_KEY}
OP_USERNAME=operator
OP_PASSWORD=${OP_PASSWORD}
ENV

API="http://127.0.0.1:${API_PORT}"
AUTH="Authorization: Bearer $KEY"

# ------------------------------------------------------------------- write --
_phase "first write"
W=$(curl -fsS -X POST "$API/v1/memories" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"content":"Customer 4821 is on the Priority Support plan.","user_id":"timed-install","timestamp":"2026-03-15"}' 2>&1) \
  || _die "POST /v1/memories: $W"
_end "201"

if [ -z "${GOOGLE_API_KEY:-}" ]; then
  echo ""
  echo "NO GOOGLE_API_KEY: stopping here. The store is up and accepts writes."
  echo "No fact will ever be mined, so the two phases that prove the product"
  echo "works are not measured and this total is not a complete install."
  PARTIAL=1
else
  PARTIAL=0
  # ------------------------------------------------------------------ fact --
  # This is the phase that matters. Everything before it is a store; the fact
  # appearing is the product.
  _phase "first fact mined"
  DEADLINE=$(( $(date +%s) + 180 ))
  FACTS="[]"
  while :; do
    FACTS=$(curl -fsS "$API/v1/facts?user_id=timed-install" -H "$AUTH" 2>/dev/null)
    echo "$FACTS" | grep -q '"predicate"' && break
    [ "$(date +%s)" -gt "$DEADLINE" ] && _die "no fact after 3 minutes. /health says: $(curl -fsS $API/health)"
    sleep 2
  done
  _end "$(echo "$FACTS" | head -c 160)"

  # ------------------------------------------------------------- supersede --
  # A second write that contradicts the first. If the store closes the old
  # fact instead of overwriting it, as_of answers differently before and
  # after, and that is the only claim on the landing page worth checking.
  _phase "supersede + as_of"
  curl -fsS -X POST "$API/v1/memories" -H "$AUTH" -H 'Content-Type: application/json' \
    -d '{"content":"Customer 4821 moved to the Basic plan.","user_id":"timed-install","timestamp":"2026-06-20"}' >/dev/null 2>&1 \
    || _die "second write"
  DEADLINE=$(( $(date +%s) + 180 ))
  while :; do
    NOW_F=$(curl -fsS "$API/v1/facts?user_id=timed-install" -H "$AUTH" 2>/dev/null)
    echo "$NOW_F" | grep -qi 'basic' && break
    [ "$(date +%s)" -gt "$DEADLINE" ] && _die "the second fact was never mined"
    sleep 2
  done
  THEN_F=$(curl -fsS "$API/v1/facts?user_id=timed-install&as_of=2026-04-01" -H "$AUTH" 2>/dev/null)
  echo "$THEN_F" | grep -qi 'priority' || _die "as_of 2026-04-01 does not show the March fact: $THEN_F"
  echo "$THEN_F" | grep -qi 'basic'    && _die "as_of 2026-04-01 shows a fact from June: $THEN_F"
  _end "April reads Priority, today reads Basic"
fi

TOTAL_MS=$(( $(_now) - START_ALL ))


_report

if [ "$KEEP" = "0" ]; then
  echo ""
  echo "stopping the stack (--keep to leave it running)"
  docker compose down -v >/dev/null 2>&1
fi
