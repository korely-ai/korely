"""The local embedder's model, pinned: what it is, where it comes from, and
how the image build fetches it.

Two things read this file, which is why it is one file with nothing but the
standard library in it:

- the image build, which copies this file alone into a stage of its own and
  runs it (Dockerfile, stage `model`), and only when the install asks for the
  local embedder. Alone, so that the 1.2 GB layer it produces is rebuilt when
  the model changes and not whenever any other line of the code does: an
  upgrade does not download the weights again;
- core/local_embedder.py, which loads what it fetched.

The files are the ONNX export of Google's EmbeddingGemma-300m published by
onnx-community, at one revision, and each is checked against its size and its
sha256 before the build goes on: a mirror, a proxy or a moved branch cannot put
other weights in the image without the build failing.

Full precision (fp32): `onnx/model.onnx` is the graph and `onnx/model.onnx_data`
holds its weights, in the same folder, where ONNX Runtime looks for them; the
graph alone does not load. The 8-bit version is the desktop's. On a set of real
notes full precision put the right note in the first five 90% of the time, the
8-bit one 88%.

The tokenizer is `tokenizer.model`, the SentencePiece model of the same
revision, read with the `sentencepiece` package (one wheel, no dependencies).
`tokenizers` with the model's `tokenizer.json` would have brought
huggingface-hub and its dependencies into the image; on 315 texts (Italian,
CJK, emoji, code, runs of whitespace and newlines, 300 random strings) the two
gave the same ids, except for a text that spells a control token such as
`<eos>`, which `tokenizers` turns into the control token and SentencePiece
reads as text. The second is the right reading for a memory: a text that
contains "<eos>" must not end its own sequence (measured 2026-09-29).

Licence: the Gemma Terms of Use (https://ai.google.dev/gemma/terms), which
allow commercial use under Google's Prohibited Use Policy. This repository
does not carry the weights: the build downloads them on the machine of whoever
builds. An image built with them does carry them, so the build writes, next to
the weights, the notice the terms ask a distribution to include (`NOTICE`),
and puts beside it the additional terms approved on 2026-10-01 and
a copy of the Gemma Terms (licenses/GEMMA-TERMS.md, licenses/gemma-terms-of-
use.txt, which the source tarball carries too).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request

NAME = "embeddinggemma-300m"
REPO = "onnx-community/embeddinggemma-300m-ONNX"
REVISION = "5090578d9565bb06545b4552f76e6bc2c93e4a66"
# The width of the model's own output, and of the embedding columns: the one
# place it is written (db/models.py and core/llm.EMBEDDING_DIM read it, and
# Gemini is asked for as many). No truncation, no schema change.
DIMENSIONS = 768
# tokenizer_config.json `model_max_length`, config.json
# `max_position_embeddings`. The prompt and <bos>/<eos> count.
MAX_TOKENS = 2048

# Path in the repository, and in the folder the build writes, then its size in
# bytes and its sha256 (the LFS object id, read from the Hub's API for this
# revision on 2026-09-29).
FILES = {
    "onnx/model.onnx": (
        479_932, "ea91fd315a7c152d427d231746f0f811a1ac93beaba656abfdf2b24e091265e4"),
    "onnx/model.onnx_data": (
        1_234_521_088, "ef835ae565d8695236652475903078e8ed794c7c35faf1164d78ec3238e8a88d"),
    "tokenizer.model": (
        4_689_074, "1299c11d7cf632ef3b4e11937501358ada021bbdf7c47638d13c0ee982f2e79c"),
}
MANIFEST = "MANIFEST.json"
DEFAULT_MIRROR = "https://huggingface.co"

# The first line is the notice the Gemma Terms of Use ask every distribution
# of the model to carry; the rest says what these files are.
NOTICE = (
    "Gemma is provided under and subject to the Gemma Terms of Use found at "
    "ai.google.dev/gemma/terms\n"
    "\n"
    "These files are Google's EmbeddingGemma-300m as exported to ONNX by\n"
    f"onnx-community (https://huggingface.co/{REPO},\n"
    f"revision {REVISION}), unmodified, downloaded by the build\n"
    "of this image. Their use is subject to the Gemma Prohibited Use Policy:\n"
    "ai.google.dev/gemma/prohibited_use_policy\n"
)


def url(path: str, mirror: str = DEFAULT_MIRROR) -> str:
    """Where one file of the pinned revision is downloaded from. A mirror must
    serve the Hub's layout (`<repo>/resolve/<revision>/<path>`)."""
    return f"{mirror.rstrip('/')}/{REPO}/resolve/{REVISION}/{path}"


# Seconds without a byte from the mirror before a download is tried again
# (`fetch`'s attempts): a deadline on each read, not on a 1.2 GB file.
READ_TIMEOUT_SECONDS = 60


def _download(source: str, target: str) -> tuple:
    """Stream one file to `target`; its size and sha256."""
    digest, size = hashlib.sha256(), 0
    req = urllib.request.Request(source, headers={"User-Agent": "korely-agent-build"})
    with urllib.request.urlopen(req, timeout=READ_TIMEOUT_SECONDS) as r, open(target, "wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def fetch(dest: str, mirror: str = DEFAULT_MIRROR, *, attempts: int = 3) -> None:
    """Every file into `dest`, checked, then the manifest. Exits on a file
    that does not match: a build with other weights must not finish."""
    for path, (size, sha256) in FILES.items():
        target = os.path.join(dest, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        for attempt in range(1, attempts + 1):
            try:
                got = _download(url(path, mirror), target + ".part")
                break
            except OSError as e:
                if attempt == attempts:
                    raise SystemExit(f"{path}: could not download from {mirror}: {e}")
                print(f"{path}: {e}, trying again", file=sys.stderr)
                time.sleep(5 * attempt)
        if got != (size, sha256):
            os.remove(target + ".part")
            raise SystemExit(f"{path}: expected {size} bytes with sha256 {sha256}, "
                             f"got {got[0]} bytes with sha256 {got[1]}")
        os.replace(target + ".part", target)
        print(f"{path}: {size} bytes, sha256 checked")
    with open(os.path.join(dest, "NOTICE"), "w") as f:
        f.write(NOTICE)
    with open(os.path.join(dest, MANIFEST), "w") as f:
        json.dump({"name": NAME, "repo": REPO, "revision": REVISION,
                   "files": {p: sha for p, (_n, sha) in FILES.items()}}, f, indent=2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"Download {NAME} ({REPO} at {REVISION[:7]}).")
    ap.add_argument("dest", help="the folder to write")
    ap.add_argument("--mirror", default=DEFAULT_MIRROR,
                    help="a host serving the Hub's layout (default: %(default)s)")
    args = ap.parse_args(argv)
    fetch(args.dest, args.mirror)
    return 0


if __name__ == "__main__":
    sys.exit(main())
