"""The dependency locks: how they are made, and the guard that keeps them true.

Two layers, each a hand-edited requirements file and a GENERATED lock:

    requirements.txt      -> requirements.lock.txt      product image (Dockerfile)
    requirements-dev.txt  -> requirements-dev.lock.txt  test image (Dockerfile.dev)

The dev layer includes the product's requirements (`-r`) and is constrained by
the product lock (`-c`), so a package the tests share with the product is
always at the product's version and hashes: the suite runs on what ships.

The requirements files name what the code imports directly, with ranges. The
locks are written by pip-tools (`pip-compile`) and pin EVERY package,
transitive ones included, with `==` and the sha256 of every file PyPI
publishes for that version. Both images install only a lock, with
`pip install --require-hashes`.

Commands, from the repository root:

    make lock                          (python scripts/requirements_lock.py lock)
        Regenerate both locks, product first, then run `check`. Existing pins
        are kept, as pip-tools does by default: `make lock ARGS="-P fastapi"`
        moves one package, `make lock ARGS=--upgrade` moves all. Runs in the
        images' base (python:3.11-slim, Linux), because a lock resolved on
        another platform or Python can pin something else.

    python scripts/requirements_lock.py check [--installed LAYER [--freeze FILE]]
        Fail if a lock no longer satisfies its requirements file (a package
        added, or a range changed, without regenerating), if the dev layer is
        not constrained by the product lock or disagrees with it, or if an
        entry is not pinned and hashed. With --installed, also fail if this
        environment is not exactly that layer's lock (pip freeze), if `pip
        check` finds a broken requirement, or if the lock holds a package the
        requirements no longer need, or misses one they do (walking the
        installed packages' own metadata). With --freeze FILE ("-" for stdin),
        compare a `pip freeze --all` taken elsewhere instead, which is how the
        product image is checked from outside (`make image-check`): it carries
        no copy of this script's dependencies of its own.

Disk, measured 2026-09-28: pip-tools 7.6.1 hashes a pin by DOWNLOADING every
file PyPI has for it (all platforms: 1241 files, 1.3 GB for the product set).
Its shortcut through PyPI's JSON digests never matches (found the day
before). `--no-cache-dir` below makes the download a stream: every file is
hashed as it arrives and nothing is written to disk. Pins already in a lock
reuse the hashes written there, so an ordinary change downloads only what
moved; `--upgrade` downloads everything again.

What this relies on (read 2026-09-27):
- pip, "Repeatable installs": pin every package, transitive ones included,
  with ==; hash-checking also guards against a package changing without its
  version changing; pip-tools is the tool it points to for generating them.
  https://pip.pypa.io/en/stable/topics/repeatable-installs/
- pip, "Secure installs": a --hash on any requirement turns hash-checking on
  for all; every requirement AND every dependency must be pinned (==) and
  hashed; several hashes per package are allowed "when a package offers
  binary distributions for a variety of platforms" (here linux/amd64, which
  most servers are, and linux/arm64, which `make release` also builds);
  --require-hashes forces the mode "in deploy scripts".
  https://pip.pypa.io/en/stable/topics/secure-installs/
- pip-tools: an existing output file's pins are kept unless --upgrade or
  -P; --generate-hashes; layered requirements with `-c`; the result can
  differ per environment, so compile on the target one.
  https://pip-tools.readthedocs.io/en/stable/
"""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]

# The pip-tools the locks are written with. Moving it can change the files'
# layout, never the versions they pin (those move only with -P / --upgrade).
PIP_TOOLS_VERSION = "7.6.1"

# The product layer comes first: the dev layer constrains itself to its lock.
PRODUCT = "product"
LAYERS = (PRODUCT, "dev")
_FILES = {
    PRODUCT: ("requirements/requirements.txt", "requirements/requirements.lock.txt"),
    "dev": ("requirements/requirements-dev.txt", "requirements/requirements-dev.lock.txt"),
}

COMPILE_OPTIONS = (
    # Hash-checking mode. pip-tools writes the hash of every file PyPI has for
    # the pinned version, so one lock verifies the amd64 and the arm64 wheels.
    "--generate-hashes",
    # The product lock is the dev layer's constraints file, and pip refuses
    # extras in a constraints file; the extras' packages are listed anyway.
    "--strip-extras",
    # Pin setuptools / pip as well if a dependency ever asks for them
    # (pip-tools' announced future default); nothing does today.
    "--allow-unsafe",
    # Stream the files being hashed instead of caching them (see the top).
    "--pip-args=--no-cache-dir",
)

# What the base image (python:3.11-slim) ships before anything is installed.
# `pip freeze --all` lists them; a lock does not, unless a dependency asks for
# one of them, and then the lock's version is the one installed.
INSTALLER_TOOLS = frozenset({"pip", "setuptools", "wheel", "packaging"})

# How to fix what `check` finds, said once.
REGENERATE = "regenerate the locks with `make lock`"
REBUILD = "rebuild the image from the locks (`make image`, or `docker compose build` for the product)"


def requirements_file(layer: str, root: Path = ROOT) -> Path:
    return root / _FILES[layer][0]


def lock_file(layer: str, root: Path = ROOT) -> Path:
    return root / _FILES[layer][1]


# --- reading requirements files (pip's own rules for lines and comments) ---

_COMMENT = re.compile(r"(^|\s+)#.*$")
_INCLUDE = re.compile(r"^(-r|--requirement|-c|--constraint)(?:=|\s+)?(\S+)$")


@dataclass(frozen=True)
class Entry:
    requirement: Requirement
    hashes: frozenset[str]
    source: Path


@dataclass(frozen=True)
class Pin:
    version: str
    hashes: frozenset[str]


def _logical_lines(path: Path):
    """Lines as pip sees them: `\\` continuations joined, comments removed."""
    pending: list[str] = []
    for raw in path.read_text().splitlines():
        if raw.endswith("\\") and not _COMMENT.match(raw):
            pending.append(raw[:-1])
            continue
        pending.append(raw)
        line = _COMMENT.sub("", " ".join(pending)).strip()
        pending = []
        if line:
            yield line
    if pending:
        raise ValueError(f"{path.name} ends inside a line continuation")


def read_requirements(path: Path) -> tuple[list[Entry], list[Path]]:
    """The requirements of a file, following `-r`, and the `-c` files it names.

    Anything else that is an option (an index URL, find-links, editable...)
    stops the guard: it would change what pip installs, and the guard does
    not pretend to know how.
    """
    entries: list[Entry] = []
    constraints: list[Path] = []
    for line in _logical_lines(path):
        include = _INCLUDE.match(line)
        if include:
            flag, target = include.groups()
            nested = path.parent / target
            if flag in ("-r", "--requirement"):
                more, more_constraints = read_requirements(nested)
                entries += more
                constraints += more_constraints
            else:
                constraints.append(nested)
            continue
        tokens = line.split()
        cut = next((i for i, t in enumerate(tokens) if t.startswith("-")), len(tokens))
        if cut == 0:
            raise ValueError(f"{path.name}: unsupported line {line!r}")
        hashes: set[str] = set()
        options = iter(tokens[cut:])
        for option in options:
            if option.startswith("--hash="):
                hashes.add(option.removeprefix("--hash="))
            elif option == "--hash":
                hashes.add(next(options))
            else:
                raise ValueError(f"{path.name}: unsupported option {option!r} in {line!r}")
        entries.append(Entry(Requirement(" ".join(tokens[:cut])), frozenset(hashes), path))
    return entries, constraints


def _applies_here(requirement: Requirement) -> bool:
    return requirement.marker is None or requirement.marker.evaluate({"extra": ""})


def _exact_version(requirement: Requirement) -> str | None:
    specs = list(requirement.specifier)
    if len(specs) == 1 and specs[0].operator in ("==", "===") and "*" not in specs[0].version:
        return specs[0].version
    return None


def read_pins(path: Path) -> tuple[dict[str, Pin], list[str]]:
    """A lock as {canonical name: Pin}, and what makes it not a lock."""
    entries, nested = read_requirements(path)
    problems = [f"{path.name} names another file ({c.name}); a lock is flat" for c in nested]
    pins: dict[str, Pin] = {}
    for entry in entries:
        req = entry.requirement
        name = canonicalize_name(req.name)
        version = _exact_version(req)
        if version is None or req.url:
            problems.append(f"{path.name}: {req} is not pinned with ==")
            continue
        if req.extras:
            problems.append(f"{path.name}: {req} carries extras; the lock must list their packages instead")
        if entry.source != path:
            problems.append(f"{path.name}: {req} comes from {entry.source.name}; a lock is flat")
        if not entry.hashes:
            problems.append(f"{path.name}: {name}=={version} has no --hash, so pip refuses the whole file")
        if name in pins:
            problems.append(f"{path.name}: {name} is pinned twice")
        pins[name] = Pin(version, entry.hashes)
    return pins, problems


# --- the checks ---

def _missing(*paths: Path) -> list[str]:
    return [f"{p.name} not found in {p.parent}: {REGENERATE}" for p in paths if not p.exists()]


def lock_problems(layer: str, root: Path = ROOT) -> list[str]:
    """Where a lock no longer matches its requirements file (no environment needed)."""
    wanted_path, lock_path = requirements_file(layer, root), lock_file(layer, root)
    missing = _missing(wanted_path, lock_path)
    if missing:
        return missing
    pins, problems = read_pins(lock_path)
    wanted, constraint_paths = read_requirements(wanted_path)
    for entry in wanted:
        req = entry.requirement
        if not _applies_here(req):
            continue
        pin = pins.get(canonicalize_name(req.name))
        if pin is None:
            problems.append(f"{entry.source.name} requires {req}, missing from {lock_path.name}: {REGENERATE}")
        elif not req.specifier.contains(pin.version, prereleases=True):
            problems.append(
                f"{entry.source.name} requires {req}, {lock_path.name} pins {pin.version}: {REGENERATE}"
            )
    held_by = list(dict.fromkeys(p.resolve() for p in constraint_paths))
    if layer != PRODUCT:
        product_lock = lock_file(PRODUCT, root).resolve()
        if product_lock not in held_by:
            problems.append(
                f"{wanted_path.name} must constrain itself to the product lock "
                f"(-c {product_lock.name}), so what it shares with the product stays at the product's version"
            )
            held_by.append(product_lock)
    for constraint_path in held_by:
        if not constraint_path.exists():
            problems.append(f"{wanted_path.name} constrains itself to {constraint_path.name}, not found")
            continue
        held, _ = read_pins(constraint_path)
        for name, pin in sorted(pins.items()):
            other = held.get(name)
            if other is not None and other != pin:
                problems.append(
                    f"{lock_path.name} has {name}=={pin.version}, {constraint_path.name} has "
                    f"{name}=={other.version} (or other hashes): {REGENERATE}"
                )
    return problems


def parse_freeze(text: str) -> dict[str, str]:
    """`pip freeze --all` output as {canonical name: version or raw line}."""
    installed: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, version = line.partition("==")
        if sep:
            installed[canonicalize_name(name)] = version
        elif " @ " in line:
            installed[canonicalize_name(line.split(" @ ")[0])] = line
        else:
            installed[line] = line
    return installed


def _installed() -> dict[str, str]:
    """This environment, as `pip freeze --all` reports it."""
    out = subprocess.run(
        [sys.executable, "-m", "pip", "freeze", "--all"], capture_output=True, text=True, check=True
    ).stdout
    return parse_freeze(out)


def installed_versus_lock(layer: str, root: Path = ROOT, installed: dict[str, str] | None = None) -> list[str]:
    """Where an environment is not exactly the lock: hand installs, leftovers, stale image.

    `installed` is this environment unless a freeze taken elsewhere is given.
    """
    lock_path = lock_file(layer, root)
    if not lock_path.exists():
        return _missing(lock_path)
    pins, _ = read_pins(lock_path)
    if installed is None:
        installed = _installed()
    problems = []
    for name in sorted(set(pins) | set(installed)):
        pin, have = pins.get(name), installed.get(name)
        if pin is None:
            if name not in INSTALLER_TOOLS:
                problems.append(f"{name} {have} is installed and not in {lock_path.name}: installed by hand or left over; {REBUILD}")
        elif have is None:
            problems.append(f"{name}=={pin.version} is in {lock_path.name} and not installed: {REBUILD}")
        elif not _same_version(have, pin.version):
            problems.append(f"{name} {have} is installed, {lock_path.name} pins {pin.version}: {REBUILD}")
    return problems


def _same_version(a: str, b: str) -> bool:
    try:
        return Version(a) == Version(b)
    except ValueError:
        return a == b


def pip_check() -> list[str]:
    """`pip check`: every installed package's requirements are installed and compatible."""
    result = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    if result.returncode == 0:
        return []
    return [f"pip check: {line}" for line in (result.stdout + result.stderr).splitlines() if line.strip()]


def needed(requirements: list[Requirement]) -> set[str]:
    """Every distribution the requirements pull in, from the installed packages' metadata.

    Walks Requires-Dist with the requested extras, markers evaluated for this
    interpreter and platform (the test image is arm64 on a Mac and amd64 on
    most servers; every marker in the tree today gives the same answer on both).
    """
    visited: dict[str, set[str]] = {}
    todo = [
        (canonicalize_name(r.name), {canonicalize_name(x) for x in r.extras})
        for r in requirements
        if _applies_here(r)
    ]
    while todo:
        name, extras = todo.pop()
        seen = visited.setdefault(name, set())
        fresh = ({""} | extras) - seen
        if not fresh:
            continue
        seen |= fresh
        try:
            requires = importlib.metadata.distribution(name).requires or []
        except importlib.metadata.PackageNotFoundError:
            continue  # not installed: installed_versus_lock names it
        for line in requires:
            dep = Requirement(line)
            if dep.marker is None or any(dep.marker.evaluate({"extra": x}) for x in fresh):
                todo.append((canonicalize_name(dep.name), {canonicalize_name(x) for x in dep.extras}))
    return set(visited)


def lock_versus_needs(layer: str, root: Path = ROOT) -> list[str]:
    """The lock holds exactly what the requirements need: nothing orphaned, nothing missing."""
    lock_path, wanted_path = lock_file(layer, root), requirements_file(layer, root)
    missing = _missing(wanted_path, lock_path)
    if missing:
        return missing
    pins, _ = read_pins(lock_path)
    wanted, _ = read_requirements(wanted_path)
    need = needed([e.requirement for e in wanted])
    problems = [
        f"{name} is in {lock_path.name} and nothing in {wanted_path.name} needs it any more: {REGENERATE}"
        for name in sorted(set(pins) - need)
    ]
    problems += [
        f"{name} is needed by {wanted_path.name} and missing from {lock_path.name}: {REGENERATE}"
        for name in sorted(need - set(pins))
    ]
    return problems


def environment_problems(layer: str, root: Path = ROOT) -> list[str]:
    mismatched = installed_versus_lock(layer, root)
    if mismatched:
        # pip check and the walk over metadata describe the packages that are
        # installed; until those are the lock, they would blame the lock for
        # what a rebuild fixes.
        return mismatched
    return pip_check() + lock_versus_needs(layer, root)


def check(installed: str | None, freeze: str | None = None, root: Path = ROOT) -> list[str]:
    problems = [p for layer in LAYERS for p in lock_problems(layer, root)]
    if installed and freeze is not None:
        # Another environment: only what a freeze can say. The image's own
        # `pip check` is run next to it by `make image-check`.
        problems += installed_versus_lock(installed, root, parse_freeze(freeze))
    elif installed:
        problems += environment_problems(installed, root)
    return problems


# --- regenerating ---

def lock(pip_compile_args: list[str], root: Path = ROOT) -> None:
    """pip-compile both layers with a pinned pip-tools in a throwaway venv."""
    with tempfile.TemporaryDirectory() as scratch:
        venv = Path(scratch, "venv")
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        subprocess.run(
            [venv / "bin" / "pip", "install", "--quiet", "--no-cache-dir", f"pip-tools=={PIP_TOOLS_VERSION}"],
            check=True,
        )
        # The lock's header records how to make it again.
        env = {**os.environ, "CUSTOM_COMPILE_COMMAND": "make lock  (see requirements.txt)"}
        for layer in LAYERS:
            subprocess.run(
                [
                    venv / "bin" / "pip-compile", "--quiet", *COMPILE_OPTIONS,
                    "--cache-dir", Path(scratch, "cache"),
                    "--output-file", lock_file(layer, root).name,
                    requirements_file(layer, root).name,
                    *pip_compile_args,
                ],
                # In the files' folder: `-r requirements.txt` and the locks'
                # `via` lines are relative to it.
                cwd=requirements_file(layer, root).parent, env=env, check=True,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make or check the dependency locks.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("lock", help="regenerate both locks; extra arguments go to pip-compile")
    check_parser = commands.add_parser("check", help="fail if a lock or an environment has drifted")
    check_parser.add_argument("--installed", choices=LAYERS, help="also compare an environment with that layer's lock")
    check_parser.add_argument("--freeze", help="`pip freeze --all` of that environment ('-' for stdin); default: this one")
    args, rest = parser.parse_known_args(argv)
    freeze = None
    if args.command == "lock":
        lock(rest)
        installed = None
    else:
        if rest:
            parser.error(f"unrecognized arguments: {' '.join(rest)}")
        installed = args.installed
        if args.freeze is not None:
            if not installed:
                parser.error("--freeze needs --installed LAYER")
            freeze = sys.stdin.read() if args.freeze == "-" else Path(args.freeze).read_text()
    problems = check(installed, freeze)
    for problem in problems:
        print(f"LOCK DRIFT: {problem}", file=sys.stderr)
    if problems:
        return 1
    counts = ", ".join(f"{lock_file(layer).name} {len(read_pins(lock_file(layer))[0])} packages" for layer in LAYERS)
    where = "that environment" if freeze is not None else "this environment"
    print(f"locks ok ({counts})" + (f"; {where} is exactly the {installed} lock" if installed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
