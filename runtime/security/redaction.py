import re

SENSITIVE = re.compile(
    r"(^|_)(token|password|secret|api_key|authorization|cookie|credential)(_|$)", re.I
)
SIGNATURE = re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+)")


class Redactor:
    def __init__(self, values=()):
        self.values = tuple(v for v in values if isinstance(v, str) and v and v != "REDACTED")

    def clean(self, value):
        if isinstance(value, dict):
            return {
                k: "REDACTED" if SENSITIVE.search(k) else self.clean(v) for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in self.values:
                value = value.replace(secret, "REDACTED")
            return SIGNATURE.sub("REDACTED", value)
        return value
