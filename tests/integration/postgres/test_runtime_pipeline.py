"""Real PostgreSQL + wire HTTP + official-CLI-shaped subprocess, cloud-free."""

import os
import sys
from types import SimpleNamespace

import httpx
from control.application.assessments import result_rows
from control.composition import assemble
from control.runtime_client.client import RuntimeClient
from control.security.vault import EnvelopeVault
from control.storage.local import LocalObjects
from fastapi.testclient import TestClient
from runtime.daemon.app import Runtime, create_runtime_app
from runtime.harnesses.opencode import OpenCodeHarness


def test_full_pipeline_response_loss_resume_checkpoint_child_and_private_export(
    database, principal, tmp_path
):
    cli = tmp_path / "cli.py"
    cli.write_text("""import json,sys,os,pathlib,subprocess,shlex
args=sys.argv
home=pathlib.Path(os.environ['HOME'])
root=pathlib.Path(args[args.index('--dir')+1])
native=home/'.local/share/opencode'
native.mkdir(parents=True,exist_ok=True)
(native/'opencode.db').write_text('native isolated fixture')
sid=args[args.index('--session')+1] if '--session' in args else 'native_'+home.parent.name
prompt=args[args.index('run')+1]
if 'ResultContract:' not in prompt:
 (root/'answer.py').write_text('def answer(): return 42\\n# marker-42\\n')
 result='marker-42'
else:
 contract=json.JSONDecoder().raw_decode(prompt.split('ResultContract: ',1)[1])[0]
 command=[sys.executable,'-c','from answer import answer; assert answer()==42']
 observed=subprocess.run(command,cwd=root,capture_output=True)
 check=shlex.join(command)
 print(json.dumps(dict(type='tool_use',sessionID=sid,part=dict(id='tool1',tool='bash',state=dict(status='completed',input=dict(command=check),metadata=dict(exit=observed.returncode))))),flush=True)
 result=json.dumps(dict(
  kind=contract['kind'],subject_digest=contract['subject_digest'],head_sha=contract['head_sha'],
  verdict='approve',findings=[],
  checks=[dict(name='answer',status='pass',command=check,evidence='observed exit zero')]))
for obj in [dict(type='step_start',sessionID=sid),
            dict(type='text',sessionID=sid,part=dict(id='part',text=result)),
            dict(type='step_finish',sessionID=sid,part=dict(reason='stop'))]:
 print(json.dumps(obj),flush=True)
""")
    runtimes, stopped = {}, set()
    allocations = []
    lose = [True]

    class Backend:
        def __init__(self, token):
            self.token = token

        def lookup(self, operation):
            return operation if operation in runtimes else None

        def allocate(self, spec, operation):
            allocations.append(operation)
            runtimes[operation] = Runtime(
                tmp_path / spec.lease_id,
                spec.session_id,
                spec.lease_id,
                spec.generation,
                spec.runtime_token,
                lambda _: OpenCodeHarness(binary=(sys.executable, str(cli))),
            )
            if lose[0]:
                lose[0] = False
                raise RuntimeError("allocation committed but response lost")
            return operation

        def connect_runtime(self, handle):
            app = TestClient(create_runtime_app(runtimes[handle]))

            def bridge(request):
                response = app.request(
                    request.method,
                    str(request.url),
                    content=request.content,
                    headers=dict(request.headers),
                )
                return httpx.Response(
                    response.status_code, content=response.content, headers=response.headers
                )

            return RuntimeClient(
                "http://runtime", self.token, transport=httpx.MockTransport(bridge)
            )

        def terminate(self, handle, effect):
            stopped.add(handle)

        def describe(self, handle):
            return {"status": "stopped" if handle in stopped else "ready"}

    class Zen:
        def validate(self, material):
            assert material == {"api_key": "REDACTED"}
            return {"models": [{"id": "opencode/test", "free": True}]}

    master = os.urandom(32)
    r = assemble(
        database,
        EnvelopeVault({"1": master}),
        LocalObjects(tmp_path / "objects"),
        master,
        lambda session, lease, token: Backend(token),
        {"opencode_zen": Zen(), "github": SimpleNamespace()},
    )
    wid = principal.workspace_ids[0]
    connection = r.connections.create(
        principal, wid, {"kind": "opencode_zen", "credential": {"api_key": "REDACTED"}}, "zen"
    )
    assert r.worker.once()
    assert r.connections.safe(principal, connection["connection_id"])["health"] == "ready"
    created = r.sessions.create(
        principal,
        wid,
        {
            "backend": "local",
            "zen_connection_id": connection["connection_id"],
            "model": "opencode/test",
            "message": {"content": "code answer"},
        },
        "create",
    )
    assert r.worker.once()  # lost allocation response
    with database.transaction() as repo:
        repo.execute("UPDATE jobs SET due_at=now() WHERE state='retry_wait'")
    assert r.worker.once()
    sid = created["session_id"]
    assert r.sessions.get(principal, sid)["turns"][0]["state"] == "succeeded"
    assert len(allocations) == 1
    follow = r.sessions.send(principal, sid, {"content": "continue marker"}, "follow")
    assert r.worker.once()
    with database.transaction() as repo:
        native = repo.one(
            "SELECT native_id FROM executions WHERE turn_id=%s", (created["turn_id"],)
        )["native_id"]
        assert (
            repo.one("SELECT native_id FROM executions WHERE turn_id=%s", (follow["turn_id"],))[
                "native_id"
            ]
            == native
        )
    snap = r.worktrees.checkpoint(principal, sid, 2, "checkpoint")
    assert r.worker.once()
    r.worktrees.release(principal, sid, "release")
    assert r.worker.once()
    third = r.sessions.send(principal, sid, {"content": "restore and continue"}, "third")
    assert r.worker.once()
    with database.transaction() as repo:
        assert (
            repo.one("SELECT native_id FROM executions WHERE turn_id=%s", (third["turn_id"],))[
                "native_id"
            ]
            == native
        )
        assert (
            repo.one("SELECT state FROM snapshots WHERE id=%s", (snap["snapshot_id"],))["state"]
            == "ready"
        )
        assert repo.one("SELECT count(*) AS n FROM executor_leases")["n"] == 2
    # A checkpoint taken on warm compute must not rewind the next Turn.
    r.worktrees.checkpoint(principal, sid, 3, "checkpoint-warm")
    assert r.worker.once()
    fourth = r.sessions.send(principal, sid, {"content": "warm after newer checkpoint"}, "fourth")
    assert r.worker.once()
    assert r.queries.detail(principal, "turns", fourth["turn_id"])["state"] == "succeeded"
    captured = r.changes.capture(
        principal, sid, {"generation": 4, "source_turn_id": fourth["turn_id"]}, "capture"
    )
    assert r.worker.once()
    cs = r.changes.get(principal, captured["changeset_id"])
    child = r.delegations.spawn(
        principal, sid, {"changeset_id": cs["id"], "role": "review"}, "child"
    )
    assert r.worker.once()
    assert r.worker.once()  # result publication
    result = r.delegations.get(principal, child["delegation_id"])["result"]
    assert result["validated"] and result["verdict"] == "approve"
    with database.transaction() as repo:
        assert result_rows(repo, cs)[0]["independent"]
    released = r.worktrees.release(principal, sid, "release-author")
    with database.transaction() as repo:
        leases = repo.all(
            "SELECT session_id,generation,state,cleanup_confirmed FROM executor_leases "
            "ORDER BY session_id,generation"
        )
    assert released.get("job_id"), leases
    assert r.worker.once(), released
    delivery = r.deliveries.request(principal, cs["id"], {"transport": "export"}, "export")
    assert r.worker.once()
    assert (
        r.queries.detail(principal, "deliveries", delivery["delivery_id"])["state"] == "succeeded"
    )
