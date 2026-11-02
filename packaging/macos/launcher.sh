#!/bin/sh
# The hands of Korely Installer.app (packaging/macos, 29 Oct 2026). The app
# asks the questions in its dialogs; this runs install.sh, the release's own
# copy bundled next to it, in a Terminal window, where the user watches it
# work. No installer logic lives here: install.sh decides and checks
# everything, as it does under `curl | sh`.
#
#   launcher.sh terminal DIR   (the app) DIR/answers is written: copies
#                              install.sh and this script into DIR, writes
#                              DIR/install.command and opens it in Terminal
#   launcher.sh run DIR        (Terminal) reads DIR/answers and deletes it,
#                              runs install.sh, opens the dashboard, deletes DIR
#
# DIR is the app's `mktemp -d`, readable by the user alone. DIR/answers holds
# one value a line: the folder, the provider (empty: none), its key (empty:
# none), and, for tests only, a checkout for --from-local, a port and a
# Compose project (the app reads them from KORELY_INSTALLER_FROM_LOCAL,
# KORELY_INSTALLER_PORT and KORELY_INSTALLER_PROJECT, which nobody sets).
#
# The key is never an argument, of this script, of the command Terminal
# runs, or of install.sh: `ps` and the shell's history never see it. It
# reaches install.sh in KORELY_PROVIDER_KEY, as install.sh asks of anything
# that runs it without a terminal, and install.sh writes it into .env alone.
# The copies in DIR make the run independent of the disk image: ejecting it
# half way stops nothing.
set -eu
unset CDPATH
umask 077

die() { printf 'Korely Installer: %s\n' "$*" >&2; exit 1; }
# A word for /bin/sh, between single quotes.
quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

here=$(cd -P -- "$(dirname -- "$0")" >/dev/null && pwd -P)
mode=${1:-}
tmp=${2:-}
[ -n "$tmp" ] && [ -d "$tmp" ] || die "usage: launcher.sh terminal|run DIR"
[ -f "$tmp/answers" ] || die "$tmp holds no answers"

case "$mode" in
  terminal)
    if [ "$here" != "$(cd -P -- "$tmp" && pwd -P)" ]; then
      cp -- "$here/install.sh" "$here/launcher.sh" "$tmp/"
    fi
    {
      echo '#!/bin/sh'
      echo "exec /bin/sh $(quote "$tmp/launcher.sh") run $(quote "$tmp")"
    } > "$tmp/install.command"
    chmod 700 "$tmp/install.command"
    open -b com.apple.Terminal "$tmp/install.command"
    ;;

  run)
    dir=""; provider=""; key=""; from_local=""; port=""; project=""
    {
      IFS= read -r dir || true
      IFS= read -r provider || true
      IFS= read -r key || true
      IFS= read -r from_local || true
      IFS= read -r port || true
      IFS= read -r project || true
    } < "$tmp/answers"
    rm -f -- "$tmp/answers"
    [ -n "$dir" ] || die "no folder was chosen"

    # The answers decide, never a KORELY_* exported in the user's shell for
    # another install: every name install.sh reads.
    for name in $(grep -o '\${KORELY_[A-Z_]*' "$here/install.sh" | cut -c3- | sort -u); do
      unset "$name"
    done
    if [ -n "$provider" ]; then
      KORELY_PROVIDER=$provider
      export KORELY_PROVIDER
    fi
    if [ -n "$key" ]; then
      KORELY_PROVIDER_KEY=$key
      export KORELY_PROVIDER_KEY
    fi
    key=""
    set -- --dir "$dir"
    [ -z "$from_local" ] || set -- "$@" --from-local "$from_local"
    [ -z "$port" ] || set -- "$@" --port "$port"
    [ -z "$project" ] || set -- "$@" --project "$project"

    # Docker Desktop's command, wherever this Mac has it, after the user's own.
    PATH="$PATH:/usr/local/bin:/opt/homebrew/bin:$HOME/.docker/bin:/Applications/Docker.app/Contents/Resources/bin"
    export PATH

    echo "Installing Korely Agents in $dir"
    status=0
    /bin/sh "$here/install.sh" "$@" || status=$?
    unset KORELY_PROVIDER_KEY
    if [ "$status" -eq 0 ]; then
      port=$(sed -n 's/^PORT=//p' "$dir/.env" 2>/dev/null | tail -n 1)
      url="http://127.0.0.1:${port:-8000}"
      echo
      echo "Opening your dashboard: $url"
      echo "Log in with the login and the password above. Copy them first: they are"
      echo "shown only this once."
      open "$url" || echo "Open $url in your browser."
    else
      echo
      echo "The install stopped, and the lines above say why. Once that is fixed, open"
      echo "Korely Installer again: it goes on from where it stopped and makes nothing twice."
    fi
    cd /
    rm -rf -- "$tmp"
    printf '\nPress Return to close this window. '
    read -r _ || true
    exit "$status"
    ;;

  *) die "usage: launcher.sh terminal|run DIR" ;;
esac
