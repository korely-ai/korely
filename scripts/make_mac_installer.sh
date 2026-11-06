#!/usr/bin/env bash
# Korely Installer.app in a .dmg, for the Mac user who would rather not open a
# terminal (the decision of 29 Oct 2026: the curl line for technical
# users, a download for everybody else).
#
#     scripts/make_mac_installer.sh 1.0.1             signed, notarized if it can be
#     scripts/make_mac_installer.sh 1.0.1 --unsigned  for a local test only
#
# The app is a thin launcher (packaging/macos): a small Swift executable that
# runs installer.applescript, whose native dialogs ask for Docker, the folder
# and the optional model key, then launcher.sh, which opens Terminal running
# install.sh. No Xcode project and no dependency: swiftc and osacompile from
# the Command Line Tools. The
# install.sh it bundles is the release's own copy, taken from the tag v<version>
# and checked here to be exactly what scripts/release.sh stamps there
# (scripts/stamp_installer.py check: THIS_RELEASE and the sha256 of the files it
# downloads, with or without the image digest stamped after CI's push). Any
# other copy is refused before anything is built: the app and `curl | sh`
# install the same thing, from one source.
#
# Signing: `codesign --options runtime --timestamp` with the Developer ID
# Application identity of this Mac's keychain, or the one KORELY_SIGN_IDENTITY
# names. Without one the build stops, unless --unsigned says this is a local
# test (ad-hoc signature, a dmg named -unsigned that must never be shipped).
#
# Notarization: if the notarytool keychain profile KORELY_NOTARY_PROFILE
# (default korely) exists, the dmg is submitted with `notarytool submit --wait`
# and stapled. Otherwise the exact commands are printed. The profile is made
# once, by the account holder, with
#     xcrun notarytool store-credentials korely --apple-id <id> --team-id <team>
#
# Options:
#   --unsigned        ad-hoc signature, no notarization: a local test
#   --out DIR         where the .dmg goes (default dist/)
#   --installer PATH  a copy of install.sh instead of the tag's, and
#   --root DIR        the release files it was stamped from (both together:
#                     for a release not tagged yet, or the tests)
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Korely Installer"
BUNDLE_ID="ai.korely.installer"
PROFILE="${KORELY_NOTARY_PROFILE:-korely}"

die() { echo "make_mac_installer.sh: $*" >&2; exit 1; }

version=""; unsigned=""; out="$here/dist"; installer=""; root=""
while [ $# -gt 0 ]; do
  case "$1" in
    --unsigned) unsigned=1; shift ;;
    --out) [ $# -ge 2 ] || die "--out needs a folder"; out="$2"; shift 2 ;;
    --installer) [ $# -ge 2 ] || die "--installer needs a file"; installer="$2"; shift 2 ;;
    --root) [ $# -ge 2 ] || die "--root needs a folder"; root="$2"; shift 2 ;;
    -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) [ -z "$version" ] || die "one version only"; version="$1"; shift ;;
  esac
done
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage: scripts/make_mac_installer.sh <x.y.z> [--unsigned]"
if [ -n "$installer" ] || [ -n "$root" ]; then
  [ -n "$installer" ] && [ -n "$root" ] || die "--installer and --root go together"
fi

tmpdir="${TMPDIR:-/tmp}"
work="$(mktemp -d "${tmpdir%/}/korely-mac-installer.XXXXXX")"
trap 'rm -rf -- "$work"' EXIT

# ── the release's install.sh, and nothing else ─────────────────────────────
tagged=""
if git -C "$here" rev-parse -q --verify "refs/tags/v$version" >/dev/null 2>&1; then
  mkdir -p "$work/tag"
  git -C "$here" show "v$version:install.sh" > "$work/tag/install.sh"
  tagged="$work/tag/install.sh"
fi
if [ -z "$installer" ]; then
  [ -n "$tagged" ] \
    || die "there is no tag v$version: the app bundles the install.sh of a release (scripts/release.sh $version first)"
  root="$work/release"
  mkdir -p "$root"
  files=$(sed -n 's/^RELEASE_FILES="\([^"]*\)"$/\1/p' "$tagged")
  [ -n "$files" ] || die "install.sh at v$version names no RELEASE_FILES"
  # shellcheck disable=SC2086
  git -C "$here" archive "v$version" -- $files | tar -x -C "$root"
  installer="$tagged"
fi
[ -f "$installer" ] || die "$installer is not a file"
[ -d "$root" ] || die "$root is not a folder"
sha=$(python3 "$here/scripts/stamp_installer.py" check "$version" --installer "$installer" \
      --root "$root" ${tagged:+--tagged "$tagged"}) \
  || die "refusing to bundle $installer: it is not the install.sh release.sh stamps for $version"
[ -n "$tagged" ] || echo "warning: no tag v$version here, so only the stamps of $installer were checked (a local test)"
echo "install.sh: the release copy of $version, sha256 $sha"

# ── the tools, and who signs ───────────────────────────────────────────────
[ "$(uname -s)" = Darwin ] || die "the app is built on a Mac (swiftc, osacompile, codesign, hdiutil)"
for tool in osacompile codesign hdiutil sips iconutil plutil lipo xcrun; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is missing (the Command Line Tools: xcode-select --install)"
done
if [ -z "$unsigned" ]; then
  identity="${KORELY_SIGN_IDENTITY:-}"
  if [ -z "$identity" ]; then
    identity=$(security find-identity -v -p codesigning \
      | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -n 1)
  fi
  [ -n "$identity" ] || die "no Developer ID Application identity in the keychain (KORELY_SIGN_IDENTITY names one). An unsigned app is refused by Gatekeeper: --unsigned is for a local test only."
  security find-identity -v -p codesigning | grep -qF "\"$identity\"" \
    || die "the keychain has no valid codesigning identity \"$identity\""
  echo "signing as: $identity"
fi

# ── the app ────────────────────────────────────────────────────────────────
# Its executable is packaging/macos/main.swift, for both architectures and
# macOS 13 on (the Command Line Tools have no x86_64 Swift back-deployment
# libraries for 12; Script Editor's applet stub asks for the macOS it was built
# on), and it runs the compiled installer.applescript in its own process.
MIN_MACOS="13.0"
app="$work/$APP_NAME.app"
res="$app/Contents/Resources"
mkdir -p "$app/Contents/MacOS" "$res/Scripts"
for arch in arm64 x86_64; do
  xcrun --sdk macosx swiftc -swift-version 5 -O -target "$arch-apple-macos$MIN_MACOS" \
    -o "$work/main-$arch" "$here/packaging/macos/main.swift"
done
lipo -create -output "$app/Contents/MacOS/$APP_NAME" "$work/main-arm64" "$work/main-x86_64"
osacompile -o "$res/Scripts/main.scpt" "$here/packaging/macos/installer.applescript"
install -m 644 "$installer" "$res/install.sh"
install -m 755 "$here/packaging/macos/launcher.sh" "$res/launcher.sh"
printf 'APPL????' > "$app/Contents/PkgInfo"

# The icon: the dashboard's mark on a rounded square, rendered by sips.
python3 - "$here/dashboard/public/korely-mark.svg" "$work/icon.svg" <<'PY'
import re, sys
mark = open(sys.argv[1]).read()
inner = re.search(r"<svg\b[^>]*>(.*)</svg>", mark, re.S).group(1)
open(sys.argv[2], "w").write(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">'
    '<rect x="100" y="100" width="824" height="824" rx="185" fill="#0F172A"/>'
    f'<g transform="translate(192 180) scale(20)">{inner}</g></svg>')
PY
iconset="$work/AppIcon.iconset"
mkdir "$iconset"
sips -s format png -z 1024 1024 "$work/icon.svg" --out "$work/icon.png" >/dev/null
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$work/icon.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$work/icon.png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$res/AppIcon.icns"

# Info.plist: who the app is. It asks for no permission: it reads nothing of
# the user's, and drives no other app (Terminal is opened with `open`, not
# with Apple Events).
python3 - "$app/Contents/Info.plist" "$APP_NAME" "$BUNDLE_ID" "$version" "$MIN_MACOS" <<'PY'
import plistlib, sys
path, name, bundle_id, version, least = sys.argv[1:]
with open(path, "wb") as f:
    plistlib.dump({
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": name,
        "CFBundleExecutable": name,
        "CFBundleIconFile": "AppIcon",
        "CFBundleIdentifier": bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": least,
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": f"Korely Agents {version}, Apache License 2.0",
        "NSPrincipalClass": "NSApplication",
    }, f)
PY
plutil -lint "$app/Contents/Info.plist" >/dev/null

cmp -s "$installer" "$res/install.sh" || die "the bundled install.sh is not the release copy"

xattr -cr "$app"
if [ -n "$unsigned" ]; then
  codesign --force --sign - "$app"
else
  codesign --force --options runtime --timestamp --sign "$identity" "$app"
fi
codesign --verify --deep --strict --verbose=2 "$app"

# ── the disk image ─────────────────────────────────────────────────────────
mkdir -p "$out"
dmg="$out/Korely-Installer-$version${unsigned:+-unsigned}.dmg"
stage="$work/dmg"
mkdir "$stage"
ditto "$app" "$stage/$APP_NAME.app"
ln -s /Applications "$stage/Applications"
rm -f "$dmg"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$stage" -fs HFS+ -format UDZO -ov "$dmg"
if [ -z "$unsigned" ]; then
  codesign --force --timestamp --sign "$identity" "$dmg"
  codesign --verify --strict --verbose=2 "$dmg"
fi

# ── notarization ───────────────────────────────────────────────────────────
notarized=""
if [ -z "$unsigned" ]; then
  if xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
    echo "notarizing with the keychain profile $PROFILE (a few minutes)"
    xcrun notarytool submit "$dmg" --keychain-profile "$PROFILE" --wait
    xcrun stapler staple "$dmg"
    xcrun stapler validate "$dmg"
    notarized=1
  else
    echo
    echo "Not notarized: the keychain has no notarytool profile \"$PROFILE\". Once, by the"
    echo "Apple Developer account holder (an app-specific password from appleid.apple.com):"
    echo "  xcrun notarytool store-credentials $PROFILE --apple-id <apple id> --team-id <team id>"
    echo "then, for this dmg:"
    echo "  xcrun notarytool submit \"$dmg\" --keychain-profile $PROFILE --wait"
    echo "  xcrun stapler staple \"$dmg\""
    echo "  xcrun stapler validate \"$dmg\""
    echo "  spctl -a -vv -t open --context context:primary-signature \"$dmg\""
    echo "(or run this script again: it notarizes when the profile exists)"
  fi
fi

# ── what Gatekeeper will say ───────────────────────────────────────────────
echo
echo "== checks =="
codesign -dvv "$app" 2>&1 | grep -E '^(Identifier|Authority|TeamIdentifier|Timestamp|Runtime Version|CodeDirectory)' || true
spctl -a -vv -t exec "$app" 2>&1 || true
[ -z "$unsigned" ] && { spctl -a -vv -t open --context context:primary-signature "$dmg" 2>&1 || true; }
echo
echo "$dmg"
echo "  $(du -h "$dmg" | cut -f1 | tr -d ' '), sha256 $(shasum -a 256 "$dmg" | cut -d' ' -f1)"
if [ -n "$unsigned" ]; then
  echo "  unsigned: for a local test, never for a release"
elif [ -z "$notarized" ]; then
  echo "  signed, not notarized yet: the commands above"
else
  echo "  signed, notarized and stapled"
fi
