#!/bin/sh
# Restore a dump over the current database. Destructive.
#
#   ./deploy/restore.sh backups/korely-20260910T120000Z.dump
#
# Practise this on a scratch database before you need it in anger. The first
# time you run a restore should not be the day you need one.
set -e

DUMP="$1"
[ -n "$DUMP" ] || { echo "usage: $0 <dump-file>" >&2; exit 1; }
[ -f "$DUMP" ] || { echo "no such file: $DUMP" >&2; exit 1; }

if [ -z "$FORCE" ]; then
  printf 'This REPLACES the contents of the korely database. Type yes to continue: '
  read -r ans
  [ "$ans" = "yes" ] || { echo "aborted"; exit 1; }
fi

# Stop the API first, so nothing writes from here on: not while the schema is
# being replaced, and not while the live database is read below. The exports
# ran with the API up until the audit of 19 Oct 2026, so an erasure or a
# revocation committed between them and the stop was in neither file and the
# restore undid it. The worker runs in the API's container, so this stops
# every writer; the MANAGE commands below run a container of their own
# (`run --no-deps`) and do not need the API up.
docker compose stop api >/dev/null 2>&1 || true

# The erasures of the live database, before it is replaced: a dump taken
# before an erasure still holds what it erased, and the records go back with
# the database. Kept owner-only and deleted at the end; they are made again
# after the restore (services/erasure_replay.py, 17 Oct 2026). Without them
# the restore stops, unless SKIP_ERASURES=1 says the live database is gone.
#
# The credentials of the live database too: a key revoked, a password reset,
# an operator disabled or a webhook secret rotated after the dump came back as
# the dump had them, and the revocation the documentation prescribes for a leaked key
# was undone by the restore it prescribes after a failed upgrade (audit of
# 17 Oct 2026); a key moved out of a project after the dump was back in it
# (audit of 18 Oct 2026). They are made again after the restore
# (services/credential_replay.py). The file holds password hashes and webhook
# secrets, as the dump does.
#
# An interrupt (Ctrl-C, a closed terminal) leaves through the EXIT trap too:
# under dash, the shell of `sh` on Debian and Ubuntu, the EXIT trap alone
# does not run on a signal, and the two files stayed in $TMPDIR.
#
# From just before pg_restore begins until both replays are done the two
# files are the only copy of what the live database did since the dump: an
# exit there, an interrupt included, keeps them owner-only in this folder,
# with the commands to finish, and the API stopped (audit of 20 Oct 2026: an
# interrupt deleted them, and a run again read the restored database as the
# live one).
# Before pg_restore the live database is intact, and they go.
umask 077
ERASURES=$(mktemp)
CREDENTIALS=$(mktemp)
MANAGE="docker compose run --rm -T --no-deps api python -m korely_agent.manage"
# Where a file of what must be made again is kept, owner-only in this folder
# (.gitignore keeps it out of git), since the temporary copies go at the exit.
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
KEEP="./erasures-$STAMP.json"
KEEP_CREDENTIALS="./credentials-$STAMP.json"
REPLACING=""
leave() {
  if [ -n "$REPLACING" ]; then
    cp "$ERASURES" "$KEEP"
    cp "$CREDENTIALS" "$KEEP_CREDENTIALS"
    echo "" >&2
    echo "stopped after the database began to be replaced, so the API stays stopped. The" >&2
    echo "erasures and the credentials of the live database are kept in" >&2
    echo "$KEEP and $KEEP_CREDENTIALS. Finish the restore, from this folder:" >&2
    echo "  $MANAGE erasures-replay < $KEEP" >&2
    echo "  $MANAGE credentials-replay < $KEEP_CREDENTIALS" >&2
    echo "then: docker compose start api, and delete the kept files." >&2
  fi
  rm -f "$ERASURES" "$CREDENTIALS"
}
trap leave EXIT
trap 'exit 1' INT TERM HUP
READ=yes
$MANAGE erasures-export > "$ERASURES" || { READ=""; echo "[]" > "$ERASURES"; }
$MANAGE credentials-export > "$CREDENTIALS" || { READ=""; echo "{}" > "$CREDENTIALS"; }
if [ -z "$READ" ] && [ -z "$SKIP_ERASURES" ]; then
  echo "could not read the erasures and the credentials of the live database, so a" >&2
  echo "restore would bring back the people erased and the keys revoked since the dump." >&2
  echo "Nothing was restored, and the API is stopped. Start the database and try again," >&2
  echo "or SKIP_ERASURES=1 if it is gone and there is nothing to keep. To go back to the" >&2
  echo "database as it was instead: docker compose start api" >&2
  exit 1
fi

# --clean --if-exists drops what it is about to recreate, so a restore onto a
# populated database is a replacement rather than a merge.
#
# A pg_restore that exits with an error ended the script under set -e with the
# API stopped and not a word about it, and the erasures and credentials of the
# live database deleted with the temporary files (audit of 17 Oct 2026). Its
# status cannot tell a broken dump from errors it went past (without
# --exit-on-error it restores what it can and counts the rest); the errors it
# printed say which. So the script keeps both files and the API stopped, and
# says so: starting it on a database that may hold half the dump, and none of
# the changes since, is the operator's call.
REPLACING=yes
if ! docker compose exec -T db pg_restore -U korely -d korely --clean --if-exists --no-owner < "$DUMP"; then
  cp "$ERASURES" "$KEEP"
  cp "$CREDENTIALS" "$KEEP_CREDENTIALS"
  REPLACING=""
  echo "" >&2
  echo "pg_restore reported errors (above), so the API stays stopped: the database may" >&2
  echo "hold part of the dump and none of the changes made since it. The erasures and the" >&2
  echo "credentials of the live database, read before the restore, are kept in" >&2
  echo "$KEEP and $KEEP_CREDENTIALS." >&2
  echo "If the errors are ones you can go past (\"errors ignored on restore\"), finish it," >&2
  echo "from this folder:" >&2
  echo "  $MANAGE erasures-replay < $KEEP" >&2
  echo "  $MANAGE credentials-replay < $KEEP_CREDENTIALS" >&2
  echo "  docker compose start api" >&2
  echo "Otherwise restore another dump with this script, then run the two replays above" >&2
  echo "as well: the database it reads first is no longer the live one. Delete the kept" >&2
  echo "files after." >&2
  exit 1
fi

# The migrations of this version first (the entrypoint), then the erasures and
# the credential changes made after the dump, again, before anything can read
# the restored data. The credentials step also ends every operator session and
# every OAuth grant whatever the live database said (a new sign-in, a new
# consent), and prints the keys, endpoints and operators in force.
#
# If either fails the API stays stopped, and its file is kept owner-only in
# this folder with the command to run (the acceptance run of 0.1.40: a failed replay
# left the API down and deleted the only copy of what was to be made again).
# A record the erasures' replay could not make again stops no other: it names
# each by its record's id and exits 4, and a run again with the kept file
# repeats only what the restored data does not hold yet (the audit before
# 1.0.0).
FAILED=""
if ! $MANAGE erasures-replay < "$ERASURES"; then
  cp "$ERASURES" "$KEEP"
  FAILED=erasures
fi
if ! $MANAGE credentials-replay < "$CREDENTIALS"; then
  cp "$CREDENTIALS" "$KEEP_CREDENTIALS"
  FAILED="$FAILED credentials"
fi
if [ -n "$FAILED" ]; then
  # The failed step's file is kept above; an interrupt from here on loses
  # nothing the message below does not name.
  REPLACING=""
  echo "the changes made since the dump could not all be made again, so the API stays" >&2
  echo "stopped: the restored data may still hold people erased or credentials ended since." >&2
  echo "Run, from this folder:" >&2
  case "$FAILED" in *erasures*)
    echo "  $MANAGE erasures-replay < $KEEP" >&2
    echo "  (it repeats only what is not made yet; if the records it names fail again," >&2
    echo "  keep the API stopped and report the lines it printed for them: docs/guide/" >&2
    echo "  operations.md, Backups)" >&2 ;;
  esac
  case "$FAILED" in *credentials*) echo "  $MANAGE credentials-replay < $KEEP_CREDENTIALS" >&2 ;; esac
  echo "then: docker compose start api, and delete the kept file(s)." >&2
  exit 1
fi

REPLACING=""
docker compose start api >/dev/null 2>&1 || true
echo "restored from $DUMP"
