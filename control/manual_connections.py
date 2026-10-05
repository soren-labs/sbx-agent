"""Manual integration secrets in the existing encrypted, owner-bound SQL store."""

from __future__ import annotations

import json
import re

import httpx

from control.connections import ConnectionStore
from control.github_remote import RemoteGitHub, RemoteGitHubError
from control.hosted_auth import HostedAuthError, _write


class ManualConnections:
    def __init__(self, store: ConnectionStore, *, github=None, zen=None):
        self.store = store
        self.providers = {
            "github_token": github or GitHubTokenProvider(),
            "opencode": zen or ZenProvider(),
        }

    def connect(self, owner: str, provider: str, secret: str):
        secret = secret.strip()
        if not secret or len(secret) > 4096 or any(c.isspace() for c in secret):
            raise HostedAuthError("integration_secret_required", 422)
        metadata = self.providers[provider].validate(secret)
        record = self.store.connect(
            owner, provider, {"secret": secret}, metadata_factory=lambda _: metadata
        )
        self._clear_health(record)
        return record

    def _clear_health(self, record):
        from control.postgres_state import DatabaseRecords

        DatabaseRecords(self.store.auth.database).delete(
            "connection_health", record.id, owner=record.user_id
        )

    def validate(self, owner: str, provider: str):
        record = self.require(owner, provider)
        try:
            metadata = self.providers[provider].validate(self.store.credentials(record)["secret"])
        except HostedAuthError as exc:
            if exc.status in (400, 401, 403, 422):
                record.state = "invalid"
                record.metadata = {"error": exc.code}
                self.store.save(record)
            raise
        record.state, record.metadata = "connected", metadata
        self.store.save(record)
        self._clear_health(record)
        return record

    def require(self, owner: str, provider: str):
        record = self.store.get(owner, provider)
        if record is None or record.state == "disabled" or record.credential_cipher is None:
            raise HostedAuthError(f"{provider}_connection_required", 409)
        return record

    def secret(self, owner: str, provider: str):
        record = self.require(owner, provider)
        if record.state != "connected":
            raise HostedAuthError(f"{provider}_connection_invalid", 409)
        return self.store.credentials(record)["secret"]

    def disable(self, owner: str, provider: str):
        with _write(self.store.auth, f"connection:{owner}:{provider}") as conn:
            record = self.store.get(owner, provider, conn=conn)
            if record is None:
                return None
            if provider == "modal":
                # Do not expire provisioning authority on a timer: a crashed
                # worker may still have created remote resources to reconcile.
                if record.state == "provisioning" or "lease_until" in record.metadata:
                    raise HostedAuthError("modal_provisioning_in_progress_retry_disconnect", 409)
                rows = self.store.auth.database.execute(
                    conn,
                    "SELECT namespace, payload FROM control_records WHERE owner = ? "
                    "AND namespace IN ('hosted_sandboxes', 'hosted_sandbox_creates')",
                    (owner,),
                ).fetchall()
                if any(
                    row["namespace"] == "hosted_sandbox_creates"
                    or json.loads(row["payload"]).get("state") != "released"
                    for row in rows
                ):
                    raise HostedAuthError("modal_resources_require_cleanup_before_disconnect", 409)
            record.state, record.credential_cipher, record.metadata = "disabled", None, {}
            return self.store.save(record, conn=conn)

    def blob(self, owner: str):
        key = self.secret(owner, "opencode")
        return opencode_blob(key)


def opencode_blob(key):
    return {
        "provider": "opencode",
        "files": {
            ".local/share/opencode/auth.json": json.dumps({"opencode": {"type": "api", "key": key}})
        },
    }


class GitHubTokenProvider:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=15, trust_env=False, follow_redirects=False)

    def request(self, secret, path):
        try:
            response = self.client.get(
                "https://api.github.com" + path,
                headers={
                    "Authorization": "Bearer " + secret,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except Exception:
            raise HostedAuthError("github_unavailable_retry_validation", 503) from None
        if response.status_code == 401:
            raise HostedAuthError("github_token_invalid_or_expired_replace_token", 401)
        if response.status_code == 403:
            if response.headers.get("x-ratelimit-remaining") == "0":
                raise HostedAuthError("github_rate_limited_retry_later", 503)
            raise HostedAuthError(
                "github_token_requires_repository_access_contents_and_pull_requests_write", 403
            )
        if response.status_code == 404:
            raise HostedAuthError(
                "github_repository_not_granted_check_token_selection_and_org_approval", 403
            )
        if response.status_code != 200:
            raise HostedAuthError("github_unavailable_retry_validation", 503)
        return response

    def repositories(self, secret):
        repos = []
        for page in range(1, 101):
            response = self.request(secret, f"/user/repos?per_page=100&page={page}&sort=full_name")
            rows = response.json()
            if not isinstance(rows, list):
                raise HostedAuthError("github_unavailable_retry_validation", 503)
            for row in rows:
                slug = row.get("full_name", "")
                if re.fullmatch(r"[\w.-]+/[\w.-]+", slug):
                    repos.append(
                        {"name": slug, "push": bool(row.get("permissions", {}).get("push"))}
                    )
            if len(rows) < 100:
                return repos
        raise HostedAuthError("github_repository_list_too_large", 422)

    def validate(self, secret):
        user = self.request(secret, "/user")
        scopes = user.headers.get("x-oauth-scopes")
        if scopes is not None and not {"repo", "public_repo"} & set(
            scopes.replace(" ", "").split(",")
        ):
            raise HostedAuthError(
                "github_token_requires_repo_scope_or_fine_grained_repository_permissions", 403
            )
        login = user.json().get("login")
        if not isinstance(login, str) or not re.fullmatch(r"[\w-]+", login):
            raise HostedAuthError("github_unavailable_retry_validation", 503)
        return {"login": login, "repositories": self.repositories(secret), "method": "manual_token"}


class ZenProvider:
    """Discover with the key, then probe inference: /models alone is not auth proof.

    Limit the MVP catalog to chat-completions models whose access was proved.
    models.dev supplies CLI transport/cost metadata, never account permissions.
    """

    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=30, trust_env=False, follow_redirects=False)

    def validate(self, secret):
        try:
            headers = {"Authorization": "Bearer " + secret}
            listing = self.client.get("https://opencode.ai/zen/v1/models", headers=headers)
            if listing.status_code in (401, 403):
                raise HostedAuthError("opencode_key_invalid_or_expired_replace_key", 401)
            listing.raise_for_status()
            catalog = self.client.get("https://models.dev/api.json")
            catalog.raise_for_status()
            provider_catalog = catalog.json()["opencode"]
            metadata = provider_catalog["models"]
            candidates = []
            for row in listing.json()["data"]:
                model = row["id"]
                details = metadata.get(model, {})
                package = details.get("provider", {}).get("npm", provider_catalog.get("npm"))
                if package != "@ai-sdk/openai-compatible" or details.get("tool_call") is not True:
                    continue
                cost = details.get("cost", {})
                free = cost.get("input") == 0 and cost.get("output") == 0
                candidates.append((not free, model))
            candidates.sort()
            usable, free_models = [], []
            for paid, model in candidates[:12]:
                if paid and usable:
                    break
                response = self.client.post(
                    "https://opencode.ai/zen/v1/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Hi"}],
                        "max_tokens": 1,
                    },
                )
                if response.status_code == 401:
                    raise HostedAuthError("opencode_key_invalid_or_expired_replace_key", 401)
                if response.status_code == 200:
                    usable.append("opencode/" + model)
                    if not paid:
                        free_models.append("opencode/" + model)
                elif response.status_code >= 500 or response.status_code == 429:
                    raise HostedAuthError("opencode_unavailable_retry_validation", 503)
            if not usable:
                raise HostedAuthError(
                    "opencode_no_accessible_coding_models_check_key_model_access_and_balance", 403
                )
            return {
                "models": usable,
                "free_models": free_models,
                "default_model": usable[0],
                "method": "zen_api_key",
            }
        except HostedAuthError:
            raise
        except Exception:
            raise HostedAuthError("opencode_unavailable_retry_validation", 503) from None


class TokenGitHubRemote(RemoteGitHub):
    def _request(self, method, path, *, body=None):
        try:
            return super()._request(method, path, body=body)
        except RemoteGitHubError as exc:
            message = "GitHub unavailable; retry the operation"
            if exc.status == 401:
                message = "GitHub token invalid or expired; replace it in Connections"
            elif exc.status in (403, 404):
                message = (
                    "Check token repository selection and organization approval; "
                    "grant Contents and Pull requests read/write (classic PAT: repo)"
                )
            raise RemoteGitHubError("repo_unavailable", message, status=exc.status) from None
