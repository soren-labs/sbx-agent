"""Opaque prefixed identifiers with UUIDv7 suffixes (RFC 02)."""

from __future__ import annotations

import os
import time

PREFIXES: dict[str, str] = {
    "user": "usr",
    "workspace": "wsp",
    "project": "prj",
    "project_version": "pver",
    "connection": "con",
    "credential_version": "cred",
    "session": "sess",
    "message": "msg",
    "message_part": "part",
    "turn": "turn",
    "execution": "exec",
    "lease": "lease",
    "worktree": "wt",
    "worktree_operation": "wtop",
    "snapshot": "snap",
    "changeset": "cs",
    "delivery": "dlv",
    "delivery_step": "dstep",
    "merge_request": "mr",
    "delegation": "del",
    "result": "res",
    "wait": "wait",
    "service_instance": "svc",
    "service_desire": "sdes",
    "job": "job",
    "job_attempt": "jatt",
    "blob": "blob",
    "event": "evt",
    "outbox": "obx",
    "native_binding": "nctx",
    "reservation": "resv",
    "grant": "grant",
    "api_key": "key",
    "login_session": "lsn",
    "verification": "ver",
    "audit": "aud",
    "observation": "obs",
    "operation": "op",
    "machine_slot": "slot",
    "slot_login": "slogin",
}


def uuid7_hex() -> str:
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand >> 68 & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return f"{value:032x}"


def new_id(kind: str) -> str:
    return f"{PREFIXES[kind]}_{uuid7_hex()}"


def kind_of(identifier: str) -> str | None:
    prefix = identifier.split("_", 1)[0]
    for kind, value in PREFIXES.items():
        if value == prefix:
            return kind
    return None
