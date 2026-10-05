"""Purpose-bound Git clone helper; secrets never enter repo URLs/argv/history."""

from contextlib import contextmanager
from pathlib import Path


@contextmanager
def clone_auth(root: Path, credential: dict, env: dict):
    if not credential:
        yield env
        return
    folder = root / "clone-private"
    folder.mkdir(mode=0o700, exist_ok=True)
    token = folder / "token"
    script = folder / "askpass.py"
    token.write_text(credential["token"])
    token.chmod(0o600)
    script.write_text("""#!/usr/bin/python3
import os,sys,pathlib
value=pathlib.Path(os.environ['SBX_GIT_TOKEN_FILE']).read_text()
print('x-access-token' if 'username' in sys.argv[1].lower() else value)
""")
    script.chmod(0o700)
    try:
        yield {**env, "GIT_ASKPASS": str(script), "SBX_GIT_TOKEN_FILE": str(token)}
    finally:
        token.unlink(missing_ok=True)
        script.unlink(missing_ok=True)
