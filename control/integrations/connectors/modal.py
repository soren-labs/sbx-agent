from control.domain.errors import DomainError


class ModalConnector:
    def validate(self, credential):
        import modal

        try:
            client = modal.Client.from_credentials(
                credential["token_id"], credential["token_secret"]
            )
            workspace = modal.Workspace.from_context(client=client)
            workspace.hydrate(client=client)
            return {"workspace": workspace.name, "scope": "compute", "runtime_provisioned": False}
        except Exception:
            raise DomainError("credential_invalid") from None
