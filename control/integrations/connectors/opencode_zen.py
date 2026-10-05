import httpx

from control.domain.errors import DomainError


class ZenConnector:
    def __init__(self, transport=None):
        self.transport = transport

    def validate(self, credential):
        try:
            with httpx.Client(transport=self.transport, timeout=20) as client:
                response = client.get(
                    "https://opencode.ai/zen/v1/models",
                    headers={"Authorization": "Bearer " + credential["api_key"]},
                )
                if response.status_code in {401, 403}:
                    raise DomainError("credential_invalid")
                if response.status_code == 429:
                    raise DomainError("rate_limited")
                if response.status_code != 200:
                    raise DomainError("provider_unavailable")
                available = response.json()["data"]
                # Current cost metadata is connector discovery, never a model call.
                metadata = (
                    client.get("https://models.dev/api.json")
                    .json()
                    .get("opencode", {})
                    .get("models", {})
                )
                models = []
                for model in available:
                    name = model["id"]
                    if name.startswith("opencode/"):
                        name = name.split("/", 1)[1]
                    details = metadata.get(name, {})
                    costs = details.get("cost", {})
                    free = costs.get("input") == 0 and costs.get("output") == 0
                    models.append(
                        {
                            "id": "opencode/" + name,
                            "name": details.get("name", name),
                            "free": free,
                            "source": "zen-catalog+models.dev",
                            "availability": "catalog",
                            "connection_id": None,
                        }
                    )
                return {
                    "models": sorted(models, key=lambda m: (not m["free"], m["id"])),
                    "scope": "catalog",
                    "inference_verified": False,
                }
        except DomainError:
            raise
        except Exception:
            raise DomainError("provider_unavailable") from None
