#!/bin/sh
# Korely Agents on this machine, in one command (macOS or Linux; on Windows,
# inside WSL).
#
#   curl -fsSL https://raw.githubusercontent.com/korely-ai/korely/main/install.sh | sh
#
# or, to read it before it runs:
#
#   curl -fsSLO https://raw.githubusercontent.com/korely-ai/korely/main/install.sh
#   sh install.sh --help
#
# What it does, in a folder of its own (./korely unless told otherwise) and
# nowhere else on the disk: checks Docker; downloads this release's
# docker-compose.yml, docker-compose.image.yml, .env.example and Caddyfile and
# checks each against the sha256 this script carries (or, with --build,
# fetches the release's source and checks the same files in it); writes .env,
# readable by you alone, with two random secrets; writes ./korely, the one
# command to start, stop and remove this install; starts the database and the
# API; waits for /health; makes the account and the login with the commands
# in korely_agent/manage.py; and prints, once, the address, the login, the
# API key and how to give the key to Claude Code.
#
# Your own model key, optionally: the one model call here is the writer that
# mines facts from memories, billed to you. Given at the first install, it
# goes into .env as scripts/choose_models.py writes it (GOOGLE_API_KEY for
# Gemini; the OpenAI-compatible door, MISTRAL_API_*, for the others), the
# path an install configured from a terminal always had, and the dashboard
# then skips its first screen. Name the provider and the script asks for the
# key on the terminal, without showing it:
#
#   curl -fsSL .../install.sh | KORELY_PROVIDER=scaleway sh
#
# The key is never a flag (a flag is in `ps` and the shell's history), never
# typed on the command line for the same reason, and never taken from a key
# already in your shell (a GEMINI_API_KEY exported for another tool is not
# this install's to bill). Without a terminal: `read -rs KORELY_PROVIDER_KEY`,
# `export KORELY_PROVIDER_KEY`, then the command. Without a key at all, the
# dashboard's first screen asks for it (or Skip for now).
#
# Run again in the same folder, it starts what is there and makes nothing new:
# the account is in the database, and the database says it exists.
#
# It never asks for sudo, and never writes the password or the key to a file
# unless --save-credentials says so. Everything below is one function called
# on the last line, so a download cut half way runs nothing.
set -eu
# A CDPATH in the environment turns `cd korely` into a jump to another folder
# that has the same name, and makes `cd` print where it went into $(...).
unset CDPATH
# The patterns, sort and tr below read bytes, whatever the user's locale.
LC_ALL=C
export LC_ALL
# Everything this script writes is yours alone: .env, the mark, the checkout.
umask 077

# The release this script installs, and the oldest release it can install:
# 1.0.0 has no docker-compose.image.yml and no `manage --json`, and 1.0.9 is
# the first release published under the repository's present name
# (CHANGELOG 1.0.9): the images of the releases before it are under the
# name the repository had, where this script does not look, and 1.0.8 was
# never published. So nothing older than 1.0.9 is installed by it: an older
# release is installed by hand, as its own README says. A release from 1.0.9
# on that was not published stops at the download of its files. scripts/stamp_installer.py writes THIS_RELEASE
# and RELEASE_SHA256 at the tag (scripts/release.sh), and
# scripts/check_the_version.py holds both: the copy on main, which is what the
# line above downloads, installs the newest release.
THIS_RELEASE="1.0.37"
MIN_RELEASE="1.0.9"
# The files downloaded, and their sha256 at THIS_RELEASE's tag, one
# `sha256  path` a line. Empty in a checkout between the first release of
# this script and its tag: such a copy installs nothing unverified without
# --unverified.
RELEASE_FILES="docker-compose.yml docker-compose.image.yml .env.example deploy/Caddyfile"
RELEASE_SHA256="
817398faff21adea2c79e484433e59ea792032c55dadf688cc3c6fffa80ed98a  docker-compose.yml
9086424ae88eef613af2da364e30d7c4831ef7b87db25283889e20fc7dc39cbc  docker-compose.image.yml
3a8e75f54828ff22a68700d2855c4215b6188ea68f72ab5a228c562e3741f8c5  .env.example
213d1bce928e94b22204b7a418434b1e322470faf89072a57ade56cb10f8e394  deploy/Caddyfile
"
# The digest of THIS_RELEASE's image, which only exists once CI has pushed it:
# stamped after that by `scripts/stamp_installer.py digest` (the last step
# release.sh prints). Until then the image is pulled by its tag, with a
# warning.
THIS_IMAGE_DIGEST=""

# The public repository, owner/name, the one name the three addresses below
# are made of. The image is the one its CI pushes, ghcr.io/<owner>/<repository>
# in lower case (.github/workflows/image.yml, GITHUB_REPOSITORY), so a rename
# of the repository renames the image with it (CHANGELOG 1.0.9).
# scripts/release.sh and the terminal demos read it from here.
PUBLIC_REPO="korely-ai/korely"
REPO_URL="https://github.com/$PUBLIC_REPO.git"
RAW_URL="https://raw.githubusercontent.com/$PUBLIC_REPO"
IMAGE_REPO="ghcr.io/$PUBLIC_REPO"
# What a folder this script made carries, so that a second run recognises it
# and anything else is left alone.
MARK=".korely-install"
# docker-compose.image.yml takes the build away with `!reset`, which Compose
# reads from 2.24.4 on (January 2024).
COMPOSE_FOR_IMAGES="2.24.4"

STACK_UP=""
say() { printf '%s\n' "$*" >&2; }
step() { printf '\n==> %s\n' "$*" >&2; }
die() {
  printf '\ninstall.sh: %s\n' "$*" >&2
  if [ -n "$STACK_UP" ]; then
    printf '%s\n' "" \
      "The database and the API are running in $DIR, and the install did not finish." \
      "Run this script again to retry: it makes nothing twice. To remove it all:" \
      "  \"$DIR/korely\" uninstall --yes" >&2
  fi
  exit 1
}

usage() {
  cat <<EOF
Install Korely Agents $THIS_RELEASE with Docker, on this machine.

  sh install.sh [options]
  curl -fsSL $RAW_URL/main/install.sh | sh -s -- [options]

Options (each also as the environment variable on its right):
  --dir DIR             the folder to install into (default ./korely)       KORELY_DIR
  --version X.Y.Z       the release (default $THIS_RELEASE, this script's; from      KORELY_VERSION
                        $MIN_RELEASE on; another one needs --unverified)
  --unverified          install files this script has no sha256 for         KORELY_UNVERIFIED=1
  --port N              the port on 127.0.0.1 (default 8000)                KORELY_PORT
  --build               fetch the release's source and build the image
                        here, instead of pulling the published one         KORELY_BUILD=1
  --from-local PATH     build from this git checkout instead of GitHub      KORELY_FROM_LOCAL
                        (its HEAD, with the version its VERSION file says)
  --project NAME        the Docker Compose project (default korely)        KORELY_PROJECT
  --user NAME           the dashboard login (default admin)                 KORELY_USER
  --wait SECONDS        how long to wait for the API (default 300)          KORELY_WAIT
  --save-credentials    also write the login and the key to
                        DIR/credentials.txt, readable by you alone         KORELY_SAVE_CREDENTIALS=1
  -h, --help            this text

Your model key, for the writer that mines facts (optional, and checked by
nothing here: no call is made). Name the provider, and the key is asked on
the terminal without being shown:
  --provider NAME       gemini, scaleway, mistral, openai or openrouter     KORELY_PROVIDER
                        (or the region's: eu Scaleway, global Gemini)
  --region eu|global    where the facts are written: eu is Scaleway or      KORELY_REGION
                        Mistral, global the others (default: the provider's)
  --model NAME          the writing model (default: the one we measured)    KORELY_MODEL
Without a terminal, put the key in KORELY_PROVIDER_KEY without typing it on a
command line (bash or zsh): read -rs KORELY_PROVIDER_KEY; export KORELY_PROVIDER_KEY
A key already in your shell for another tool (GEMINI_API_KEY, GOOGLE_API_KEY,
OPENAI_API_KEY, ...) is never read. Without a key, the dashboard's first
screen asks for one (or Skip for now).

Run it again in the same folder to start what is there: nothing new is made.
EOF
}

# ── small tools ─────────────────────────────────────────────────────────────

# x.y.z compared as numbers (a -dev suffix ignored): 0 when $1 >= $2.
version_at_least() {
  printf '%s\n%s\n' "${1%%-*}" "${2%%-*}" | awk -F. '
    NR == 1 { a1 = $1 + 0; a2 = $2 + 0; a3 = $3 + 0 }
    NR == 2 { b1 = $1 + 0; b2 = $2 + 0; b3 = $3 + 0 }
    END {
      if (a1 != b1) exit !(a1 > b1)
      if (a2 != b2) exit !(a2 > b2)
      exit !(a3 >= b3)
    }'
}

# Random hex of $1 bytes, from openssl or, without it, the kernel.
random_hex() {
  if command -v openssl >/dev/null 2>&1; then
    rh_out=$(openssl rand -hex "$1") || rh_out=""
  else
    rh_out=""
  fi
  if [ -z "$rh_out" ]; then
    rh_out=$(od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n') || rh_out=""
  fi
  case "$rh_out" in
    *[!0-9a-f]*|"") die "could not read random bytes (openssl rand, /dev/urandom)" ;;
  esac
  [ "${#rh_out}" -eq $(( $1 * 2 )) ] || die "could not read $1 random bytes"
  printf '%s' "$rh_out"
}

# The sha256 of a file, with whichever of the two usual tools exists.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -- "$1" | cut -d' ' -f1
  else
    die "neither sha256sum nor shasum is here, and the release's files must be checked"
  fi
}

# The sha256 this script carries for a release file, or nothing.
expected_sha256() {
  printf '%s\n' "$RELEASE_SHA256" | awk -v f="$1" '$2 == f { print $1 }'
}

# One field of a JSON object, `manage --json`'s or /health's (which writes no
# spaces): a string, as long as it holds no quote or backslash (the ids, keys,
# passwords, dates and paths read here never do). Anything else answers empty,
# and the caller stops: a value read wrong would be printed as the login,
# which is worse than no value.
json_field() {
  printf '%s\n' "$1" | sed -n "s/.*\"$2\": *\"\\([^\"\\\\]*\\)\".*/\\1/p" | head -n 1
}

# How many objects of a `manage --json` list begin with the field $2 (each
# one does: the field is written first). A name holding the same text has
# its quotes escaped, so it is not counted.
json_count() {
  printf '%s\n' "$1" | awk -v pat="{\"$2\": \"" '
    { n = 0; s = $0
      while ((i = index(s, pat)) > 0) { n++; s = substr(s, i + length(pat)) } }
    END { print n + 0 }'
}

# The value of KEY in an env file, the last one written.
env_value() {
  sed -n "s/^$1=//p" "$2" | tail -n 1
}

# A folder as an absolute path, symbolic links resolved and CDPATH unread.
real_dir() {
  (cd -P -- "$1" >/dev/null 2>&1 && pwd -P)
}

# The writers .env can name, as scripts/choose_models.py writes them:
# id|label|region|OpenAI-compatible base (none: Gemini's own client)|model.
# Each row is a provider of services/model_choice.PROVIDERS, with its `base`,
# its region and its first preference, and the hosts are choose_models.py's:
# tests/test_the_one_command_install.py holds the three equal.
writer_row() {
  case "$1" in
    google|gemini) echo "google|Gemini|global||gemini-3.1-flash-lite" ;;
    scaleway) echo "scaleway|Scaleway|eu|https://api.scaleway.ai/v1|gpt-oss-120b" ;;
    mistral) echo "mistral|Mistral|eu|https://api.mistral.ai/v1|mistral-small-latest" ;;
    openai) echo "openai|OpenAI|global|https://api.openai.com/v1|gpt-4.1-mini" ;;
    openrouter) echo "openrouter|OpenRouter|global|https://openrouter.ai/api/v1|openai/gpt-oss-120b:nitro" ;;
    *) return 1 ;;
  esac
}

# The provider the first screen recommends for a region (model_choice,
# `recommended`).
recommended_for() {
  case "$1" in eu) echo scaleway ;; global) echo google ;; esac
}

# Downloads only from this release's folder on GitHub, only over HTTPS,
# checks the answer came from the address asked (a redirect elsewhere stops
# the install), and checks the bytes against the sha256 this script carries
# before the file takes its name.
fetch() {
  case "$1" in
    "$RAW_URL"/v[0-9]*) ;;
    *) die "refusing to download $1: not this project's release files" ;;
  esac
  fetch_got=$(curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --retry 3 \
      -o "$2.part" -w '%{url_effective}' "$1" </dev/null) || {
    rm -f -- "$2.part"
    die "could not download $1. Is $VERSION a release of $PUBLIC_REPO? (--build fetches the source instead)"
  }
  if [ "$fetch_got" != "$1" ]; then
    rm -f -- "$2.part"
    die "asked for $1 and was answered from $fetch_got: stopping"
  fi
  if [ -n "$VERIFY" ]; then
    check_sha256 "$2.part" "$3" || { rm -f -- "$2.part"; die "$3 of $VERSION is not the file of the release (sha256): nothing was installed from it"; }
  fi
  mv -- "$2.part" "$2"
}

check_sha256() {
  want=$(expected_sha256 "$2")
  [ -n "$want" ] || die "this script carries no sha256 for $2"
  [ "$(sha256_of "$1")" = "$want" ]
}

# docker compose for this install alone, through the folder's own ./korely
# (write_wrapper says why): the same command the summary prints.
dc() {
  "$DIR/korely" compose "$@"
}

manage() {
  dc exec -T api python -m korely_agent.manage "$@" </dev/null
}

# ── the steps ───────────────────────────────────────────────────────────────

parse_args() {
  DIR="${KORELY_DIR:-./korely}"
  VERSION="${KORELY_VERSION:-}"
  PORT_ASKED="${KORELY_PORT:-}"
  BUILD="${KORELY_BUILD:-}"
  FROM_LOCAL="${KORELY_FROM_LOCAL:-}"
  PROJECT_ASKED="${KORELY_PROJECT:-}"
  LOGIN="${KORELY_USER:-admin}"
  WAIT="${KORELY_WAIT:-300}"
  SAVE="${KORELY_SAVE_CREDENTIALS:-}"
  UNVERIFIED="${KORELY_UNVERIFIED:-}"
  PROVIDER="${KORELY_PROVIDER:-}"
  REGION="${KORELY_REGION:-}"
  MODEL="${KORELY_MODEL:-}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --dir) [ $# -ge 2 ] || die "--dir needs a folder"; DIR="$2"; shift 2 ;;
      --dir=*) DIR="${1#*=}"; shift ;;
      --version) [ $# -ge 2 ] || die "--version needs x.y.z"; VERSION="$2"; shift 2 ;;
      --version=*) VERSION="${1#*=}"; shift ;;
      --unverified) UNVERIFIED=1; shift ;;
      --port) [ $# -ge 2 ] || die "--port needs a number"; PORT_ASKED="$2"; shift 2 ;;
      --port=*) PORT_ASKED="${1#*=}"; shift ;;
      --build) BUILD=1; shift ;;
      --from-local) [ $# -ge 2 ] || die "--from-local needs a checkout"; FROM_LOCAL="$2"; shift 2 ;;
      --from-local=*) FROM_LOCAL="${1#*=}"; shift ;;
      --project) [ $# -ge 2 ] || die "--project needs a name"; PROJECT_ASKED="$2"; shift 2 ;;
      --project=*) PROJECT_ASKED="${1#*=}"; shift ;;
      --user) [ $# -ge 2 ] || die "--user needs a name"; LOGIN="$2"; shift 2 ;;
      --user=*) LOGIN="${1#*=}"; shift ;;
      --wait) [ $# -ge 2 ] || die "--wait needs seconds"; WAIT="$2"; shift 2 ;;
      --wait=*) WAIT="${1#*=}"; shift ;;
      --save-credentials) SAVE=1; shift ;;
      --provider) [ $# -ge 2 ] || die "--provider needs a name"; PROVIDER="$2"; shift 2 ;;
      --provider=*) PROVIDER="${1#*=}"; shift ;;
      --region) [ $# -ge 2 ] || die "--region needs eu or global"; REGION="$2"; shift 2 ;;
      --region=*) REGION="${1#*=}"; shift ;;
      --model) [ $# -ge 2 ] || die "--model needs a name"; MODEL="$2"; shift 2 ;;
      --model=*) MODEL="${1#*=}"; shift ;;
      --key|--key=*|--provider-key|--provider-key=*)
        die "the key is never a flag (a flag is in ps and in the shell's history): name the provider with --provider and the key is asked on the terminal" ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "unknown option: $1" ;;
    esac
  done

  for flag in BUILD SAVE UNVERIFIED; do
    eval "flag_value=\${$flag}"
    case "$flag_value" in
      1|true|yes) eval "$flag=1" ;;
      ""|0|false|no) eval "$flag=''" ;;
      *) die "'$flag_value' is not 1 or nothing (KORELY_BUILD, KORELY_SAVE_CREDENTIALS, KORELY_UNVERIFIED)" ;;
    esac
  done
  [ -n "$FROM_LOCAL" ] && BUILD=1
  PORT="${PORT_ASKED:-8000}"
  case "$PORT" in
    ""|*[!0-9]*) die "--port is '$PORT': a number from 1 to 65535" ;;
  esac
  [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port is $PORT: a number from 1 to 65535"
  case "$WAIT" in ""|*[!0-9]*) die "--wait is '$WAIT': a number of seconds" ;; esac
  PROJECT="${PROJECT_ASKED:-korely}"
  # Compose's own rule for a project name.
  case "$PROJECT" in
    ""|[!a-z0-9]*|*[!a-z0-9_-]*) die "--project is '$PROJECT': lowercase letters, digits, - and _, starting with a letter or digit" ;;
  esac
  LOGIN=$(printf '%s' "$LOGIN" | tr '[:upper:]' '[:lower:]')
  case "$LOGIN" in
    ""|*[!a-z0-9._-]*) die "--user is '$LOGIN': letters, digits, '.', '_' and '-'" ;;
  esac
  [ "${#LOGIN}" -le 64 ] || die "--user is longer than 64 characters"
  # Spaces are fine. These would break the commands printed at the end,
  # which put the folder between double quotes.
  case "$DIR" in
    *'"'*|*'$'*|*'`'*|*'\'*) die "--dir '$DIR': a folder whose name has no \", \$, \` or \\" ;;
  esac
  case "$DIR" in *'
'*) die "--dir has a newline in it" ;; esac
  VERSION_ASKED="$VERSION"
  VERIFY=""
  if [ -z "$FROM_LOCAL" ]; then
    VERSION="${VERSION:-$THIS_RELEASE}"
    # A release, never `latest` and never a -dev checkout: the tag and the
    # image are the same promise (scripts/release.sh).
    printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
      || die "--version is '$VERSION': a release, x.y.z"
    version_at_least "$VERSION" "$MIN_RELEASE" \
      || die "--version is $VERSION, older than this script ($MIN_RELEASE is the first release it can install): install $VERSION by hand, as its README says"
    if [ "$VERSION" = "$THIS_RELEASE" ] && [ -n "$(expected_sha256 docker-compose.yml)" ]; then
      VERIFY=1
    elif [ -z "$UNVERIFIED" ]; then
      if [ "$VERSION" = "$THIS_RELEASE" ]; then
        die "this copy of install.sh carries no sha256 of $VERSION's files (it is not a release's copy), so nothing it downloads can be checked. Use the copy on main, or say --unverified."
      fi
      die "this script carries the sha256 of $THIS_RELEASE's files, not of $VERSION's: install $THIS_RELEASE, use $VERSION's own install.sh, or say --unverified."
    fi
  fi
  choose_writer
}

# The writer. Its key comes from KORELY_PROVIDER_KEY or the terminal alone: a
# provider's own variable in the shell (a GEMINI_API_KEY exported for another
# tool) would be written into .env and billed without anybody asking for it.
# Decided before anything is touched, and never echoed: only the provider,
# the region and the model.
choose_writer() {
  WRITER_KEY="${KORELY_PROVIDER_KEY:-}"
  WRITER=""
  case "$REGION" in ""|eu|global) ;; *) die "--region is '$REGION': eu or global" ;; esac
  if [ -z "$WRITER_KEY$PROVIDER$REGION$MODEL" ]; then
    return 0
  fi
  if [ -z "$PROVIDER" ] && [ -z "$REGION" ]; then
    die "whose key is it? Say it with --provider (gemini, scaleway, mistral, openai, openrouter) or --region (eu: Scaleway; global: Gemini)"
  fi
  PROVIDER=$(printf '%s' "${PROVIDER:-$(recommended_for "$REGION")}" | tr '[:upper:]' '[:lower:]')
  WRITER=$(writer_row "$PROVIDER") \
    || die "--provider is '$PROVIDER': gemini, scaleway, mistral, openai or openrouter (the dashboard has the others)"
  WRITER_ID=$(printf '%s' "$WRITER" | cut -d'|' -f1)
  WRITER_LABEL=$(printf '%s' "$WRITER" | cut -d'|' -f2)
  WRITER_REGION=$(printf '%s' "$WRITER" | cut -d'|' -f3)
  WRITER_BASE=$(printf '%s' "$WRITER" | cut -d'|' -f4)
  WRITER_MODEL="${MODEL:-$(printf '%s' "$WRITER" | cut -d'|' -f5)}"
  if [ -n "$REGION" ] && [ "$REGION" != "$WRITER_REGION" ]; then
    die "$WRITER_LABEL writes in $WRITER_REGION, and --region asks for $REGION (eu: scaleway or mistral)"
  fi
  case "$WRITER_MODEL" in *[!A-Za-z0-9._:/-]*) die "--model is '$WRITER_MODEL': not a model name" ;; esac
  [ -n "$WRITER_KEY" ] || ask_for_the_key
  case "$WRITER_KEY" in
    *[!A-Za-z0-9._:+/=-]*) die "that key holds a character no provider key has (a space, a quote?)" ;;
  esac
}

# The key, from the terminal with the echo off: never on a command line,
# where the shell's history would keep it. /dev/tty, not stdin: under
# `curl | sh` stdin is this script.
ask_for_the_key() {
  if ! { (exec </dev/tty) && (exec >/dev/tty); } 2>/dev/null; then
    die "--provider $PROVIDER and no key, and no terminal to ask on. Put the key in the environment without typing it on a command line, in bash or zsh:
  read -rs KORELY_PROVIDER_KEY     # paste it, then Enter
  export KORELY_PROVIDER_KEY
then run this again. Or leave --provider out: the dashboard's first screen asks for it."
  fi
  tty_was=$(stty -g </dev/tty) || die "could not read the terminal's settings"
  trap 'stty "$tty_was" </dev/tty 2>/dev/null' EXIT
  trap 'stty "$tty_was" </dev/tty 2>/dev/null; exit 130' INT TERM HUP
  # The echo goes off before the question: a key pasted the moment it
  # appears is not shown either.
  stty -echo </dev/tty
  printf 'Your %s key (it is not shown as you type it): ' "$WRITER_LABEL" >/dev/tty
  IFS= read -r WRITER_KEY </dev/tty || WRITER_KEY=""
  stty "$tty_was" </dev/tty
  trap - EXIT INT TERM HUP
  printf '\n' >/dev/tty
  [ -n "$WRITER_KEY" ] || die "no key was given. Leave --provider out and the dashboard's first screen asks for it."
}

check_docker() {
  step "Checking Docker"
  command -v docker >/dev/null 2>&1 || die "Docker is not installed. Get it at https://docs.docker.com/get-docker/ (Docker Desktop on a Mac), then run this again."
  if ! docker info >/dev/null 2>&1 </dev/null; then
    die "Docker is installed and not answering. Start Docker Desktop (or the docker service), and on Linux make sure your user may use it (the docker group: https://docs.docker.com/engine/install/linux-postinstall/). This script never runs sudo."
  fi
  cv=$(docker compose version --short 2>/dev/null </dev/null) \
    || die "Docker Compose v2 is missing (the \`docker compose\` command). It comes with Docker Desktop; on Linux, the docker-compose-plugin package."
  cv=${cv#v}
  version_at_least "$cv" 2.0.0 || die "Docker Compose $cv is too old: version 2 or later."
  if [ -z "$BUILD" ] && ! version_at_least "$cv" "$COMPOSE_FOR_IMAGES"; then
    die "Docker Compose $cv cannot run the published image (it needs $COMPOSE_FOR_IMAGES or later): update Docker, or run with --build."
  fi
  command -v curl >/dev/null 2>&1 || die "curl is missing: it is how this script asks the API whether it is up."
  if [ -n "$BUILD" ]; then
    command -v git >/dev/null 2>&1 || die "git is missing: --build fetches the source with it."
  fi
  if [ -n "$VERIFY" ]; then
    command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 \
      || die "neither sha256sum nor shasum is here, and the release's files must be checked"
  fi
  # A warning, never a stop: Docker Desktop's memory is a setting, and the
  # install may still start on less.
  mem=$(docker info --format '{{.MemTotal}}' 2>/dev/null </dev/null) || mem=""
  case "$mem" in
    ""|*[!0-9]*) ;;
    *)
      if [ "$mem" -lt 3600000000 ]; then
        say "Warning: Docker has $(awk -v b="$mem" 'BEGIN { printf "%.1f", b / 1e9 }') GB of memory, and Korely wants about 4 GB"
        say "(the API alone may take 1.5). In Docker Desktop: Settings, Resources, Memory."
      fi ;;
  esac
  say "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null </dev/null || echo '?'), Compose $cv"
}

project_taken() {
  docker ps -aq --filter "label=com.docker.compose.project=$1" </dev/null \
    || die "could not ask Docker which projects exist"
  docker volume ls -q --filter "label=com.docker.compose.project=$1" </dev/null \
    || die "could not ask Docker which volumes exist"
}

# The folder: empty or new (a Finder's .DS_Store aside), or one this script
# made before.
prepare_dir() {
  FRESH=1
  if [ -e "$DIR" ] && [ ! -d "$DIR" ]; then
    die "$DIR exists and is not a folder"
  fi
  if [ -d "$DIR" ]; then
    if [ -f "$DIR/$MARK" ]; then
      FRESH=""
    elif [ -n "$(ls -A -- "$DIR" | grep -vx '.DS_Store' || true)" ]; then
      die "$DIR is not empty, and is not an install this script made: nothing was touched. Pick another --dir (or an empty folder)."
    fi
  else
    mkdir -- "$DIR" || die "could not make $DIR (does its parent folder exist?)"
  fi
  DIR=$(real_dir "$DIR") || die "could not enter $DIR"

  if [ -n "$FRESH" ]; then
    # Another install of the same project name would share its database.
    if [ -n "$(project_taken "$PROJECT")" ]; then
      die "Docker already has a project called '$PROJECT' (another install?), and this one would share its database. Pick another name with --project NAME."
    fi
    # Anything but "could not connect" (curl's 7) means the port is someone's:
    # an answer, a timeout, a reset.
    port_rc=0
    curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/" </dev/null || port_rc=$?
    if [ "$port_rc" -ne 7 ]; then
      die "something already holds 127.0.0.1:$PORT. Pick another port with --port N."
    fi
    if [ -n "$FROM_LOCAL" ]; then SOURCE=local; elif [ -n "$BUILD" ]; then SOURCE=build; else SOURCE=image; fi
    if [ "$SOURCE" = local ]; then
      FROM_LOCAL=$(real_dir "$FROM_LOCAL") || die "$FROM_LOCAL is not a folder"
      [ -f "$FROM_LOCAL/VERSION" ] || die "$FROM_LOCAL is not a checkout of korely-agent (no VERSION)"
      local_version=$(cat -- "$FROM_LOCAL/VERSION")
      if [ -n "$VERSION_ASKED" ] && [ "$VERSION_ASKED" != "$local_version" ]; then
        die "--version $VERSION_ASKED, and $FROM_LOCAL is $local_version"
      fi
      version_at_least "$local_version" "$MIN_RELEASE" \
        || die "$FROM_LOCAL is $local_version, older than this script ($MIN_RELEASE)"
      VERSION="$local_version"
    fi
    {
      echo "# Made by Korely's install.sh. A second run of it here starts this install"
      echo "# and makes nothing new. No secret is kept in this file."
      echo "release=$VERSION"
      echo "source=$SOURCE"
      echo "project=$PROJECT"
    } > "$DIR/$MARK"
  else
    SOURCE=$(env_value source "$DIR/$MARK")
    there=$(env_value release "$DIR/$MARK")
    their_project=$(env_value project "$DIR/$MARK")
    if [ -n "$VERSION_ASKED" ] && [ "$VERSION_ASKED" != "$there" ]; then
      die "$DIR holds $there, and --version asks for $VERSION_ASKED. This script installs and does not upgrade: the steps for its folder are in https://github.com/$PUBLIC_REPO/blob/main/docs/guide/operations.md#upgrading"
    fi
    if [ -n "$PROJECT_ASKED" ] && [ -n "$their_project" ] && [ "$PROJECT_ASKED" != "$their_project" ]; then
      die "$DIR is the Docker project '$their_project', and --project asks for '$PROJECT_ASKED': a folder is one install."
    fi
    if [ -f "$DIR/.env" ] && [ -n "$PORT_ASKED" ] && [ "$PORT_ASKED" != "$(env_value PORT "$DIR/.env")" ]; then
      die "$DIR answers on port $(env_value PORT "$DIR/.env") (its .env), and --port asks for $PORT_ASKED: change PORT in its .env instead."
    fi
    VERSION="$there"
    PROJECT="${their_project:-$PROJECT}"
    if [ "$VERSION" = "$THIS_RELEASE" ] && [ -n "$(expected_sha256 docker-compose.yml)" ]; then
      VERIFY=1
    fi
    say "$DIR is an install this script made ($VERSION): starting it."
  fi
  case "$SOURCE" in image|build|local) ;; *) die "$DIR/$MARK names no source" ;; esac
  write_wrapper
}

# ./korely: docker compose for this install and nothing else. The commands
# printed at the end go through it, because `docker compose` alone takes the
# shell's variables over .env's: a COMPOSE_PROJECT_NAME or a PORT exported for
# another project turned `down -v` into the deletion of that project's
# database. It unsets every name the compose files read (and Compose's own),
# names the project, and never starts Caddy.
write_wrapper() {
  {
    cat <<'EOF'
#!/bin/sh
# Korely Agents in this folder, written by install.sh:
#   ./korely start | stop | status | logs | manage <command> | uninstall --yes
#   ./korely compose <docker compose arguments>
set -eu
unset CDPATH
here=$(cd -P -- "$(dirname -- "$0")" >/dev/null && pwd -P)
cd -P -- "$here"
EOF
    printf 'project=%s\n' "$PROJECT"
    cat <<'EOF'
# .env decides, never the shell: the names the compose files read.
for name in $(cat docker-compose*.yml | grep -o '\${[A-Za-z_][A-Za-z0-9_]*' | cut -c3- | sort -u) \
    COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES; do
  unset "$name"
done
dc() { docker compose -p "$project" "$@"; }
case "${1:-}" in
  start) shift; dc up -d api "$@" ;;      # the database and the API, never Caddy
  stop) shift; dc down "$@" ;;            # the data stays
  status) dc ps ;;
  logs) shift; dc logs "$@" api ;;
  manage) shift; dc exec api python -m korely_agent.manage "$@" ;;
  compose) shift; dc "$@" ;;
  uninstall)
    if [ "${2:-}" != "--yes" ]; then
      echo "This deletes the database (every memory, fact, account and key), the image"
      echo "and the folder $here. To go on: \"$here/korely\" uninstall --yes"
      exit 1
    fi
    # facts/ belongs to the API's user (uid 1000), not always yours: emptied
    # from a container, as root, before the folder goes.
    dc run --rm --no-deps -T --entrypoint sh facts-dir -c \
      'rm -rf /facts/* /facts/.[!.]* /facts/..?*' </dev/null \
      || echo "facts/ could not be emptied from a container; it may need removing by hand."
    dc down -v --remove-orphans
    image=$(sed -n 's/^KORELY_IMAGE=//p' .env 2>/dev/null | tail -n 1)
    image=${image:-$project-api}
    docker image rm "$image" >/dev/null 2>&1 \
      || echo "The image $image was kept (another container uses it, or it is gone): docker image rm $image"
    cd /
    rm -rf -- "$here" || { echo "Remove the folder by hand: rm -rf \"$here\""; exit 1; }
    echo "Removed: the containers, the database, the image and $here."
    ;;
  *)
    echo "usage: \"$here/korely\" start | stop | status | logs | manage <command> | uninstall --yes | compose <args>" >&2
    exit 2 ;;
esac
EOF
  } > "$DIR/korely.part"
  chmod 700 "$DIR/korely.part"
  mv -- "$DIR/korely.part" "$DIR/korely"
}

get_files() {
  if [ -f "$DIR/docker-compose.yml" ] && [ -f "$DIR/.env.example" ] \
      && [ -n "$(env_value files "$DIR/$MARK")" ]; then
    return 0
  fi
  case "$SOURCE" in
    image)
      step "Downloading the files of release $VERSION"
      mkdir -p -- "$DIR/deploy"
      for f in $RELEASE_FILES; do
        fetch "$RAW_URL/v$VERSION/$f" "$DIR/$f" "$f"
      done
      ;;
    build|local)
      # init and fetch, not clone: clone wants an empty folder, and this one
      # holds the mark, so a run stopped half way can be run again.
      if [ "$SOURCE" = build ]; then
        step "Fetching the source of release $VERSION"
        from="$REPO_URL"
        ref="refs/tags/v$VERSION"
      else
        step "Fetching the source of $FROM_LOCAL"
        from="$FROM_LOCAL"
        ref="HEAD"
      fi
      git -C "$DIR" init -q </dev/null >&2
      # No prompt for a login: the repository is public, and a question on a
      # terminal fed by `curl | sh` would wait for ever.
      GIT_TERMINAL_PROMPT=0 git -C "$DIR" fetch -q --depth 1 "$from" "$ref" </dev/null \
        || die "could not fetch $ref from $from"
      git -C "$DIR" -c advice.detachedHead=false checkout -q FETCH_HEAD </dev/null
      if [ "$SOURCE" = build ]; then
        git -C "$DIR" tag "v$VERSION" FETCH_HEAD </dev/null 2>/dev/null || true
        git -C "$DIR" remote add origin "$REPO_URL" </dev/null 2>/dev/null || true
        got=$(cat -- "$DIR/VERSION")
        [ "$got" = "$VERSION" ] || die "the tag v$VERSION says it is $got: stopping"
        if [ -n "$VERIFY" ]; then
          for f in $RELEASE_FILES; do
            check_sha256 "$DIR/$f" "$f" \
              || die "$f at the tag v$VERSION is not the file of the release (sha256): stopping before anything runs"
          done
        fi
      fi
      ;;
  esac
  [ -f "$DIR/docker-compose.yml" ] && [ -f "$DIR/.env.example" ] \
    || die "the files of $VERSION are not all there: run this again"
  if [ "$SOURCE" = local ]; then files=local; elif [ -n "$VERIFY" ]; then files=verified; else files=unverified; fi
  echo "files=$files" >> "$DIR/$MARK"
  [ "$files" = unverified ] && say "Warning: the files of $VERSION were not checked against a sha256 (--unverified)."
  return 0
}

# .env from .env.example, readable by you alone, with what this script decides.
write_env() {
  WRITER_WRITTEN=""
  if [ -f "$DIR/.env" ]; then
    chmod 600 "$DIR/.env"
    if [ -n "$WRITER" ]; then
      say "This install has its .env already: the key was not written. Change the writer"
      say "in the dashboard (Settings, Writing model) or with python3 scripts/choose_models.py."
    fi
    unset KORELY_PROVIDER_KEY WRITER_KEY
    return 0
  fi
  # A database made with another .env's password: a new one would not open it.
  if [ -n "$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" </dev/null)" ]; then
    die "$DIR has no .env, and the Docker project '$PROJECT' still has its database, made with the password of the .env that is gone: a new .env would not open it. Put the old .env back (from your backup), or delete that data with \"$DIR/korely\" uninstall --yes and run this again."
  fi
  step "Writing .env"
  grep -q '^POSTGRES_PASSWORD=' "$DIR/.env.example" && grep -q '^SECRET_KEY=' "$DIR/.env.example" \
    || die ".env.example of $VERSION has no POSTGRES_PASSWORD= or SECRET_KEY= line"
  ENV_KEYS="POSTGRES_PASSWORD SECRET_KEY PORT PUBLIC_BASE_URL COMPOSE_PROJECT_NAME"
  ENV_POSTGRES_PASSWORD=$(random_hex 16)
  ENV_SECRET_KEY=$(random_hex 32)
  ENV_PORT="$PORT"
  # No Caddy on a laptop (start_stack): the address the API advertises to MCP
  # and OAuth clients is the one it answers on.
  ENV_PUBLIC_BASE_URL="http://127.0.0.1:$PORT"
  ENV_COMPOSE_PROJECT_NAME="$PROJECT"
  ENV_COMPOSE_FILE=""
  ENV_KORELY_IMAGE=""
  ENV_GOOGLE_API_KEY=""; ENV_EXTRACTION_PROVIDER=""; ENV_EXTRACTION_MODEL=""
  ENV_MISTRAL_API_BASE=""; ENV_MISTRAL_API_KEY=""; ENV_MISTRAL_EXTRACTION_MODEL=""
  if [ -n "$WRITER" ] && [ -z "$WRITER_BASE" ]; then
    # Gemini keeps its own client (scripts/choose_models.py).
    ENV_KEYS="$ENV_KEYS GOOGLE_API_KEY EXTRACTION_PROVIDER EXTRACTION_MODEL"
    ENV_GOOGLE_API_KEY="$WRITER_KEY"
    ENV_EXTRACTION_PROVIDER="gemini"
    ENV_EXTRACTION_MODEL="$WRITER_MODEL"
  elif [ -n "$WRITER" ]; then
    # The OpenAI-compatible door, named mistral since it was Mistral's.
    ENV_KEYS="$ENV_KEYS EXTRACTION_PROVIDER MISTRAL_API_BASE MISTRAL_API_KEY MISTRAL_EXTRACTION_MODEL"
    ENV_EXTRACTION_PROVIDER="mistral"
    ENV_MISTRAL_API_BASE="$WRITER_BASE"
    ENV_MISTRAL_API_KEY="$WRITER_KEY"
    ENV_MISTRAL_EXTRACTION_MODEL="$WRITER_MODEL"
  fi
  if [ "$SOURCE" = image ]; then
    ENV_KEYS="$ENV_KEYS COMPOSE_FILE KORELY_IMAGE"
    ENV_COMPOSE_FILE="docker-compose.yml:docker-compose.image.yml"
    if [ "$VERSION" = "$THIS_RELEASE" ] && [ -n "$THIS_IMAGE_DIGEST" ]; then
      ENV_KORELY_IMAGE="$IMAGE_REPO:$VERSION@$THIS_IMAGE_DIGEST"
    else
      ENV_KORELY_IMAGE="$IMAGE_REPO:$VERSION"
    fi
  fi
  # The values reach awk in its environment, not its arguments, which any
  # user of the machine can read in `ps`.
  ENV_KEYS="$ENV_KEYS" ENV_POSTGRES_PASSWORD="$ENV_POSTGRES_PASSWORD" \
  ENV_SECRET_KEY="$ENV_SECRET_KEY" ENV_PORT="$ENV_PORT" \
  ENV_PUBLIC_BASE_URL="$ENV_PUBLIC_BASE_URL" ENV_COMPOSE_PROJECT_NAME="$ENV_COMPOSE_PROJECT_NAME" \
  ENV_COMPOSE_FILE="$ENV_COMPOSE_FILE" ENV_KORELY_IMAGE="$ENV_KORELY_IMAGE" \
  ENV_GOOGLE_API_KEY="$ENV_GOOGLE_API_KEY" ENV_EXTRACTION_PROVIDER="$ENV_EXTRACTION_PROVIDER" \
  ENV_EXTRACTION_MODEL="$ENV_EXTRACTION_MODEL" ENV_MISTRAL_API_BASE="$ENV_MISTRAL_API_BASE" \
  ENV_MISTRAL_API_KEY="$ENV_MISTRAL_API_KEY" ENV_MISTRAL_EXTRACTION_MODEL="$ENV_MISTRAL_EXTRACTION_MODEL" \
  awk '
    BEGIN { n = split(ENVIRON["ENV_KEYS"], k, " ")
            for (i = 1; i <= n; i++) want[k[i]] = ENVIRON["ENV_" k[i]] }
    match($0, /^[A-Z_][A-Z0-9_]*=/) {
      key = substr($0, 1, RLENGTH - 1)
      if (key in want) { print key "=" want[key]; done[key] = 1; next }
    }
    { print }
    END {
      head = 0
      for (i = 1; i <= n; i++) if (!(k[i] in done)) {
        if (!head) {
          print ""
          print "# Written by install.sh. COMPOSE_PROJECT_NAME is the name Docker gives this"
          print "# install (its containers and volumes); PUBLIC_BASE_URL the address the API"
          print "# answers on, without Caddy; COMPOSE_FILE and KORELY_IMAGE, when present,"
          print "# run the published image instead of building one (docker-compose.image.yml);"
          print "# EXTRACTION_PROVIDER and the lines after it, the writer of your key, as"
          print "# scripts/choose_models.py writes them."
          head = 1
        }
        print k[i] "=" want[k[i]]
      }
    }' "$DIR/.env.example" > "$DIR/.env.part"
  chmod 600 "$DIR/.env.part"
  mv -- "$DIR/.env.part" "$DIR/.env"
  unset ENV_POSTGRES_PASSWORD ENV_SECRET_KEY ENV_GOOGLE_API_KEY ENV_MISTRAL_API_KEY \
    KORELY_PROVIDER_KEY WRITER_KEY
  say ".env written (readable by you alone), with a random database password and SECRET_KEY."
  if [ -n "$WRITER" ]; then
    say "The writer: $WRITER_LABEL ($WRITER_REGION), $WRITER_MODEL, with your key (in .env only)."
    WRITER_WRITTEN=1
  fi
}

# The database and the API. Not Caddy: on a laptop the address is
# http://127.0.0.1:PORT, and Caddy would want 443 and 80 for a certificate
# nobody trusts. `docker compose up -d` starts it too, for a server with a
# DOMAIN (docs/guide/install.md).
start_stack() {
  PORT=$(env_value PORT "$DIR/.env")
  PORT="${PORT:-8000}"
  if [ "$SOURCE" = image ]; then
    image=$(env_value KORELY_IMAGE "$DIR/.env")
    [ -n "$image" ] || die "$DIR/.env names no KORELY_IMAGE"
    case "$image" in
      *@sha256:*) ;;
      *) say "Warning: $image is pulled by its tag: this script carries no digest of it yet." ;;
    esac
    if ! docker image inspect "$image" >/dev/null 2>&1 </dev/null; then
      step "Pulling $image (about 2.5 GB, once)"
      docker pull "$image" </dev/null >&2 \
        || die "could not pull $image. A release whose image is not published yet installs with --build."
    fi
    step "Starting Korely Agents"
    dc up -d api </dev/null >&2
  elif [ -n "$FRESH" ]; then
    # --build the first time: an image left by an earlier install of the
    # same project name would otherwise be started in place of this one.
    step "Building the image and starting Korely Agents (a few minutes, once)"
    dc up -d --build api </dev/null >&2
  else
    step "Starting Korely Agents"
    dc up -d api </dev/null >&2
  fi
  STACK_UP=1
}

wait_for_health() {
  step "Waiting for the API on http://127.0.0.1:$PORT (the first start applies the migrations and loads the embedding model)"
  waited=0
  until HEALTH=$(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/health" 2>/dev/null </dev/null); do
    if [ "$waited" -ge "$WAIT" ]; then
      say ""
      say "The API did not answer within $WAIT seconds. Its last lines:"
      dc logs --tail 30 api </dev/null >&2 || true
      die "the API is not up. To see why: \"$DIR/korely\" logs
(then run this again, or with --wait 600 on a slow machine)"
    fi
    sleep 2
    waited=$(( waited + 2 ))
    [ $(( waited % 20 )) -eq 0 ] && say "  still starting ($waited s)"
  done
  RUNNING=$(json_field "$HEALTH" version)
  MCP_PATH=$(json_field "$HEALTH" mcp_path)
  MCP_PATH="${MCP_PATH:-/mcp}"
  EXTRACTION=$(printf '%s\n' "$HEALTH" | sed -n 's/.*"extraction": *{"state": *"\([^"]*\)".*/\1/p')
  say "Up: version ${RUNNING:-unknown}."
  if [ -n "$RUNNING" ] && [ "$RUNNING" != "$VERSION" ]; then
    say "Warning: the folder holds $VERSION and the API says it is $RUNNING."
  fi
}

# The account and the login, unless the database already has them.
make_account() {
  step "Making the account and the login"
  ops=$(manage list-operators --json) || die "could not list the logins (manage list-operators)"
  if [ "$(json_count "$ops" username)" -gt 0 ]; then
    EXISTING_LOGIN=$(json_field "$ops" username)
    tenants=$(manage list-tenants --json) || tenants="[]"
    EXISTING_TENANT=$(json_field "$tenants" id)
    say "The account already exists: nothing new was made."
    return 0
  fi
  EXISTING_LOGIN=""
  tenants=$(manage list-tenants --json) || die "could not list the accounts (manage list-tenants)"
  n=$(json_count "$tenants" id)
  if [ "$n" -eq 0 ]; then
    out=$(manage create-tenant --name "My account" --json) || die "manage create-tenant failed"
  elif [ "$n" -eq 1 ]; then
    # A run stopped between the two commands: the account exists and its key
    # was never shown. A new key for it, and the login it lacks.
    tid=$(json_field "$tenants" id)
    out=$(manage issue-key --tenant "$tid" --name "install.sh $(date +%Y%m%d%H%M%S)" --json) \
      || die "manage issue-key failed"
  else
    die "this database has $n accounts and no login: make one by hand,
  \"$DIR/korely\" manage list-tenants
  \"$DIR/korely\" manage create-operator --tenant <id> --username $LOGIN"
  fi
  TENANT=$(json_field "$out" tenant)
  KEY=$(json_field "$out" key)
  EXPIRES=$(json_field "$out" expires)
  case "$TENANT" in
    *[!0-9a-f-]*|"") die "could not read the account id in the answer of manage" ;;
  esac
  case "$KEY" in
    kor_*) ;;
    *) die "could not read the API key in the answer of manage" ;;
  esac
  out=$(manage create-operator --tenant "$TENANT" --username "$LOGIN" --json) \
    || die "manage create-operator failed (the account $TENANT exists)"
  PASSWORD=$(json_field "$out" password)
  [ -n "$PASSWORD" ] || die "could not read the password in the answer of manage"
}

save_credentials() {
  [ -n "$SAVE" ] || return 0
  if [ -n "$EXISTING_LOGIN" ]; then
    say "--save-credentials: nothing new to save, the account was made before."
    return 0
  fi
  {
    echo "Korely Agents, $DIR"
    echo "dashboard  http://127.0.0.1:$PORT"
    echo "login      $LOGIN"
    echo "password   $PASSWORD"
    echo "api key    $KEY"
    echo "expires    ${EXPIRES:-never}"
  } > "$DIR/credentials.txt"
  chmod 600 "$DIR/credentials.txt"
}

summary() {
  url="http://127.0.0.1:$PORT"
  k="\"$DIR/korely\""
  echo
  echo "Korely Agents ${RUNNING:-$VERSION} is running in $DIR"
  if [ -z "$EXISTING_LOGIN" ]; then
    echo
    echo "  Dashboard  $url"
    echo "  Login      $LOGIN"
    echo "  Password   $PASSWORD"
    echo "  API key    $KEY"
    echo "             (expires ${EXPIRES:-never})"
    echo
    echo "The password and the key are shown this once and stored only as hashes: copy them now."
    [ -n "$SAVE" ] && echo "A copy is in $DIR/credentials.txt, readable by you alone."
    echo
    echo "  Claude Code, with the key in a variable rather than in your shell's history"
    echo "  (claude keeps it in its own settings):"
    echo "    read -rs KORELY_API_KEY      # paste the key above, then Enter (bash or zsh)"
    echo "    claude mcp add --transport http korely $url$MCP_PATH --header \"Authorization: Bearer \$KORELY_API_KEY\""
    echo
    if [ -n "$WRITER_WRITTEN" ]; then
      echo "Facts are written by $WRITER_LABEL ($WRITER_REGION), $WRITER_MODEL, with your key in .env;"
      echo "nothing has called it yet (/health: ${EXTRACTION:-?}). Settings, Writing model changes it."
    else
      echo "The dashboard's first screen asks where facts are written and for that provider's"
      echo "key; \"Skip for now\" works too: memories are stored and searched meanwhile."
    fi
  else
    echo "The account was made at the first install, and nothing new was made."
    echo
    echo "  Dashboard  $url"
    echo "  Login      $EXISTING_LOGIN (its password was shown once, at the first install)"
    echo
    echo "  A new password:  $k manage reset-password --username $EXISTING_LOGIN"
    echo "  A new API key:   $k manage issue-key --tenant ${EXISTING_TENANT:-<id>} --name laptop"
  fi
  echo
  echo "  Stop:       $k stop          (your data stays)"
  echo "  Start:      $k start         (or run this script again)"
  echo "  Uninstall:  $k uninstall --yes"
  echo "              (deletes every memory in it, the image and the folder)"
}

main() {
  parse_args "$@"
  check_docker
  prepare_dir
  get_files
  write_env
  start_stack
  wait_for_health
  make_account
  save_credentials
  summary
}

main "$@"
