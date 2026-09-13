"""Artifacts and modal app logs must not contain plaintext tokens."""

from __future__ import annotations

from pathlib import Path

from tests.e2e_modal.helpers import (
    artifacts_dir,
    leak_reason,
    modal_app_logs,
    scan_path_for_leaks,
    write_json,
)


def test_artifacts_and_modal_logs_have_no_secrets() -> None:
    hits: list[str] = []
    root = artifacts_dir()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name == ".gitkeep":
            continue
        why = scan_path_for_leaks(path)
        if why:
            hits.append(f"{path.name}:{why}")
    logs = modal_app_logs("sbx-control", tail=500)
    (root / "modal_app_logs.txt").write_text(_sanitize_log_copy(logs), encoding="utf-8")
    why_logs = leak_reason(logs)
    if why_logs:
        hits.append(f"modal-app-logs:{why_logs}")
    write_json("no_secret_leak.json", {"hits": hits, "files_scanned": _count_files(root)})
    assert hits == [], f"secret-like material found: {hits}"


def _count_files(root: Path) -> int:
    return sum(1 for path in root.rglob("*") if path.is_file() and path.name != ".gitkeep")


def _sanitize_log_copy(text: str) -> str:
    """Persist logs for debugging without copying obvious token shapes."""
    from tests.e2e_modal.helpers import JWT_RE, SK_RE

    text = JWT_RE.sub("eyJ-REDACTED", text)
    text = SK_RE.sub("sk-REDACTED", text)
    return text[-200_000:]
