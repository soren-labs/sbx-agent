"""A private, unprivileged OpenSSH server for real resident-machine tests.

It listens on 127.0.0.1 with its own host key and authorized_keys in the test's tmp dir and
accepts only the test's own client key, so the resident executor is exercised over real SSH
with strict host-key checking. Tests skip when ``sshd``/``ssh`` are not installed.
"""

from __future__ import annotations

import getpass
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SSHD_CANDIDATES = ("/usr/sbin/sshd", "/usr/local/sbin/sshd", "/opt/homebrew/sbin/sshd")


def sshd_binary() -> str | None:
    for path in SSHD_CANDIDATES:
        if Path(path).exists():
            return path
    return shutil.which("sshd")


def available() -> bool:
    return bool(sshd_binary() and shutil.which("ssh") and shutil.which("ssh-keygen"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _keygen(path: Path) -> None:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
        check=True,
        capture_output=True,
    )


class PrivateSshd:
    def __init__(self, base: Path) -> None:
        self.base = base
        base.mkdir(parents=True, exist_ok=True)
        base.chmod(0o700)
        self.port = _free_port()
        self.host_key = base / "host_ed25519"
        self.client_key = base / "client_ed25519"
        self.known_hosts = base / "known_hosts"
        _keygen(self.host_key)
        _keygen(self.client_key)
        (base / "authorized_keys").write_text(Path(f"{self.client_key}.pub").read_text())
        (base / "authorized_keys").chmod(0o600)
        public = Path(f"{self.host_key}.pub").read_text().split()
        self.known_hosts.write_text(f"[127.0.0.1]:{self.port} {public[0]} {public[1]}\n")
        self.config = base / "sshd_config"
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.config.write_text(
            "\n".join(
                [
                    f"Port {self.port}",
                    "ListenAddress 127.0.0.1",
                    f"HostKey {self.host_key}",
                    f"AuthorizedKeysFile {self.base / 'authorized_keys'}",
                    f"PidFile {self.base / 'sshd.pid'}",
                    "UsePAM no",
                    "StrictModes no",
                    "PasswordAuthentication no",
                    "KbdInteractiveAuthentication no",
                    "PubkeyAuthentication yes",
                    "AllowTcpForwarding local",
                    "",
                ]
            )
        )
        self.proc = subprocess.Popen(
            [sshd_binary() or "sshd", "-D", "-f", str(self.config), "-E", str(self.base / "log")],
            stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("private sshd did not start: " + (self.base / "log").read_text())

    def stop(self) -> None:
        """Take the machine's SSH endpoint offline, dropping established connections too.

        Runtimes the agent started are not affected: they left the SSH session (setsid)
        and were re-parented when the agent exited, so they are not sshd descendants.
        """
        if self.proc is None:
            return
        doomed = _descendants(self.proc.pid)
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        for pid in doomed:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.proc = None

    def target(self, state_root: Path, **overrides: Any) -> dict[str, Any]:
        return {
            "host": "127.0.0.1",
            "port": self.port,
            "user": getpass.getuser(),
            "identity_file": str(self.client_key),
            "known_hosts_file": str(self.known_hosts),
            "remote_python": sys.executable,
            "remote_root": str(REPO_ROOT),
            "state_root": str(state_root),
            "remote_path": "/usr/local/bin:/usr/bin:/bin",
            "cli_overrides": {},
            "connect_timeout": 5,
            **overrides,
        }


def _descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        ppid = int(stat[stat.rindex(")") + 2 :].split()[1])
        children.setdefault(ppid, []).append(int(entry.name))
    out, queue = [], [root]
    while queue:
        for child in children.get(queue.pop(), []):
            out.append(child)
            queue.append(child)
    return out
