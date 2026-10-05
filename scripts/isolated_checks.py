"""Run cloud-free checks with an isolated HOME/XDG and explicit environment."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="sbx-gpt-checks-") as temp:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": temp,
            "LANG": "C.UTF-8",
            "UV_CACHE_DIR": str(root / ".cache" / "uv"),
        }
        for name in ("CONFIG", "CACHE", "DATA", "STATE"):
            env[f"XDG_{name}_HOME"] = str(Path(temp) / name.lower())
        # Only a disposable test DB DSN is passed. Never pass product/cloud keys.
        if "SBX_TEST_PG_DSN" in os.environ:
            env["SBX_TEST_PG_DSN"] = os.environ["SBX_TEST_PG_DSN"]
        result = subprocess.run(sys.argv[1:] or ["make", "test"], env=env, cwd=root)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
