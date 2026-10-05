"""Opt-in real acceptance. Only disposable PostgreSQL/state and sbx-e2e-test.

Secrets are read inside this process, submitted write-only through the product,
never passed to the server environment or saved in the evidence report.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from control.app import build
from psycopg.conninfo import make_conninfo

from sbx.sdk.client import ApiError, Client

REPOSITORY = "soren-labs/sbx-e2e-test"


class GateFailure(Exception):
    pass


def credentials():
    with Path("/home/zheng/.config/sbx/production-gate-user.json").open() as stream:
        user = json.load(stream)
    email, password = user["email"], user["password"]
    modal = {}
    with Path("/home/zheng/.config/sbx/real-integration.env").open() as stream:
        for line in stream:
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name in {
                "SBX_TEST_MODAL_TOKEN_ID",
                "SBX_TEST_MODAL_TOKEN_SECRET",
                "SBX_TEST_MODAL_WORKSPACE",
            }:
                modal[name] = value.strip().strip("\"'")
    zen = os.environ.get("OPENCODE_ZEN_API_KEY") or os.environ.get("OPENCODE_API_KEY")
    github = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if (
        not zen
        or not github
        or not modal.get("SBX_TEST_MODAL_TOKEN_ID")
        or not modal.get("SBX_TEST_MODAL_TOKEN_SECRET")
    ):
        raise GateFailure("required_credential_class_unavailable")
    return email, password, modal, zen, github


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.root = Path(tempfile.mkdtemp(prefix="sbx-gpt-real-mvp-"))
        self.root.chmod(0o700)
        self.schema = "mvp_gpt_" + uuid4().hex[:16]
        with psycopg.connect(args.dsn, autocommit=True) as connection:
            connection.execute("CREATE SCHEMA " + self.schema)
        self.dsn = make_conninfo(args.dsn, options="-c search_path=" + self.schema)
        self.state = self.root / "state"
        self.report = {
            "baseline": "bf8cbe065da2f4c6bbca3781b4dce90cc31d39d3",
            "code_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "private_state": str(self.root),
            "schema": self.schema,
            "worktree_dirty": bool(
                subprocess.check_output(["git", "status", "--porcelain"], text=True)
            ),
            "steps": {},
            "sessions": [],
            "repository": REPOSITORY,
            "cleanup": {},
        }
        self.server = None
        self.api = None
        self.delivery = None
        self.email, self.password, self.modal, self.zen, self.github = credentials()
        self.values = [
            self.password,
            self.zen,
            self.github,
            self.modal["SBX_TEST_MODAL_TOKEN_ID"],
            self.modal["SBX_TEST_MODAL_TOKEN_SECRET"],
        ]
        self.resources = build(self.dsn, self.state)
        self.user = self.resources.identity.register(self.email, self.password, verified=True)
        self.wid = self.user["workspace_id"]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = "http://127.0.0.1:" + str(self.port)

    def record(self, name, **evidence):
        self.report["steps"][name] = {"passed": True, **evidence}
        self.save()
        print("PASS " + name, flush=True)

    def save(self):
        value = json.dumps(self.report, indent=2, sort_keys=True) + "\n"
        if any(secret in value for secret in self.values):
            raise GateFailure("secret_in_evidence")
        path = self.root / "evidence.json"
        path.write_text(value)
        path.chmod(0o600)
        Path(self.args.report).write_text(value)

    def start(self):
        home = self.root / "server-home"
        home.mkdir(exist_ok=True, mode=0o700)
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8"}
        for name in ("CONFIG", "CACHE", "DATA", "STATE"):
            env["XDG_" + name + "_HOME"] = str(home / name.lower())
        log = self.root / "server.log"
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "control.serve",
                "--dsn",
                self.dsn,
                "--state-dir",
                str(self.state),
                "--port",
                str(self.port),
                "--insecure-local-cookie",
                "--console-dir",
                "console/dist",
            ],
            env=env,
            stdout=descriptor,
            stderr=descriptor,
            start_new_session=True,
        )
        os.close(descriptor)
        end = time.monotonic() + 30
        while time.monotonic() < end:
            try:
                if httpx.get(self.url + "/readyz").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.2)
        else:
            raise GateFailure("control_start_failed")
        self.api = Client(self.url)

        def inspect(response):
            response.read()
            if any(secret.encode() in response.content for secret in self.values):
                raise GateFailure("secret_in_http_response")

        self.api.http.event_hooks["response"] = [inspect]
        self.api.login(self.email, self.password)

    def stop(self):
        if self.server and self.server.poll() is None:
            os.killpg(self.server.pid, signal.SIGTERM)
            try:
                self.server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.server.pid, signal.SIGKILL)
                self.server.wait(timeout=5)
        if self.api:
            self.api.close()

    def wait_job(self, jid, deadline=900):
        end = time.monotonic() + deadline
        next_notice = time.monotonic() + 20
        while time.monotonic() < end:
            job = self.api.request("GET", "/api/jobs/" + jid)
            if job["state"] == "succeeded":
                return job
            if job["state"] in {"failed", "cancelled"}:
                raise GateFailure("job_" + str(job.get("last_error") or job["state"]))
            if time.monotonic() > next_notice:
                print("Waiting for " + job["kind"] + " (" + job["state"] + ")", flush=True)
                next_notice = time.monotonic() + 20
            time.sleep(0.5)
        raise GateFailure("job_deadline")

    def wait_idle(self, sid, deadline=700):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            session = self.api.sessions.get(sid)
            if not any(
                t["state"] in {"queued", "preparing", "running", "cancelling"}
                for t in session["turns"]
            ):
                return session
            time.sleep(0.5)
        raise GateFailure("session_idle_deadline")

    def connect(self, kind, credential):
        receipt = self.api.create(
            self.wid,
            "connections",
            {"kind": kind, "label": "GPT disposable benchmark", "credential": credential},
        )
        self.wait_job(receipt["job_id"], 120)
        safe = self.api.connections.get(receipt["connection_id"])
        if safe["health"] != "ready":
            raise GateFailure(kind + "_validation_" + safe["health"])
        self.record("connection_" + kind, connection_id=safe["id"])
        return safe["id"]

    def run(self):
        self.start()
        self.record("login", user_id=self.user["user_id"], workspace_id=self.wid)
        zen = self.connect("opencode_zen", {"api_key": self.zen})
        modal = self.connect(
            "modal",
            {
                "token_id": self.modal["SBX_TEST_MODAL_TOKEN_ID"],
                "token_secret": self.modal["SBX_TEST_MODAL_TOKEN_SECRET"],
            },
        )
        github = self.connect("github", {"token": self.github})
        listed = self.api.request("GET", f"/api/workspaces/{self.wid}/connections")["items"]
        if {c["kind"] for c in listed} != {"opencode_zen", "modal", "github"}:
            raise GateFailure("unexpected_connection_kind")
        self.record("no_codex_connection")
        models = self.api.request("GET", "/api/models?connection_id=" + zen)["models"]
        if not models:
            raise GateFailure("zen_catalog_empty")
        chosen = next((m for m in models if m["free"]), models[0])
        self.report["model"] = chosen["id"]
        self.record("model_discovery", model=chosen["id"], free=chosen["free"])
        if self.args.browser_package:
            safe_env = {
                "PATH": os.environ["PATH"],
                "HOME": str(self.root / "browser-home"),
                "LANG": "C.UTF-8",
                "PLAYWRIGHT_BROWSERS_PATH": self.args.browser_cache,
            }
            Path(safe_env["HOME"]).mkdir(mode=0o700)
            proof = subprocess.run(
                [
                    "node",
                    "scripts/browser_mvp.mjs",
                    self.args.browser_package,
                    str(self.root / "console.png"),
                ],
                input=json.dumps(
                    {
                        "url": self.url,
                        "email": self.email,
                        "password": self.password,
                        "secrets": self.values,
                        "model": chosen["id"],
                        "modal": modal,
                        "github": github,
                    }
                ),
                env=safe_env,
                text=True,
                capture_output=True,
                timeout=120,
            )
            browser = json.loads(proof.stdout)
            if not browser.get("passed"):
                raise GateFailure("browser_" + browser.get("stage", "unavailable"))
            self.record(
                "real_browser_login_composer_secret_scan",
                screenshot=str(self.root / "console.png"),
                **{k: v for k, v in browser.items() if k != "passed"},
            )
        with httpx.Client(headers={"Authorization": "Bearer " + self.github}, timeout=30) as gh:
            remote = gh.get("https://api.github.com/repos/" + REPOSITORY)
            if remote.status_code != 200:
                raise GateFailure("disposable_repository_unavailable")
            base_ref = remote.json()["default_branch"]
        project = self.api.create(
            self.wid,
            "projects",
            {
                "name": "GPT disposable acceptance",
                "repository": "https://github.com/" + REPOSITORY,
                "base_ref": base_ref,
                "spec": {},
            },
        )
        suffix = uuid4().hex[:8]
        module = "benchmark_gpt_" + suffix
        test = "test_" + module
        marker = "SBX_GPT_CONTINUITY_" + uuid4().hex
        self.report["test_module"] = test
        self.report["marker"] = marker
        prompt = (
            "Only modify this disposable repository by adding "
            + module
            + ".py and "
            + test
            + ".py. "
            "Implement answer() returning 42 in the first file. Add a real unittest.TestCase in th"
            "e second file "
            "that imports answer and asserts it equals 42. Store the unique marker "
            + marker
            + " as a comment in "
            + module
            + ".py. "
            "Run python -m unittest -v "
            + test
            + ". Do not modify other existing files. Do not commit, push or open a PR. "
            "Report the exact marker and successful test result in your final response."
        )
        accepted = self.api.create(
            self.wid,
            "sessions",
            {
                "title": "GPT real MVP",
                "project_version_id": project["project_version_id"],
                "provider_id": "opencode",
                "model": chosen["id"],
                "backend": "modal",
                "modal_connection_id": modal,
                "github_connection_id": github,
                "zen_connection_id": zen,
                "message": {"content": prompt},
            },
        )
        sid = accepted["session_id"]
        self.report["sessions"].append(sid)
        self.save()
        self.wait_job(accepted["job_id"])
        first = self.api.turns.get(accepted["turn_id"])
        if first["state"] != "succeeded" or not first["evidence_complete"]:
            raise GateFailure("first_turn_not_complete_success")
        self.record("official_opencode_first_turn", session_id=sid, turn_id=first["id"])
        follow = self.api.send(
            sid,
            "Report the unique marker saved during the first Turn. Run python -m unittest -v "
            + test
            + " again. Do not edit any files.",
        )
        self.wait_job(follow["job_id"])
        second = self.api.turns.get(follow["turn_id"])
        if second["state"] != "succeeded" or marker not in (second.get("outcome") or {}).get(
            "text", ""
        ):
            raise GateFailure("followup_marker_not_verified")
        with self.resources.uow.transaction() as repo:
            executions = repo.all(
                "SELECT native_id,lease_id FROM executions WHERE turn_id IN (%s,%s) ORDER BY creat"
                "ed_at",
                (first["id"], second["id"]),
            )
            lease = repo.one(
                "SELECT fingerprint,handle FROM executor_leases WHERE id=%s",
                (executions[0]["lease_id"],),
            )
        if (
            len(executions) != 2
            or not executions[0]["native_id"]
            or executions[0]["native_id"] != executions[1]["native_id"]
        ):
            raise GateFailure("native_continuity_mismatch")
        self.report["runtime_fingerprint"] = lease["fingerprint"]
        self.report["native_id"] = executions[0]["native_id"]
        self.record("native_followup", turn_id=second["id"], native_id=executions[1]["native_id"])
        terminal = self.api.sessions.action(sid, "terminals", {})
        self.wait_job(terminal["job_id"])
        tid = terminal["operation_id"]
        self.api.request(
            "POST",
            f"/api/sessions/{sid}/terminals/{tid}/inputs",
            {"content": "python -m unittest -v " + test + '; printf "SBX_TEST_EXIT=%s\\n" "$?"\n'},
        )
        end = time.monotonic() + 60
        while time.monotonic() < end:
            output = self.api.request("GET", f"/api/sessions/{sid}/terminals/{tid}")
            if "SBX_TEST_EXIT=0" in output["text"] and "OK" in output["text"]:
                break
            time.sleep(0.3)
        else:
            raise GateFailure("sandbox_tests_failed")
        self.wait_job(
            self.api.request("POST", f"/api/sessions/{sid}/terminals/{tid}/closures", {})["job_id"]
        )
        self.record("sandbox_tests", command="python -m unittest -v " + test)
        current = self.wait_idle(sid)
        captured = self.api.sessions.action(
            sid,
            "changesets",
            {
                "generation": current["worktree"]["generation"],
                "source_turn_id": second["id"],
                "origin": "explicit",
            },
        )
        self.wait_job(captured["job_id"])
        cs = self.api.changesets.get(captured["changeset_id"])
        if cs["state"] != "ready" or not cs["subject_digest"]:
            raise GateFailure("changeset_not_ready")
        self.record(
            "immutable_changeset", changeset_id=cs["id"], subject_digest=cs["subject_digest"]
        )
        shipping = self.api.changesets.action(
            cs["id"], "deliveries", {"transport": "pull_request", "base_ref": base_ref}
        )
        self.delivery = self.api.deliveries.get(shipping["delivery_id"])
        self.save()
        self.wait_job(shipping["job_id"])
        self.delivery = self.api.deliveries.get(shipping["delivery_id"])
        self.report["delivery"] = self.delivery
        self.save()
        self.record(
            "draft_delivery",
            delivery_id=self.delivery["id"],
            pr_url=self.delivery["pr_url"],
            head=self.delivery["mapped_head"],
        )
        spawned = self.api.sessions.action(
            sid,
            "delegations",
            {
                "changeset_id": cs["id"],
                "role": "review",
                "budget_seconds": 1800,
                "summary": (
                    "Inspect the two new Python files and their test, run the unittest, "
                    "and approve only if correct. Finish with the exact required JSON "
                    "object and no commentary."
                ),
            },
        )
        self.report["sessions"].append(spawned["child_session_id"])
        self.save()
        self.wait_job(spawned["job_id"])
        for attempt in range(3):
            try:
                result = self.api.wait_result(spawned["delegation_id"], deadline=60)
                break
            except (ApiError, GateFailure) as error:
                if getattr(error, "code", str(error)) != "output_contract_invalid" or attempt == 2:
                    raise
                correction = self.api.send(
                    spawned["child_session_id"],
                    "Your previous output failed the strict ResultContract. Use your actual "
                    "independent inspection/test evidence; do not invent results. Return ONLY "
                    "JSON with kind ReviewAssessment; verdict must be exactly one of approve, "
                    "request_changes, comment (approved is invalid); subject_digest must be "
                    + cs["subject_digest"]
                    + "; head_sha must be "
                    + json.dumps(cs["head_sha"])
                    + ". Include findings and checks lists. No other final prose.",
                )
                self.wait_job(correction["job_id"])
        else:
            raise GateFailure("child_contract_retry_exhausted")
        if not result["validated"] or result["subject_digest"] != cs["subject_digest"]:
            raise GateFailure("child_result_not_pinned")
        self.record(
            "independent_child_result",
            delegation_id=spawned["delegation_id"],
            child_session_id=spawned["child_session_id"],
            result_id=result["id"],
            verdict=result["verdict"],
        )
        self.wait_job(self.api.deliveries.action(self.delivery["id"], "refreshes", {})["job_id"])
        latest = self.api.deliveries.get(self.delivery["id"])
        try:
            self.api.deliveries.action(
                latest["id"],
                "merge-requests",
                {
                    "expected_version": latest["version"],
                    "subject_digest": "stale",
                    "expected_head": latest["mapped_head"],
                },
            )
        except ApiError as error:
            if error.code != "stale_subject":
                raise GateFailure("wrong_stale_subject_error")
        else:
            raise GateFailure("stale_subject_was_accepted")
        merge = self.api.deliveries.action(
            latest["id"],
            "merge-requests",
            {
                "expected_version": latest["version"],
                "subject_digest": latest["subject_digest"],
                "expected_head": latest["mapped_head"],
                "method": "squash",
            },
        )
        try:
            self.wait_job(merge["job_id"], 60)
        except GateFailure as error:
            if str(error) != "job_remote_not_mergeable":
                raise
        else:
            raise GateFailure("draft_merge_was_not_blocked")
        self.record(
            "exact_subject_and_merge_reconcile", stale_denied=True, draft_merge_blocked=True
        )
        current = self.wait_idle(sid)
        snap = self.api.sessions.action(
            sid, "snapshots", {"generation": current["worktree"]["generation"]}
        )
        self.wait_job(snap["job_id"])
        self.wait_job(self.api.sessions.action(sid, "executor/releases", {})["job_id"])
        third = self.api.send(
            sid,
            "After restoring the checkpoint, report the unique marker from the first Turn and run "
            "python -m unittest -v " + test + ". Do not edit files.",
        )
        self.wait_job(third["job_id"])
        final_turn = self.api.turns.get(third["turn_id"])
        if final_turn["state"] != "succeeded" or marker not in (
            final_turn.get("outcome") or {}
        ).get("text", ""):
            raise GateFailure("checkpoint_continuity_failed")
        with self.resources.uow.transaction() as repo:
            restored = repo.one(
                "SELECT native_id,lease_id FROM executions WHERE turn_id=%s", (third["turn_id"],)
            )
        if (
            restored["native_id"] != self.report["native_id"]
            or restored["lease_id"] == executions[0]["lease_id"]
        ):
            raise GateFailure("checkpoint_identity_not_replaced")
        self.record(
            "checkpoint_destroy_restore", snapshot_id=snap["snapshot_id"], turn_id=third["turn_id"]
        )
        self.stop()
        self.start()
        recovered = self.api.sessions.get(sid)
        if (
            recovered["turns"][-1]["id"] != third["turn_id"]
            or len(self.api.request("GET", f"/api/workspaces/{self.wid}/connections")["items"]) != 3
        ):
            raise GateFailure("restart_durability_failed")
        self.record("control_restart_connections_and_session")
        other_email = "isolation-" + uuid4().hex + "@example.test"
        other_password = uuid4().hex
        other = self.resources.identity.register(other_email, other_password, verified=True)
        outsider = Client(self.url)
        outsider.login(other_email, other_password)
        paths = [
            "/api/connections/" + modal,
            "/api/connections/" + zen,
            "/api/projects/" + project["project_id"],
            "/api/sessions/" + sid,
            "/api/sessions/" + sid + "/executor",
            "/api/sessions/" + sid + "/files",
            "/api/deliveries/" + self.delivery["id"],
            "/api/changesets/" + cs["id"],
        ]
        for path in paths:
            try:
                outsider.request("GET", path)
            except ApiError as error:
                if error.status not in {403, 404}:
                    raise GateFailure("wrong_owner_isolation_status")
            else:
                raise GateFailure("cross_owner_access_allowed")
        try:
            outsider.create(
                other["workspace_id"],
                "sessions",
                {"modal_connection_id": modal, "zen_connection_id": zen},
            )
        except ApiError as error:
            if error.status != 404:
                raise GateFailure("wrong_connection_binding_isolation")
        else:
            raise GateFailure("cross_owner_binding_allowed")
        outsider.close()
        self.record("second_owner_isolation", denied_resources=len(paths) + 1)
        try:
            self.api.request("DELETE", "/api/connections/" + modal, {"expected_version": 1})
        except ApiError as error:
            if error.code != "dependent_resources_active":
                raise GateFailure("wrong_disconnect_guard")
        else:
            raise GateFailure("disconnect_orphaned_compute")
        self.record("disconnect_retains_teardown_authority")
        self.cleanup()
        if any(
            secret.encode() in (self.root / "server.log").read_bytes() for secret in self.values
        ):
            raise GateFailure("secret_in_server_logs")
        with self.resources.uow.transaction() as repo:
            rows = repo.all("SELECT payload FROM session_events")
        if any(secret in json.dumps(rows, default=str) for secret in self.values):
            raise GateFailure("secret_in_events")
        self.record("no_secret_http_logs_events_evidence")
        self.report["passed"] = True
        self.save()

    def cleanup(self):
        if self.api:
            for sid in self.report["sessions"]:
                try:
                    session = self.api.sessions.get(sid)
                    for turn in session["turns"]:
                        if turn["state"] in {"queued", "preparing", "running", "cancelling"}:
                            self.api.turns.action(turn["id"], "cancellations", {})
                    self.wait_idle(sid, 60)
                    receipt = self.api.sessions.action(sid, "executor/releases", {})
                    if receipt.get("job_id"):
                        self.wait_job(receipt["job_id"], 120)
                except Exception:
                    self.report["cleanup"][sid] = "unresolved"
                else:
                    self.report["cleanup"][sid] = "released"
        if self.delivery and self.api:
            try:
                self.delivery = self.api.deliveries.get(self.delivery["id"])
            except Exception:
                pass
        if self.delivery and self.delivery.get("pr_number"):
            with httpx.Client(headers={"Authorization": "Bearer " + self.github}, timeout=30) as gh:
                base = "https://api.github.com/repos/" + REPOSITORY
                pr = gh.get(base + "/pulls/" + str(self.delivery["pr_number"]))
                if pr.status_code == 200 and ("sbx-delivery:" + self.delivery["id"]) in (
                    pr.json().get("body") or ""
                ):
                    closed = gh.patch(
                        base + "/pulls/" + str(self.delivery["pr_number"]), json={"state": "closed"}
                    )
                    self.report["cleanup"]["pr_closed"] = closed.status_code == 200
                    ref = gh.get(base + "/git/ref/heads/" + self.delivery["target_ref"])
                    if (
                        ref.status_code == 200
                        and ref.json()["object"]["sha"] == self.delivery["mapped_head"]
                    ):
                        deleted = gh.delete(base + "/git/refs/heads/" + self.delivery["target_ref"])
                        self.report["cleanup"]["branch_deleted"] = deleted.status_code == 204
        self.save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--browser-package")
    parser.add_argument("--browser-cache")
    args = parser.parse_args()
    acceptance = None
    try:
        acceptance = Acceptance(args)
        acceptance.run()
    except Exception as error:
        code = (
            error.code
            if isinstance(error, ApiError)
            else str(error)
            if isinstance(error, GateFailure)
            else type(error).__name__
        )
        print("FAIL " + code, flush=True)
        if acceptance:
            acceptance.report["failure"] = code
            acceptance.report["passed"] = False
            try:
                acceptance.cleanup()
            finally:
                acceptance.save()
        return 1
    finally:
        if acceptance:
            acceptance.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
