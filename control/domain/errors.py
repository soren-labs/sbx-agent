class DomainError(Exception):
    def __init__(self, code: str, *, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise DomainError(code)
