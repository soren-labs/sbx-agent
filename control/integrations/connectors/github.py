import httpx

from control.domain.errors import DomainError


class GitHubConnector:
    def __init__(self, transport=None):
        self.transport = transport

    def client(self, credential):
        return httpx.Client(
            base_url="https://api.github.com",
            timeout=30,
            headers={
                "Authorization": "Bearer " + credential["token"],
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            transport=self.transport,
        )

    def validate(self, credential):
        with self.client(credential) as client:
            result = client.get("/user")
            if result.status_code in {401, 403}:
                raise DomainError("credential_invalid")
            if result.status_code != 200:
                raise DomainError("provider_unavailable")
            return {
                "login": result.json()["login"],
                "scope": "identity",
                "repository_access": "validate_per_effect",
            }

    def resolve_base(self, credential, repository, ref):
        path = repository.removeprefix("https://github.com/").removesuffix(".git")
        with self.client(credential) as client:
            response = client.get(f"/repos/{path}/commits/{ref}")
            if response.status_code != 200:
                raise DomainError("repository_unavailable")
            return response.json()["sha"]
