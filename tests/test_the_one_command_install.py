"""install.sh, the README's one command (29 Oct 2026), and the ./korely it
leaves in the folder.

Played under the image's /bin/sh (dash, the strictest POSIX shell at hand),
with a docker, a curl and a git that answer from this checkout and record what
they were asked, in temporary directories. The script played is a copy
stamped as scripts/stamp_installer.py stamps it at a release (THIS_RELEASE and
the sha256 of the files it downloads), unless a test says otherwise. What
manage prints is the real thing: the account's answers come from
`manage.main(... --json)` on the test database, so a change of that shape
fails here and not on a stranger's laptop. The runs against a real Docker are
in the commits that made them (a scratch folder, `sh install.sh --build`)."""
import importlib.util
import json
import os
import pty
import re
import select
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
REL = re.search(r'^THIS_RELEASE="([^"]+)"$', INSTALL.read_text(), re.M).group(1)
LEAST = re.search(r'^MIN_RELEASE="([^"]+)"$', INSTALL.read_text(), re.M).group(1)
# The public repository (renamed on 5 Nov 2026, CHANGELOG 1.0.9): its git
# address, its release files and its image.
PUBLIC = "korely-ai/korely"
KEY = "kor_self_" + "k" * 43
PASSWORD = "Pw" * 12
TENANT = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
PROVIDER_KEY = "scw-fake-0000-1111-2222-k3y9"
GEMINI_KEY = "AIzaFakeFakeFakeFakeFakeFakeFakeFak9g00"  # made up, gitleaks:allow
WRITER_LINES = ("EXTRACTION_PROVIDER", "EXTRACTION_MODEL", "MISTRAL_API_BASE",
                "MISTRAL_API_KEY", "MISTRAL_EXTRACTION_MODEL")
RELEASE_FILES = ("docker-compose.yml", "docker-compose.image.yml", ".env.example",
                 "deploy/Caddyfile")

_spec = importlib.util.spec_from_file_location("stamp_installer",
                                               ROOT / "scripts" / "stamp_installer.py")
stamp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stamp)

DOCKER = r"""#!/bin/sh
echo "$*" >> "$CALLS"
case "$1" in
  info) [ "${2:-}" = "--format" ] && echo "${FAKE_MEM:-8000000000}"; exit 0 ;;
  version) echo 29.0.0; exit 0 ;;
  ps|volume) printf '%s' "${FAKE_TAKEN:-}"; exit 0 ;;
  image) [ "$2" = rm ] && exit "${FAKE_IMAGE_RM:-0}"; exit "${FAKE_IMAGE_INSPECT:-1}" ;;
  pull) exit "${FAKE_PULL:-0}" ;;
  compose)
    shift
    if [ "$*" = "version --short" ]; then echo "${FAKE_COMPOSE:-2.29.1}"; exit 0; fi
    echo "PORT=${PORT-unset} PASSWORD=${POSTGRES_PASSWORD-unset} PROJECT=${COMPOSE_PROJECT_NAME-unset} FILE=${COMPOSE_FILE-unset} IMAGE=${KORELY_IMAGE-unset}" >> "$CALLS.env"
    case "$*" in
      *" up "*) echo "#12 [base 2/15] RUN apt-get update (what a build prints on stdout)" ;;
      *"manage list-operators --json"*) cat "$FAKE/list-operators" ;;
      *"manage list-tenants --json"*) cat "$FAKE/list-tenants" ;;
      *"manage create-tenant"*) cat "$FAKE/create-tenant" ;;
      *"manage issue-key"*) cat "$FAKE/issue-key" ;;
      *"manage create-operator"*) [ -n "${FAKE_OPERATOR_FAILS:-}" ] && exit 1
                                  cat "$FAKE/create-operator" ;;
    esac
    exit 0 ;;
esac
exit 0
"""

# The three things install.sh asks curl: a release file, the port before the
# first start, /health.
CURL = r"""#!/bin/sh
echo "curl $*" >> "$CALLS"
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift 2 ;; -w|--proto|--proto-redir|--max-time|--retry) shift 2 ;; *) url=$1; shift ;; esac
done
case "$url" in
  */health) [ -n "${FAKE_DOWN:-}" ] && exit 7
            printf '{"version":"%s","status":"ok","extraction":{"state":"%s"},"mcp_path":"/mcp"}' \
              "$FAKE_RUNNING" "${FAKE_STATE:-not configured}"; exit 0 ;;
  http://127.0.0.1:*) exit "${FAKE_PORT:-7}" ;;
  https://raw.githubusercontent.com/@PUBLIC@/v*)
    path=${url#https://raw.githubusercontent.com/@PUBLIC@/v*/}
    [ -f "$SRC/$path" ] || exit 22
    cp "$SRC/$path" "$out"; printf '%s' "${FAKE_EFFECTIVE:-$url}"; exit 0 ;;
esac
exit 1
""".replace("@PUBLIC@", PUBLIC)

GIT = r"""#!/bin/sh
echo "git $*" >> "$CALLS"
d=.
[ "$1" = "-C" ] && { d=$2; shift 2; }
case "$1" in
  init) mkdir -p "$d/.git" ;;
  -c) mkdir -p "$d/deploy"
      for f in docker-compose.yml docker-compose.image.yml .env.example deploy/Caddyfile; do cp "$SRC/$f" "$d/$f"; done
      echo "${FAKE_TAG_VERSION:-$FAKE_RUNNING}" > "$d/VERSION" ;;
esac
exit 0
"""


def _fake_bin(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for name, text in (("docker", DOCKER), ("curl", CURL), ("git", GIT)):
        (bin_ / name).write_text(text)
        (bin_ / name).chmod(0o755)
    return bin_


def _unstamped_text():
    """install.sh as main carries it between two releases' stamps: no sums,
    no digest. Built here, not read from the checkout: after a release the
    checkout's copy keeps that release's sums until the next one (release.sh
    stamps, the -dev bump leaves them), and these tests then read a stamped
    file (found by release.sh 1.0.1, 30 Oct 2026)."""
    text = re.sub(r'^RELEASE_SHA256="[^"]*"$', 'RELEASE_SHA256="\n"', INSTALL.read_text(),
                  count=1, flags=re.M | re.S)
    return re.sub(r'^THIS_IMAGE_DIGEST="[^"]*"$', 'THIS_IMAGE_DIGEST=""', text, count=1, flags=re.M)


def _unstamped(tmp_path):
    copy = tmp_path / "install.sh"
    copy.write_text(_unstamped_text())
    return copy


def _stamped(tmp_path, version=REL, root=ROOT):
    """install.sh as a release carries it."""
    copy = tmp_path / "install.sh"
    copy.write_text(stamp.stamp_release(INSTALL.read_text(), version, root))
    return copy


def _answers(tmp_path, operators="[]", tenants="[]"):
    fake = tmp_path / "answers"
    fake.mkdir(exist_ok=True)
    (fake / "list-operators").write_text(operators + "\n")
    (fake / "list-tenants").write_text(tenants + "\n")
    (fake / "create-tenant").write_text(json.dumps(
        {"tenant": TENANT, "name": "My account", "key": KEY, "expires": "2027-01-27"}) + "\n")
    (fake / "issue-key").write_text(json.dumps(
        {"tenant": TENANT, "name": "install.sh", "key": KEY, "expires": "2027-01-27",
         "admin": False}) + "\n")
    (fake / "create-operator").write_text(json.dumps(
        {"username": "admin", "password": PASSWORD, "tenant": TENANT,
         "install_admin": True}) + "\n")
    return fake


def _env(tmp_path, release=REL, **env):
    bin_ = _fake_bin(tmp_path)
    fake = tmp_path / "answers"
    if not fake.exists():
        _answers(tmp_path)
    return {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path),
            "CALLS": str(tmp_path / "calls"), "FAKE": str(fake), "SRC": str(ROOT),
            "FAKE_RUNNING": release, "KORELY_DIR": str(tmp_path / "korely"), **env}


def _run(tmp_path, *args, release=REL, installer=None, **env):
    script = installer or (tmp_path / "install.sh")
    if installer is None and not script.exists():
        _stamped(tmp_path)
    return subprocess.run(["sh", str(script), *args], env=_env(tmp_path, release, **env),
                          capture_output=True, text=True, cwd=tmp_path, timeout=60,
                          stdin=subprocess.DEVNULL, start_new_session=True)


def _calls(tmp_path):
    p = tmp_path / "calls"
    return p.read_text() if p.exists() else ""


def _env_file(tmp_path, folder="korely"):
    text = (tmp_path / folder / ".env").read_text()
    return dict(re.findall(r"^([A-Z_][A-Z0-9_]*)=(.*)$", text, re.M))


class TestAFirstInstall:
    def test_it_prints_everything_once_and_only_at_the_end(self, tmp_path):
        r = _run(tmp_path)
        assert r.returncode == 0, r.stderr
        out = r.stdout
        assert out.lstrip().startswith(f"Korely Agents {REL} is running in")
        assert "Dashboard  http://127.0.0.1:8000" in out
        assert "Login      admin" in out
        assert out.count(PASSWORD) == 1 and out.count(KEY) == 1
        # The MCP line names a variable, not the key: a key typed on a
        # command line stays in the shell's history.
        assert "read -rs KORELY_API_KEY" in out
        assert ("claude mcp add --transport http korely http://127.0.0.1:8000/mcp "
                '--header "Authorization: Bearer $KORELY_API_KEY"') in out
        wrapper = f'"{tmp_path}/korely/korely"'
        assert f"Stop:       {wrapper} stop" in out
        assert f"Start:      {wrapper} start" in out
        assert f"Uninstall:  {wrapper} uninstall --yes" in out
        assert "docker compose" not in out
        # The progress is on stderr, Compose's and the build's included, and
        # never holds a secret; stdout is the summary alone.
        assert KEY not in r.stderr and PASSWORD not in r.stderr
        assert "apt-get" in r.stderr and "apt-get" not in out
        assert f"Up: version {REL}." in r.stderr
        assert not (tmp_path / "korely" / "credentials.txt").exists()

    def test_everything_it_writes_is_yours_alone_with_random_secrets(self, tmp_path):
        assert _run(tmp_path).returncode == 0
        folder = tmp_path / "korely"
        for name in (".env", ".korely-install", "docker-compose.yml", ".env.example"):
            assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600, name
        assert stat.S_IMODE((folder / "korely").stat().st_mode) == 0o700
        env = _env_file(tmp_path)
        assert re.fullmatch(r"[0-9a-f]{32}", env["POSTGRES_PASSWORD"])
        assert re.fullmatch(r"[0-9a-f]{64}", env["SECRET_KEY"])
        assert env["PORT"] == "8000"
        assert env["PUBLIC_BASE_URL"] == "http://127.0.0.1:8000"
        assert env["COMPOSE_PROJECT_NAME"] == "korely"
        text = (folder / ".env").read_text()
        assert "RATE_LIMIT_PER_MINUTE=0" in text and "# Where the vectors are computed." in text
        other = tmp_path / "second"
        assert _run(tmp_path, KORELY_DIR=str(other), KORELY_PROJECT="korely2").returncode == 0
        assert (other / ".env").read_text() != text

    def test_the_release_files_are_checked_and_the_image_pinned_by_tag(self, tmp_path):
        r = _run(tmp_path)
        assert r.returncode == 0, r.stderr
        env = _env_file(tmp_path)
        assert env["KORELY_IMAGE"] == f"ghcr.io/{PUBLIC}:{REL}"
        assert "pulled by its tag" in r.stderr, "no digest stamped yet: said"
        assert env["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.image.yml"
        calls = _calls(tmp_path)
        for f in RELEASE_FILES:
            assert f"https://raw.githubusercontent.com/{PUBLIC}/v{REL}/{f}" in calls
            assert (tmp_path / "korely" / f).read_text() == (ROOT / f).read_text()
        assert "--proto =https" in calls and "--proto-redir =https" in calls
        assert f"pull ghcr.io/{PUBLIC}:{REL}" in calls
        assert "files=verified" in (tmp_path / "korely" / ".korely-install").read_text()
        # The database and the API, not Caddy (443 and 80 on a laptop).
        assert re.search(r"^compose -p korely up -d api$", calls, re.M)

    def test_a_stamped_digest_pins_the_image(self, tmp_path):
        digest = "sha256:" + "d" * 64
        _stamped(tmp_path).write_text(stamp.stamp_digest(_stamped(tmp_path).read_text(), digest))
        r = _run(tmp_path)
        assert r.returncode == 0, r.stderr
        assert _env_file(tmp_path)["KORELY_IMAGE"] == f"ghcr.io/{PUBLIC}:{REL}@{digest}"
        assert "pulled by its tag" not in r.stderr

    def test_the_script_names_its_release_and_nothing_else(self):
        text = INSTALL.read_text()
        assert re.fullmatch(r"\d+\.\d+\.\d+", REL) and re.fullmatch(r"\d+\.\d+\.\d+", LEAST)
        # No `latest` tag or version (a model may be called mistral-small-latest).
        assert not re.search(r"(?<![-\w])latest(?![-\w])", re.sub(r"#.*", "", text))
        # The files it downloads are the ones it carries sums for.
        assert re.search(r'^RELEASE_FILES="([^"]+)"$', text, re.M).group(1).split() == \
            list(RELEASE_FILES)

    def test_the_three_addresses_are_made_of_the_public_name(self):
        """The repository was renamed (5 Nov 2026) and its CI pushes the image
        under the new name, ghcr.io/<owner>/<repository> (image.yml): the git
        address, the release files and the image are one name, written once."""
        text = INSTALL.read_text()
        assert re.search(r'^PUBLIC_REPO="([^"]+)"$', text, re.M).group(1) == PUBLIC
        assert PUBLIC == PUBLIC.lower(), "ghcr.io names are lower case"
        for line in ('REPO_URL="https://github.com/$PUBLIC_REPO.git"',
                     'RAW_URL="https://raw.githubusercontent.com/$PUBLIC_REPO"',
                     'IMAGE_REPO="ghcr.io/$PUBLIC_REPO"'):
            assert re.search(rf"^{re.escape(line)}$", text, re.M), line

    def test_the_options_as_flags(self, tmp_path):
        target = tmp_path / "elsewhere"
        r = _run(tmp_path, "--dir", str(target), "--port", "8123", "--project", "mine",
                 "--user", "Anna", "--version", "9.2.3", "--unverified", release="9.2.3")
        assert r.returncode == 0, r.stderr
        env = dict(re.findall(r"^([A-Z_]+)=(.*)$", (target / ".env").read_text(), re.M))
        assert env["PORT"] == "8123" and env["COMPOSE_PROJECT_NAME"] == "mine"
        assert env["KORELY_IMAGE"].endswith(":9.2.3")
        assert "not checked against a sha256 (--unverified)" in r.stderr
        assert "files=unverified" in (target / ".korely-install").read_text()
        assert f"create-operator --tenant {TENANT} --username anna --json" in _calls(tmp_path)
        assert "compose -p mine up -d api" in _calls(tmp_path)
        assert "http://127.0.0.1:8123/mcp" in r.stdout

    def test_save_credentials_writes_a_file_for_you_alone(self, tmp_path):
        r = _run(tmp_path, "--save-credentials")
        assert r.returncode == 0, r.stderr
        f = tmp_path / "korely" / "credentials.txt"
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert PASSWORD in f.read_text() and KEY in f.read_text()

    def test_the_shells_variables_do_not_reach_compose(self, tmp_path):
        """Compose prefers the shell's variables to .env's: a PORT=3000
        exported for a Node app would have put the API on 3000."""
        r = _run(tmp_path, PORT="3000", POSTGRES_PASSWORD="another-projects",
                 COMPOSE_PROJECT_NAME="theirs", COMPOSE_FILE="theirs.yml", KORELY_IMAGE="x")
        assert r.returncode == 0, r.stderr
        seen = set((tmp_path / "calls.env").read_text().splitlines())
        assert seen == {"PORT=unset PASSWORD=unset PROJECT=unset FILE=unset IMAGE=unset"}

    def test_build_fetches_the_tag_checks_it_and_builds(self, tmp_path):
        r = _run(tmp_path, "--build")
        assert r.returncode == 0, r.stderr
        calls = _calls(tmp_path)
        assert (f"fetch -q --depth 1 https://github.com/{PUBLIC}.git "
                f"refs/tags/v{REL}") in calls
        assert re.search(r"^compose -p korely up -d --build api$", calls, re.M)
        assert "pull " not in calls and "raw.githubusercontent" not in calls
        env = _env_file(tmp_path)
        assert "KORELY_IMAGE" not in env and "COMPOSE_FILE" not in env
        assert "files=verified" in (tmp_path / "korely" / ".korely-install").read_text()

    def test_a_tag_that_says_another_version_stops(self, tmp_path):
        r = _run(tmp_path, "--build", FAKE_TAG_VERSION="0.9.0")
        assert r.returncode == 1 and "says it is 0.9.0" in r.stderr

    def test_from_local_takes_the_checkouts_version(self, tmp_path):
        src = tmp_path / "checkout"
        src.mkdir()
        (src / "VERSION").write_text(f"{LEAST}-dev\n")
        r = _run(tmp_path, "--from-local", str(src), release=f"{LEAST}-dev")
        assert r.returncode == 0, r.stderr
        assert f"fetch -q --depth 1 {src} HEAD" in _calls(tmp_path)
        mark = (tmp_path / "korely" / ".korely-install").read_text()
        assert f"release={LEAST}-dev" in mark and "files=local" in mark

    def test_a_path_with_spaces(self, tmp_path):
        parent = tmp_path / "my installs"
        parent.mkdir()
        target = parent / "kor ely"
        r = _run(tmp_path, KORELY_DIR=str(target))
        assert r.returncode == 0, r.stderr
        assert (target / ".env").exists() and (target / "korely").exists()
        assert f'"{target}/korely" stop' in r.stdout

    def test_cdpath_does_not_send_it_elsewhere(self, tmp_path):
        decoy = tmp_path / "decoy"
        (decoy / "korely").mkdir(parents=True)
        r = _run(tmp_path, KORELY_DIR="korely", CDPATH=str(decoy))
        assert r.returncode == 0, r.stderr
        assert (tmp_path / "korely" / ".env").exists()
        assert list((decoy / "korely").iterdir()) == []

    def test_a_folder_with_only_a_ds_store_is_empty(self, tmp_path):
        (tmp_path / "korely").mkdir()
        (tmp_path / "korely" / ".DS_Store").write_bytes(b"\0")
        r = _run(tmp_path)
        assert r.returncode == 0, r.stderr


class TestASecondRun:
    def _installed(self, tmp_path):
        assert _run(tmp_path).returncode == 0
        (tmp_path / "calls").unlink()
        _answers(tmp_path, operators=json.dumps([{"username": "admin", "tenant": TENANT,
                                                  "active": True, "last_login": None}]),
                 tenants=json.dumps([{"id": TENANT, "name": "My account",
                                      "created": "2026-10-29"}]))

    def test_it_starts_and_makes_nothing_new(self, tmp_path):
        self._installed(tmp_path)
        env_before = (tmp_path / "korely" / ".env").read_text()
        r = _run(tmp_path)
        assert r.returncode == 0, r.stderr
        calls = _calls(tmp_path)
        assert "create-tenant" not in calls and "create-operator" not in calls
        assert "issue-key" not in calls and "raw.githubusercontent" not in calls
        assert "already exists" in r.stderr and "nothing new was made" in r.stdout
        assert KEY not in r.stdout and PASSWORD not in r.stdout
        wrapper = f'"{tmp_path}/korely/korely"'
        assert f"{wrapper} manage issue-key --tenant {TENANT}" in r.stdout
        assert f"{wrapper} manage reset-password --username admin" in r.stdout
        assert (tmp_path / "korely" / ".env").read_text() == env_before

    def test_an_account_without_its_login_gets_a_key_and_the_login(self, tmp_path):
        """A run stopped between create-tenant and create-operator."""
        _answers(tmp_path, tenants=json.dumps([{"id": TENANT, "name": "My account",
                                                "created": "2026-10-29"}]))
        r = _run(tmp_path)
        assert r.returncode == 0, r.stderr
        calls = _calls(tmp_path)
        assert "create-tenant" not in calls
        assert f"issue-key --tenant {TENANT}" in calls and "create-operator" in calls
        assert KEY in r.stdout and PASSWORD in r.stdout

    def test_an_account_that_cannot_be_made_says_how_to_retry_and_clean_up(self, tmp_path):
        r = _run(tmp_path, FAKE_OPERATOR_FAILS="1")
        assert r.returncode == 1
        assert "the install did not finish" in r.stderr
        assert "Run this script again to retry" in r.stderr
        assert f'"{tmp_path}/korely/korely" uninstall --yes' in r.stderr

    @pytest.mark.parametrize("args, said", [
        (("--version", "9.0.7", "--unverified"),
         "does not upgrade: the steps for its folder are in https://github.com/korely-ai/korely/"
         "blob/main/docs/guide/operations.md#upgrading"),
        (("--project", "other"), "--project asks for 'other'"),
        (("--port", "8111"), "--port asks for 8111"),
    ])
    def test_another_version_project_or_port_is_refused(self, tmp_path, args, said):
        assert _run(tmp_path).returncode == 0
        r = _run(tmp_path, *args)
        assert r.returncode == 1 and said in r.stderr, r.stderr

    def test_a_lost_env_with_its_database_still_there_is_refused(self, tmp_path):
        self._installed(tmp_path)
        (tmp_path / "korely" / ".env").unlink()
        r = _run(tmp_path, FAKE_TAKEN="korely_pgdata\n")
        assert r.returncode == 1 and "still has its database" in r.stderr
        assert "Put the old .env back" in r.stderr
        assert not (tmp_path / "korely" / ".env").exists()

    def test_a_download_stopped_half_way_is_finished_by_the_next_run(self, tmp_path):
        r = _run(tmp_path, FAKE_EFFECTIVE="https://example.com/x")
        assert r.returncode == 1
        r = _run(tmp_path)
        assert r.returncode == 0, r.stderr


class TestTheFolderCommand:
    """./korely, the one command printed: the shell's variables never reach
    Compose, the project is named, and uninstall leaves nothing."""

    def _installed(self, tmp_path):
        assert _run(tmp_path).returncode == 0
        (tmp_path / "calls").unlink()
        (tmp_path / "calls.env").unlink()
        return tmp_path / "korely" / "korely"

    def _korely(self, tmp_path, wrapper, *args, cwd=None, **env):
        return subprocess.run([str(wrapper), *args], env=_env(tmp_path, **env),
                              capture_output=True, text=True, cwd=cwd or "/", timeout=30)

    def test_it_ignores_the_shell_and_names_its_project(self, tmp_path):
        wrapper = self._installed(tmp_path)
        decoy = tmp_path / "decoy"
        (decoy / "korely").mkdir(parents=True)
        r = self._korely(tmp_path, wrapper, "start", PORT="3000", POSTGRES_PASSWORD="x",
                         COMPOSE_PROJECT_NAME="theirs", COMPOSE_FILE="theirs.yml",
                         KORELY_IMAGE="theirs", CDPATH=str(decoy))
        assert r.returncode == 0, r.stderr
        assert _calls(tmp_path).splitlines() == ["compose -p korely up -d api"]
        assert set((tmp_path / "calls.env").read_text().splitlines()) == \
            {"PORT=unset PASSWORD=unset PROJECT=unset FILE=unset IMAGE=unset"}
        r = self._korely(tmp_path, wrapper, "stop", COMPOSE_PROJECT_NAME="theirs")
        assert r.returncode == 0 and _calls(tmp_path).splitlines()[-1] == "compose -p korely down"

    def test_uninstall_asks_for_yes(self, tmp_path):
        wrapper = self._installed(tmp_path)
        r = self._korely(tmp_path, wrapper, "uninstall")
        assert r.returncode == 1 and "uninstall --yes" in r.stdout
        assert _calls(tmp_path) == "" and wrapper.exists()

    @pytest.mark.parametrize("image_rm", ["0", "1"])
    def test_uninstall_removes_it_all_even_when_the_image_is_shared(self, tmp_path, image_rm):
        wrapper = self._installed(tmp_path)
        r = self._korely(tmp_path, wrapper, "uninstall", "--yes", FAKE_IMAGE_RM=image_rm)
        assert r.returncode == 0, r.stdout + r.stderr
        calls = _calls(tmp_path).splitlines()
        assert calls[0].startswith("compose -p korely run --rm --no-deps -T --entrypoint sh "
                                   "facts-dir -c")
        assert calls[1] == "compose -p korely down -v --remove-orphans"
        assert calls[2] == f"image rm ghcr.io/{PUBLIC}:{REL}"
        assert not (tmp_path / "korely").exists()
        assert ("was kept" in r.stdout) == (image_rm == "1")


class TestWhatItRefuses:
    def test_a_folder_it_did_not_make_is_left_alone(self, tmp_path):
        target = tmp_path / "korely"
        target.mkdir()
        (target / ".env").write_text("SECRET_KEY=the-only-copy\n")
        r = _run(tmp_path)
        assert r.returncode == 1 and "nothing was touched" in r.stderr
        assert sorted(p.name for p in target.iterdir()) == [".env"]
        assert "compose -p" not in _calls(tmp_path) and "fetch" not in _calls(tmp_path)

    def test_a_project_name_docker_already_has(self, tmp_path):
        r = _run(tmp_path, FAKE_TAKEN="korely_pgdata\n")
        assert r.returncode == 1 and "--project NAME" in r.stderr
        assert not (tmp_path / "korely" / ".env").exists()

    @pytest.mark.parametrize("rc", ["0", "28", "52", "56"])
    def test_a_port_anything_holds(self, tmp_path, rc):
        """Only curl's 7, could not connect, is a free port."""
        r = _run(tmp_path, FAKE_PORT=rc)
        assert r.returncode == 1 and "--port N" in r.stderr

    @pytest.mark.parametrize("version", ["latest", "1.0.1-dev", "v1.0.0", "1.0"])
    def test_a_version_that_is_not_a_release(self, tmp_path, version):
        r = _run(tmp_path, "--version", version, "--unverified")
        assert r.returncode == 1 and "a release, x.y.z" in r.stderr
        assert not (tmp_path / "korely").exists()

    @pytest.mark.parametrize("version", ["1.0.0", "0.2.1", "1.0.1", "1.0.7", "1.0.8"])
    def test_a_release_older_than_this_script(self, tmp_path, version):
        """1.0.0 has no docker-compose.image.yml and no `manage --json`: it
        died at the account, with the stack up. The images before 1.0.9 are
        under the repository's old name (CHANGELOG 1.0.9): `--version 1.0.7
        --unverified` went on to a pull of an image that is not there, and
        stopped with a message about the pull (the verifier's finding of 13
        Nov 2026). Each stops before anything is downloaded, naming the
        oldest release the script installs."""
        assert LEAST == "1.0.9"
        r = _run(tmp_path, "--version", version, "--unverified")
        assert r.returncode == 1 and "1.0.9 is the first release it can install" in r.stderr
        assert f"install {version} by hand" in r.stderr
        assert not (tmp_path / "korely").exists() and _calls(tmp_path) == ""

    def test_the_oldest_release_it_installs_passes_the_check(self, tmp_path):
        r = _run(tmp_path, "--version", LEAST, "--unverified")
        assert "first release it can install" not in r.stderr, r.stderr

    def test_another_release_needs_unverified(self, tmp_path):
        r = _run(tmp_path, "--version", "9.0.0")
        assert r.returncode == 1 and "--unverified" in r.stderr
        assert not (tmp_path / "korely").exists()

    def test_a_copy_without_sums_needs_unverified(self, tmp_path):
        """main between the first release of this script and its tag."""
        r = _run(tmp_path, installer=_unstamped(tmp_path))
        assert r.returncode == 1 and "carries no sha256" in r.stderr
        assert not (tmp_path / "korely").exists()

    @pytest.mark.parametrize("build", [False, True])
    def test_a_file_that_is_not_the_releases(self, tmp_path, build):
        """The sums are this checkout's; the files served are not."""
        served = tmp_path / "served"
        for f in RELEASE_FILES:
            (served / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / f, served / f)
        (served / "docker-compose.yml").write_text(
            (ROOT / "docker-compose.yml").read_text() + "\n# one more line\n")
        r = _run(tmp_path, *(["--build"] if build else []), SRC=str(served))
        assert r.returncode == 1 and "is not the file of the release (sha256)" in r.stderr
        assert "compose -p korely up" not in _calls(tmp_path)
        assert not (tmp_path / "korely" / ".env").exists()
        if not build:
            assert not (tmp_path / "korely" / "docker-compose.yml").exists()

    def test_a_download_answered_from_elsewhere(self, tmp_path):
        r = _run(tmp_path, FAKE_EFFECTIVE="https://example.com/docker-compose.yml")
        assert r.returncode == 1 and "answered from https://example.com" in r.stderr
        assert not (tmp_path / "korely" / "docker-compose.yml").exists()

    def test_an_image_not_published_points_at_build(self, tmp_path):
        r = _run(tmp_path, FAKE_PULL="1")
        assert r.returncode == 1 and "--build" in r.stderr

    def test_a_compose_too_old_for_the_image_file(self, tmp_path):
        r = _run(tmp_path, FAKE_COMPOSE="2.20.2")
        assert r.returncode == 1 and "2.24.4" in r.stderr and "--build" in r.stderr

    def test_little_memory_is_a_warning_not_a_stop(self, tmp_path):
        r = _run(tmp_path, FAKE_MEM="2000000000")
        assert r.returncode == 0, r.stderr
        assert "Warning: Docker has 2.0 GB of memory" in r.stderr

    def test_an_api_that_does_not_come_up_says_where_to_look(self, tmp_path):
        r = _run(tmp_path, "--wait", "0", FAKE_DOWN="1")
        assert r.returncode == 1
        assert f'"{tmp_path}/korely/korely" logs' in r.stderr
        assert "compose -p korely logs --tail 30 api" in _calls(tmp_path)

    def test_no_sudo_and_nothing_runs_until_the_last_line(self):
        text = INSTALL.read_text()
        code = re.sub(r"#.*", "", text)
        assert "sudo" not in code.replace("never runs sudo", "")
        assert re.search(r"^set -eu$", text, re.M) and re.search(r"^unset CDPATH$", text, re.M)
        assert re.search(r"^umask 077$", text, re.M)
        assert text.rstrip().splitlines()[-1] == 'main "$@"'

    def test_a_download_cut_anywhere_runs_nothing(self, tmp_path):
        """`curl | sh` of a script that stops arriving half way: everything
        is a function, called on the last line."""
        lines = _stamped(tmp_path).read_text().splitlines(keepends=True)
        env = _env(tmp_path)
        for cut in range(1, len(lines) - 1, 23):
            r = subprocess.run(["sh"], input="".join(lines[:cut]), env=env,
                               capture_output=True, text=True, cwd=tmp_path, timeout=30)
            assert r.stdout == "", f"cut at line {cut} printed {r.stdout!r}"
            assert _calls(tmp_path) == "", f"cut at line {cut} ran {_calls(tmp_path)!r}"
            assert not (tmp_path / "korely").exists(), f"cut at line {cut}"


class TestTheAnswersItReadsAreManages:
    """The JSON the fakes answer above is written by hand; here it is what
    manage really prints, read by the script as it is."""

    def test_a_real_account_read_back(self, db, monkeypatch, capsys, tmp_path):
        from korely_agent import manage

        monkeypatch.setattr(manage, "_session", lambda: db)
        assert manage.main(["list-operators", "--json"]) == 0
        operators = capsys.readouterr().out.strip()
        assert manage.main(["create-tenant", "--name", "My account", "--json"]) == 0
        tenant_line = capsys.readouterr().out.strip()
        tid = json.loads(tenant_line)["tenant"]
        assert manage.main(["create-operator", "--tenant", tid, "--username", "admin",
                            "--json"]) == 0
        operator_line = capsys.readouterr().out.strip()
        fake = _answers(tmp_path, operators=operators)
        (fake / "create-tenant").write_text(tenant_line + "\n")
        (fake / "create-operator").write_text(operator_line + "\n")
        r = _run(tmp_path)
        assert r.returncode == 0, r.stderr
        assert json.loads(tenant_line)["key"] in r.stdout
        assert json.loads(operator_line)["password"] in r.stdout
        assert f"(expires {json.loads(tenant_line)['expires']})" in r.stdout

        assert manage.main(["list-operators", "--json"]) == 0
        operators = capsys.readouterr().out.strip()
        assert manage.main(["list-tenants", "--json"]) == 0
        tenants = capsys.readouterr().out.strip()
        _answers(tmp_path, operators=operators, tenants=tenants)
        r = _run(tmp_path)
        assert r.returncode == 0, r.stderr
        assert "nothing new was made" in r.stdout and f"--tenant {tid}" in r.stdout


class TestTheStamp:
    """What scripts/stamp_installer.py writes, release.sh runs, and
    check_the_version.py holds."""

    def _check(self):
        spec = importlib.util.spec_from_file_location(
            "check_the_version", ROOT / "scripts" / "check_the_version.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_a_stamped_release_passes_the_check_and_a_stale_sum_does_not(self):
        check = self._check()
        text = stamp.stamp_release(INSTALL.read_text(), REL, ROOT)
        read = lambda p: (ROOT / p).read_bytes()  # noqa: E731
        assert check.installer_disagreements(REL, text, read) == []
        stale = text.replace(stamp.sums_block(INSTALL.read_text(), ROOT).split()[0], "0" * 64)
        assert check.installer_disagreements(REL, stale, read)

    def test_release_sh_stamps_and_says_the_digest_step(self):
        text = (ROOT / "scripts" / "release.sh").read_text()
        assert 'python3 scripts/stamp_installer.py release "$want"' in text
        assert "stamp_installer.py digest" in text
        assert "git add VERSION pyproject.toml install.sh" in text

    def test_a_new_release_forgets_the_old_digest(self):
        text = stamp.stamp_digest(INSTALL.read_text(), "sha256:" + "e" * 64)
        assert 'THIS_IMAGE_DIGEST=""' in stamp.stamp_release(text, "9.9.9", ROOT)

    @pytest.mark.parametrize("bad", ["latest", "sha256:abc", "e" * 64])
    def test_a_digest_that_is_not_one_is_refused(self, bad):
        with pytest.raises(SystemExit):
            stamp.stamp_digest(INSTALL.read_text(), bad)


def test_the_published_image_file_takes_the_build_away():
    text = (ROOT / "docker-compose.image.yml").read_text()
    assert "build: !reset null" in text
    assert "${KORELY_IMAGE:?" in text
    # docker-compose.yml alone builds, as it always did.
    assert "image:" not in (ROOT / "docker-compose.yml").read_text().split("\n  api:", 1)[1] \
        .split("\nvolumes:", 1)[0]


# ── your own model key (29 Oct 2026) ────────────────────────────────────────

def _writer_rows():
    """install.sh's writer table: {id: (label, region, base, model)}."""
    rows = re.findall(r'echo "([a-z]+)\|([^|"]+)\|(eu|global)\|([^|"]*)\|([^|"]+)"',
                      INSTALL.read_text())
    assert rows, "install.sh has no writer table"
    return {r[0]: r[1:] for r in rows}


def _with_a_terminal(tmp_path, args, typed, **env):
    """install.sh with a terminal of its own, as on a laptop: what it showed
    on the terminal, and its exit status."""
    script = _stamped(tmp_path)
    run_env = _env(tmp_path, **env)
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child
        os.chdir(tmp_path)
        os.execve("/bin/sh", ["sh", str(script), *args], run_env)
    shown, deadline, sent = b"", time.time() + 30, False
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            shown += chunk
        if not sent and b"is not shown as you type it" in shown:
            os.write(fd, typed.encode() + b"\n")
            sent = True
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return shown.decode(errors="replace"), os.waitstatus_to_exitcode(status)


class TestYourOwnModelKey:
    def test_without_one_the_first_screen_asks(self, tmp_path):
        r = _run(tmp_path)
        assert r.returncode == 0, r.stderr
        env = _env_file(tmp_path)
        assert env["GOOGLE_API_KEY"] == ""
        assert not set(WRITER_LINES) & set(env)
        assert "first screen asks where facts are written" in r.stdout
        assert '"Skip for now"' in r.stdout

    def test_the_key_is_asked_on_the_terminal_and_never_shown(self, tmp_path):
        shown, status = _with_a_terminal(tmp_path, ["--provider", "scaleway"], PROVIDER_KEY)
        assert status == 0, shown
        assert "Your Scaleway key (it is not shown as you type it)" in shown
        assert PROVIDER_KEY not in shown, "the terminal echoed the key"
        env = _env_file(tmp_path)
        assert env["MISTRAL_API_KEY"] == PROVIDER_KEY
        assert env["MISTRAL_API_BASE"] == "https://api.scaleway.ai/v1"
        assert "Facts are written by Scaleway (eu)" in shown

    def test_no_terminal_says_how_to_give_it_without_the_history(self, tmp_path):
        r = _run(tmp_path, "--provider", "scaleway")
        assert r.returncode == 1
        assert "read -rs KORELY_PROVIDER_KEY" in r.stderr
        assert "export KORELY_PROVIDER_KEY" in r.stderr
        assert not (tmp_path / "korely").exists()

    def test_a_gemini_key_is_the_file_writer_choose_models_writes(self, tmp_path):
        r = _run(tmp_path, KORELY_PROVIDER="gemini", KORELY_PROVIDER_KEY=GEMINI_KEY)
        assert r.returncode == 0, r.stderr
        env = _env_file(tmp_path)
        assert env["GOOGLE_API_KEY"] == GEMINI_KEY
        assert env["EXTRACTION_PROVIDER"] == "gemini"
        assert env["EXTRACTION_MODEL"] == "gemini-3.1-flash-lite"
        assert "MISTRAL_API_KEY" not in env
        assert stat.S_IMODE((tmp_path / "korely" / ".env").stat().st_mode) == 0o600
        assert GEMINI_KEY not in r.stdout + r.stderr
        assert GEMINI_KEY[-4:] not in r.stdout + r.stderr
        assert "Facts are written by Gemini (global), gemini-3.1-flash-lite" in r.stdout
        assert "first screen" not in r.stdout

    def test_a_key_already_in_the_shell_is_never_taken(self, tmp_path):
        """Exported for another tool, it is not this install's to bill: the
        install completes as without a key, and nothing of it is written."""
        shell = {"GEMINI_API_KEY": GEMINI_KEY, "GOOGLE_API_KEY": GEMINI_KEY,
                 "OPENAI_API_KEY": "sk-fake-other-tool", "MISTRAL_API_KEY": PROVIDER_KEY,
                 "EXTRACTION_PROVIDER": "mistral"}
        r = _run(tmp_path, **shell)
        assert r.returncode == 0, r.stderr
        text = (tmp_path / "korely" / ".env").read_text()
        for value in set(shell.values()) - {"mistral"}:
            assert value not in text and value not in r.stdout + r.stderr
        env = _env_file(tmp_path)
        assert env["GOOGLE_API_KEY"] == "" and not set(WRITER_LINES) & set(env)
        assert "first screen asks where facts are written" in r.stdout
        compose = (ROOT / "docker-compose.yml").read_text()
        assert not any(f"${{{name}" in compose for name in shell)

    def test_another_provider_goes_through_the_compatible_door(self, tmp_path):
        r = _run(tmp_path, KORELY_PROVIDER="scaleway", KORELY_PROVIDER_KEY=PROVIDER_KEY)
        assert r.returncode == 0, r.stderr
        env = _env_file(tmp_path)
        assert env["EXTRACTION_PROVIDER"] == "mistral"
        assert env["MISTRAL_API_BASE"] == "https://api.scaleway.ai/v1"
        assert env["MISTRAL_API_KEY"] == PROVIDER_KEY
        assert env["MISTRAL_EXTRACTION_MODEL"] == "gpt-oss-120b"
        assert env["GOOGLE_API_KEY"] == ""
        assert PROVIDER_KEY not in r.stdout + r.stderr
        assert "Scaleway (eu)" in r.stdout

    def test_a_region_picks_its_recommended_provider(self, tmp_path):
        r = _run(tmp_path, "--region", "eu", "--model", "llama-3.3-70b-instruct",
                 KORELY_PROVIDER_KEY=PROVIDER_KEY)
        assert r.returncode == 0, r.stderr
        env = _env_file(tmp_path)
        assert env["MISTRAL_API_BASE"] == "https://api.scaleway.ai/v1"
        assert env["MISTRAL_EXTRACTION_MODEL"] == "llama-3.3-70b-instruct"

    @pytest.mark.parametrize("args, env, said", [
        ((), {"KORELY_PROVIDER_KEY": PROVIDER_KEY}, "whose key is it?"),
        (("--region", "eu", "--provider", "openai"), {"KORELY_PROVIDER_KEY": PROVIDER_KEY},
         "OpenAI writes in global, and --region asks for eu"),
        (("--region", "mars"), {"KORELY_PROVIDER_KEY": PROVIDER_KEY}, "eu or global"),
        (("--provider", "acme"), {"KORELY_PROVIDER_KEY": PROVIDER_KEY}, "--provider is 'acme'"),
        (("--model", "x"), {}, "whose key is it?"),
        (("--key", PROVIDER_KEY), {}, "the key is never a flag"),
        ((), {"KORELY_PROVIDER": "scaleway", "KORELY_PROVIDER_KEY": "has a space"},
         "a character no provider key has"),
    ])
    def test_what_it_refuses_before_touching_anything(self, tmp_path, args, env, said):
        r = _run(tmp_path, *args, **env)
        assert r.returncode == 1 and said in r.stderr, r.stderr
        assert not (tmp_path / "korely").exists()
        assert PROVIDER_KEY not in r.stderr
        assert "has a space" not in r.stderr

    def test_a_second_run_does_not_rewrite_the_env(self, tmp_path):
        assert _run(tmp_path).returncode == 0
        before = (tmp_path / "korely" / ".env").read_text()
        r = _run(tmp_path, KORELY_PROVIDER="scaleway", KORELY_PROVIDER_KEY=PROVIDER_KEY)
        assert r.returncode == 0, r.stderr
        assert (tmp_path / "korely" / ".env").read_text() == before
        assert "was not written" in r.stderr and "Settings, Writing model" in r.stderr
        assert PROVIDER_KEY not in r.stdout + r.stderr

    def test_the_writers_are_the_screens_and_choose_models(self):
        """Three copies of who writes with a key: the first screen's
        (services/model_choice.PROVIDERS), choose_models.py's (which runs
        without the product's packages) and this script's (which runs
        without Python). Held equal here, as test_choose_models.py holds the
        second to the first."""
        from korely_agent.services import model_choice as mc

        spec = importlib.util.spec_from_file_location(
            "choose_models", ROOT / "scripts" / "choose_models.py")
        cm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cm)
        rows = _writer_rows()
        for pid, (label, region, base, model) in rows.items():
            p = mc.PROVIDERS[pid]
            assert (label, region, model) == (p.label, p.region, p.prefer[0]), pid
            assert base == ("" if pid == "google" else p.base), pid
        hosts = {(url or "", suggested) for _label, url, suggested, _note in cm.HOSTS.values()}
        assert {(base, model) for _l, _r, base, model in rows.values()} == hosts
        text = INSTALL.read_text()
        for region in ("eu", "global"):
            [recommended] = [p.id for p in mc.PROVIDERS.values()
                             if p.region == region and p.recommended]
            assert f"{region}) echo {'scaleway' if region == 'eu' else 'google'} ;;" in text
            assert recommended == ("scaleway" if region == "eu" else "google")

    @pytest.mark.parametrize("provider, key, expected", [
        ("gemini", GEMINI_KEY, {"provider": "google", "region": "global",
                                "model": "gemini-3.1-flash-lite"}),
        ("scaleway", PROVIDER_KEY, {"provider": "scaleway", "region": "eu",
                                    "model": "gpt-oss-120b"}),
        ("openrouter", "sk-or-fake-0r0r", {"provider": "openrouter", "region": "global",
                                           "model": "openai/gpt-oss-120b:nitro"}),
    ])
    def test_the_product_reads_the_writer_without_calling_it(self, db, tmp_path, monkeypatch,
                                                              provider, key, expected):
        """What the dashboard's Models page is told about the .env install.sh
        wrote: configured, from the file, so the first screen is skipped.
        Nothing is called (the suite's network is closed)."""
        from korely_agent import config
        from korely_agent.core import llm
        from korely_agent.services import model_choice as mc

        r = _run(tmp_path, KORELY_PROVIDER=provider, KORELY_PROVIDER_KEY=key)
        assert r.returncode == 0, r.stderr
        env = _env_file(tmp_path)
        for name in ("GOOGLE_API_KEY", *WRITER_LINES):
            monkeypatch.setenv(name, env.get(name, ""))
        config.get_settings.cache_clear()
        llm.reset_provider_cache()
        try:
            out = mc.describe(db)
            assert out["configured"] is True and out["source"] == "env"
            assert out["active"] == {**expected, "label": mc.PROVIDERS[expected["provider"]].label,
                                     "last_four": key[-4:]}
            assert llm.default_region() == expected["region"]
        finally:
            config.get_settings.cache_clear()
            llm.reset_provider_cache()
