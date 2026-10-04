"""Official Codex app-server authorization and refresh, confined to the broker."""

from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import jwt

from control.auth_store import AuthDatabase, AuthStore
from control.codex_broker import InvalidGrant, TransientProviderError
from control.connections import ConnectionStore, SecretVault
from control.hosted_auth import HostedAuthError


class NativeRPC:
    def __init__(self, home, binary, timeout=40):
        env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
        env.update(
            HOME=str(home.parent),
            CODEX_HOME=str(home),
            XDG_CONFIG_HOME=str(home.parent / ".config"),
        )
        self.process = subprocess.Popen(
            [binary, "app-server"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.timeout, self.sequence = timeout, 0
        self.messages = queue.Queue(maxsize=256)
        self.notifications = []
        self.lock = threading.Lock()

        def read():
            try:
                for line in self.process.stdout:
                    self.messages.put(json.loads(line), timeout=1)
            except Exception:
                pass
            finally:
                try:
                    self.messages.put_nowait(None)
                except queue.Full:
                    pass

        self.reader = threading.Thread(target=read, daemon=True, name="sbx-native-auth-rpc")
        self.reader.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "sbx-control-plane", "version": "0.1"}})
            self.process.stdin.write('{"method":"initialized"}\n')
            self.process.stdin.flush()
        except Exception:
            self.close()
            raise

    def call(self, method, params):
        with self.lock:
            self.sequence += 1
            identifier = self.sequence
            try:
                self.process.stdin.write(
                    json.dumps({"id": identifier, "method": method, "params": params}) + "\n"
                )
                self.process.stdin.flush()
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
                    if message is None:
                        raise TransientProviderError()
                    if message.get("id") != identifier:
                        self.notifications.append(message)
                        continue
                    if "error" in message:
                        # Classify native diagnostics in memory; never return their text.
                        diagnostic = str(message["error"].get("message", "")).lower()
                        if any(
                            marker in diagnostic
                            for marker in (
                                "invalid_grant",
                                "refresh token has",
                                "revoked",
                                "reauth",
                                "expired refresh",
                                "not authenticated",
                            )
                        ):
                            raise InvalidGrant()
                        raise TransientProviderError()
                    return message.get("result", {})
            except (OSError, queue.Empty):
                raise TransientProviderError() from None
        raise TransientProviderError()

    def events(self):
        result, self.notifications = self.notifications, []
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            if message is not None:
                result.append(message)
        return result

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()


def native_credentials(home):
    """Read only an explicitly authorized product login, never the operator login."""
    path = home / "auth.json"
    if path.stat().st_mode & 0o077:
        raise ValueError("native auth file must be private")
    data = json.loads(path.read_text())
    tokens = data.get("tokens") or {}
    if not all(
        tokens.get(key) for key in ("access_token", "refresh_token", "id_token", "account_id")
    ):
        raise InvalidGrant()
    try:
        expiry = jwt.decode(tokens["access_token"], options={"verify_signature": False})["exp"]
    except Exception:
        raise InvalidGrant() from None
    return {
        **{key: tokens[key] for key in ("access_token", "refresh_token", "id_token", "account_id")},
        "expires_at": float(expiry),
        "models": ["gpt-6.1-sol", "gpt-5.6-luna"],
    }


def write_native_auth(home, credentials):
    # Only a transient private broker directory on tmpfs, never a sandbox.
    data = {
        "auth_mode": "chatgpt",
        "last_refresh": datetime.now(UTC).isoformat(),
        "tokens": {
            key: credentials[key]
            for key in ("access_token", "refresh_token", "id_token", "account_id")
        },
    }
    fd = os.open(home / "auth.json", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)


class NativeCodexProvider:
    configured = True
    mock = False

    def __init__(
        self,
        *,
        binary=None,
        root=None,
        rpc_factory=NativeRPC,
        bootstrap_home=None,
        import_root=None,
    ):
        self.binary = binary or os.environ.get("SBX_CODEX_CONTROL_BIN") or shutil.which("codex")
        if not self.binary:
            raise ValueError("install the pinned official Codex CLI on the control plane")
        self.root = Path(
            root or os.environ.get("SBX_CODEX_BROKER_TMP", "/dev/shm/sbx-codex-broker")
        )
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.rpc_factory = rpc_factory
        self.pending, self.lock = {}, threading.Lock()
        self.bootstrap_home = Path(bootstrap_home).resolve() if bootstrap_home else None
        self.bootstrap_owner = None
        self.import_root = Path(
            import_root or os.environ.get("SBX_CODEX_IMPORT_ROOT", "/home/zheng/.config/sbx")
        ).resolve()

    def bind_store(self, store):
        self.store = store

    @contextmanager
    def private_home(self):
        with tempfile.TemporaryDirectory(prefix="auth-", dir=self.root) as directory:
            home = Path(directory) / "codex"
            home.mkdir(mode=0o700)
            yield home

    def authorize(self, owner, state):
        directory = tempfile.TemporaryDirectory(prefix="login-", dir=self.root)
        home = Path(directory.name) / "codex"
        home.mkdir(mode=0o700)
        rpc = None
        try:
            rpc = self.rpc_factory(home, self.binary)
            result = rpc.call("account/login/start", {"type": "chatgptDeviceCode"})
            if result.get(
                "verificationUrl"
            ) != "https://auth.openai.com/codex/device" or not result.get("userCode"):
                raise TransientProviderError()
            with self.lock:
                self._expire()
                if len(self.pending) >= 32:
                    raise HostedAuthError("authorization_capacity", 429)
                self.pending[state] = {
                    "owner": owner,
                    "expires": time.time() + 600,
                    "rpc": rpc,
                    "directory": directory,
                    "home": home,
                    "ready": False,
                }
            return {
                "authorization_url": result["verificationUrl"],
                "user_code": result["userCode"],
                "state": state,
                "mock": False,
                "device": True,
            }
        except Exception:
            if rpc:
                rpc.close()
            directory.cleanup()
            raise HostedAuthError("codex_authorization_unavailable", 503) from None

    def _expire(self):
        for state, pending in list(self.pending.items()):
            if pending["expires"] <= time.time():
                pending["rpc"].close()
                pending["directory"].cleanup()
                del self.pending[state]

    def poll(self, owner, state):
        with self.lock:
            self._expire()
            pending = self.pending.get(state)
            if not pending or pending["owner"] != owner:
                raise HostedAuthError("invalid_authorization", 403)
            for event in pending["rpc"].events():
                if event.get("method") == "account/login/completed":
                    if not event.get("params", {}).get("success"):
                        raise HostedAuthError("codex_authorization_failed", 400)
                    pending["ready"] = True
            return pending["ready"]

    def exchange(self, owner, code):
        if not self.poll(owner, code):
            raise HostedAuthError("authorization_pending", 409)
        with self.lock:
            pending = self.pending.pop(code)
        try:
            credentials = native_credentials(pending["home"])
            if credentials["expires_at"] <= self.store.auth.clock():
                raise InvalidGrant()
            return credentials
        finally:
            pending["rpc"].close()
            pending["directory"].cleanup()

    def import_login(self, owner, directory):
        home = Path(directory).resolve()
        # Operator login directories are never eligible product import sources.
        if home == Path.home() / ".codex" or not home.is_relative_to(self.import_root):
            raise ValueError(
                "use an explicit dedicated product login under the secure SBX config root"
            )
        if home.stat().st_mode & 0o077:
            raise ValueError("dedicated product login directory must be private")
        credentials = native_credentials(home)
        rpc = self.rpc_factory(home, self.binary)
        try:
            account = rpc.call("account/read", {"refreshToken": False}).get("account")
            if not account or account.get("type") != "chatgpt":
                raise InvalidGrant()
        finally:
            rpc.close()
        if self.bootstrap_home:
            if home != self.bootstrap_home:
                raise ValueError("bootstrap source mismatch")
            self.bootstrap_owner = owner
        return self.store.connect(
            owner,
            "codex",
            credentials,
            metadata_factory=lambda version: {
                "credential_version": version,
                "expires_at": credentials["expires_at"],
                "models": credentials["models"],
                "authorization_mechanism": "official_codex_app_server",
            },
        )

    def _refresh_at(self, home, credentials, *, materialize):
        if materialize:
            write_native_auth(home, credentials)
        elif not secrets.compare_digest(
            native_credentials(home)["refresh_token"], credentials["refresh_token"]
        ):
            raise InvalidGrant()
        rpc = self.rpc_factory(home, self.binary)
        try:
            result = rpc.call("account/read", {"refreshToken": True})
            if not result.get("account") or result["account"].get("type") != "chatgpt":
                raise InvalidGrant()
            replacement = native_credentials(home)
            if replacement["expires_at"] <= self.store.auth.clock():
                raise InvalidGrant()
            if all(
                secrets.compare_digest(replacement[key], credentials[key])
                for key in ("access_token", "refresh_token")
            ):
                # account/read can return cached account metadata after a failed
                # refresh. Native status suppresses tokens on permanent failure.
                status = rpc.call("getAuthStatus", {"includeToken": True, "refreshToken": False})
                if not status.get("authToken"):
                    raise InvalidGrant()
                raise TransientProviderError()
            return replacement
        finally:
            rpc.close()

    def refresh(self, owner, refresh_token):
        record = self.store.get(owner, "codex")
        if record is None:
            raise InvalidGrant()
        credentials = self.store.credentials(record)
        if not secrets.compare_digest(credentials["refresh_token"], refresh_token):
            raise InvalidGrant()
        # The opt-in gate refreshes the original dedicated native cache through
        # the official CLI so destroying its temporary DB never loses rotation.
        if self.bootstrap_home:
            if owner != self.bootstrap_owner:
                raise InvalidGrant()
            return self._refresh_at(self.bootstrap_home, credentials, materialize=False)
        with self.private_home() as home:
            return self._refresh_at(home, credentials, materialize=True)

    def stop(self):
        with self.lock:
            for pending in self.pending.values():
                pending["rpc"].close()
                pending["directory"].cleanup()
            self.pending.clear()


def main():
    parser = argparse.ArgumentParser(
        description="Import an approved dedicated product login into the encrypted broker"
    )
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--codex-home", required=True)
    args = parser.parse_args()
    provider = NativeCodexProvider()
    provider.bind_store(
        ConnectionStore(
            AuthStore(AuthDatabase(database_url=os.environ["DATABASE_URL"])), SecretVault.from_env()
        )
    )
    provider.import_login(args.user_id, args.codex_home)
    print("Dedicated product login validated and encrypted in the broker")


if __name__ == "__main__":
    main()
