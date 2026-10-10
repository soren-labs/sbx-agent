"""Generic bring-your-own-key inference connector.

One Connection is an API key, a default model and one base URL per wire protocol the
provider speaks (OpenAI Chat Completions, OpenAI Responses, Anthropic Messages). It is
not tied to a vendor or to a Harness: any official CLI that can be pointed at one of
the offered protocols may use it.

Validation sends one minimal generation request per endpoint with the configured
model, so it proves key, base URL, protocol and model together. It can count against
provider quota and is recorded as quota-consuming. Base URLs are caller-controlled, so
they must be public HTTPS origins: private, loopback and link-local targets are refused
before any request leaves the control plane, and redirects are never followed.

A ready Connection also records, per model and protocol, whether the provider's own
"reasoning off" switch measurably works (two tiny requests each). That is the only basis
on which a thinking control is ever offered for a custom API model.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import httpx
from protocol.capabilities import INFERENCE_PROTOCOLS, REASONING_OFF

from control.domain.errors import DomainError
from control.integrations.connectors.base import Observation, require

KIND = "inference_api"
FORMAT = "inference_api/v1"
SECRET_FIELDS = ("api_key",)
MAX_MODELS = 200
HEADERS = {"User-Agent": "sbx-agent/0.2 (+inference connection probe)"}
ANTHROPIC_VERSION = "2023-06-01"
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_PATH = re.compile(r"^[A-Za-z0-9._~/@:+-]*$")


def _allow_private() -> bool:
    """Operator opt-in for self-hosted gateways on private networks (and plain HTTP)."""
    return os.environ.get("SBX_INFERENCE_ALLOW_PRIVATE_URLS") == "1"


def _bad(field: str, message: str) -> DomainError:
    # Input values are never echoed back.
    return DomainError("validation_failed", message, details={"field": f"credential.{field}"})


def _public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _base_url(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise _bad(field, "base URL is missing or malformed")
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        raise _bad(field, "base URL is missing or malformed") from None
    private_ok = _allow_private()
    if parts.scheme != "https" and not (private_ok and parts.scheme == "http"):
        raise _bad(field, "base URL must use https")
    host = parts.hostname or ""
    literal = host.strip("[]")
    is_ip = True
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        is_ip = False
    if (
        parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or not _PATH.match(parts.path)
        or not (is_ip or _HOST.match(host))
    ):
        raise _bad(field, "base URL must be a plain origin and path without credentials or query")
    if not private_ok and (
        (is_ip and not _public(literal))
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
    ):
        raise _bad(field, "base URL must be a public address")
    netloc = f"[{literal}]" if ":" in literal else host.lower()
    if port is not None:
        netloc += f":{port}"
    return f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}"


def _model(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _MODEL.match(value.strip()):
        raise _bad(field, "model id is missing or malformed")
    return value.strip()


def normalize(credential: dict[str, Any]) -> dict[str, Any]:
    require(credential, "api_key", min_len=8)
    raw = credential.get("endpoints")
    if raw is None and credential.get("base_url") is not None:
        raw = {credential.get("protocol") or "openai_chat": credential.get("base_url")}
    if not isinstance(raw, dict) or not raw:
        raise _bad("endpoints", "at least one protocol endpoint is required")
    endpoints: dict[str, str] = {}
    for protocol in INFERENCE_PROTOCOLS:
        if raw.get(protocol):
            endpoints[protocol] = _base_url(raw[protocol], f"endpoints.{protocol}")
    if not endpoints or set(raw) - set(INFERENCE_PROTOCOLS):
        raise _bad("protocol", f"protocol must be one of {', '.join(INFERENCE_PROTOCOLS)}")
    model = _model(credential.get("model"), "model")
    extra = credential.get("models") or []
    if not isinstance(extra, list) or len(extra) > MAX_MODELS:
        raise _bad("models", "models must be a bounded list of model ids")
    models = [model] + [m for m in dict.fromkeys(_model(m, "models") for m in extra) if m != model]
    return {
        "api_key": credential["api_key"].strip(),
        "endpoints": endpoints,
        "model": model,
        "models": models,
    }


def public_config(material: dict[str, Any]) -> dict[str, Any]:
    """The non-secret part of the material, safe for views and runtime payloads."""
    return {k: material[k] for k in ("endpoints", "model", "models") if k in material}


def _resolves_public(base_url: str) -> bool:
    if _allow_private():
        return True
    parts = urlsplit(base_url)
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return True  # unresolvable is a reachability error reported by the probe itself
    return all(_public(info[4][0]) for info in infos)


def _request(protocol: str, base_url: str, material: dict[str, Any]) -> tuple[str, dict, dict]:
    key, model = material["api_key"], material["model"]
    ping = [{"role": "user", "content": "ping"}]
    if protocol == "anthropic_messages":
        return (
            f"{base_url}/v1/messages",
            {**HEADERS, "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
            {"model": model, "max_tokens": 1, "messages": ping},
        )
    headers = {**HEADERS, "Authorization": f"Bearer {key}"}
    if protocol == "openai_responses":
        body = {"model": model, "input": "ping", "max_output_tokens": 16}
        return f"{base_url}/responses", headers, body
    return (
        f"{base_url}/chat/completions",
        headers,
        {"model": model, "messages": ping, "max_tokens": 1},
    )


def _probe(http: Any, protocol: str, base_url: str, material: dict[str, Any]) -> dict[str, Any]:
    if not _resolves_public(base_url):
        return {"status": "invalid", "reason": "inference_base_url_not_public"}
    url, headers, body = _request(protocol, base_url, material)
    try:
        response = http.post(url, json=body, headers=headers, timeout=60, follow_redirects=False)
    except httpx.HTTPError as exc:
        return {"status": "error", "reason": f"inference_unreachable:{type(exc).__name__}"}
    code = response.status_code
    out: dict[str, Any] = {"http_status": code}
    if code == 200:
        return {**out, "status": "ready"}
    if code in (401, 403):
        return {**out, "status": "invalid", "reason": "inference_rejected_key"}
    if code == 429:
        out["retry_after"] = _retry_after(response)
        return {**out, "status": "degraded", "reason": "inference_rate_limited"}
    if code in (400, 404, 405, 422):
        # Wrong base URL/protocol pairing or a model the provider does not serve.
        return {**out, "status": "invalid", "reason": "inference_endpoint_or_model_rejected"}
    if 300 <= code < 400:
        return {**out, "status": "invalid", "reason": "inference_base_url_redirects"}
    return {**out, "status": "error", "reason": "inference_unexpected"}


def _retry_after(response: Any) -> float:
    try:
        return max(1.0, min(float(response.headers.get("retry-after") or 60), 3600.0))
    except ValueError:
        return 60.0


def _catalog(http: Any, material: dict[str, Any]) -> dict[str, Any]:
    """Configured models first, then whatever the provider's model list adds."""
    ids = list(material["models"])
    source = "configured"
    endpoints, key = material["endpoints"], material["api_key"]
    for protocol in INFERENCE_PROTOCOLS:
        base_url = endpoints.get(protocol)
        if not base_url:
            continue
        if protocol == "anthropic_messages":
            url = f"{base_url}/v1/models"
            headers = {**HEADERS, "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        else:
            url, headers = f"{base_url}/models", {**HEADERS, "Authorization": f"Bearer {key}"}
        try:
            response = http.get(url, headers=headers, timeout=20, follow_redirects=False)
            listed = response.json().get("data") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            listed = None
        if isinstance(listed, list):
            found = [m.get("id") for m in listed if isinstance(m, dict)]
            ids += [m for m in found if isinstance(m, str) and _MODEL.match(m)]
            source = "configured + provider model list"
            break
    models = [{"id": m} for m in dict.fromkeys(ids)][:MAX_MODELS]
    return {
        "models": models,
        "preferred_model": material["model"],
        "protocols": list(endpoints),
        "source": source,
    }


REASONING_PROMPT = "What is 17 * 23? Think step by step, then give the number."
REASONING_MODELS = 3
REASONING_SOURCE = "default vs. reasoning-disabled request per endpoint"


def _reasoning_call(
    http: Any, protocol: str, base_url: str, key: str, model: str, off: bool
) -> int | None:
    """Reasoning the provider reports for one tiny request; ``None`` when it cannot be told."""
    ask = [{"role": "user", "content": REASONING_PROMPT}]
    headers = {**HEADERS, "Authorization": f"Bearer {key}"}
    if protocol == "anthropic_messages":
        url = f"{base_url}/v1/messages"
        headers = {**HEADERS, "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        body: dict[str, Any] = {"model": model, "max_tokens": 64, "messages": ask}
        if off:
            body["thinking"] = {"type": "disabled"}
    elif protocol == "openai_responses":
        url = f"{base_url}/responses"
        body = {"model": model, "input": REASONING_PROMPT, "max_output_tokens": 64}
        if off:
            body["reasoning"] = {"effort": REASONING_OFF}
    else:
        url = f"{base_url}/chat/completions"
        body = {"model": model, "messages": ask, "max_tokens": 64}
        if off:
            body["reasoning_effort"] = REASONING_OFF
    try:
        response = http.post(url, json=body, headers=headers, timeout=45, follow_redirects=False)
        if response.status_code != 200:
            return None
        data = response.json()
        if protocol == "anthropic_messages":
            blocks = data.get("content") or []
            return sum(len(b.get("thinking") or "") for b in blocks if b.get("type") == "thinking")
        usage = data.get("usage") or {}
        if protocol == "openai_responses":
            return int((usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
        message = ((data.get("choices") or [{}])[0]).get("message") or {}
        counted = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        return int(counted or 0) or len(message.get("reasoning_content") or "")
    except Exception:
        return None


def _reasoning(http: Any, material: dict[str, Any], models: list[str]) -> dict[str, dict[str, str]]:
    """Per model and protocol: ``toggle`` only when the model reasons by default and the
    provider's own off switch measurably stops it; ``none`` when it does not reason;
    ``unverified`` otherwise. Nothing is assumed from the provider's name."""
    key = material["api_key"]
    jobs = [
        (model, protocol, base_url, off)
        for model in models[:REASONING_MODELS]
        for protocol, base_url in material["endpoints"].items()
        if _resolves_public(base_url)
        for off in (False, True)
    ]
    with ThreadPoolExecutor(max_workers=6) as pool:
        used = list(pool.map(lambda j: _reasoning_call(http, j[1], j[2], key, j[0], j[3]), jobs))
    seen = {(m, p, off): n for (m, p, _, off), n in zip(jobs, used, strict=True)}
    out: dict[str, dict[str, str]] = {}
    for model in models[:REASONING_MODELS]:
        for protocol in material["endpoints"]:
            default, off = seen.get((model, protocol, False)), seen.get((model, protocol, True))
            if default == 0:
                kind = "none"
            elif default and off == 0:
                kind = "toggle"
            else:
                kind = "unverified"
            out.setdefault(model, {})[protocol] = kind
    return out


def validate(credential: dict[str, Any], *, client: Any = None) -> Observation:
    http = client or httpx
    probes = {
        protocol: _probe(http, protocol, base_url, credential)
        for protocol, base_url in credential["endpoints"].items()
    }
    details: dict[str, Any] = {
        "probe": "minimal generation request per endpoint",
        "model": credential["model"],
        "endpoints": {
            p: {k: v for k, v in r.items() if k != "retry_after"} for p, r in probes.items()
        },
    }
    for status in ("invalid", "error", "degraded"):
        failed = [r for r in probes.values() if r["status"] == status]
        if failed:
            return Observation(
                status,
                details={**details, "reason": failed[0]["reason"]},
                quota_consuming=True,
                retry_after=failed[0].get("retry_after"),
            )
    catalog = _catalog(http, credential)
    reasoning = _reasoning(http, credential, [m["id"] for m in catalog["models"]])
    catalog["models"] = [
        {**m, "reasoning": reasoning[m["id"]]} if m["id"] in reasoning else m
        for m in catalog["models"]
    ]
    catalog["reasoning_source"] = REASONING_SOURCE
    return Observation("ready", details=details, catalog=catalog, quota_consuming=True)
