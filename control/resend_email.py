"""Production verification delivery. Provider response bodies never leave this adapter."""

from __future__ import annotations

import json
import os
import re
from email.utils import parseaddr

import httpx

from control.auth_email import EmailDeliveryUnavailable


def _address(value: str, domain: str) -> str:
    if any(c in value for c in "\r\n\x00"):
        raise ValueError("invalid verification sender address")
    display, address = parseaddr(value)
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@" + re.escape(domain), address):
        raise ValueError("verification sender must use the configured domain")
    if value != address and (
        not re.fullmatch(r"[A-Za-z0-9 ._-]{1,60}", display) or value != f"{display} <{address}>"
    ):
        raise ValueError("invalid verification sender display name")
    return value


class ResendEmailSender:
    def __init__(
        self, key: str, sender: str, *, domain="sbx-agent.com", reply_to=None, client=None
    ):
        if not key or not re.fullmatch(r"[a-z0-9.-]+", domain):
            raise ValueError("verification email configuration is incomplete")
        self._key = key
        self.domain = domain
        self.sender = _address(sender, domain)
        self.reply_to = _address(reply_to, domain) if reply_to else None
        self._client = client

    @classmethod
    def from_env(cls):
        return cls(
            os.environ.get("RESEND_API_KEY", ""),
            os.environ.get("SBX_AUTH_EMAIL_FROM", ""),
            domain=os.environ.get("SBX_AUTH_EMAIL_DOMAIN", "sbx-agent.com"),
            reply_to=os.environ.get("SBX_AUTH_EMAIL_REPLY_TO") or None,
        )

    def _request(self, method, path, **kwargs):
        headers = {"Authorization": f"Bearer {self._key}"}
        try:
            if self._client is not None:
                return self._client.request(method, path, headers=headers, **kwargs)
            with httpx.Client(
                base_url="https://api.resend.com",
                timeout=15,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                return client.request(method, path, headers=headers, **kwargs)
        except Exception:
            raise EmailDeliveryUnavailable("verification email delivery unavailable") from None

    def send_verification(self, *, email: str, code: str, expires_in_s: int) -> None:
        payload = {
            "from": self.sender,
            "to": [email],
            "subject": "Verify your SBX Agent email",
            "text": (
                f"Your SBX Agent verification code is {code}.\n\n"
                f"It expires in {max(1, expires_in_s // 60)} minutes. "
                "If you did not request it, you can ignore this email."
            ),
        }
        if self.reply_to:
            payload["reply_to"] = self.reply_to
        response = self._request("POST", "/emails", json=payload)
        try:
            delivered = response.status_code in {200, 201, 202} and bool(response.json()["id"])
        except Exception:
            delivered = False
        if not delivered:
            raise EmailDeliveryUnavailable("verification email delivery unavailable") from None

    def preflight(self):
        """Read-only key/domain probe. Return only bounded, non-secret diagnostics."""
        try:
            response = self._request("GET", "/domains")
            if response.status_code != 200:
                return {"provider": "resend", "ready": False, "error": "provider_unavailable"}
            domain = next(
                (d for d in response.json()["data"] if d.get("name") == self.domain), None
            )
            ready = bool(domain and domain.get("status") == "verified")
            return {
                "provider": "resend",
                "domain": self.domain,
                "ready": ready,
                "error": None if ready else "domain_not_verified",
            }
        except Exception:
            return {"provider": "resend", "ready": False, "error": "provider_unavailable"}


if __name__ == "__main__":
    try:
        result = ResendEmailSender.from_env().preflight()
    except ValueError:
        result = {"provider": "resend", "ready": False, "error": "configuration_incomplete"}
    print(json.dumps(result))
    raise SystemExit(0 if result["ready"] else 1)
