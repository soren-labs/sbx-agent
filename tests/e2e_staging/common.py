"""Shared pieces of the external staging client. Reads configuration from the
environment and never prints secret values."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from sbx.sdk import SBXClient, SBXError  # noqa: E402

PRODUCTION_HOSTS = frozenset({"sbx-agent.com", "www.sbx-agent.com", "api.sbx-agent.com"})
SESSION_PREFIX = "staging-acceptance"


@dataclass(frozen=True)
class Account:
    name: str
    email: str
    password: str


def staging_url() -> str:
    url = os.environ.get("SBX_STAGING_URL", "").rstrip("/")
    if not url:
        raise SystemExit("SBX_STAGING_URL is required")
    host = (urlparse(url).hostname or "").lower()
    if host in PRODUCTION_HOSTS:
        # These scripts create users, sessions and Modal sandboxes.
        raise SystemExit(f"Refusing to run staging tooling against production host {host}")
    return url


def account(name: str) -> Account:
    prefix = f"SBX_STAGING_USER_{name.upper()}"
    email, password = os.environ.get(f"{prefix}_EMAIL"), os.environ.get(f"{prefix}_PASSWORD")
    if not email or not password:
        raise SystemExit(f"{prefix}_EMAIL and {prefix}_PASSWORD are required")
    return Account(name, email, password)


class _LoopbackCookies(httpx.Client):
    """httpx drops ``Secure`` cookies on plain HTTP. An operator reaching staging through
    an SSH forward to loopback still needs the session cookie, as a browser would allow."""

    def request(self, method, url, **kw):  # type: ignore[no-untyped-def]
        for cookie in self.cookies.jar:
            cookie.secure = False
        return super().request(method, url, **kw)


def client(url: str) -> SBXClient:
    loopback = urlparse(url).hostname in ("127.0.0.1", "localhost")
    http = (_LoopbackCookies if loopback else httpx.Client)(base_url=url, timeout=120)
    return SBXClient(url, http=http)


def sign_in(url: str, who: Account) -> SBXClient:
    c = client(url)
    c.login(who.email, who.password)
    return c


__all__ = ["SESSION_PREFIX", "Account", "SBXClient", "SBXError", "account", "client", "sign_in"]
