# The terminal recordings' shared steps, sourced by scripts/make_install_demo.sh
# and scripts/make_terminal_demos.sh (never run on its own):
#
#   demo_standins    the release's files and image, made here before the public release
#   demo_render      a record_terminal.py recording into frames (xterm.js in Playwright)
#   demo_convert     the frames into docs/assets/<name>.mp4 and <name>.gif
#   demo_stack       two recordings' frames as two panes, one above the other, likewise
#
# The caller sets ROOT, HERE, DEMO, RAW, RELEASE, REAL_CURL, PLAYWRIGHT_IMAGE,
# FFMPEG_IMAGE and ASSETS, and has `set -eu`.

# Before the release the README's line cannot reach GitHub: the repository and
# the image are not public. Two stand-ins, and nothing else changed:
#   - curl, first on the shell's PATH ($DEMO/bin), answers the addresses under
#     $RAW/ from $DEMO/served, laid out as GitHub would serve them:
#     main/install.sh is this checkout's install.sh stamped as scripts/release.sh
#     stamps it (THIS_RELEASE and the sha256 of the release files), v<release>/...
#     the release files of this checkout. Every other call goes to the real curl.
#   - the image ghcr.io/korely-ai/korely-agent:<release> is built here from this
#     checkout's HEAD with VERSION set to the release, as release.sh sets it.
# Sets BUILT (the image built, for the caller's cleanup) and RUN_PATH.
demo_standins() {
  echo "==> The stand-ins for $RELEASE: the files GitHub will serve, and the image" >&2
  SRC="$DEMO/src"
  mkdir -p "$SRC" "$DEMO/bin" "$DEMO/served"
  git -C "$ROOT" archive --format=tar HEAD | tar -x -C "$SRC"
  # As scripts/release.sh writes the tag's VERSION, pyproject.toml and install.sh.
  echo "$RELEASE" > "$SRC/VERSION"
  python3 - "$RELEASE" "$SRC/pyproject.toml" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[2]); s = p.read_text()
p.write_text(re.sub(r'^version = "[^"]+"', f'version = "{sys.argv[1]}"', s, count=1, flags=re.M))
PY
  python3 "$SRC/scripts/stamp_installer.py" release "$RELEASE" --installer "$SRC/install.sh" --root "$SRC" >&2
  mkdir -p "$DEMO/served/main" "$DEMO/served/v$RELEASE/deploy"
  cp "$SRC/install.sh" "$DEMO/served/main/install.sh"
  for f in $(sed -n 's/^RELEASE_FILES="\(.*\)"$/\1/p' "$SRC/install.sh"); do
    cp "$SRC/$f" "$DEMO/served/v$RELEASE/$f"
  done
  BUILT="ghcr.io/korely-ai/korely-agent:$RELEASE"
  docker build -q -t "$BUILT" "$SRC" >&2

  cat > "$DEMO/bin/curl" <<EOF
#!/bin/sh
# The demo's stand-in for curl: $RAW/<path> is answered from
# $DEMO/served/<path>, as GitHub will answer it; anything else is the real curl.
served="$DEMO/served"
raw="$RAW/"
real="$REAL_CURL"
log="$DEMO/standin.log"
EOF
  cat >> "$DEMO/bin/curl" <<'EOF'
url=""; out=""; wfmt=""
for a in "$@"; do case "$a" in "$raw"*) url=$a ;; esac; done
[ -n "$url" ] || exec "$real" "$@"
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    -w) wfmt=$2; shift 2 ;;
    --proto|--proto-redir|--retry|--max-time) shift 2 ;;
    *) shift ;;
  esac
done
file="$served/${url#"$raw"}"
if [ ! -f "$file" ]; then
  echo "404 $url" >> "$log"
  echo "curl: (22) The requested URL returned error: 404" >&2
  exit 22
fi
echo "200 $url" >> "$log"
if [ -n "$out" ]; then cp "$file" "$out"; else cat "$file"; fi
[ -n "$wfmt" ] && printf '%s' "$url"
exit 0
EOF
  chmod 755 "$DEMO/bin/curl"
  RUN_PATH="$DEMO/bin:$PATH"
}

# demo_render CAST FRAMES [VAR=value...]: the recording played into FRAMES by
# scripts/make_install_demo.mjs, in the Playwright image (Playwright from
# scripts/package-lock.json, installed in the container and gone with it;
# scripts/ is mounted read-only). Extra VAR=value pairs reach the renderer
# (BASE, OPU, OPP for the install's browser part; FINAL, TITLE): passed with
# -e NAME and the value in the environment, never on a command line `ps` shows.
demo_render() {
  local cast="$1" frames="$2" envs="" pair; shift 2
  mkdir -p "$frames"
  for pair in "$@"; do
    export "$pair"
    envs="$envs -e ${pair%%=*}"
  done
  # shellcheck disable=SC2086
  docker run --rm --network host \
    -v "$HERE":/src:ro -v "$(dirname "$cast")":/cast:ro -v "$frames":/frames \
    $envs -e CAST="/cast/$(basename "$cast")" -e OUT=/frames \
    -w /tmp "$PLAYWRIGHT_IMAGE" \
    sh -c 'cp /src/package.json /src/package-lock.json /src/make_install_demo.mjs /tmp/ \
           && npm ci --silent >/dev/null 2>&1 \
           && node /tmp/make_install_demo.mjs'
}

# demo_convert FRAMES NAME: as scripts/make_demo.sh converts. The MP4 at the
# frames' 2x, the GIF at README width, held frames as one GIF frame with its
# delay, no dithering, played once: a story that ends stays on its last
# screen. Prints the played length in seconds.
demo_convert() {
  local frames="$1" name="$2" length
  length="$(awk '$1 == "duration" { s += $2 } END { printf "%.3f", s }' "$frames/frames.txt")"
  demo_encode "$frames" "$name" "$length" "" frames.txt
  echo "$length"
}

# demo_encode DIR NAME LENGTH FILTER LIST...: demo_convert's two files from the
# concat lists LIST... of DIR, FILTER first (the lists' streams into one, e.g.
# "[0][1]vstack,"; empty for one list).
demo_encode() {
  local dir="$1" name="$2" length="$3" filter="$4" inputs="" list; shift 4
  for list in "$@"; do inputs="$inputs -f concat -safe 0 -i $list"; done
  # shellcheck disable=SC2086
  ff() { docker run --rm --platform linux/amd64 -v "$dir":/f:ro -v "$ASSETS":/o -w /f "$FFMPEG_IMAGE" \
           -hide_banner -loglevel error -y $inputs "$@"; }
  ff -filter_complex "${filter}fps=30,format=yuv420p" -t "$length" -c:v libx264 -preset slow -crf 20 \
     -tune stillimage -movflags +faststart "/o/$name.mp4"
  ff -filter_complex "${filter}scale=880:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=full[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
     -fps_mode vfr -loop -1 "/o/$name.gif"
}

# demo_stack TOP BOTTOM NAME: two rendered recordings (frame folders side by
# side in one folder) as two panes, TOP above BOTTOM, converted as
# demo_convert converts. TOP plays while BOTTOM stays on its frame at its
# "start" marker (its shell's prompt, before anything is typed; in a
# recording made before that marker, the frame at its first marker, or the one
# before if a shortened wait's badge already shows there); then BOTTOM plays
# under TOP's last frame. Nothing is cut but BOTTOM's frames before that one. Also writes NAME.still.png, the two last frames,
# next to the folders. Prints the played length in seconds.
demo_stack() {
  local top="$1" bottom="$2" name="$3" dir length
  dir="$(dirname "$top")"
  [ "$(dirname "$bottom")" = "$dir" ] || { echo "demo_stack: $top and $bottom are not side by side" >&2; return 1; }
  python3 - "$top" "$bottom" "$dir/$name" <<'PY'
import json, os, sys
top, bottom, out = sys.argv[1:4]

def frames(folder):
    """frames.txt's (file, seconds), without the last frame written twice."""
    pairs, name = [], None
    for row in open(os.path.join(folder, "frames.txt")):
        word, _, rest = row.strip().partition(" ")
        if word == "file":
            name = rest.strip("'")
        elif word == "duration":
            pairs.append([os.path.join(os.path.basename(folder), name), float(rest)])
    return pairs

up, down = frames(top), frames(bottom)
timeline = json.load(open(os.path.join(bottom, "timeline.json")))
names = [os.path.basename(f) for f, _ in down]
starts = [m for m in timeline["markers"] if m["marker"] == "start"]
if starts:
    first = names.index(starts[0]["frame"])
else:
    mark = timeline["markers"][0]
    first = names.index(mark["frame"])
    if first and any(b["at"] - 0.01 <= mark["at"] < b["at"] + b["shown"] for b in timeline["shortened"]):
        first -= 1
down = down[first:]
up_s, down_s = sum(s for _, s in up), sum(s for _, s in down)
up[-1][1] += down_s     # TOP's last frame, while BOTTOM plays
down[0][1] += up_s      # BOTTOM at its prompt, while TOP plays
for pairs, side in ((up, "top"), (down, "bottom")):
    with open(f"{out}.{side}.txt", "w") as f:
        f.write("ffconcat version 1.0\n")
        for name, s in pairs:
            f.write(f"file '{name}'\nduration {s:.4f}\n")
        f.write(f"file '{pairs[-1][0]}'\nduration 0.0333\n")
with open(f"{out}.length", "w") as f:
    f.write(f"{up_s + down_s:.3f}\n")
PY
  length="$(cat "$dir/$name.length")"
  demo_encode "$dir" "$name" "$length" "[0][1]vstack," "$name.top.txt" "$name.bottom.txt"
  docker run --rm --platform linux/amd64 -v "$dir":/f -w /f "$FFMPEG_IMAGE" -hide_banner -loglevel error -y \
    -i "$(basename "$top")/still-2.png" -i "$(basename "$bottom")/still-2.png" \
    -filter_complex vstack "$name.still.png"
  echo "$length"
}

demo_size() { wc -c < "$1" | awk '{ printf "%.2f MB", $1 / 1048576 }'; }
