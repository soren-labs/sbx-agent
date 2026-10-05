"""Native auth cache lifetime, enforced inside compute even if the VPS dies."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

AUTH_PATHS = (
    "home/.codex/auth.json",
    ".codex/auth.json",
    "auth.json",
    "home/.local/share/opencode/auth.json",
)

# Inline guards also cover retained sandboxes built before this module existed.
# The child does not take the lock again; the parent owns its entire lifetime.
RUN_SCRIPT = """import fcntl,os,pathlib,signal,subprocess,sys
root=pathlib.Path(os.environ['SBX_WORK']); root.mkdir(parents=True,exist_ok=True)
operation=os.environ['SBX_NATIVE_OPERATION']
with (root/'.native-access.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 marker=root/'.native-access-operation'; marker.write_text(operation)
 child=None
 def terminate(signum,frame):
  if child is not None and child.poll() is None: child.send_signal(signum)
 signal.signal(signal.SIGTERM,terminate); signal.signal(signal.SIGINT,terminate)
 try:
  env=dict(os.environ); env.pop('SBX_NATIVE_OPERATION',None)
  child=subprocess.Popen(sys.argv[1:],env=env,stdin=subprocess.DEVNULL)
  result=child.wait()
 finally:
  for rel in ('home/.codex/auth.json','.codex/auth.json','auth.json',
              'home/.local/share/opencode/auth.json'):
   (root/rel).unlink(missing_ok=True)
  marker.unlink(missing_ok=True)
sys.exit(result)
"""

CLEAN_SCRIPT = """import fcntl,pathlib,sys
root=pathlib.Path(sys.argv[1]); operation=sys.argv[2]
with (root/'.native-access.lock').open('a') as lock:
 try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError: sys.exit(0)
 marker=root/'.native-access-operation'
 if marker.exists() and marker.read_text()==operation:
  for rel in ('home/.codex/auth.json','.codex/auth.json','auth.json',
              'home/.local/share/opencode/auth.json'):
   (root/rel).unlink(missing_ok=True)
  marker.unlink(missing_ok=True)
"""


def cleanup(root: Path, operation: str) -> bool:
    """An old watcher must never delete a newer operation's lease.

    The advisory lock serializes marker checks and cache writes/deletes.
    A live runner owns the lock and cleans in finally; control cleanup
    handles a killed runner without waiting on a later operation.
    """
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".native-access.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        marker = root / ".native-access-operation"
        if marker.exists() and marker.read_text() == operation:
            for relative in AUTH_PATHS:
                (root / relative).unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
        return True


@contextmanager
def access_scope():
    operation = os.environ.get("SBX_NATIVE_OPERATION")
    if not operation:
        yield
        return
    root = Path(os.environ["SBX_WORK"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".native-access.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = root / ".native-access-operation"
        marker.write_text(operation)
        try:
            yield
        finally:
            for relative in AUTH_PATHS:
                (root / relative).unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
