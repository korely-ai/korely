"""Korely Installer.app, the Mac download (29 Oct 2026): scripts/
make_mac_installer.sh, which bundles the release's install.sh and nothing
else, and packaging/macos/launcher.sh, which runs it in Terminal with the
user's answers.

The launcher is played under /bin/sh with an `open` that records what it was
asked, and an install.sh that is the real one with its last line (`main
"$@"`) swapped for a recorder: the names it reads are the real names. The
build itself needs a Mac (osacompile, codesign, hdiutil): those tests skip
elsewhere, and the refusal of a wrong install.sh, which comes before any of
them, runs everywhere."""
import importlib.util
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
LAUNCHER = ROOT / "packaging" / "macos" / "launcher.sh"
APPLESCRIPT = ROOT / "packaging" / "macos" / "installer.applescript"
BUILD = ROOT / "scripts" / "make_mac_installer.sh"
REL = re.search(r'^THIS_RELEASE="([^"]+)"$', INSTALL.read_text(), re.M).group(1)
PROVIDER_KEY = "scw-fake-0000-1111-2222-k3y9"


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

_spec = importlib.util.spec_from_file_location("stamp_installer",
                                               ROOT / "scripts" / "stamp_installer.py")
stamp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stamp)

# The build's tools (make_mac_installer.sh checks the same list) and swiftc,
# which a Mac without the Command Line Tools lacks behind a working xcrun shim.
# The build is --unsigned: no test needs this Mac's signing identity.
ON_A_MAC = (sys.platform == "darwin"
            and all(shutil.which(t) for t in ("osacompile", "codesign", "hdiutil", "sips",
                                              "iconutil", "plutil", "lipo", "vtool", "xcrun"))
            # xcode-select -p first: it never offers to install the tools.
            and subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0
            and subprocess.run(["xcrun", "--sdk", "macosx", "--find", "swiftc"],
                               capture_output=True).returncode == 0)


def _stamped(tmp_path, version=REL):
    copy = tmp_path / "install.stamped.sh"
    copy.write_text(stamp.stamp_release(INSTALL.read_text(), version, ROOT))
    return copy


# ── the copy it bundles ─────────────────────────────────────────────────────

class TestTheReleaseCopy:
    def test_the_stamped_copy_passes_with_or_without_its_digest(self):
        text = stamp.stamp_release(INSTALL.read_text(), REL, ROOT)
        assert stamp.release_copy_disagreement(text, REL, ROOT) == ""
        pinned = stamp.stamp_digest(text, "sha256:" + "d" * 64)
        assert stamp.release_copy_disagreement(pinned, REL, ROOT) == ""

    def test_anything_else_is_refused(self, tmp_path):
        text = stamp.stamp_release(INSTALL.read_text(), REL, ROOT)
        # A copy with no sums, as main between two releases.
        assert stamp.release_copy_disagreement(_unstamped_text(), REL, ROOT)
        # Another release's copy.
        assert stamp.release_copy_disagreement(text, "9.9.9", ROOT)
        # A sum that is not the file's.
        first = stamp.sums_block(INSTALL.read_text(), ROOT).split()[0]
        assert stamp.release_copy_disagreement(text.replace(first, "0" * 64), REL, ROOT)
        # The right sums, and a line changed elsewhere: the tag's copy says so.
        changed = text.replace("set -eu", "set -u", 1)
        assert stamp.release_copy_disagreement(changed, REL, ROOT, tagged=text)
        assert stamp.release_copy_disagreement(text, REL, ROOT, tagged=text) == ""
        pinned = stamp.stamp_digest(text, "sha256:" + "d" * 64)
        assert stamp.release_copy_disagreement(pinned, REL, ROOT, tagged=text) == ""
        # Files that are not there.
        assert "missing" in stamp.release_copy_disagreement(text, REL, tmp_path)

    def test_the_check_prints_the_sha256_of_the_copy(self, tmp_path):
        copy = _stamped(tmp_path)
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "stamp_installer.py"), "check",
                            REL, "--installer", str(copy), "--root", str(ROOT)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        import hashlib
        assert r.stdout.strip() == hashlib.sha256(copy.read_bytes()).hexdigest()

    def test_the_build_refuses_an_unstamped_install_sh_before_building_anything(self, tmp_path):
        out = tmp_path / "dist"
        r = subprocess.run(["bash", str(BUILD), REL, "--unsigned", "--installer",
                            str(_unstamped(tmp_path)), "--root", str(ROOT), "--out", str(out)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 1
        assert "refusing to bundle" in r.stderr
        assert not out.exists()

    def test_the_build_wants_a_release_and_the_two_options_together(self, tmp_path):
        for args in (["latest"], ["1.0"], [REL, "--installer", str(INSTALL)]):
            r = subprocess.run(["bash", str(BUILD), *args, "--out", str(tmp_path / "d")],
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 1, args
        assert not (tmp_path / "d").exists()

    def test_by_default_it_reads_the_tag_and_never_the_checkout(self):
        text = BUILD.read_text()
        assert 'git -C "$here" show "v$version:install.sh" > "$work/tag/install.sh"' in text
        assert '--tagged "$tagged"' in text
        assert 'stamp_installer.py" check "$version"' in text
        # Signed for real unless a local test says otherwise, then notarized.
        assert "codesign --force --options runtime --timestamp --sign" in text
        assert "xcrun notarytool submit" in text and "xcrun stapler staple" in text
        assert 'PROFILE="${KORELY_NOTARY_PROFILE:-korely}"' in text


def test_release_sh_says_how_the_dmg_is_made_and_attached():
    text = (ROOT / "scripts" / "release.sh").read_text()
    assert "scripts/make_mac_installer.sh $want" in text
    assert "gh release upload" in text


# ── the launcher ────────────────────────────────────────────────────────────

OPEN = r"""#!/bin/sh
printf '%s\n' "$*" >> "$CALLS"
exit 0
"""


def _recording_install_sh():
    """The real install.sh, recording its arguments and environment instead
    of installing."""
    text = INSTALL.read_text()
    assert text.rstrip().endswith('main "$@"')
    recorder = (
        'printf "%s\\n" "$@" > "$RECORD.argv"\n'
        'env > "$RECORD.env"\n'
        'mkdir -p "$2" && printf "PORT=%s\\n" "${FAKE_PORT:-8000}" > "$2/.env"\n'
        'exit "${FAKE_STATUS:-0}"\n')
    return text.rstrip()[:-len('main "$@"')] + recorder


def _app_tmp(tmp_path, answers):
    """What the app leaves before calling the launcher: a folder of the
    user's alone with the answers in it."""
    res = tmp_path / "Resources"
    res.mkdir()
    shutil.copy(LAUNCHER, res / "launcher.sh")
    (res / "install.sh").write_text(_recording_install_sh())
    tmp = tmp_path / "korely-installer.abc123"
    tmp.mkdir(mode=0o700)
    (tmp / "answers").write_text("".join(line + "\n" for line in answers))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "open").write_text(OPEN)
    (bin_ / "open").chmod(0o755)
    return res, tmp


def _env(tmp_path, **extra):
    return {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(tmp_path),
            "CALLS": str(tmp_path / "calls"), "RECORD": str(tmp_path / "record"), **extra}


def _launch(tmp_path, answers, **extra):
    """The app's call, then Terminal's: the command install.command runs."""
    res, tmp = _app_tmp(tmp_path, answers)
    env = _env(tmp_path, **extra)
    r = subprocess.run(["/bin/sh", str(res / "launcher.sh"), "terminal", str(tmp)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    command = (tmp / "install.command").read_text()
    assert command.startswith("#!/bin/sh\nexec /bin/sh ") and command.count("\n") == 2
    run = subprocess.run(["/bin/sh", str(tmp / "install.command")], env=env,
                         capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    return tmp, command, run


class TestTheLauncher:
    def test_terminal_gets_a_command_with_paths_and_never_the_key(self, tmp_path):
        res, tmp = _app_tmp(tmp_path, [str(tmp_path / "Korely"), "scaleway", PROVIDER_KEY])
        r = subprocess.run(["/bin/sh", str(res / "launcher.sh"), "terminal", str(tmp)],
                           env=_env(tmp_path), capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        command = tmp / "install.command"
        assert PROVIDER_KEY not in command.read_text()
        assert command.stat().st_mode & 0o777 == 0o700
        calls = (tmp_path / "calls").read_text()
        assert calls == f"-b com.apple.Terminal {command}\n"
        assert PROVIDER_KEY not in calls
        # The copies it runs from, so that ejecting the disk image stops nothing.
        assert (tmp / "install.sh").read_bytes() == (res / "install.sh").read_bytes()
        assert (tmp / "launcher.sh").read_bytes() == LAUNCHER.read_bytes()

    def test_the_key_reaches_install_sh_in_its_environment_alone(self, tmp_path):
        folder = tmp_path / "My Korely"
        tmp, command, run = _launch(tmp_path, [str(folder), "scaleway", PROVIDER_KEY])
        assert run.returncode == 0, run.stderr
        argv = (tmp_path / "record.argv").read_text().splitlines()
        assert argv == ["--dir", str(folder)]
        env = dict(l.split("=", 1) for l in (tmp_path / "record.env").read_text().splitlines()
                   if "=" in l)
        assert env["KORELY_PROVIDER_KEY"] == PROVIDER_KEY
        assert env["KORELY_PROVIDER"] == "scaleway"
        assert PROVIDER_KEY not in command and PROVIDER_KEY not in run.stdout + run.stderr
        calls = (tmp_path / "calls").read_text()
        assert PROVIDER_KEY not in calls
        # The answers, the copies and the command are gone; the dashboard opens.
        assert not tmp.exists()
        assert calls.splitlines()[-1] == "http://127.0.0.1:8000"
        assert "Press Return to close this window." in run.stdout

    def test_without_a_key_no_provider_is_named(self, tmp_path):
        _, _, run = _launch(tmp_path, [str(tmp_path / "Korely"), "", ""])
        assert run.returncode == 0, run.stderr
        env = (tmp_path / "record.env").read_text()
        assert "KORELY_PROVIDER=" not in env and "KORELY_PROVIDER_KEY=" not in env

    def test_the_answers_decide_never_the_shell(self, tmp_path):
        """A KORELY_* exported for another install, a key included, is not
        install.sh's to read here."""
        _, _, run = _launch(tmp_path, [str(tmp_path / "Korely"), "", ""],
                            KORELY_PORT="9999", KORELY_PROJECT="other",
                            KORELY_PROVIDER_KEY="sk-from-the-shell", KORELY_DIR="/elsewhere")
        assert run.returncode == 0, run.stderr
        env = (tmp_path / "record.env").read_text()
        assert "KORELY_PORT" not in env and "KORELY_PROJECT" not in env
        assert "sk-from-the-shell" not in env and "/elsewhere" not in env

    def test_the_test_only_answers_become_install_sh_flags(self, tmp_path):
        _, _, run = _launch(tmp_path, [str(tmp_path / "Korely"), "", "", "/src/korely-agent",
                                       "8123", "mine"], FAKE_PORT="8123")
        assert run.returncode == 0, run.stderr
        assert (tmp_path / "record.argv").read_text().splitlines() == [
            "--dir", str(tmp_path / "Korely"), "--from-local", "/src/korely-agent",
            "--port", "8123", "--project", "mine"]
        assert (tmp_path / "calls").read_text().splitlines()[-1] == "http://127.0.0.1:8123"

    def test_a_failed_install_opens_nothing_and_says_how_to_go_on(self, tmp_path):
        tmp, _, run = _launch(tmp_path, [str(tmp_path / "Korely"), "", ""], FAKE_STATUS="1")
        assert run.returncode == 1
        assert "open Korely Installer again" in run.stdout.replace("\n", " ")
        assert "127.0.0.1" not in (tmp_path / "calls").read_text()
        assert not tmp.exists()

    def test_a_quote_in_a_path_stays_one_word(self, tmp_path):
        weird = tmp_path / "it's here"
        weird.mkdir()
        _, command, run = _launch(weird, [str(weird / "Korely"), "", ""])
        assert run.returncode == 0, run.stderr
        assert "'\\''" in command


def test_the_app_never_puts_the_key_on_a_command_line():
    """The key lives in theKey and the answers string, which AppleScript
    writes to the file itself; no `do shell script` line names either."""
    text = APPLESCRIPT.read_text()
    shell_lines = [l for l in text.splitlines() if "do shell script" in l]
    assert shell_lines
    for line in shell_lines:
        assert "theKey" not in line and "answers" not in line, line
    assert "with hidden answer" in text
    assert "write answers to fh" in text
    # The test-only inputs, read from the app's environment and nowhere else.
    for name in ("KORELY_INSTALLER_FROM_LOCAL", "KORELY_INSTALLER_PORT",
                 "KORELY_INSTALLER_PROJECT"):
        assert f'system attribute "{name}"' in text


def test_the_script_finds_its_files_in_the_app_that_runs_it():
    """main.scpt runs inside the app's executable, not as an applet: `path to
    resource` looks in the script's own bundle, which it has none of, and
    stopped the first build with "A resource wasn't found" (-192)."""
    code = [l for l in APPLESCRIPT.read_text().splitlines() if not l.lstrip().startswith("--")]
    assert not [l for l in code if "path to resource" in l]
    assert any("path to current application" in l for l in code)


def test_the_providers_are_install_sh_s():
    text = APPLESCRIPT.read_text()
    ids = re.search(r'property providerIds : \{([^}]*)\}', text).group(1)
    ids = [s.strip().strip('"') for s in ids.split(",") if s.strip().strip('"')]
    rows = set(re.findall(r'^\s+([a-z|]+)\) echo "', INSTALL.read_text(), re.M))
    names = {n for row in rows for n in row.split("|")}
    assert ids and set(ids) <= names


# ── the build, on a Mac ─────────────────────────────────────────────────────

@pytest.mark.skipif(not ON_A_MAC, reason="swiftc, osacompile, codesign and hdiutil are a Mac's")
class TestTheBuild:
    @pytest.fixture(scope="class")
    def built(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("mac")
        copy = _stamped(tmp)
        out = tmp / "dist"
        r = subprocess.run(["bash", str(BUILD), REL, "--unsigned", "--installer", str(copy),
                            "--root", str(ROOT), "--out", str(out)],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr
        dmg = out / f"Korely-Installer-{REL}-unsigned.dmg"
        mnt = tmp / "mnt"
        mnt.mkdir()
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mnt),
                        str(dmg)], check=True, capture_output=True)
        try:
            yield copy, mnt, r.stdout
        finally:
            subprocess.run(["hdiutil", "detach", str(mnt)], capture_output=True)

    def test_the_bundled_install_sh_is_the_release_copy(self, built):
        copy, mnt, _ = built
        res = mnt / "Korely Installer.app" / "Contents" / "Resources"
        assert (res / "install.sh").read_bytes() == copy.read_bytes()
        assert (res / "launcher.sh").read_bytes() == LAUNCHER.read_bytes()
        assert (res / "AppIcon.icns").stat().st_size > 10_000
        assert (res / "Scripts" / "main.scpt").stat().st_size > 1000
        for name in re.findall(r'resourcePath\("([^"]+)"\)', APPLESCRIPT.read_text()):
            assert (res / name).is_file(), name

    def test_the_executable_runs_on_both_architectures_from_macos_13(self, built):
        _, mnt, _ = built
        exe = mnt / "Korely Installer.app" / "Contents" / "MacOS" / "Korely Installer"
        archs = subprocess.run(["lipo", "-archs", str(exe)], capture_output=True, text=True)
        assert set(archs.stdout.split()) == {"arm64", "x86_64"}
        build = subprocess.run(["vtool", "-show-build", str(exe)], capture_output=True, text=True)
        assert build.stdout.count("minos 13.0") == 2, build.stdout

    def test_the_info_plist(self, built):
        _, mnt, _ = built
        info = plistlib.loads((mnt / "Korely Installer.app" / "Contents" / "Info.plist").read_bytes())
        assert info["CFBundleIdentifier"] == "ai.korely.installer"
        assert info["CFBundleName"] == info["CFBundleDisplayName"] == "Korely Installer"
        assert info["CFBundleShortVersionString"] == info["CFBundleVersion"] == REL
        assert info["CFBundleExecutable"] == "Korely Installer"
        assert info["CFBundleIconFile"] == "AppIcon" and "CFBundleIconName" not in info
        assert info["LSMinimumSystemVersion"] == "13.0"
        assert not [k for k in info if k.endswith("UsageDescription")]

    def test_the_disk_image_has_the_applications_shortcut(self, built):
        _, mnt, out = built
        assert os.readlink(mnt / "Applications") == "/Applications"
        assert "unsigned: for a local test, never for a release" in out
