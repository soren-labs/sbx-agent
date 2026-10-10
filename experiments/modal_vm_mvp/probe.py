"""Opt-in cloud MVP. No SBX imports, local files, secrets or credential forwarding."""

from __future__ import annotations

import argparse
import json
import signal
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import modal

PREFIX = "sbx-exp-vm-mvp-"
MOUNT = "/mvp-profile"
CAPABILITIES = r"""
set -eu
uname -a
printf 'boot_id='; cat /proc/sys/kernel/random/boot_id
printf 'pid1='; cat /proc/1/comm
printf 'pid_namespace='; readlink /proc/self/ns/pid
printf 'cgroup_filesystem='; stat -f -c %T /sys/fs/cgroup
unshare --mount --pid --fork --mount-proc sh -c 'printf "nested_pid="; echo $$; cat /proc/1/comm'
mkdir -p /tmp/mvp-mount
mount -t tmpfs tmpfs /tmp/mvp-mount
printf 'tmpfs_mount='; stat -f -c %T /tmp/mvp-mount
umount /tmp/mvp-mount
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-cloud", action="store_true", required=True)
    parser.add_argument("--label", default=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.label.replace("-", "").isalnum() or len(args.label) > 32:
        parser.error("label must be at most 32 alphanumeric/hyphen characters")
    if modal.__version__ != "1.6.1":
        parser.error("use isolated modal==1.6.1 for this reproducible probe")
    app_name = PREFIX + args.label
    volume_names = [app_name + "-slot-a", app_name + "-slot-b"]
    report = {
        "started_utc": datetime.now(UTC).isoformat(),
        "sdk": modal.__version__,
        "app": app_name,
        "volumes": volume_names,
        "runtime": "vm",
        "cpu": 1,
        "memory_mib": 1024,
        "timeout_seconds": 180,
        "events": [],
        "outcome": "PARTIAL",
    }
    active = {}
    app = None

    def record(event: str, **data):
        item = {"event": event, **data}
        report["events"].append(item)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(item), flush=True)

    def stop(sb):
        started = time.monotonic()
        code = sb.terminate(wait=True)
        active.pop(sb.object_id, None)
        record("terminated", id=sb.object_id, returncode=code, seconds=time.monotonic() - started)

    def execute(sb, command):
        process = sb.exec("sh", "-c", "set -eu\n" + command, timeout=30)
        out = process.stdout.read()
        err = process.stderr.read()
        process.wait()
        record(
            "exec",
            id=sb.object_id,
            command=command,
            stdout=out,
            stderr=err,
            returncode=process.returncode,
        )
        if process.returncode:
            raise RuntimeError("probe command failed; see recorded synthetic output")
        return out

    def launch(volume, image, stage):
        started = time.monotonic()
        sb = modal.Sandbox.create(
            "sleep",
            "180",
            app=app,
            image=image,
            runtime="vm",
            cpu=1,
            memory=1024,
            timeout=180,
            volumes={MOUNT: volume},
            env={"HOME": MOUNT, "CODEX_HOME": MOUNT + "/.codex"},
            secrets=[],
            block_network=True,
            include_oidc_identity_token=False,
            name=stage + "-" + uuid.uuid4().hex[:8],
            tags={"experiment": app_name},
        )
        active[sb.object_id] = sb
        record("created", stage=stage, id=sb.object_id, create_seconds=time.monotonic() - started)
        boot_id = execute(sb, "cat /proc/sys/kernel/random/boot_id").strip()
        record(
            "ready",
            stage=stage,
            id=sb.object_id,
            boot_id=boot_id,
            create_to_exec_seconds=time.monotonic() - started,
        )
        return sb, boot_id

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        app = modal.App.lookup(app_name, create_if_missing=True)
        volumes = [
            modal.Volume.from_name(name, create_if_missing=True, version=2) for name in volume_names
        ]
        # No local source, environment, directories or credentials enter the image.
        image = modal.Image.debian_slim(python_version="3.12")
        first, first_boot = launch(volumes[0], image, "first-unbuilt-image")
        execute(first, CAPABILITIES)
        marker = "synthetic-profile-" + uuid.uuid4().hex
        execute(
            first,
            f"mkdir -p {MOUNT}/.codex; printf '%s\\n' '{marker}' > {MOUNT}/.codex/mvp-marker; sync {MOUNT}",
        )
        # A root-only marker must disappear when a genuinely fresh VM boots.
        execute(first, "touch /tmp/mvp-ephemeral-marker")
        first_id = first.object_id
        stop(first)
        started = time.monotonic()
        built = image.build(app)
        record(
            "image_build_after_first", image_id=built.object_id, seconds=time.monotonic() - started
        )
        second, second_boot = launch(volumes[0], built, "second-built-image")
        assert second.object_id != first_id and second_boot != first_boot
        actual = execute(
            second, f"test ! -e /tmp/mvp-ephemeral-marker; cat {MOUNT}/.codex/mvp-marker"
        ).strip()
        assert actual == marker
        record("fresh_vm_persistence_pass", first_id=first_id, second_id=second.object_id)
        # Keep A running while B is created: independent synthetic slots, not accounts.
        other, _ = launch(volumes[1], built, "concurrent-slot-b")
        assert second.poll() is None and other.poll() is None
        execute(second, f"printf 'slot-a\\n' > {MOUNT}/slot-a-only; sync {MOUNT}")
        execute(
            other,
            f"test ! -e {MOUNT}/.codex/mvp-marker; test ! -e {MOUNT}/slot-a-only; printf 'slot-b\\n' > {MOUNT}/slot-b-only; sync {MOUNT}",
        )
        execute(
            second, f'test ! -e {MOUNT}/slot-b-only; test "$(cat {MOUNT}/slot-a-only)" = slot-a'
        )
        execute(other, f'test ! -e {MOUNT}/slot-a-only; test "$(cat {MOUNT}/slot-b-only)" = slot-b')
        record("concurrent_volume_isolation_pass", ids=[second.object_id, other.object_id])
        report["outcome"] = "PASS"
    except (Exception, KeyboardInterrupt) as exc:
        # Don't print cloud exception messages that might contain credential context.
        record("failure", exception_type=type(exc).__name__)
    finally:
        for sb in list(active.values()):
            try:
                stop(sb)
            except Exception as exc:
                report["outcome"] = "PARTIAL"
                record("cleanup_failure", id=sb.object_id, exception_type=type(exc).__name__)
        # App-scoped sweep covers a create response lost before handle registration.
        try:
            remaining = []
            if app is None:
                raise RuntimeError("no experiment app handle; refusing workspace-wide cleanup")
            for sb in modal.Sandbox.list(app_id=app.app_id):
                if sb.poll() is None:
                    stop(sb)
                if sb.poll() is None:
                    remaining.append(sb.object_id)
            if remaining:
                report["outcome"] = "PARTIAL"
            record("cleanup_verified", running_ids=remaining)
        except Exception as exc:
            report["outcome"] = "PARTIAL"
            record("cleanup_verification_failure", exception_type=type(exc).__name__)
        report["finished_utc"] = datetime.now(UTC).isoformat()
        record("complete", outcome=report["outcome"])
    return 0 if report["outcome"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
