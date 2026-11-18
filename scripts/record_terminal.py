#!/usr/bin/env python3
r"""Record a real terminal session, as an asciicast v2 file.

    python3 scripts/record_terminal.py --out run.cast --cols 100 --rows 32 \
        --cwd DIR --line 'curl -fsSL https://... | sh'
    python3 scripts/record_terminal.py --out run.cast --cols 100 --rows 24 \
        --cwd DIR --steps steps.json

scripts/make_install_demo.sh runs the first form. It starts zsh on a pseudo-terminal of
that size, in DIR, with this process's environment; waits for the prompt;
pastes the line all at once, as somebody pasting it from the README would;
presses Enter after a moment; and records everything the terminal shows,
with the time it showed it, until the next prompt. Nothing is typed into the
recording afterwards and nothing is edited in it: what the command printed is
what the file holds. The pauses before Enter are the only ones made here.

Three markers ("m" events) say where the line was pasted, where Enter was
pressed and where the command ended, for the renderer's stills. The exit
status of the line is this script's exit status.

--steps (scripts/make_terminal_demos.sh) is a session of several commands,
typed a key at a time as somebody typing them would, each waited for the
same way. steps.json is a list of objects:

    {"type": "./korely status"}       typed, then Enter, then the next prompt
    {"type": "...", "paste": true}    pasted at once instead of typed
    {"type": "./korely logs", "send": [[4, "\u0003"]]}
                                      after Enter, wait 4 s and send Ctrl-C
    {"type": "read -rs K", "send": [[1.2, "<key>\r"]]}
                                      a key pasted into `read -s`: the cast
                                      holds what the screen showed, so the
                                      key, never echoed, is not in it
    "expect": 1                       the exit status the step must end with
                                      (default 0; null for any)
    "pause": 0.8                      seconds before the step starts
    "enter": 0.5                      seconds between the last key and Enter
    "typing": 0.06                    seconds per key for this step (default --typing)
    "at": "zsh"                       the prompt the step ends at: "zsh" (a
                                      number, the shell's), "py", or a word a
                                      program writes (below); default the first
    "until": "sh command", "why": "the writer at work"
                                      after the pause, wait (the screen still
                                      recorded) until the command, run outside
                                      the terminal and never shown, exits 0;
                                      a "wait:<why>" marker names the wait

A "\n" in "type" is Enter inside the command (a continuation line). The next
prompt is a zsh prompt or, inside `python3`, a Python one: PYTHONSTARTUP
makes the REPL write to the same file before each `>>>`, and a Python
prompt's status is "py". Any program can do the same: a line written to the
file $RECORD_MARK that is not a number (Claude Code's hooks write "agent"
when a session starts and when a turn ends) is a prompt, and ends the step
whatever its "expect". Markers "start" says where the shell's first prompt
was up, "typing" and "enter" where each command began and where its Enter
was pressed; "done" where the session ended.

The host's python3 (3.9 on a Mac), standard library only: this has to run
where Docker runs, outside any container, so that the recorded install is
the one a user would get on this machine.
"""
from __future__ import annotations

import argparse
import codecs
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

# The zsh startup file: a plain prompt, no prompt tricks, and a hook that
# writes the last status to a file before every prompt. The file, not the
# screen, says when the shell is waiting: nothing is looked for in the output.
ZSHRC = r"""
PROMPT='$ '
RPROMPT=''
PS2='> '
unsetopt PROMPT_CR PROMPT_SP
precmd() { print -r -- $? >> "$RECORD_MARK" }
"""

# The Python REPL's startup file, for --steps: the prompt is an object whose
# text is ">>> " and which writes "py" to the same file each time it is shown.
PYSTARTUP = r"""
import os as _os, sys as _sys
class _Prompt:
    def __str__(self):
        with open(_os.environ["RECORD_MARK"], "a") as f:
            f.write("py\n")
        return ">>> "
_sys.ps1 = _Prompt()
del _Prompt
"""


def _winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _prompts(mark: Path) -> list[str]:
    return mark.read_text().split() if mark.exists() else []


def write_cast(out: Path, events: list, cols: int, rows: int, env: dict) -> None:
    header = {"version": 2, "width": cols, "height": rows, "timestamp": int(time.time()),
              "env": {"SHELL": "/bin/zsh", "TERM": env.get("TERM", "")}}
    with out.open("w") as f:
        f.write(json.dumps(header) + "\n")
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def record(out: Path, line, cols: int, rows: int, cwd: Path,
           pause_before: float, pause_enter: float, tail: float, limit: float,
           steps=None, typing: float = 0.03, extra_env=None) -> int:
    zdot = Path(tempfile.mkdtemp(prefix="record-zsh."))
    (zdot / ".zshrc").write_text(ZSHRC)
    (zdot / "pystartup.py").write_text(PYSTARTUP)
    mark = zdot / "prompts"
    env = dict(os.environ, **(extra_env or {}), ZDOTDIR=str(zdot), RECORD_MARK=str(mark),
               PYTHONSTARTUP=str(zdot / "pystartup.py"),
               COLUMNS=str(cols), LINES=str(rows))

    pid, fd = pty.fork()
    if pid == 0:
        _winsize(0, rows, cols)
        os.chdir(cwd)
        os.execve("/bin/zsh", ["zsh", "-i"], env)

    decode = codecs.getincrementaldecoder("utf-8")(errors="replace")
    events: list[list] = []
    t0 = time.monotonic()

    def now() -> float:
        return round(time.monotonic() - t0, 6)

    def pump(seconds: float, until=None) -> bool:
        """Record for `seconds`, or until `until()` holds; False at EOF."""
        end = time.monotonic() + seconds
        while True:
            if until is not None and until():
                return True
            left = end - time.monotonic()
            if left <= 0:
                return until is None
            r, _, _ = select.select([fd], [], [], min(left, 0.05))
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    return False
                text = decode.decode(data)
                if text:
                    events.append([now(), "o", text])

    def write(data: bytes) -> None:
        os.write(fd, data)

    def session() -> int:
        """--steps: each command typed, its Enter, its keys, its next prompt."""
        failed = 0
        for i, step in enumerate(steps):
            pump(float(step.get("pause", 0.8)))
            if step.get("until"):
                events.append([now(), "m", "wait:" + step.get("why", "")])
                end = time.monotonic() + limit
                while subprocess.run(["sh", "-c", step["until"]], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                    if time.monotonic() > end:
                        raise SystemExit(f"record_terminal.py: step {i + 1}'s wait did not "
                                         f"end within {limit:.0f} s")
                    pump(0.5)
            seen = len(_prompts(mark))
            text = step["type"]
            events.append([now(), "m", "typing"])
            parts = text.split("\n")
            for n, part in enumerate(parts):
                if step.get("paste"):
                    write(part.encode())
                    pump(0.4)
                else:
                    for k, ch in enumerate(part):
                        write(ch.encode())
                        # A steady hand with a little unevenness, the same at every run.
                        pump(float(step.get("typing", typing))
                             * (0.7 + 0.6 * ((k * 7 + i * 3) % 5) / 4))
                if n < len(parts) - 1:
                    write(b"\r")
                    pump(0.25)
            pump(float(step.get("enter", 0.5)))
            events.append([now(), "m", "enter"])
            write(b"\r")
            for delay, keys in step.get("send", []):
                pump(float(delay))
                write(keys.encode())
            at = step.get("at")

            def ended():
                return [p for p in _prompts(mark)[seen:]
                        if at is None or p == at or (at == "zsh" and p.lstrip("-").isdigit())]

            if not pump(limit, lambda: bool(ended())):
                raise SystemExit(f"record_terminal.py: step {i + 1} ({text[:40]!r}) "
                                 f"did not end within {limit:.0f} s")
            got = ended()[0]
            want = step.get("expect", 0)
            if want is not None and got.lstrip("-").isdigit() and got != str(want):
                print(f"record_terminal.py: step {i + 1} ({text[:40]!r}) exited {got}, "
                      f"expected {want}", file=sys.stderr)
                failed = 1
        pump(tail)
        events.append([now(), "m", "done"])
        return failed

    status = 1
    try:
        if not pump(30, lambda: len(_prompts(mark)) >= 1):
            raise SystemExit("record_terminal.py: zsh showed no prompt in 30 s")
        if steps is not None:
            pump(0.2)
            events.append([now(), "m", "start"])
            try:
                status = session()
            finally:
                # A session that did not go as written is kept too, to be looked at.
                write_cast(out, events, cols, rows, env)
            return status
        pump(pause_before)
        write(line.encode())
        events.append([now(), "m", "pasted"])
        pump(pause_enter)
        if len(_prompts(mark)) != 1:
            raise SystemExit("record_terminal.py: zsh showed a second prompt before Enter")
        events.append([now(), "m", "enter"])
        write(b"\r")
        if not pump(limit, lambda: len(_prompts(mark)) >= 2):
            raise SystemExit(f"record_terminal.py: the line did not end within {limit:.0f} s")
        pump(tail)
        events.append([now(), "m", "done"])
        status = int(_prompts(mark)[1])
        write_cast(out, events, cols, rows, env)
    finally:
        # The master is closed before the shell is waited for: on macOS a
        # process leaving a terminal whose output nobody reads never exits.
        for step in (lambda: os.write(fd, b"exit\r"), lambda: os.close(fd),
                     lambda: os.kill(pid, 9)):
            try:
                step()
            except OSError:
                pass
        for _ in range(50):
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
            except OSError:
                break
            time.sleep(0.1)
        for p in zdot.iterdir():
            p.unlink()
        zdot.rmdir()
    return status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--line", help="the command line pasted at the prompt")
    what.add_argument("--steps", type=Path, help="a JSON list of commands to type (see above)")
    ap.add_argument("--typing", type=float, default=0.03, help="seconds per key, --steps")
    ap.add_argument("--cwd", type=Path, required=True)
    ap.add_argument("--cols", type=int, default=100)
    ap.add_argument("--rows", type=int, default=32)
    ap.add_argument("--pause-before", type=float, default=1.5)
    ap.add_argument("--pause-enter", type=float, default=1.6)
    ap.add_argument("--tail", type=float, default=1.0)
    ap.add_argument("--limit", type=float, default=1800)
    ap.add_argument("--env-file", type=Path,
                    help="NAME=value lines added to the session's environment: a secret "
                         "the session needs reaches it without a command line `ps` shows")
    a = ap.parse_args(argv)
    steps = json.loads(a.steps.read_text()) if a.steps else None
    extra = {}
    if a.env_file:
        for row in a.env_file.read_text().splitlines():
            name, sep, value = row.partition("=")
            if sep and name.strip():
                extra[name.strip()] = value
    return record(a.out, a.line, a.cols, a.rows, a.cwd, a.pause_before, a.pause_enter,
                  a.tail, a.limit, steps=steps, typing=a.typing, extra_env=extra)


if __name__ == "__main__":
    sys.exit(main())
