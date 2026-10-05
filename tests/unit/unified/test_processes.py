import time

import pytest
from protocol.runtime import ProtocolError
from runtime.daemon.app import Runtime
from runtime.daemon.processes import Processes


def test_pty_writer_barrier_redaction_bounded_buffer_and_close(tmp_path):
    runtime = Runtime(tmp_path, "sess_test", "lease_test", 1, "REDACTED")
    processes = Processes(runtime)
    opened = processes.open(
        {"terminal_id": "pty_one", "command": ["/bin/bash", "--noprofile", "--norc"]}
    )
    assert opened["running"] and processes.has_writer()
    processes.write("pty_one", "printf 'hello\\n'\n")
    end = time.monotonic() + 3
    while "hello" not in processes.read("pty_one")["text"] and time.monotonic() < end:
        time.sleep(0.02)
    assert "hello" in processes.read("pty_one")["text"]
    assert processes.close("pty_one")["stopped"]
    assert not processes.has_writer()
    with pytest.raises(ProtocolError):
        processes.write("missing", "hello")


def test_declared_service_process_has_no_interactive_writer(tmp_path):
    runtime = Runtime(tmp_path, "sess_test", "lease_test", 1, "REDACTED")
    runtime.processes.open(
        {"service_id": "service_one", "command": ["sleep", "30"], "port": 8000}, service=True
    )
    assert not runtime.processes.has_writer()
    with pytest.raises(ProtocolError):
        runtime.processes.write("service_one", "input")
    assert runtime.processes.close("service_one")["stopped"]
