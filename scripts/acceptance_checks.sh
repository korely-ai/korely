# The verdict helpers of the acceptance scripts, sourced by acceptance.sh and
# acceptance_survives.sh. They were two copies that had drifted (one had
# hasnt and nomatch, the other did not, and the failure counters had two
# names) with their messages in Italian, where CONTRIBUTING.md wants the
# code's messages in English (audit of 17 Oct 2026).
#
# A check prints ok or FAIL with its name; FAIL adds the name to
# FAILED_NAMES, which the script prints at the end with `printf "$FAILED_NAMES"`.
PASS=0
FAIL=0
FAILED_NAMES=""

ok()   { PASS=$((PASS+1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILED_NAMES="${FAILED_NAMES}    $1\n"; printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
block(){ printf '\n\033[1m%s\033[0m\n' "$1"; }

# is  <name>  <expected>  <actual>
is()   { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected $2, got $3"; fi; }
# has <name>  <needle>  <haystack>
has()  { case "$3" in *"$2"*) ok "$1";; *) bad "$1" "missing: $2";; esac; }
hasnt(){ case "$3" in *"$2"*) bad "$1" "present, and should not be: $2";; *) ok "$1";; esac; }
# nomatch <name> <extended regex> <haystack>
nomatch() { if printf '%s' "$3" | grep -qE "$2"; then bad "$1" "matches, and should not: $2"; else ok "$1"; fi; }
