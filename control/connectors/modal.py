"""Modal connector: token-pair validation via a non-destructive
identity/workspace probe. Plaintext stays in the executor worker context
only — never runtime HOME, never events (RFC 167 §06)."""

from __future__ import annotations

from control.connectors.base import ConnectorResult


class ModalConnector:
    kind = "modal"

    def validate(self, fmt: str, payload: dict) -> ConnectorResult:
        if fmt != "token_pair":
            return ConnectorResult(
                ok=False, reason="unsupported_format", message="expected token_pair"
            )
        token_id = payload.get("token_id")
        token_secret = payload.get("token_secret")
        if not token_id or not token_secret:
            return ConnectorResult(
                ok=False, reason="invalid_payload", message="missing token fields"
            )
        try:
            # Import inside method: modal must never load on the hot path
            # (AGENTS.md rule).
            import modal

            client = modal.client.Client.from_credentials(token_id, token_secret)
            client.hello()
        except Exception as exc:
            msg = type(exc).__name__
            if "auth" in msg.lower() or "permission" in msg.lower() or "unauth" in msg.lower():
                return ConnectorResult(
                    ok=False,
                    reason="auth_failed",
                    message="Modal rejected the token pair",
                )
            return ConnectorResult(
                ok=False,
                reason="probe_failed",
                message=f"Modal probe failed ({msg})",
            )
        return ConnectorResult(
            ok=True,
            external_identity={"token_id_prefix": token_id[:4]},
            capabilities={
                "executor_worker": ["sandbox.create", "sandbox.exec", "sandbox.terminate"]
            },
        )
