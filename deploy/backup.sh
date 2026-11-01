#!/bin/sh
# Dump the database to ./backups, keeping the last N.
#
#   ./deploy/backup.sh
#   0 3 * * *  cd /srv/korely-agent && ./deploy/backup.sh >> backup.log 2>&1
#
# Custom format (-Fc), not plain SQL: it restores selectively, in parallel, and
# compresses. A dump you have never restored is not a backup, it is a file. See
# restore.sh, and run it against a scratch database once before you need it.
set -e

KEEP="${KEEP:-14}"
DIR="${BACKUP_DIR:-./backups}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$DIR/korely-$STAMP.dump"

# A dump is every memory and fact of every end user, plus the model key
# (encrypted with SECRET_KEY, which is not in the dump). It was written with
# the shell's default mask, readable by every account on the machine, which a
# tester found by looking. Owner only, the directory and the files.
umask 077
mkdir -p "$DIR"
chmod 700 "$DIR" 2>/dev/null || true

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
