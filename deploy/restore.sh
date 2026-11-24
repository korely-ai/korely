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

# The files of erasures backup.sh kept beside the dumps
# (korely-erasures-<time>.json), every one, the oldest first, or the file
# ERASURES_FILE names (one copied back from off the machine) alone: made
# again after the live database's, they hold the erasures made after the
# dump when the live database is lost with its disk, or replaced by an empty
# one. backup.sh removes a file only once the newest holds all its records,
# so after a loss the good files stay beside the poorer ones written since;
# each is made again, and what the restored data holds already is counted as
# such. Until the review of 16 Nov 2026 only the newest by name was, an
# export of an empty database included.
BACKUPS="${BACKUP_DIR:-./backups}"
if [ -n "$ERASURES_FILE" ]; then
  [ -f "$ERASURES_FILE" ] || { echo "no such file: ERASURES_FILE=$ERASURES_FILE" >&2; exit 1; }
fi
# The files a restore that did not finish kept in this folder
# (erasures-<time>.json, credentials-<time>.json, below): the erasures and
# the credentials of the live database, read before that restore began to
# replace it, which the database here no longer holds. A restore run again
# read the half-replaced database as the live one and never made them again
# (the review of 16 Nov 2026). It runs again only with each of them named,
# KEPT_ERASURES and KEPT_CREDENTIALS, and reads those in place of the
# database's; they go once they are made again.
if [ -n "$KEPT_ERASURES" ] && [ ! -f "$KEPT_ERASURES" ]; then
  echo "no such file: KEPT_ERASURES=$KEPT_ERASURES" >&2; exit 1
fi
if [ -n "$KEPT_CREDENTIALS" ] && [ ! -f "$KEPT_CREDENTIALS" ]; then
  echo "no such file: KEPT_CREDENTIALS=$KEPT_CREDENTIALS" >&2; exit 1
fi
UNNAMED=""
for f in ./erasures-*.json ./credentials-*.json; do
  [ -f "$f" ] || continue
  if [ -n "$KEPT_ERASURES" ] && [ "$f" -ef "$KEPT_ERASURES" ]; then continue; fi
  if [ -n "$KEPT_CREDENTIALS" ] && [ "$f" -ef "$KEPT_CREDENTIALS" ]; then continue; fi
  UNNAMED="$UNNAMED $f"
done
if [ -n "$UNNAMED" ]; then
  echo "a restore that did not finish kept, in this folder:$UNNAMED" >&2
  echo "They hold what the live database held before that restore began to replace it;" >&2
  echo "the database here is no longer the live one, so this restore would read it as" >&2
  echo "such and lose them. Nothing was stopped or changed. Either finish that restore" >&2
  echo "with the commands it printed, then delete them, or name them, and this restore" >&2
  echo "makes them again in place of what it would read from the database:" >&2
  echo "  KEPT_ERASURES=./erasures-<time>.json KEPT_CREDENTIALS=./credentials-<time>.json $0 $DUMP" >&2
  echo "(name only the ones there are; with several of a kind, the oldest is the live" >&2
  echo "database's: the later ones were read from a database already being replaced)." >&2
  exit 1
fi

# A file pg_restore cannot read whole as a dump is refused here, before
# anything is stopped or read: `pg_restore -f /dev/null` reads all of it, as
# a restore would, and writes to no database. The files of erasures sit
# beside the dumps under the same prefix (korely-erasures-<time>.json); one
# given here stopped the API, then pg_restore refused it with the database
# untouched, and the script said the database might hold part of the dump
# and kept the API stopped (the upgrade check of 16 Nov 2026). `--list`
# read only the table of contents, and a dump cut short passed it, stopped
# the API and left the database half replaced (the review of 16 Nov 2026).
if ! docker compose exec -T db pg_restore -f /dev/null < "$DUMP"; then
  echo "$DUMP is not a whole dump pg_restore can read (its error is above), or the db" >&2
  echo "container is not up. Nothing was stopped or changed. backup.sh writes the dumps" >&2
  echo "as $BACKUPS/korely-<time>.dump." >&2
  exit 1
fi
# journals <command>: <command> <file> for each of those files, the oldest
# first. Never in a pipeline, so what <command> sets stays set.
journals() {
  if [ -n "$ERASURES_FILE" ]; then "$@" "$ERASURES_FILE"; return 0; fi
  for f in "$BACKUPS"/korely-erasures-*.json; do
    if [ -f "$f" ]; then "$@" "$f"; fi
  done
  return 0
}
NJ=0
NEWEST=""
count_journal() { NJ=$((NJ + 1)); NEWEST="$1"; }
journals count_journal
MANAGE="docker compose run --rm -T --no-deps api python -m korely_agent.manage"
say_journal() { echo "  $MANAGE erasures-replay < $1" >&2; }

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
# the restore stops, unless SKIP_ERASURES=1 says the live database is gone:
# the files of erasures backup.sh kept (`journals`, above) are then the
# only copy of the erasures made after the dump, up to when the newest was
# written.
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
# Where a file of what must be made again is kept, owner-only in this folder
# (.gitignore keeps it out of git), since the temporary copies go at the exit.
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
KEEP="./erasures-$STAMP.json"
KEEP_CREDENTIALS="./credentials-$STAMP.json"
# A file named (KEPT_ERASURES, KEPT_CREDENTIALS, above) is its own place.
[ -z "$KEPT_ERASURES" ] || KEEP="$KEPT_ERASURES"
[ -z "$KEPT_CREDENTIALS" ] || KEEP_CREDENTIALS="$KEPT_CREDENTIALS"
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
    journals say_journal
    echo "  $MANAGE credentials-replay < $KEEP_CREDENTIALS" >&2
    echo "then: docker compose start api, and delete the kept files." >&2
  fi
  rm -f "$ERASURES" "$CREDENTIALS"
}
trap leave EXIT
trap 'exit 1' INT TERM HUP
READ=yes
if [ -n "$KEPT_ERASURES" ]; then
  cp "$KEPT_ERASURES" "$ERASURES"
else
  $MANAGE erasures-export > "$ERASURES" || { READ=""; echo "[]" > "$ERASURES"; }
fi
if [ -n "$KEPT_CREDENTIALS" ]; then
  cp "$KEPT_CREDENTIALS" "$CREDENTIALS"
else
  $MANAGE credentials-export > "$CREDENTIALS" || { READ=""; echo "{}" > "$CREDENTIALS"; }
fi
if [ -z "$READ" ] && [ -z "$SKIP_ERASURES" ]; then
  echo "could not read the erasures and the credentials of the live database, so a" >&2
  echo "restore would bring back the people erased and the keys revoked since the dump." >&2
  echo "Nothing was restored, and the API is stopped. Start the database and try again," >&2
  echo "or SKIP_ERASURES=1 if it is gone: the erasures are then made again from the" >&2
  echo "$NJ file(s) of erasures backup.sh kept (the newest: ${NEWEST:-none in $BACKUPS})," >&2
  echo "and the keys revoked since the dump are not. To go back to the database as it was instead:" >&2
  echo "docker compose start api" >&2
  exit 1
fi
# The files of erasures backup.sh could not read, set aside: nothing of
# them is made again, and the restore says so.
if [ -z "$ERASURES_FILE" ]; then
  for f in "$BACKUPS"/korely-erasures-*.json.unreadable; do
    [ -f "$f" ] || continue
    echo "$f could not be read when backup.sh set it aside: the erasures it held are" >&2
    echo "not made again. Replay a good copy of it, from off the machine, if you have one." >&2
  done
fi
if [ -z "$READ" ]; then
  # SKIP_ERASURES=1: the live database is gone. What the files hold is what
  # can be made again; an erasure made after the newest is in none.
  if [ -n "$NEWEST" ]; then
    echo "the live database could not be read: the erasures are made again from $NJ file(s)," >&2
    echo "the newest $NEWEST; any made after that file was written are not, nor the" >&2
    echo "credentials changed since the dump." >&2
  else
    echo "the live database could not be read and no file of erasures was found in $BACKUPS:" >&2
    echo "nothing made after the dump is made again." >&2
  fi
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
  journals say_journal
  echo "  $MANAGE credentials-replay < $KEEP_CREDENTIALS" >&2
  echo "  docker compose start api" >&2
  echo "Delete the kept files after. Otherwise restore another dump, naming them: the" >&2
  echo "database here is no longer the live one, and is not read as such:" >&2
  echo "  KEPT_ERASURES=$KEEP KEPT_CREDENTIALS=$KEEP_CREDENTIALS $0 <dump>" >&2
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
# The files of erasures after the live database's, the oldest first: what
# the live one holds is in the restored data by then, and is counted as such;
# what it lost, its disk included, is made again from the files. One that
# fails stops no other; each is named below.
# One the replay reads nothing of (status 1: not the JSON erasures-export
# wrote, or a newer Korely's) fails the same way at every run again, and
# kept the API stopped with that command alone to run (the review of 16 Nov
# 2026): it is named apart, with what to do instead.
JFAILED=""
JUNREAD=""
replay_journal() {
  st=0
  $MANAGE erasures-replay < "$1" || st=$?
  if [ "$st" = 1 ]; then
    JUNREAD="${JUNREAD}  $1
"
  elif [ "$st" != 0 ]; then
    JFAILED="${JFAILED}  $MANAGE erasures-replay < $1
"
  fi
}
journals replay_journal
[ -z "$JFAILED$JUNREAD" ] || FAILED="$FAILED journal"
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
  case "$FAILED" in *journal*) printf '%s' "$JFAILED" >&2 ;; esac
  if [ -n "$JUNREAD" ]; then
    echo "The replay read nothing of these files of erasures (its words are above):" >&2
    printf '%s' "$JUNREAD" >&2
    echo "  One written by a newer Korely: replay it with that version. One that is not" >&2
    echo "  the JSON erasures-export wrote is damaged, and running it again changes nothing:" >&2
    echo "  replay a good copy of it, from off the machine, with" >&2
    echo "  $MANAGE erasures-replay < <the copy>" >&2
    echo "  then move the damaged one out of $BACKUPS, as backup.sh does (<file>.unreadable)." >&2
    echo "  With no good copy, the erasures it held cannot be made again from it: erase" >&2
    echo "  those people again by hand if you know who they are." >&2
  fi
  case "$FAILED" in *credentials*) echo "  $MANAGE credentials-replay < $KEEP_CREDENTIALS" >&2 ;; esac
  echo "then: docker compose start api, and delete the kept file(s)." >&2
  exit 1
fi

REPLACING=""
# What a restore that did not finish kept, made again now: it goes.
for kept in "$KEPT_ERASURES" "$KEPT_CREDENTIALS"; do
  if [ -n "$kept" ]; then rm -f "$kept"; echo "made again, and removed: $kept"; fi
done
docker compose start api >/dev/null 2>&1 || true
echo "restored from $DUMP"
