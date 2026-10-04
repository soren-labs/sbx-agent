"""GitHub App installation tokens with explicit control-plane owner approval."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import httpx
import jwt

from control.auth_store import AuthDatabase, AuthStore
from control.connections import ConnectionStore, SecretVault
from control.github_app import GitHubAppClient, GitHubAppConfig, GitHubAppError
from control.hosted_github import HostedGitHubService
from control.postgres_state import DatabaseRecords
from control.tasks import canonicalize_repo


class SafeAppClient(GitHubAppClient):
    def _jwt(self):
        # Allow clock skew at both ends while keeping the total JWT window at 10 minutes.
        now = int(self._clock())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self._config.app_id},
            self._config.private_key,
            algorithm="RS256",
        )

    def _request(self, method, path, *, authorization, json_body=None, expected=(200,), what):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if authorization:
            headers["Authorization"] = authorization
        try:
            response = self._client.request(
                method, self._api_url + path, headers=headers, json=json_body
            )
            if response.status_code not in expected:
                error = GitHubAppError(
                    "github_app_upstream",
                    "GitHub App request failed",
                    status_code=response.status_code,
                )
                error.operation = what
                raise error
            return response.json() if response.content else None
        except GitHubAppError:
            raise
        except Exception:
            raise GitHubAppError(
                "github_unavailable", "GitHub unavailable", status_code=503
            ) from None

    def installation(self, installation_id):
        return self._request(
            "GET",
            f"/app/installations/{int(installation_id)}",
            authorization="Bearer " + self._jwt(),
            what="installation",
        )


class BoundAppClient:
    """Only server-approved installations are visible to this owner."""

    def __init__(self, source, records, owner):
        self.source, self.records, self.owner = source, records, owner

    def binding(self, installation_id):
        return self.records.get(
            "github_bindings", f"{self.owner}:{installation_id}", owner=self.owner
        )

    def list_installations(self):
        result = []
        for _, binding in self.records.rows("github_bindings", owner=self.owner):
            try:
                result.append(self.source.installation(binding["installation_id"]))
            except GitHubAppError as error:
                if error.status_code != 404:
                    raise
        return result

    def create_installation_token(self, installation_id, *, repositories=None):
        binding = self.binding(installation_id)
        if not binding:
            raise GitHubAppError("not_found", "installation not found", status_code=404)
        approved = [repo.split("/", 1)[1] for repo in binding["repositories"]]
        if repositories and not set(repositories) <= set(approved):
            raise GitHubAppError("not_found", "repository not found", status_code=404)
        return self.source.create_installation_token(
            installation_id, repositories=repositories or approved
        )

    def installation_repositories(self, token):
        return self.source.installation_repositories(token)

    def delete_installation(self, installation_id):
        # Disconnect one SBX owner, not an installation shared with other owners.
        self.records.delete("github_bindings", f"{self.owner}:{installation_id}", owner=self.owner)
        return False


class RealGitHubService(HostedGitHubService):
    def __init__(self, connections, owner, source, config):
        records = DatabaseRecords(connections.auth.database)
        super().__init__(connections, owner, BoundAppClient(source, records, owner), config)

    def begin_authorization(self):
        result = super().begin_authorization()
        result["binding_mode"] = "operator_approved"
        return result

    def status(self):
        self.sync()
        return {**super().status(), "binding_mode": "operator_approved"}

    def sandbox_token(self, repo=None):
        # Observe repository selection, suspension and uninstall before every mint.
        self.sync()
        return super().sandbox_token(repo)

    def _record_installation(self, installation):
        record = super()._record_installation(installation)
        approved = self._client.binding(record.installation_id)["repositories"]
        record.repositories = sorted(set(record.repositories) & set(approved))
        self._store.put(record)
        return record


def config_from_env():
    path = Path(os.environ["SBX_GITHUB_APP_PRIVATE_KEY_PATH"])
    if path.stat().st_mode & 0o077:
        raise ValueError("GitHub App key file must be private")
    return GitHubAppConfig(
        os.environ["SBX_GITHUB_APP_ID"], os.environ["SBX_GITHUB_APP_SLUG"], path.read_text()
    )


class GitHubFactory:
    def __init__(self, config=None, client=None):
        self.config = config or config_from_env()
        self.client = client or SafeAppClient(self.config)
        # Disable ambient proxy credentials for the credentialed REST client.
        if client is None:
            self.client._client.close()
            self.client._client = httpx.Client(timeout=15, trust_env=False, follow_redirects=False)

    def __call__(self, connections, owner):
        return RealGitHubService(connections, owner, self.client, self.config)

    def bind_installation(self, connections, owner, installation_id, repositories):
        """Trusted operator operation, intentionally not exposed through browser APIs."""
        with connections.auth.database.transaction() as conn:
            connections.auth._require_user(conn, owner)
        installation = self.client.installation(installation_id)
        if installation.get("suspended_at"):
            raise GitHubAppError("not_found", "installation suspended", status_code=404)
        approved = sorted({canonicalize_repo(repo).slug for repo in repositories})
        if not approved or any(not repo for repo in approved):
            raise ValueError("explicit GitHub repositories required")
        token, _ = self.client.create_installation_token(installation_id)
        _, visible = self.client.installation_repositories(token)
        if not set(approved) <= set(visible):
            raise GitHubAppError("not_found", "repository not granted", status_code=404)
        DatabaseRecords(connections.auth.database).put_owned(
            "github_bindings",
            f"{owner}:{installation_id}",
            owner,
            {"installation_id": installation_id, "repositories": approved},
        )
        service = self(connections, owner)
        state = service.begin_authorization()["state"]
        return service.complete_authorization(installation_id, state)


def main():
    parser = argparse.ArgumentParser(description="Operator-approved GitHub installation binding")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--installation-id", type=int, required=True)
    parser.add_argument("--repo", action="append", required=True)
    args = parser.parse_args()
    store = ConnectionStore(
        AuthStore(AuthDatabase(database_url=os.environ["DATABASE_URL"])), SecretVault.from_env()
    )
    GitHubFactory().bind_installation(store, args.user_id, args.installation_id, args.repo)
    print("GitHub installation binding validated and persisted")


if __name__ == "__main__":
    main()
