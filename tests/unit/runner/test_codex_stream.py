"""The app-server bridge preserves actual deltas and existing canonical IDs."""

import json
import os
import subprocess
import sys
from pathlib import Path

from runtime.runner.codex_stream import Stream


def test_cumulative_unicode_text_and_terminal_identity(capsys):
    stream = Stream()
    stream.notification("item/started", {"item": {"id": "m1", "type": "agentMessage", "text": ""}})
    for text in ["你好", " **stream", "ing**"]:
        stream.notification("item/agentMessage/delta", {"itemId": "m1", "delta": text})
    progress = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["item"]["text"] for event in progress] == [
        "",
        "你好",
        "你好 **stream",
        "你好 **streaming**",
    ]
    assert {event["item"]["id"] for event in progress} == {"m1"}
    assert not stream.completed  # available while the provider turn remains open
    stream.notification(
        "item/completed", {"item": {"id": "m1", "type": "agentMessage", "text": ""}}
    )
    terminal = json.loads(capsys.readouterr().out)
    assert terminal["item"]["text"] == "你好 **streaming**"
    assert terminal["type"] == "item.completed"


def test_command_delta_usage_and_failed_turn(capsys):
    stream = Stream()
    stream.notification(
        "item/started",
        {
            "item": {
                "id": "c1",
                "type": "commandExecution",
                "command": "printf hi",
                "status": "inProgress",
            }
        },
    )
    stream.notification("item/commandExecution/outputDelta", {"itemId": "c1", "delta": "hi"})
    stream.notification(
        "item/completed",
        {
            "item": {
                "id": "c1",
                "type": "commandExecution",
                "command": "printf hi",
                "exitCode": 0,
                "status": "completed",
                "aggregatedOutput": "hi",
            }
        },
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[1]["item"]["aggregated_output"] == "hi"
    assert events[-1]["item"]["id"] == "c1"
    assert events[-1]["item"]["exit_code"] == 0
    stream.notification("turn/completed", {"turn": {"status": "interrupted"}})
    assert stream.completed and not stream.ok
    assert json.loads(capsys.readouterr().out)["type"] == "turn.failed"


def test_stdio_handshake_resume_and_immediate_delta(tmp_path: Path, monkeypatch):
    fake = tmp_path / "app_server.py"
    fake.write_text("""import json, sys
for line in sys.stdin:
 m=json.loads(line); method=m.get("method"); ident=m.get("id")
 if method=="initialize": result={}
 elif method=="thread/resume":
  assert m["params"]["threadId"]=="existing-thread"
  result={"thread":{"id":"existing-thread"}}
 elif method=="turn/start":
  assert m["params"]["input"][0]["text"]=="follow up"
  print(json.dumps({"method":"item/agentMessage/delta","params":{"itemId":"m","delta":"真实"}}),flush=True)
  print(json.dumps({"method":"turn/completed","params":{"turn":{"status":"completed"}}}),flush=True)
  result={}
 else: continue
 print(json.dumps({"id":ident,"result":result}),flush=True)
""")
    monkeypatch.setenv("CODEX_BIN", str(fake))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "runtime.runner.codex_stream",
            "--resume",
            "existing-thread",
            "--",
            "follow up",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env=dict(os.environ),
    )
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["type"] for event in events] == [
        "thread.started",
        "turn.started",
        "item.updated",
        "turn.completed",
    ]
    assert events[2]["item"]["text"] == "真实"


def test_child_thread_and_old_turn_cannot_complete_root(capsys):
    stream = Stream("root")
    stream.notification("turn/started", {"threadId": "root", "turn": {"id": "current"}})
    stream.notification(
        "item/agentMessage/delta",
        {"threadId": "child", "turnId": "child-turn", "itemId": "m", "delta": "child"},
    )
    stream.notification(
        "turn/completed", {"threadId": "child", "turn": {"id": "child-turn", "status": "completed"}}
    )
    stream.notification(
        "turn/completed", {"threadId": "root", "turn": {"id": "old", "status": "completed"}}
    )
    assert not stream.completed
    assert not capsys.readouterr().out
    stream.notification(
        "turn/completed", {"threadId": "root", "turn": {"id": "current", "status": "completed"}}
    )
    assert stream.completed and stream.ok


def test_rpc_auth_error_preserves_health_feedback(tmp_path, monkeypatch):
    from runtime.runner.adapter import CodexAdapter

    fake = tmp_path / "auth_error.py"
    fake.write_text("""import json, sys
for line in sys.stdin:
 m=json.loads(line)
 if m.get("method")=="initialize": result={"result":{}}
 elif m.get("method")=="thread/start":
  result={"error":{"code":401,"message":"Unauthorized token=REDACTED"}}
 else: continue
 print(json.dumps(dict(id=m["id"],**result)),flush=True)
""")
    monkeypatch.setenv("CODEX_BIN", str(fake))
    result = subprocess.run(
        [sys.executable, "-m", "runtime.runner.codex_stream", "--", "hello"],
        capture_output=True,
        text=True,
        timeout=10,
        env=dict(os.environ),
    )
    assert result.returncode == 1
    assert CodexAdapter().health_from(result.returncode, result.stderr) == "auth_invalid"
    assert json.loads(result.stdout)["type"] == "turn.failed"
