"""What a build sends to Docker, and what the image copies out of it.

There was no .dockerignore, so every `docker build` and every
`docker compose up --build` sent the whole directory: 160 MB, of which 127 MB
was dashboard/node_modules and the rest .git and scripts/node_modules. And
`COPY dashboard/ ./` came after `npm ci`, so the builder's own node_modules
landed on top of the packages just installed for the image's platform. A
customer building on a Mac put the Mac's packages in a Linux image.

These tests read .dockerignore with Docker's rules and the Dockerfiles as text,
so they cost no image build (the machine this runs on has a few GB of disk and
a product image build uses most of them).

Docker's rules, from moby/patternmatcher, which is what the builder runs
(https://docs.docker.com/build/concepts/context/#dockerignore-files): a pattern
is anchored at the root of the context; `*` and `?` stop at `/`; `**` crosses
any number of directories; a pattern matches a path when it matches the path or
one of its parent directories; `!` lets a path back in; the last matching line
wins. Not git's rules: in a .dockerignore `*.pyc` means the root only.
"""
import os
import posixpath
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = ("Dockerfile", "dev/Dockerfile.dev")

# Files of the dashboard that the build does not read. Anything else in
# dashboard/ has to be copied by the Dockerfile, or the image is built without
# it and nobody is told: a missing postcss.config.js is not an error, it is a
# page with no styles.
NOT_READ_BY_THE_BUILD = {"README.md", ".env.example", ".gitignore",
                         # `npm test` (check_the_reads.mjs), run beside the build.
                         "scripts"}


def _regex(pattern: str) -> "re.Pattern[str]":
    """moby/patternmatcher's translation of one pattern into a regular
    expression, for the syntax this repository uses."""
    out, i = "^", 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i + 1:i + 2] == "*":
                i += 1
                if pattern[i + 1:i + 2] == "/":      # `**/` is read as `**`
                    i += 1
                # `**` at the end takes everything, elsewhere any number of
                # directories, none included.
                out += ".*" if i + 1 >= len(pattern) else "(.*/)?"
            else:
                out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        elif ch == "\\" and i + 1 < len(pattern):
            i += 1
            out += re.escape(pattern[i])
        else:
            out += re.escape(ch)
        i += 1
    return re.compile(out + "$")


def _patterns():
    rules = []
    for line in (ROOT / ".dockerignore").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        back_in = line.startswith("!")
        p = posixpath.normpath(line[1:].strip() if back_in else line).lstrip("/")
        rules.append((back_in, p, _regex(p)))
    return rules


def _left_out(path: str, rules) -> bool:
    """Whether Docker leaves `path` (relative, with /) out of the context."""
    parents = path.split("/")[:-1]
    prefixes = ["/".join(parents[:i + 1]) for i in range(len(parents))]
    left_out = False
    for back_in, _p, rx in rules:
        # Only the lines that could change the answer so far are read, as in
        # moby: exclusions while the path is in, `!` lines while it is out.
        if back_in != left_out:
            continue
        if rx.match(path) or any(rx.match(p) for p in prefixes):
            left_out = not back_in
    return left_out


def _could_come_back_under(directory: str, rules) -> bool:
    """Whether a `!` line could let something under `directory` back in, in
    which case the walk has to go inside rather than skip it."""
    for back_in, p, _rx in rules:
        if back_in and (any(c in p for c in "*?[") or p.startswith(directory + "/")):
            return True
    return False


def _context(rules):
    """{relative path: size} of every file the builder would receive."""
    sent = {}
    for here, dirs, files in os.walk(ROOT):
        rel = Path(here).relative_to(ROOT).as_posix()
        rel = "" if rel == "." else rel + "/"
        dirs[:] = [d for d in dirs
                   if not (_left_out(rel + d, rules)
                           and not _could_come_back_under(rel + d, rules))]
        for f in files:
            if not _left_out(rel + f, rules):
                sent[rel + f] = os.lstat(os.path.join(here, f)).st_size
    return sent


def _instructions(dockerfile: str):
    """(stage, instruction, arguments) for each instruction, continuation lines
    joined, comments dropped."""
    text = (ROOT / dockerfile).read_text()
    logical, buf = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        logical.append(buf + line)
        buf = ""
    stage, out = None, []
    for line in logical:
        word, _, rest = line.partition(" ")
        word = word.upper()
        if word == "FROM":
            parts = rest.split()
            stage = parts[-1] if len(parts) >= 3 and parts[-2].upper() == "AS" else parts[0]
        out.append((stage, word, rest))
    return out


def _copy_sources(dockerfile: str):
    """(stage, [sources]) of every COPY that reads the build context."""
    found = []
    for stage, word, rest in _instructions(dockerfile):
        if word not in ("COPY", "ADD"):
            continue
        args = shlex.split(rest)
        if any(a.startswith("--from") for a in args):
            continue
        args = [a for a in args if not a.startswith("--")]
        found.append((stage, [posixpath.normpath(s.rstrip("/")) for s in args[:-1]]))
    return found


@pytest.fixture(scope="module")
def rules():
    assert (ROOT / ".dockerignore").is_file(), (
        "no .dockerignore: every build sends the whole directory, node_modules, "
        ".git, .env and backups/ included")
    return _patterns()


@pytest.fixture(scope="module")
def context(rules):
    return _context(rules)


class TestWhatIsNeverSent:
    @pytest.mark.parametrize("path", [
        "dashboard/node_modules/vite/package.json",
        "dashboard/node_modules/.bin/vite",
        "scripts/node_modules/playwright/index.js",
        "node_modules/anything.js",
        ".git/HEAD",
        ".git/objects/ab/cdef",
        ".env",
        "dashboard/.env",
        "backups/korely-20260928-0400.dump",
        "dashboard/dist/index.html",
        "korely_agent/__pycache__/main.cpython-311.pyc",
        "korely_agent/api/__pycache__/admin.cpython-311.pyc",
        ".pytest_cache/v/cache/nodeids",
        "dashboard/.pytest_cache/v/cache/nodeids",
        "korely_agent/.DS_Store",
        ".venv/bin/python",
        # The developer's tools: see TestTheProductImageCarriesNoScripts.
        "scripts/check_migration.py",
        "scripts/release.sh",
        "scripts/bench_locomo.py",
    ])
    def test_left_out(self, rules, path):
        assert _left_out(path, rules), f"{path} is sent to the builder"

    def test_nothing_on_this_disk_gets_in_that_should_not(self, context):
        """The rules above, checked against the files actually here. On the
        machine that found this, dashboard/node_modules and scripts/node_modules
        are both on disk."""
        bad = [p for p in context
               if {"node_modules", ".git", "__pycache__"} & set(p.split("/"))
               or p.startswith(("dashboard/dist/", "backups/"))
               or p.split("/")[-1] in (".env", ".env.local")]
        assert bad == [], f"sent to the builder: {bad[:10]}"

    def test_the_context_is_small(self, context):
        """160 MB before. What the images are made of is a few MB; a context
        this ceiling cannot hold means something like node_modules is back."""
        total = sum(context.values())
        assert total < 10 * 1024 * 1024, (
            f"the build context is {total / 1024 / 1024:.1f} MB; the biggest: "
            f"{sorted(context, key=context.get, reverse=True)[:5]}")


class TestWhatTheDockerfilesCopyIsSent:
    @pytest.mark.parametrize("dockerfile", DOCKERFILES)
    def test_every_copy_source_is_in_the_context(self, rules, context, dockerfile):
        """An allowlist fails the other way: a file added to a COPY and not to
        .dockerignore is a build that stops with "not found"."""
        missing = []
        for _stage, sources in _copy_sources(dockerfile):
            for src in sources:
                assert (ROOT / src).exists(), f"{dockerfile} copies {src}, which does not exist"
                if (ROOT / src).is_dir():
                    if not any(p.startswith(src + "/") for p in context):
                        missing.append(src)
                elif src not in context:
                    missing.append(src)
        assert missing == [], f"{dockerfile} copies what .dockerignore leaves out: {missing}"


class TestTheProductImageCarriesNoScripts:
    """scripts/ is the developer's: tests of a release (acceptance*.sh,
    release.sh, check_*), measurements against a model (bench_*),
    the lock tool. Every one runs from a checkout, on the
    host or in the test image with the repository mounted, and none is run
    inside the product container. What an operator runs there is
    `python -m korely_agent.manage`, which is in the package; backups are
    deploy/, on the host.

    The image carried the whole directory anyway, and one of them,
    check_migration.py, begins with DROP SCHEMA public CASCADE on whatever
    DATABASE_URL says. Inside the product container that is the customer's
    database: `docker compose exec api python scripts/check_migration.py`
    would have emptied it."""

    def test_no_copy_reads_scripts(self):
        copied = [(stage, s) for dockerfile in DOCKERFILES
                  for stage, srcs in _copy_sources(dockerfile) for s in srcs
                  if s == "scripts" or s.startswith("scripts/")]
        assert copied == [], f"an image copies developer scripts: {copied}"

    def test_none_of_them_is_even_sent(self, context):
        sent = sorted(p for p in context if p.startswith("scripts/"))
        assert sent == [], f"sent to the builder: {sent[:5]}"


class TestTheDashboardStage:
    def test_built_once_on_the_builders_platform(self):
        """Static files and model weights are the same for every target. Built
        per target, the arm64 `npm ci` ran under QEMU and hung for an hour in
        the 0.1.20 image workflow (7 Oct 2026)."""
        froms = {}
        for line in (ROOT / "Dockerfile").read_text().splitlines():
            parts = line.split()
            if parts[:1] == ["FROM"] and "AS" in parts:
                froms[parts[parts.index("AS") + 1]] = parts
        for stage in ("dashboard", "model"):
            assert "--platform=$BUILDPLATFORM" in froms[stage], (
                f"the {stage} stage is built for each target platform")
        assert not any(p.startswith("--platform") for p in froms["base"]), (
            "the runtime stage must be built for each target")

    def test_it_never_copies_the_whole_directory(self):
        """The whole directory is where the builder's own node_modules lives."""
        for stage, sources in _copy_sources("Dockerfile"):
            if stage == "dashboard":
                assert "dashboard" not in sources, (
                    "COPY dashboard/ brings the builder's node_modules over the "
                    "ones `npm ci` installed in the image")

    def test_the_sources_come_after_the_install(self):
        """Manifest and lock, then `npm ci`, then the sources: the install is
        cached until the lock changes, and nothing copied afterwards is a
        package."""
        seen_install = False
        order = []
        for stage, word, rest in _instructions("Dockerfile"):
            if stage != "dashboard":
                continue
            if word == "RUN" and "npm ci" in rest:
                seen_install = True
            if word == "COPY":
                sources = [s for s in shlex.split(rest)[:-1] if not s.startswith("--")]
                order.append((seen_install, sources))
        before = [s for done, srcs in order if not done for s in srcs]
        assert sorted(before) == ["dashboard/package-lock.json", "dashboard/package.json"]
        assert any(done for done, _ in order), "no sources copied after the install"

    def test_every_file_the_build_reads_is_copied(self, rules):
        """Named one by one, a new config file would be silently left out.
        This makes that a red test instead of an unstyled page."""
        copied = {s for stage, srcs in _copy_sources("Dockerfile")
                  if stage == "dashboard" for s in srcs}
        on_disk = {e.name for e in (ROOT / "dashboard").iterdir()
                   if not _left_out(f"dashboard/{e.name}", rules)}
        left = sorted(n for n in on_disk
                      if f"dashboard/{n}" not in copied and n not in NOT_READ_BY_THE_BUILD)
        assert left == [], (
            f"in dashboard/ and not copied by the Dockerfile: {left}. Copy them, "
            f"or add them to NOT_READ_BY_THE_BUILD if the build does not read them.")


def test_the_licence_and_its_notices_go_into_the_product_image():
    """The Apache License 2.0 asks that its text and the NOTICE go with every
    copy distributed (section 4), and the image is one: 0.1.39 shipped
    without LICENSE and NOTICE (17 Oct 2026)."""
    text = (ROOT / "Dockerfile").read_text()
    final = text.rsplit("\nFROM ", 1)[1]
    copies = [shlex.split(line)[1:-1] for line in final.splitlines() if line.startswith("COPY ")]
    assert any("LICENSE" in c and "NOTICE" in c for c in copies)
    assert (ROOT / "LICENSE").is_file() and (ROOT / "NOTICE").is_file()


def test_the_code_belongs_to_root_and_the_service_runs_as_another_user():
    """The security review of 3 Nov 2026: `chown -R korely:korely /app`
    handed the application's files to the user the service runs as, so a
    process of the service taken over by a crafted input could have
    rewritten the code it ran in. Now every COPY is root's (no --chown, no
    chown after), and the service still runs as its own user."""
    text = (ROOT / "Dockerfile").read_text()
    final = text.rsplit("\nFROM ", 1)[1]
    instructions = [line for line in final.splitlines() if line and not line.startswith("#")]
    assert not any("--chown" in line for line in instructions if line.startswith("COPY"))
    assert not any(re.search(r"\bchown\b", line) for line in instructions)
    assert "USER korely" in instructions
    assert any(line.startswith("RUN useradd --uid 1000") for line in instructions)
    assert "PYTHONDONTWRITEBYTECODE=1" in final, "Python runs from a tree it cannot write"
