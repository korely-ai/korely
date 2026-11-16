#!/usr/bin/env bash
# The test the product has to pass, run against a real installation.
#
#   GOOGLE_API_KEY=... bash scripts/acceptance.sh --label laptop
#
# It installs from source on this machine, then interrogates the result from
# outside as a customer would. It grows a block per sprint, and when the last
# one lands it IS the final test rather than something that resembles it:
#
#   an empty machine, one command, open the address, log in, see the dashboard,
#   point an SDK at it, write a memory and read it back, and nothing left us.
#
# Three rules, and the third is the one that took a week to learn.
#
#   It runs against a real install, not fixtures.
#   Every block must go red when the thing it checks is deliberately broken.
#   No model decides whether anything passed. Rows, status codes, numbers.
#
# Flags:
#   --label NAME     goes in the output. Required by timed_install.
#   --keep           leave the stack up afterwards
#   --on DIR         use the install already in DIR instead of making one
set -u

LABEL="acceptance"
KEEP=0
ON=""
HERE="$(cd "$(dirname "$0")/.." && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --label) LABEL="${2:-}"; shift 2 ;;
    --keep)  KEEP=1; shift ;;
    --on)    ON="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# Everything this suite creates carries this, because names are unique within
# an account and a second run would otherwise collide with the first run's
# leftovers: 409, no id in the reply, and a cascade of failures that look like
# the product and are the suite.
RUN="$(date +%H%M%S)"

# ok, bad, block, is, has, hasnt, nomatch, and the counters.
. "$HERE/scripts/acceptance_checks.sh"

code() {  # code METHOD URL [AUTH] [BODY]
  local m="$1" u="$2" a="${3:-}" b="${4:-}"
  if [ -n "$b" ]; then
    curl -s -o /dev/null -w '%{http_code}' -X "$m" "$u" \
      ${a:+-H "Authorization: Bearer $a"} -H 'Content-Type: application/json' -d "$b"
  else
    curl -s -o /dev/null -w '%{http_code}' -X "$m" "$u" ${a:+-H "Authorization: Bearer $a"}
  fi
}

body() {  # body METHOD URL [AUTH] [BODY]
  local m="$1" u="$2" a="${3:-}" b="${4:-}"
  if [ -n "$b" ]; then
    curl -s -X "$m" "$u" ${a:+-H "Authorization: Bearer $a"} \
      -H 'Content-Type: application/json' -d "$b"
  else
    curl -s -X "$m" "$u" ${a:+-H "Authorization: Bearer $a"}
  fi
}

# ────────────────────────────────────────────────────────── the installation ──
if [ -n "$ON" ]; then
  DIR="$ON"
  echo "using the installation in $DIR"
else
  DIR="${TMPDIR:-/tmp}/korely-acceptance"
  echo "installing into $DIR"
  # A previous run left with --keep still holds the ports, and the failure that
  # follows looks like a broken install rather than a busy machine. Take the old
  # one down before the directory it lives in is removed.
  if [ -f "$DIR/docker-compose.yml" ]; then
    echo "  (taking down the stack from a previous run)"
    (cd "$DIR" && docker compose down -v >/dev/null 2>&1)
  fi
  rm -rf "$DIR"
  PORT="${PORT:-8123}" HTTP_PORT="${HTTP_PORT:-8180}" HTTPS_PORT="${HTTPS_PORT:-8543}" \
    bash "$HERE/scripts/timed_install.sh" --label "$LABEL" --skip-docker \
      --from-local "$HERE" --dir "$DIR" --keep > "$DIR.install.log" 2>&1 \
    || { echo "the installation itself failed:"; tail -25 "$DIR.install.log"; exit 1; }
  echo "installed. $(grep -E '^\*\*Total' "$DIR/install-report-$LABEL"-*.md 2>/dev/null | head -1)"
fi

# `install.env` is written by timed_install.sh. An install made by following
# the README has none, and refusing to run against it made this suite useless
# to the one person who most wants it: somebody who has just installed the
# thing and would like to know whether it is right.
if [ ! -f "$DIR/install.env" ]; then
  echo "  no install.env here, so making what this needs from the install itself"
  OUT=$(cd "$DIR" && docker compose exec -T api python -m korely_agent.manage \
          create-tenant --name "Acceptance $(date +%H%M%S)" 2>&1) \
    || { echo "could not reach the install in $DIR:"; echo "$OUT"; exit 1; }
  TENANT=$(echo "$OUT" | awk '/^tenant /{print $2}')
  KEY=$(echo "$OUT" | awk '/^key /{print $2}')
  ADMIN_KEY=$(cd "$DIR" && docker compose exec -T api python -m korely_agent.manage \
                issue-key --tenant "$TENANT" --name "acceptance operator" --admin 2>/dev/null | tr -d '\r')
  OPUSER="acceptance$(date +%H%M%S)"
  OPOUT=$(cd "$DIR" && docker compose exec -T api python -m korely_agent.manage \
            create-operator --tenant "$TENANT" --username "$OPUSER" 2>&1)
  OP_USERNAME="$OPUSER"
  OP_PASSWORD=$(echo "$OPOUT" | awk '/^password /{print $2}')
  # The ports and the name come out of the install's own .env.
  API_PORT=$(grep -E "^PORT=" "$DIR/.env" 2>/dev/null | cut -d= -f2); API_PORT="${API_PORT:-8000}"
  TLS_PORT=$(grep -E "^HTTPS_PORT=" "$DIR/.env" 2>/dev/null | cut -d= -f2); TLS_PORT="${TLS_PORT:-443}"
  SITE=$(grep -E "^DOMAIN=" "$DIR/.env" 2>/dev/null | cut -d= -f2); SITE="${SITE:-localhost}"
  API_URL="http://127.0.0.1:${API_PORT}"
  TLS_URL="https://${SITE}:${TLS_PORT}"
  cat > "$DIR/install.env" <<ENV
API_URL=${API_URL}
TLS_URL=${TLS_URL}
SITE=${SITE}
TLS_PORT=${TLS_PORT}
TENANT=${TENANT}
KEY=${KEY}
ADMIN_KEY=${ADMIN_KEY}
OP_USERNAME=${OP_USERNAME}
OP_PASSWORD=${OP_PASSWORD}
ENV
fi
# shellcheck disable=SC1091
. "$DIR/install.env"

# A stored password that no longer works fails this suite in thirteen places,
# none of which says "the password is stale". Check it once, here, and mend it.
if [ -n "${OP_USERNAME:-}" ]; then
  CHECK=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${API_URL}/admin/session" \
          -H 'Content-Type: application/json' \
          -d "{\"username\":\"$OP_USERNAME\",\"password\":\"$OP_PASSWORD\"}" 2>/dev/null)
  if [ "$CHECK" != "200" ]; then
    echo "  (the stored password no longer works, getting a new one)"
    NEW=$(cd "$DIR" && docker compose exec -T api python -m korely_agent.manage \
            reset-password --username "$OP_USERNAME" 2>/dev/null | awk '/^password /{print $2}' | tr -d '\r')
    [ -n "$NEW" ] && OP_PASSWORD="$NEW" && sed -i "s|^OP_PASSWORD=.*|OP_PASSWORD=$NEW|" "$DIR/install.env"
  fi
fi

# What this suite registered that would act on its own after it ends: the
# webhook endpoint the S1 block tests delivery with. Subscribed to every event,
# it went on receiving the install's real deliveries at an address that is
# nobody's (functional test of 28 Oct 2026). Removed where it is tested, and
# here too when the run stops before that.
THID=""
cleanup() {
  if [ -n "$THID" ]; then
    code DELETE "$API_URL/admin/webhooks/$THID" "$ADMIN_KEY" >/dev/null 2>&1
  fi
  if [ "$KEEP" = "0" ] && [ -z "$ON" ]; then
    (cd "$DIR" && docker compose down -v >/dev/null 2>&1)
  fi
}
trap cleanup EXIT

# a_model_key_given: whether whoever runs this gave a model key, read apart
# from what the install says about itself: GOOGLE_API_KEY, as the line at the
# top says (timed_install.sh writes it into the install's .env), or a key in
# the install's own .env (an install made by following the README, --on): a
# setting named like config.py's model keys (google_api_key, mistral_api_key),
# its last line written, as install.sh's env_value reads it. Only whether it
# is empty is looked at; the value is never printed.
a_model_key_given() {
  [ -n "${GOOGLE_API_KEY:-}" ] && return 0
  local name
  for name in $(sed -n 's/^\([A-Z0-9_]*_API_KEY\)=.*/\1/p' "$DIR/.env" 2>/dev/null | sort -u); do
    [ -n "$(sed -n "s/^$name=//p" "$DIR/.env" | tail -n 1)" ] && return 0
  done
  return 1
}

# Whether a model writes here, as the install says (GET /admin/models,
# `configured`). With none and no model key given, the checks that read what
# a model extracted are not run, and said so (`with_a_writer`,
# acceptance_checks.sh `unmet`): up to 1.0.28 they failed on every run
# without a model key and a real failure hid among them. With a key given,
# an install that says none fails here, and those checks run and fail too:
# the key did not reach the writer (a .env written wrong, the install's
# reading of it). An answer that says neither leaves them to run, and fail
# if the install cannot write.
MODELS=$(body GET "$API_URL/admin/models" "$ADMIN_KEY")
case "$MODELS" in
  *'"configured":true'*)  WRITER=configured ;;
  *'"configured":false'*) WRITER=none ;;
  *)                      WRITER=unknown ;;
esac
if a_model_key_given; then KEY_GIVEN=yes; else KEY_GIVEN=no; fi
echo "a model that writes: $WRITER (a model key given: $KEY_GIVEN)"
if [ "$KEY_GIVEN" = yes ]; then
  block "the model key given reaches the install"
  is "the install says a model writes" "configured" "$WRITER"
fi

# with_a_writer [check args...]: a check that needs a model that writes; with
# no check, the rest of the block does. Not run only on an install with none
# when no model key was given.
with_a_writer() {
  if [ "$WRITER" = none ] && [ "$KEY_GIVEN" = no ]; then unmet "needs a model key" "$@"
  elif [ $# -gt 0 ]; then "$@"; fi
}

# ═══════════════════════════════════════════════════════════════════ blocks ══

block "the store answers, and only to a key"
is "health is ok"              "200" "$(code GET "$API_URL/health")"
# 401, not 403: no credential at all is "who are you", and a credential
# without the scope is "not you". Conflating them makes a missing header look
# like a permissions problem for whoever is debugging at 2am.
is "no key, no data"           "401" "$(code GET "$API_URL/v1/facts")"
is "a wrong key is refused"    "401" "$(code GET "$API_URL/v1/facts" "kor_live_nonsense")"
is "the agent key works"       "200" "$(code GET "$API_URL/v1/facts" "$KEY")"

block "TLS, on the name the operator gave it"
is "https answers" "200" "$(curl -sk --resolve "$SITE:$TLS_PORT:127.0.0.1" \
  -o /dev/null -w '%{http_code}' "$TLS_URL/health")"

block "through Caddy: no end user in its log, and the client does not choose its address"
# The real Caddy of the install (audit of 18 Oct 2026): the Caddyfile's
# filters were read by a test only, and caddy 2.11.7 kept the end user of
# an absolute-form target and of the Location of a slash redirect. No key:
# each answer is a 401 or a 307 and nothing is read or erased.
MARK="collaudo+caddy$RUN"
through_caddy() { curl -sk --resolve "$SITE:$TLS_PORT:127.0.0.1" -o /dev/null "$@"; }
through_caddy -X DELETE "$TLS_URL/v1/users/$MARK%40korely.ai/memories"
through_caddy -X DELETE "$TLS_URL/v1/users/$MARK%40korely.ai/memories/"
through_caddy "$TLS_URL/v1/context/?query=q$MARK&user_id=$MARK"
through_caddy --request-target "$TLS_URL/v1/users/$MARK%40korely.ai/memories" --http1.1 -X DELETE "$TLS_URL/"
through_caddy -H "X-Forwarded-For: 203.0.113.77" "$TLS_URL/health"
sleep 1
CADDY_LOG=$(cd "$DIR" && docker compose logs --since 2m caddy 2>&1)
API_LOG=$(cd "$DIR" && docker compose logs --since 2m api 2>&1)
hasnt "Caddy's log keeps no end user" "$MARK" "$CADDY_LOG"
hasnt "nor does the API's"            "$MARK" "$API_LOG"
hasnt "the address the client wrote is not the one the API took" \
  "203.0.113.77:0" "$API_LOG"

block "the moat: a later fact closes an earlier one, and the past stays readable"
# These two writes used to come from timed_install.sh, and running this suite
# against an install somebody made by following the README found nothing to
# read: the suite assumed its own installer had gone first. It plants them
# itself now, and skips if they are already there.
with_a_writer
if [ -z "$UNMET" ] && ! printf '%s' "$(body GET "$API_URL/v1/facts?user_id=timed-install" "$KEY")" | grep -q '"predicate"'; then
  echo "  (planting the two writes this block reads)"
  body POST "$API_URL/v1/memories" "$KEY" \
    '{"content":"Customer 4821 is on the Priority Support plan.","user_id":"timed-install","timestamp":"2026-03-15"}' >/dev/null
  body POST "$API_URL/v1/memories" "$KEY" \
    '{"content":"Customer 4821 moved to the Basic plan.","user_id":"timed-install","timestamp":"2026-06-20"}' >/dev/null
  DEADLINE=$(( $(date +%s) + 180 ))
  while :; do
    printf '%s' "$(body GET "$API_URL/v1/facts?user_id=timed-install" "$KEY")" | grep -qi 'basic' && break
    [ "$(date +%s)" -gt "$DEADLINE" ] && break
    sleep 4
  done
fi
NOW=$(body GET "$API_URL/v1/facts?user_id=timed-install" "$KEY")
THEN=$(body GET "$API_URL/v1/facts?user_id=timed-install&as_of=2026-04-01" "$KEY")
has   "today reads Basic"        "Basic"    "$NOW"
has   "April reads Priority"     "Priority" "$THEN"
hasnt "April does not read Basic" "Basic"   "$THEN"

block "S1: administration is a different credential"
is "an agent key cannot list keys"      "403" "$(code GET "$API_URL/admin/keys" "$KEY")"
is "an agent key cannot mint one"       "403" "$(code POST "$API_URL/admin/keys" "$KEY" '{"name":"x"}')"
is "an agent key cannot list projects"  "403" "$(code GET "$API_URL/admin/projects" "$KEY")"
is "an agent key cannot list webhooks"  "403" "$(code GET "$API_URL/admin/webhooks" "$KEY")"
is "the operator key can"               "200" "$(code GET "$API_URL/admin/keys" "$ADMIN_KEY")"

block "S1: keys"
KEYS=$(body GET "$API_URL/admin/keys" "$ADMIN_KEY")
has   "both keys are listed"        "operator" "$KEYS"
# `kor_self_` on its own is the prefix field, which the listing is meant to
# carry. A whole key is the prefix plus a long random tail, and that is what
# must never come back. Either prefix: an install can hold keys minted before
# self-hosted keys said where they came from (962c4a6).
nomatch "no whole key in a listing" "kor_(live|self)_[A-Za-z0-9_-]{20,}" "$KEYS"
MADE=$(body POST "$API_URL/admin/keys" "$ADMIN_KEY" "{\"name\":\"acceptance $RUN\"}")
NEWKEY=$(echo "$MADE" | sed -n 's/.*"key":"\([^"]*\)".*/\1/p')
NEWID=$(echo "$MADE" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
# A key minted here says kor_self_ (962c4a6): kor_live_ is the hosted service.
case "$NEWKEY" in kor_self_*) ok "a new key comes back once";; *) bad "a new key comes back once" "$MADE";; esac
is "the new key works"              "200" "$(code GET "$API_URL/v1/facts" "$NEWKEY")"
is "and cannot administer"          "403" "$(code GET "$API_URL/admin/keys" "$NEWKEY")"
is "revoking it"                    "200" "$(code POST "$API_URL/admin/keys/$NEWID/revoke" "$ADMIN_KEY")"
is "it stops working at once"       "401" "$(code GET "$API_URL/v1/facts" "$NEWKEY")"
is "revoking twice is a 404"        "404" "$(code POST "$API_URL/admin/keys/$NEWID/revoke" "$ADMIN_KEY")"

block "S1: projects"
PROJ=$(body POST "$API_URL/admin/projects" "$ADMIN_KEY" "{\"name\":\"Acceptance $RUN\"}")
PID=$(echo "$PROJ" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
# The slug is derived from the name, and the name now carries a per-run
# suffix, so the check is that there IS one and that it survives a rename,
# rather than that it equals a literal that moved when the name did.
SLUG=$(printf '%s' "$PROJ" | sed -n 's/.*"slug":"\([^"]*\)".*/\1/p')
if [ -n "$SLUG" ]; then ok "created with a slug ($SLUG)"; else bad "created with a slug" "$PROJ"; fi
is  "a duplicate name is 409"   "409" "$(code POST "$API_URL/admin/projects" "$ADMIN_KEY" "{\"name\":\"Acceptance $RUN\"}")"
is  "rename"                    "200" "$(code PATCH "$API_URL/admin/projects/$PID" "$ADMIN_KEY" '{"name":"Accepted"}')"
has "the slug survives a rename" "\"slug\":\"$SLUG\"" "$(body GET "$API_URL/admin/projects/$PID" "$ADMIN_KEY")"
INPROJ=$(body POST "$API_URL/admin/keys" "$ADMIN_KEY" "{\"name\":\"in project $RUN\",\"project_id\":\"$PID\"}")
INKEY=$(echo "$INPROJ" | sed -n 's/.*"key":"\([^"]*\)".*/\1/p')
DEL=$(body DELETE "$API_URL/admin/projects/$PID" "$ADMIN_KEY")
has "deleting moves its keys"   '"keys_moved":1' "$DEL"
is  "the key survives its project" "200" "$(code GET "$API_URL/v1/facts" "$INKEY")"
DEFID=$(body GET "$API_URL/admin/projects" "$ADMIN_KEY" | sed -n 's/.*"id":"\([^"]*\)","name":"Default".*/\1/p')
if [ -n "$DEFID" ]; then
  is "the default project cannot be deleted" "409" "$(code DELETE "$API_URL/admin/projects/$DEFID" "$ADMIN_KEY")"
fi

block "S1: webhooks"
is "a private address is refused" "400" \
  "$(code POST "$API_URL/admin/webhooks" "$ADMIN_KEY" '{"url":"http://169.254.169.254/latest/"}')"
is "plain http is refused"        "400" \
  "$(code POST "$API_URL/admin/webhooks" "$ADMIN_KEY" '{"url":"http://example.com/hook"}')"
HOOK=$(body POST "$API_URL/admin/webhooks" "$ADMIN_KEY" '{"url":"https://example.com/hook"}')
HID=$(echo "$HOOK" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
has   "the secret comes back once" '"secret":"whsec_' "$HOOK"
hasnt "and never in a listing"     "whsec_" "$(body GET "$API_URL/admin/webhooks" "$ADMIN_KEY")"
is    "delete"                     "200" "$(code DELETE "$API_URL/admin/webhooks/$HID" "$ADMIN_KEY")"
is    "deleting twice is a 404"    "404" "$(code DELETE "$API_URL/admin/webhooks/$HID" "$ADMIN_KEY")"

block "S2: the consumption, which is the page the hosted product cannot have"
USAGE=$(body GET "$API_URL/admin/usage" "$ADMIN_KEY")
is    "operator only"              "403" "$(code GET "$API_URL/admin/usage" "$KEY")"
has   "grouped by stage"           '"by_stage"'  "$USAGE"
has   "grouped by agent"           '"by_agent"'  "$USAGE"
with_a_writer has "extraction is in there" '"extraction"' "$USAGE"
# timed_install writes two memories, so the model has certainly been called.
CALLS=$(printf '%s' "$USAGE" | sed -n 's/.*"totals":{"calls":\([0-9]*\).*/\1/p')
if [ "${CALLS:-0}" -gt 0 ]; then ok "model calls were recorded ($CALLS)"; else bad "model calls were recorded" "totals.calls=$CALLS"; fi
# Without a rate card the money must be blank rather than zero: zero reads as
# "nothing was spent", and nothing was spent is not what happened.
has   "no rate card, so no money"  '"rates_configured":false' "$USAGE"
has   "and the cost is null"       '"cost":null' "$USAGE"

block "S2: a bill you can split between two agents"
# Two writes under two different agent ids, then check they come out apart.
curl -fsS -X POST "$API_URL/v1/memories" -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"content":"Giulia works at Acme.","user_id":"g1","agent_id":"ZZALPHA","timestamp":"2026-05-01"}' >/dev/null 2>&1
curl -fsS -X POST "$API_URL/v1/memories" -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"content":"Marco is the new ops lead.","user_id":"m1","agent_id":"ZZBETA","timestamp":"2026-05-02"}' >/dev/null 2>&1
DEADLINE=$(( $(date +%s) + 120 ))
while :; do
  USAGE=$(body GET "$API_URL/admin/usage" "$ADMIN_KEY")
  case "$USAGE" in *ZZALPHA*ZZBETA*|*ZZBETA*ZZALPHA*) break;; esac
  [ "$(date +%s)" -gt "$DEADLINE" ] && break
  sleep 3
done
has "the first agent has a bill"   "ZZALPHA" "$USAGE"
has "and so does the second"       "ZZBETA"  "$USAGE"

block "S3: forgetting does not delete, which is the whole product"
with_a_writer
# Take the fact that is true today, forget it, and check that April still has it.
FID=$(body GET "$API_URL/v1/facts?user_id=timed-install" "$KEY" \
      | sed -n 's/.*"id":"\(fct_[^"]*\)".*/\1/p' | head -1)
if [ -z "$FID" ]; then
  bad "a fact to forget" "no fact id in the listing"
else
  is  "forgetting it"              "200" "$(code POST "$API_URL/admin/facts/$FID/forget" "$ADMIN_KEY")"
  hasnt "it is out of today"       "$FID" "$(body GET "$API_URL/v1/facts?user_id=timed-install" "$KEY")"
  has "but March still has it"     "Priority" \
      "$(body GET "$API_URL/v1/facts?user_id=timed-install&as_of=2026-04-01&include_invalidated=true" "$KEY")"
  has "forgetting twice says so"   "already_forgotten" \
      "$(body POST "$API_URL/admin/facts/$FID/forget" "$ADMIN_KEY")"
  is  "an agent key cannot forget" "403" "$(code POST "$API_URL/admin/facts/$FID/forget" "$KEY")"
fi

block "S3: correcting supersedes, it does not edit"
with_a_writer
CORR=$(body POST "$API_URL/v1/memories" "$KEY" \
  '{"content":"Ugo is based in Turin.","user_id":"zzcorr","agent_id":"ZZALPHA","timestamp":"2026-07-01"}')
DEADLINE=$(( $(date +%s) + 120 ))
CFID=""
while [ -z "$CFID" ] && [ -z "$UNMET" ] && [ "$(date +%s)" -lt "$DEADLINE" ]; do
  sleep 3
  CFID=$(body GET "$API_URL/v1/facts?user_id=zzcorr" "$KEY" \
         | sed -n 's/.*"id":"\(fct_[^"]*\)".*/\1/p' | head -1)
done
if [ -z "$CFID" ]; then
  bad "a fact to correct" "no fact was mined for zzcorr"
else
  NEW=$(body PATCH "$API_URL/admin/facts/$CFID" "$ADMIN_KEY" '{"object":"Genoa"}')
  has   "the correction is the new value" "Genoa" "$NEW"
  AFTER=$(body GET "$API_URL/v1/facts?user_id=zzcorr&include_invalidated=true" "$KEY")
  has   "the old value is still on file"  "Turin" "$AFTER"
  hasnt "and is not current"              '"object":"Turin","valid_from"' \
        "$(body GET "$API_URL/v1/facts?user_id=zzcorr" "$KEY")"
  is    "an empty correction is refused"  "422" "$(code PATCH "$API_URL/admin/facts/$CFID" "$ADMIN_KEY" '{}')"
fi

block "S3: the rest of the screen"
with_a_writer has "categories are predicate families" '"category"' "$(body GET "$API_URL/admin/memories/categories" "$ADMIN_KEY")"
has "request activity"                  '"by_endpoint"' "$(body GET "$API_URL/admin/requests/summary" "$ADMIN_KEY")"
DAILY=$(body GET "$API_URL/admin/requests/daily?days=7" "$ADMIN_KEY")
if [ "$(printf '%s' "$DAILY" | grep -o '"date"' | wc -l | tr -d ' ')" = "7" ]; then
  ok "seven days, no holes"
else
  bad "seven days, no holes" "$DAILY"
fi
MK=$(body POST "$API_URL/admin/keys" "$ADMIN_KEY" "{\"name\":\"to move $RUN\"}")
MKID=$(printf '%s' "$MK" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
MP=$(body POST "$API_URL/admin/projects" "$ADMIN_KEY" "{\"name\":\"Movers $RUN\"}")
MPID=$(printf '%s' "$MP" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
has "a key moves into a project" "\"project_id\":\"$MPID\"" \
    "$(body POST "$API_URL/admin/keys/$MKID/move" "$ADMIN_KEY" "{\"project_id\":\"$MPID\"}")"
# A registered endpoint that answers something other than 2xx. The first
# version of this pointed at 127.0.0.1.nip.io, which resolves to loopback and
# is correctly refused at registration: the product was right and the test was
# wrong. This needs egress, which the install needs anyway for the model.
TH=$(body POST "$API_URL/admin/webhooks" "$ADMIN_KEY" '{"url":"https://example.com/korely-acceptance-nope"}')
THID=$(printf '%s' "$TH" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
if [ -z "$THID" ]; then
  bad "a webhook to test" "$TH"
else
  TEST=$(body POST "$API_URL/admin/webhooks/$THID/test" "$ADMIN_KEY")
  has "the test says what came back" '"delivered":false' "$TEST"
  has "and reports the status"       '"status":' "$TEST"
  is  "and the endpoint is removed"  "200" "$(code DELETE "$API_URL/admin/webhooks/$THID" "$ADMIN_KEY")"
  THID=""
fi

block "S4: somebody logs in, and there is no default password"
A=$(code POST "$API_URL/admin/session" "" '{"username":"admin","password":"admin"}')
B=$(code POST "$API_URL/admin/session" "" '{"username":"operator","password":""}')
C=$(code POST "$API_URL/admin/session" "" '{"username":"operator","password":"nope"}')
is "admin/admin is not a thing" "401" "$A"
is "nor is a blank password"    "422" "$B"
is "a wrong password"           "401" "$C"
# Built into a variable first. Nested inside `is "..." "$(code ... "{\"a\":1}")"`
# the escaping came apart and the server answered 422, which looked like a
# broken lockout and was a broken test.
GOOD_LOGIN="{\"username\":\"$OP_USERNAME\",\"password\":\"$OP_PASSWORD\"}"
SESS=$(body POST "$API_URL/admin/session" "" "$GOOD_LOGIN")
TOKEN=$(printf '%s' "$SESS" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
case "$TOKEN" in kor_sess_*) ok "the right password opens a session";; *) bad "the right password opens a session" "$SESS";; esac
is "the session administers"       "200" "$(code GET "$API_URL/admin/keys" "$TOKEN")"
has "and knows who it is"          '"kind":"operator"' "$(body GET "$API_URL/admin/whoami" "$TOKEN")"
is "but it is not an API key"      "401" "$(code GET "$API_URL/v1/facts" "$TOKEN")"
is "logging out"                   "200" "$(code DELETE "$API_URL/admin/session" "$TOKEN")"
is "the token dies at once"        "401" "$(code GET "$API_URL/admin/keys" "$TOKEN")"

block "S4: a login box nobody can grind"
# Ten wrong answers, then the right one must still be refused.
i=0
while [ $i -lt 10 ]; do
  curl -s -o /dev/null -X POST "$API_URL/admin/session" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$OP_USERNAME\",\"password\":\"wrong-$i\"}"
  i=$(( i + 1 ))
done
LOCKED=$(code POST "$API_URL/admin/session" "" "$GOOD_LOGIN")
is "the right password is locked out too" "429" "$LOCKED"

# Put it back, and check that resetting a password is also how you unlock an
# account: otherwise the only way out of a lockout is waiting fifteen minutes.
RESET=$(cd "$DIR" && docker compose exec -T api python -m korely_agent.manage \
          reset-password --username "$OP_USERNAME" 2>/dev/null)
NEWPW=$(echo "$RESET" | awk '/^password /{print $2}')
if [ -z "$NEWPW" ]; then
  bad "reset-password gives a new one" "$RESET"
else
  ok "reset-password gives a new one"
  # Back into install.env. This block is the reason a second run used to fail
  # everywhere: it changed the credential the file holds and did not say so.
  if [ -f "$DIR/install.env" ]; then
    sed -i.bak "s|^OP_PASSWORD=.*|OP_PASSWORD=$NEWPW|" "$DIR/install.env" 2>/dev/null \
      || sed -i "s|^OP_PASSWORD=.*|OP_PASSWORD=$NEWPW|" "$DIR/install.env"
    rm -f "$DIR/install.env.bak"
  fi
  AFTER_RESET="{\"username\":\"$OP_USERNAME\",\"password\":\"$NEWPW\"}"
  is "and the lock is gone with it" "200" \
     "$(code POST "$API_URL/admin/session" "" "$AFTER_RESET")"
fi

block "S5: the screen is served by the machine that holds the memories"
ROOT=$(body GET "$API_URL/")
has "the root is a page"          "<!doctype html" "$(printf '%s' "$ROOT" | tr 'A-Z' 'a-z')"
is  "and it answers 200"          "200" "$(code GET "$API_URL/")"
# The routes after the first exist only in the browser. Reloading one has to
# give the page back, or the back button breaks.
is  "a browser route reloads"     "200" "$(code GET "$API_URL/memories")"
has "with the page, not a 404"    "<!doctype html" "$(printf '%s' "$(body GET "$API_URL/facts/fct_abc")" | tr 'A-Z' 'a-z')"
# And the other direction: a typo under an API prefix must not come back as a
# page with a 200 on it.
is  "a typo under /v1 is a 404"   "404" "$(code GET "$API_URL/v1/nonsense" "$KEY")"
hasnt "and is not html"           "<!doctype" "$(body GET "$API_URL/v1/nonsense" "$KEY")"
is  "the API is untouched"        "200" "$(code GET "$API_URL/v1/ping" "$KEY")"
# Through TLS too, which is how a person will actually open it.
is  "and it opens over https"     "200" \
    "$(curl -sk --resolve "$SITE:$TLS_PORT:127.0.0.1" -o /dev/null -w '%{http_code}' "$TLS_URL/")"

block "S6: the screen talks to this machine and to nothing else"
# The served bundle, not the source: what a browser actually downloads.
ASSET=$(printf '%s' "$(body GET "$API_URL/")" | sed -n 's/.*src="\(\/assets\/[^"]*\.js\)".*/\1/p' | head -1)
if [ -z "$ASSET" ]; then
  bad "the page names its bundle" "no /assets/*.js in index.html"
else
  ok "the page names its bundle"
  BUNDLE=$(body GET "$API_URL$ASSET")
  hasnt "no api.korely.ai in what the browser runs" "api.korely.ai"  "$BUNDLE"
  hasnt "and no firebase either"                    "firebaseapp.com" "$BUNDLE"
fi
# Every URL the screen calls, checked against what this install publishes.
# Without a browser, and without anybody clicking fourteen pages.
# What the three checks below print is kept in a variable, never in a file
# at a fixed /tmp path that any account on the machine reads and the next run
# overwrites (functional test of 28 Oct 2026).
if OUT=$(python3 "$HERE/scripts/check_dashboard_endpoints.py" --openapi "$API_URL/openapi.json" 2>&1); then
  ok "every endpoint the screen calls exists here ($(printf '%s\n' "$OUT" | grep -o 'calls [0-9]*' | head -1))"
else
  bad "every endpoint the screen calls exists here" "$(printf '%s\n' "$OUT" | tail -8 | tr '\n' ' ')"
fi
# And the fields in what they answer: five routes once answered 200 with
# other fields than the screen reads, and five pages went blank with every
# endpoint present (check_response_shapes.py). Not run by anything until 17 Oct
# 2026 (audit of 17 Oct 2026).
if OUT=$(python3 "$HERE/scripts/check_response_shapes.py" --base "$API_URL" --key "$ADMIN_KEY" 2>&1); then
  ok "every field the screen reads is in the reply ($(printf '%s\n' "$OUT" | grep -o '^[0-9]* shapes' | head -1))"
else
  bad "every field the screen reads is in the reply" "$(printf '%s\n' "$OUT" | tail -8 | tr '\n' ' ')"
fi

block "the OAuth metadata names the address clients use"
# PUBLIC_BASE_URL follows DOMAIN (entrypoint.sh, 17 Oct 2026): it said
# http://localhost:8000 on every install, and no OAuth client could connect.
META=$(curl -sk --resolve "$SITE:$TLS_PORT:127.0.0.1" "$TLS_URL/.well-known/oauth-authorization-server")
has "the issuer is this install's https address" "\"issuer\":\"https://$SITE" "$(printf '%s' "$META" | tr -d ' ')"

block "the licence travels with the image"
# The Apache License 2.0 asks that its text and the NOTICE go with every copy
# distributed (section 4; found missing on 17 Oct 2026).
LIC=$(cd "$DIR" && docker compose exec -T api sh -c 'head -c 200 /app/LICENSE; echo; head -c 200 /app/NOTICE' 2>&1)
has "LICENSE is in the image" "Apache License" "$LIC"
has "and NOTICE next to it"   "Korely"                  "$LIC"

block "keys that stop working, and say so first"
LT=$(body GET "$API_URL/admin/keys/lifetimes" "$ADMIN_KEY")
has "the install says what it offers" '"default_days":90' "$LT"
has "including never"                 '"Never expires"'   "$LT"
# A key with a lifetime, and one without: both are choices somebody made.
SHORT=$(body POST "$API_URL/admin/keys" "$ADMIN_KEY" "{\"name\":\"short lived $RUN\",\"expires_in_days\":30}")
hasnt "a dated key carries its date"  '"expires_at":null' "$SHORT"
# null is the one way to write never; 0 expires the key now (keys.py
# _expiry: two spellings of never let a script mint an immortal key).
FOREVER=$(body POST "$API_URL/admin/keys" "$ADMIN_KEY" "{\"name\":\"forever $RUN\",\"expires_in_days\":null}")
has "and never means never"           '"expires_at":null' "$FOREVER"
NOW=$(body POST "$API_URL/admin/keys" "$ADMIN_KEY" "{\"name\":\"retired $RUN\",\"expires_in_days\":0}")
has "and zero means now"              '"state":"expired"' "$NOW"
# The one that matters: past the date, it stops. Moved by hand because waiting
# thirty days is not a test.
SHORTID=$(printf '%s' "$SHORT" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
SHORTKEY=$(printf '%s' "$SHORT" | sed -n 's/.*"key":"\([^"]*\)".*/\1/p')
is "it works before"  "200" "$(code GET "$API_URL/v1/ping" "$SHORTKEY")"
(cd "$DIR" && docker compose exec -T db psql -U korely -d korely -q -c \
  "UPDATE agent_api_keys SET expires_at = now() - interval '1 day' WHERE id = '$SHORTID';" >/dev/null 2>&1)
is "and not after"    "401" "$(code GET "$API_URL/v1/ping" "$SHORTKEY")"
has "and the listing says which it is" '"state":"expired"' \
    "$(body GET "$API_URL/admin/keys" "$ADMIN_KEY")"

block "the model key, pasted from the screen instead of edited into a file"
PROV=$(body GET "$API_URL/admin/provider" "$ADMIN_KEY")
is   "operator only"            "403" "$(code GET "$API_URL/admin/provider" "$KEY")"
has  "it says whether one is set" '"set":' "$PROV"
has  "and where it came from"     '"source":' "$PROV"
has  "and what the model last did" '"last_call":' "$PROV"
has  "and that it can store one"   '"can_store":true' "$PROV"
nomatch "the key itself never comes back" "AIza[A-Za-z0-9_-]{20,}" "$PROV"
# And the hole in that property, found by mistyping the field name while
# pasting a real key into a real install: pydantic puts the offending value in
# `input`, so a 422 read the key back out. The caller knows what they sent, so
# nobody learns a secret here; what happens is that the key lands in the
# caller's log, their terminal, and the bug report they paste into a chat.
WRONGFIELD=$(body PUT "$API_URL/admin/provider" "$ADMIN_KEY" \
             '{"api_key":"ZZNOTTHEFIELDNAME'"$RUN"'"}')
hasnt "a mistyped field does not echo the key" "ZZNOTTHEFIELDNAME$RUN" "$WRONGFIELD"
has   "but still says which field was wrong"   '"key"' "$WRONGFIELD"
# A key the provider refuses must not be stored. Saving it is the only way to
# find out it is wrong, and finding out later means nothing was extracted in
# between.
BADSAVE=$(code PUT "$API_URL/admin/provider" "$ADMIN_KEY" '{"key":"AIzaSyThisIsNotARealKeyAtAll12345"}')
is  "a key the provider refuses is not saved" "400" "$BADSAVE"
# What it had, read before: a key, or none on an install with no model key,
# where up to 1.0.28 this asked for `"set":true` and failed.
HAD=$(printf '%s' "$PROV" | grep -o '"set":[a-z]*' | head -1)
has "and the install still runs on the one it had" "${HAD:-the set of before, unread}" \
    "$(body GET "$API_URL/admin/provider" "$ADMIN_KEY")"

block "the SDK from PyPI, pointed at this machine"
# The half of the test that happens on somebody else's computer: pip install
# the published package, give it this address, write a memory and read it back.
# Nothing here is a local copy of the SDK; it comes off PyPI like a customer's
# would.
SDK_OUT=$(docker run --rm --network host python:3.11-slim sh -c "
  pip install --quiet --no-cache-dir korely-memory >/dev/null 2>&1 || exit 3
  python - <<'PY'
import sys
from korely_memory import Korely

k = Korely(api_key='$KEY', base_url='$API_URL')
m = k.add('Ugo prefers to be called by phone.', user_id='zzsdk')
print('ADDED', getattr(m, 'id', m))
hits = k.search('how do I reach Ugo', user_id='zzsdk')
print('SEARCHED', len(list(hits)))
ctx = k.get_context(query='contact', user_id='zzsdk')
print('CONTEXT', bool(getattr(ctx, 'context', None) is not None))
PY
" 2>&1)
case "$SDK_OUT" in
  *ADDED*)    ok "pip install korely-memory, and it writes here";;
  *)          bad "pip install korely-memory, and it writes here" "$(printf '%s' "$SDK_OUT" | tail -3 | tr '\n' ' ')";;
esac
case "$SDK_OUT" in
  *SEARCHED*) ok "and reads back";;
  *)          bad "and reads back" "$(printf '%s' "$SDK_OUT" | tail -3 | tr '\n' ' ')";;
esac
# And the thing that decides whether the data stayed here: the memory is in
# this machine's database, not in ours.
IN_DB=$(cd "$DIR" && docker compose exec -T db psql -U korely -d korely -tAc \
        "SELECT count(*) FROM agent_memories WHERE end_user_id = 'zzsdk';" 2>/dev/null | tr -d ' \r')
# At least one, not exactly one. The property is "what the SDK wrote is in
# this machine's database"; insisting on exactly one also insisted the suite
# had never been run here before, which is not the same claim and is the one
# that failed.
if [ "${IN_DB:-0}" -ge 1 ]; then
  ok "and what it wrote is in this machine's database"
else
  bad "and what it wrote is in this machine's database" "rows found: ${IN_DB:-0}"
fi

# The address the library picks when NOBODY passes one as an argument. Same
# package, and with no address it talks to our hosted service, so getting this
# wrong does not fail loudly: the key is refused with a 401 and the memory has
# already left the machine. Checked on the published package in a container that
# has nothing else in it, exactly like the block above.
SDK_ENV=$(docker run --rm --network host \
  -e KORELY_API_KEY="$KEY" -e KORELY_BASE_URL="$API_URL" python:3.11-slim sh -c "
  pip install --quiet --no-cache-dir korely-memory >/dev/null 2>&1 || exit 3
  python - <<'PYENV'
from korely_memory import Korely
k = Korely()
print('BASE', k.base_url)
m = k.add('Written with no argument at all.', user_id='zzenv')
print('ADDED', getattr(m, 'id', m))
PYENV
" 2>&1)
has "the address comes from the environment, not from us" "BASE $API_URL" "$SDK_ENV"
has "and the write lands here"                            "ADDED mem_"       "$SDK_ENV"

block "one install holds several clients, and they never see each other"
OTHER=$(cd "$DIR" && docker compose exec -T api python -m korely_agent.manage \
          create-tenant --name "Somebody Else" 2>/dev/null)
OKEY=$(echo "$OTHER" | awk '/^key /{print $2}')
OTID=$(echo "$OTHER" | awk '/^tenant /{print $2}')
OADMIN=$(cd "$DIR" && docker compose exec -T api python -m korely_agent.manage \
           issue-key --tenant "$OTID" --name "their operator" --admin 2>/dev/null | tr -d '\r')
hasnt "their facts are not ours"   "Priority" "$(body GET "$API_URL/v1/facts" "$OKEY")"
# "Timed Install" is ours and cannot appear on their side. The first version of
# this looked for "operator", which their own key is also called.
hasnt "their key list is not ours" "Timed Install" "$(body GET "$API_URL/admin/keys" "$OADMIN")"
hasnt "their bill is not ours"     "ZZALPHA" "$(body GET "$API_URL/admin/usage" "$OADMIN")"
is    "they cannot revoke our key" "404" \
  "$(code POST "$API_URL/admin/keys/$(echo "$KEYS" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p' | head -1)/revoke" "$OADMIN")"

block "a project is a wall, and a key is on one side of it"
# Shown on a real install on 12 September: two projects, one key each, and the
# key of the second read the sentence written by the first. What follows is
# that demonstration, run against the machine rather than against a fixture.
# `$GOOD_LOGIN` is stale by here: the lockout block above resets the operator
# password on purpose, so anything after it has to log in with the new one.
PSESS=$(body POST "$API_URL/admin/session" "" "${AFTER_RESET:-$GOOD_LOGIN}")
PTOK=$(printf '%s' "$PSESS" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
mkproj() {
  body POST "$API_URL/admin/projects" "$PTOK" "{\"name\":\"$1\"}" \
    | sed -n 's/.*"id":"\([^"]*\)".*/\1/p'
}
mkkey() {
  body POST "$API_URL/admin/keys" "$PTOK" "{\"name\":\"$1\",\"project_id\":\"$2\"}" \
    | sed -n 's/.*"key":"\(kor_self_[^"]*\)".*/\1/p'
}
PA=$(mkproj "Alfa $RUN"); PB=$(mkproj "Beta $RUN")
KA=$(mkkey "k-alfa-$RUN" "$PA"); KB=$(mkkey "k-beta-$RUN" "$PB")
if [ -z "$KA" ] || [ -z "$KB" ]; then
  bad "two projects, one key each" "alfa=${KA:0:12} beta=${KB:0:12}"
else
  ok "two projects, one key each"
  SECRET="ZZWALL$RUN turnover is 2.4 million"
  code POST "$API_URL/v1/memories" "$KA" \
    "{\"content\":\"$SECRET\",\"user_id\":\"shared$RUN\"}" >/dev/null
  hasnt "the other project cannot read it" "ZZWALL$RUN" \
        "$(body GET "$API_URL/v1/memories?user_id=shared$RUN" "$KB")"
  has   "and the one that wrote it can"    "ZZWALL$RUN" \
        "$(body GET "$API_URL/v1/memories?user_id=shared$RUN" "$KA")"
  # The direction that leaves nothing behind to notice it by.
  code DELETE "$API_URL/v1/memories?user_id=shared$RUN" "$KB" >/dev/null
  has   "and cannot erase it either"       "ZZWALL$RUN" \
        "$(body GET "$API_URL/v1/memories?user_id=shared$RUN" "$KA")"
  # The operator owns the machine, so the screen is above the walls.
  has   "the operator still sees everything" "ZZWALL$RUN" \
        "$(body GET "$API_URL/admin/memories?user_id=shared$RUN" "$PTOK")"
fi

block "the screen in a real browser"
# A login that did not log in and three blank pages, on 2026-09-12, with every
# check above green; a modal that could be neither filled in nor closed, later.
# curl sees neither (acceptance_browser.sh). The operator's password is the one
# the lockout block above reset, when it did; it goes in the environment, out
# of `ps`.
#
# An install with no model key opens on the dashboard's first screen, which
# the checks meet and skip as a person would; with a writer there is no first
# screen. Which one this install is, the install said at the start (WRITER,
# from GET /admin/models), and the checks hold the screen to it
# (scripts/browser_sign_in.mjs, which refuses a WRITER of `unknown`). On a
# failure the whole of what they printed follows the line, here, in this
# run's own output: the last eight lines alone had cut the 'on /keys we are
# on' that showed the first screen in the way (acceptance of 1.0.21 to
# 1.0.23).
if OUT=$(OPERATOR_PASSWORD="${NEWPW:-$OP_PASSWORD}" WRITER="$WRITER" bash "$HERE/scripts/acceptance_browser.sh" \
           "$API_URL" "$OP_USERNAME" 2>&1); then
  ok "it logs in (writer: $WRITER), every page loads and the modals work"
else
  bad "it logs in (writer: $WRITER), every page loads and the modals work" \
    "$(printf '%s\n' "$OUT" | tail -8 | tr '\n' ' ')"
  printf '%s\n' "$OUT" | sed 's/^/      | /'
fi

# ══════════════════════════════════════════════════════════════════ verdict ══
verdict "the installation does what it says" || exit 1
