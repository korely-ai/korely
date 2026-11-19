#!/bin/sh
# Dump the database to ./backups, keeping the last N, and the erasures beside it.
#
#   ./deploy/backup.sh            a dump, then the erasures
#   ./deploy/backup.sh erasures   the erasures alone
#   0 3 * * *  cd /srv/korely-agent && ./deploy/backup.sh >> backup.log 2>&1
#   10 * * * * cd /srv/korely-agent && ./deploy/backup.sh erasures >> backup.log 2>&1
#
# Custom format (-Fc), not plain SQL: it restores selectively, in parallel, and
# compresses. A dump you have never restored is not a backup, it is a file. See
# restore.sh, and run it against a scratch database once before you need it.
#
# The erasures (`manage erasures-export`: every erasure record, pseudonyms,
# ids, counts and times, never a memory's text) go to
# korely-erasures-<time>.json in the same folder. A restore brings back the
# people erased after its dump; restore.sh makes those erasures again from
# the live database, and from every one of these files too, which are the
# only copy when the live database is lost with its disk. An older file goes
# only once the newest holds all its records (`manage erasures-superseded`):
# after a loss the hourly export goes on from an empty or older database,
# and the good files stay beside it. The erasures made after the newest file
# are in none; hourly, as above, an hour at most on this machine, and off it
# as long as the copy of the folder is old. Copy the folder off the machine
# with the dumps, as often as the export runs.
set -e

KEEP="${KEEP:-14}"
DIR="${BACKUP_DIR:-./backups}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$DIR/korely-$STAMP.dump"
ERASURES="$DIR/korely-erasures-$STAMP.json"

# A dump is every memory and fact of every end user, plus the model key
# (encrypted with SECRET_KEY, which is not in the dump). It was written with
# the shell's default mask, readable by every account on the machine, which a
# tester found by looking. Owner only, the directory and the files.
umask 077
mkdir -p "$DIR"
chmod 700 "$DIR" 2>/dev/null || true

# The erasures, each to a file of its own written whole before it gets its
# name, like the dump below: an export that failed or wrote no records is
# never the newest file, the one a restore reads. The API's container reads
# them (`exec`, so nothing is migrated here); with the API stopped there is
# nothing new to export, and the run says it failed.
EPART="$ERASURES.partial"
export_erasures() {
  trap 'rm -f "$PART" "$EPART"' EXIT
  trap 'exit 1' INT TERM HUP
  if ! docker compose exec -T api python -m korely_agent.manage erasures-export > "$EPART"; then
    echo "FAILED: the erasures were not exported (is the API up?)" >&2
    return 1
  fi
  if ! docker compose exec -T api python -c \
      'import json,sys; d=json.load(sys.stdin); assert isinstance(d.get("records"), list)' < "$EPART"; then
    echo "FAILED: the erasures export is not one restore.sh can read, nothing kept" >&2
    return 1
  fi
  mv "$EPART" "$ERASURES"
  echo "$ERASURES"
  # The older files the newest holds whole, by the ids of their records,
  # read in the API's container from a tar of them (it reads no database).
  # A file the newest lacks a record of stays, and so does one it cannot
  # read: restore.sh makes every kept file again. Until the review of 16 Nov
  # 2026 the last 48 by name were kept, so after a loss 48 hours of exports
  # of an empty database rotated the good files away.
  # A plain archive: the tar of macOS adds an AppleDouble member
  # (._<name>) for each file with extended attributes unless
  # COPYFILE_DISABLE is set (bsdtar), and the rotation read those as files
  # it could not read (the upgrade check of 16 Nov 2026).
  if ! GONE=$( (cd "$DIR" && COPYFILE_DISABLE=1 tar -cf - korely-erasures-*.json) \
      | docker compose exec -T api python -m korely_agent.manage erasures-superseded); then
    echo "FAILED: could not tell which files of erasures the newest holds whole; none removed" >&2
    return 1
  fi
  printf '%s\n' "$GONE" | while IFS= read -r old; do
    case "$old" in
      */*) ;;
      korely-erasures-*.json) rm -f "$DIR/$old" ;;
    esac
  done
  # A file that is not one of erasures at all (`unreadable <name>`) was
  # kept for ever, and every restore failed on it with the API stopped (the
  # review of 16 Nov 2026). It is set aside under .unreadable, which
  # restore.sh does not make again and names, and the run fails, saying so.
  # The run fails for what it set aside, and for nothing else it was told.
  SET_ASIDE=0
  while IFS= read -r bad; do
    case "$bad" in
      */*) ;;
      korely-erasures-*.json)
        mv "$DIR/$bad" "$DIR/$bad.unreadable"
        SET_ASIDE=$((SET_ASIDE + 1))
        echo "FAILED: $DIR/$bad is not a file of erasures any more (damaged): set aside as" \
             "$DIR/$bad.unreadable, and no restore makes it again. Put back a good copy" \
             "of it, from off the machine, under its name." >&2 ;;
    esac
  done <<EOF
$(printf '%s\n' "$GONE" | sed -n 's/^unreadable //p')
EOF
  LEFT=$(ls -1 "$DIR"/korely-erasures-*.json 2>/dev/null | wc -l | tr -d ' ')
  if [ "$LEFT" -gt 1 ]; then
    echo "kept $((LEFT - 1)) older file(s) of erasures holding records the newest lacks;" \
         "restore.sh makes each one again" >&2
  fi
  [ "$SET_ASIDE" = 0 ]
}

if [ "$1" = "erasures" ]; then
  umask 077
  mkdir -p "$DIR"
  chmod 700 "$DIR" 2>/dev/null || true
  export_erasures
  exit $?
fi

# Written under a name the rotation below does not count, and renamed only
# once pg_dump has succeeded and the size is plausible. The redirection used to
# create the final name before pg_dump ran: when it failed (the database
# stopped, the disk full) `set -e` left at once, the check below never ran,
# and the empty or half file stayed as a backup. A few of those filled the
# KEEP places and rotated the good dumps away (audit of 17 Oct 2026).
PART="$OUT.partial"
trap 'rm -f "$PART"' EXIT
trap 'exit 1' INT TERM HUP

# pg_dump runs inside the db container, so the host needs no postgres client
# and the version always matches the server.
if ! docker compose exec -T db pg_dump -U korely -d korely -Fc > "$PART"; then
  echo "FAILED: pg_dump did not finish, nothing kept" >&2
  exit 1
fi

# An empty or truncated dump is worse than none: it looks like a backup in the
# listing and fails when it matters.
SIZE=$(wc -c < "$PART")
if [ "$SIZE" -lt 1000 ]; then
  echo "FAILED: dump is only ${SIZE} bytes, refusing to keep it" >&2
  exit 1
fi
mv "$PART" "$OUT"

echo "$OUT ($(( SIZE / 1024 )) KB)"

ls -1t "$DIR"/korely-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  echo "removing $old"
  rm -f "$old"
done

# The dump is kept whatever happens here; a failed export fails the run.
export_erasures
