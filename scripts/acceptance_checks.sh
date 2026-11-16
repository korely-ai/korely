# The verdict helpers of the acceptance scripts, sourced by acceptance.sh and
# acceptance_survives.sh. They were two copies that had drifted (one had
# hasnt and nomatch, the other did not, and the failure counters had two
# names) with their messages in Italian, where CONTRIBUTING.md wants the
# code's messages in English (audit of 17 Oct 2026).
#
# A check prints ok or FAIL with its name; FAIL adds the name to
# FAILED_NAMES, which `verdict` prints at the end.
#
# A check that needs what the install lacks (a model that writes, on an
# install with no model key) is neither passed nor failed: it is not run,
# printed and counted apart (NOT_RUN, NOT_RUN_NAMES), and `verdict` lists it.
# Up to 1.0.28 the checks of what a model extracted failed on every run
# without a key, six of the eight failures of every release run, so a real
# failure hid among them (code review finding of 14 Nov 2026).
# `unmet` says what is lacking, for the check it is given or, given none,
# for the rest of the block; the script calls it only where the install
# itself said it lacks it.
PASS=0
FAIL=0
NOT_RUN=0
FAILED_NAMES=""
NOT_RUN_NAMES=""
UNMET=""

not_run(){ NOT_RUN=$((NOT_RUN+1)); NOT_RUN_NAMES="${NOT_RUN_NAMES}    $1 ($2)\n"; printf '  \033[33mnot run\033[0m %s: %s\n' "$1" "$2"; }
ok()   { if [ -n "$UNMET" ]; then not_run "$1" "$UNMET"; return 0; fi
         PASS=$((PASS+1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { if [ -n "$UNMET" ]; then not_run "$1" "$UNMET"; return 0; fi
         FAIL=$((FAIL+1)); FAILED_NAMES="${FAILED_NAMES}    $1\n"; printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
block(){ UNMET=""; printf '\n\033[1m%s\033[0m\n' "$1"; }

# unmet <why> [check args...]: with a check, that check is not run; with
# none, the checks left in the block are not.
unmet(){
  local why="$1" was="$UNMET"; shift
  UNMET="$why"
  [ $# -eq 0 ] && return 0
  "$@"; UNMET="$was"
}

# is  <name>  <expected>  <actual>
is()   { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected $2, got $3"; fi; }
# has <name>  <needle>  <haystack>
has()  { case "$3" in *"$2"*) ok "$1";; *) bad "$1" "missing: $2";; esac; }
hasnt(){ case "$3" in *"$2"*) bad "$1" "present, and should not be: $2";; *) ok "$1";; esac; }
# nomatch <name> <extended regex> <haystack>
nomatch() { if printf '%s' "$3" | grep -qE "$2"; then bad "$1" "matches, and should not: $2"; else ok "$1"; fi; }

# verdict <what a run with nothing failed and nothing left out proves>: the
# counts, what was not run and what failed. Its status is 1 only for a
# failure: a check not run is said, in the counts and by name, and never
# read as passed.
verdict(){
  printf '\n────────────────────────────────────────────\n'
  printf '%d passed, %d failed, %d not run\n' "$PASS" "$FAIL" "$NOT_RUN"
  if [ "$NOT_RUN" -gt 0 ]; then printf 'not run:\n'; printf '%b' "$NOT_RUN_NAMES"; fi
  if [ "$FAIL" -gt 0 ]; then printf 'failed:\n'; printf '%b' "$FAILED_NAMES"; return 1; fi
  if [ "$NOT_RUN" -gt 0 ]; then
    printf 'nothing failed, and %d checks were not run: this run does not show that %s.\n' "$NOT_RUN" "$1"
  else
    printf '%s.\n' "$1"
  fi
}
