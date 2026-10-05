"""Supervisor — process launch, line→observation pumping, cancel
escalation (RFC 167 §03: the runtime only *runs* native invocations;
every stdout line is normalized into typed Observations and spooled)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from protocol.events import Observation, ObservationKind
from runtime.daemon import supervisor as sup_mod
from runtime.daemon.journal import Journal
from runtime.daemon.supervisor import SupervisedProcess, Supervisor
from runtime.harnesses.protocol import NativeInvocation

pytestmark = pytest.mark.unit


def _script(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fake_proc.py"
    p.write_text(body)
    return p


def _noop(line, state):
    return []


def _supervisor(journal: Journal, normalize=None) -> Supervisor:
    return Supervisor(
        spool_append=lambda kind, payload: journal.spool_append(kind, payload),
        on_terminal=lambda proc, obs: None,
        normalize=normalize or _noop,
    )


class TestPump:
    def test_lines_normalized_and_spooled(self, tmp_path):
        script = _script(
            tmp_path,
            'import json\nfor i in range(3):\n    print(json.dumps({"n": i}), flush=True)\n',
        )
        journal = Journal(tmp_path / "j.db")
        seen: list[dict] = []

        def norm(line, state):
            import json as j

            try:
                obj = j.loads(line)
            except Exception:
                return []
            obs = Observation(kind=ObservationKind.DIAGNOSTIC, payload=obj, observed_at=time.time())
            seen.append(obj)
            return [obs]

        sup = _supervisor(journal, normalize=norm)
        proc = SupervisedProcess(
            execution_id="exec_1",
            operation_id="eff_1",
            invocation=NativeInvocation(
                argv=(sys.executable, str(script)),
                cwd=tmp_path,
                env={"PATH": "/usr/bin:/bin"},
            ),
        )
        sup.launch(proc)
        assert proc.done.wait(15)
        assert proc.exit_code == 0
        assert len(seen) == 3
        rows = journal.spool_uncommitted()
        # 3 diagnostics + PROCESS_EXITED
        assert len(rows) == 4
        assert rows[-1][2]["kind"] == "process.exited"
        sup.stop_all()
        journal.close()

    def test_bad_frames_counted(self, tmp_path):
        script = _script(tmp_path, "print('NOT JSON'); print('still not json')\n")
        journal = Journal(tmp_path / "j.db")
        sup = _supervisor(journal)
        proc = SupervisedProcess(
            execution_id="exec_2",
            operation_id="eff_2",
            invocation=NativeInvocation(
                argv=(sys.executable, str(script)),
                cwd=tmp_path,
                env={"PATH": "/usr/bin:/bin"},
            ),
        )
        sup.launch(proc)
        assert proc.done.wait(15)
        assert proc.bad_frames == 2
        sup.stop_all()
        journal.close()


class TestCancel:
    def test_term_escalates_to_kill(self, tmp_path, monkeypatch):
        # Process ignores SIGTERM; supervisor must escalate to SIGKILL.
        monkeypatch.setattr(sup_mod, "_SIGTERM_GRACE_S", 0.5)
        script = _script(
            tmp_path,
            "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(3600)\n",
        )
        journal = Journal(tmp_path / "j.db")
        sup = _supervisor(journal)
        proc = SupervisedProcess(
            execution_id="exec_3",
            operation_id="eff_3",
            invocation=NativeInvocation(
                argv=(sys.executable, str(script)),
                cwd=tmp_path,
                env={"PATH": "/usr/bin:/bin"},
            ),
        )
        sup.launch(proc)
        time.sleep(0.3)
        assert sup.cancel("eff_3")
        assert proc.done.wait(10)
        assert proc.signal_number == 9
        sup.stop_all()
        journal.close()
