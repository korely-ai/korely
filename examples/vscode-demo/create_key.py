"""Step 2: an account, its key, and a login for the dashboard.

What the README's step 5 does by hand:

    python create_key.py                                    # the install two directories up
    python create_key.py --ssh root@host --dir /root/korely-agent \\
                         --url https://203-0-113-7.nip.io   # an install on a server

It writes KORELY_BASE_URL and KORELY_API_KEY to .env, and the dashboard login
to .demo-login, both readable by you only.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def manage(args: list[str], ssh: str | None, where: str) -> str:
    cmd = ["docker", "compose", "exec", "-T", "api", "python", "-m", "korely_agent.manage", *args]
    if ssh:
        quoted = " ".join(f"'{a}'" for a in cmd)
        # `--ssh "-i ~/.ssh/key root@host"` works too: split like a shell would.
        full, cwd = ["ssh", *shlex.split(ssh), f"cd '{where}' && {quoted}"], None
    else:
        full, cwd = cmd, where
    # stdin closed: `exec -T` under ssh otherwise waits on it for ever.
    r = subprocess.run(full, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"manage {args[0]} failed:\n{r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def field(out: str, name: str) -> str:
    m = re.search(rf"^{name}\s+(\S+)", out, re.M)
    if not m:
        sys.exit(f"could not read '{name}' in:\n{out}")
    return m.group(1)


def local_url() -> str:
    port = "8000"
    env = REPO / ".env"
    if env.exists():
        m = re.search(r"^PORT=(\d+)", env.read_text(), re.M)
        port = m.group(1) if m else port
    return f"http://127.0.0.1:{port}"


def private_write(path: Path, text: str) -> None:
    path.write_text(text)
    os.chmod(path, 0o600)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssh", help="user@host of a server install, with any ssh options (default: this machine)")
    ap.add_argument("--dir", default=str(REPO), help="the install's directory")
    ap.add_argument("--url", help="the address the agents use (default: the local loopback)")
    a = ap.parse_args()
    url = a.url or local_url()

    out = manage(["create-tenant", "--name", f"Demo {time.strftime('%Y-%m-%d %H:%M')}"], a.ssh, a.dir)
    tenant, key = field(out, "tenant"), field(out, "key")
    print(f"account {tenant[:8]}… created")

    user = f"demo{time.strftime('%H%M%S')}"
    password = field(manage(["create-operator", "--tenant", tenant, "--username", user], a.ssh, a.dir),
                     "password")
    private_write(HERE / ".env", f"KORELY_BASE_URL={url}\nKORELY_API_KEY={key}\n")
    private_write(HERE / ".demo-login", f"dashboard {url}\nusername  {user}\npassword  {password}\n")
    # The password is not printed: a demo is often on a shared screen or
    # recorded. It is in .demo-login, which only you can read.
    print(f"\nkey written to .env; dashboard login in .demo-login (username {user}):\n"
          f"  {url}\n"
          "The store shows both once; they are kept here only for this demo.")


if __name__ == "__main__":
    main()
