"""Globally unique opaque public identifiers.

IDs convey type, never authorization. Prefixes are the canonical vocabulary of
RFC 167 §02. Suffixes are UUIDv7 (time-ordered) rendered as lowercase hex.
"""

from __future__ import annotations

import secrets
import time

PREFIXES: dict[str, str] = {
    "user": "usr_",
    "workspace": "wsp_",
    "project": "prj_",
    "project_version": "pver_",
    "connection": "con_",
    "credential_version": "cred_",
    "session": "sess_",
    "message": "msg_",
    "turn": "turn_",
    "execution": "exec_",
    "executor_lease": "lease_",
    "worktree": "wt_",
    "snapshot": "snap_",
    "changeset": "cs_",
    "delivery": "dlv_",
    "delegation": "del_",
    "delegation_result": "res_",
    "service_instance": "svc_",
    "job": "job_",
    "job_attempt": "jatt_",
    "event": "evt_",
    "blob": "blob_",
    "api_key": "key_",
    "login_session": "lsess_",
    "email_verification": "ever_",
    "password_reset": "prst_",
    "operation": "op_",
    "merge_request": "mrq_",
    "wait_subscription": "wait_",
    "outbox_message": "out_",
    "effect": "eff_",
    "command_dedup": "ddp_",
    "delivery_step": "dstep_",
    "grant": "grt_",
    "environment_build": "ebld_",
    "service_desire": "svd_",
    "capacity_reservation": "cap_",
    "resource_fence": "rf_",
    "native_binding": "nb_",
    "import_record": "imp_",
    "audit_record": "aud_",
    "secret_binding": "sb_",
    "secret_version": "sv_",
    "refresh_claim": "rc_",
    "environment": "env_",
    "turn_message": "tm_",
    "message_part": "mp_",
    "worktree_operation": "wop_",
    "delegation_input": "di_",
    "delivery_target_claim": "dtc_",
    "connection_observation": "cobs_",
    "runtime_ingestion_offset": "rio_",
    "attachment": "att_",
    "file_upload": "fu_",
    "terminal": "pty_",
    "preview_grant": "pg_",
    "automation": "auto_",
    "webhook_endpoint": "whe_",
    "webhook_receipt": "whr_",
}


def uuid7() -> str:
    """UUIDv7 hex (RFC 9562 §5.7): 48-bit ms epoch + ver + 12-bit rand + var + 62-bit rand."""
    ms = time.time_ns() // 1_000_000
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (ms & 0xFFFFFFFFFFFF) << 80
    value |= 0x7 << 76  # version 7
    value |= rand_a << 64
    value |= 0b10 << 62  # variant 10
    value |= rand_b
    return f"{value:032x}"


def new_id(kind: str) -> str:
    """Mint a new prefixed public identifier, e.g. ``sess_01j…``."""
    return PREFIXES[kind] + uuid7()


def check_prefix(kind: str, value: str) -> str:
    if not value.startswith(PREFIXES[kind]):
        raise ValueError(f"expected {kind} id with prefix {PREFIXES[kind]!r}, got {value!r}")
    return value
