import hashlib
import hmac


def runtime_token(master: bytes, lease_id: str, generation: int) -> str:
    if len(master) < 32:
        raise ValueError("Runtime key must contain at least 32 bytes")
    return hmac.new(
        master, f"runtime:1:{lease_id}:{generation}".encode(), hashlib.sha256
    ).hexdigest()
