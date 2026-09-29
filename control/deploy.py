"""Modal deploy entry for ``make deploy`` / ``runtime.image.invoke_control_deploy``.

Do not route deploy through ``control.app.main`` — that argparse CLI eats
pytest arguments (SOR-47). WP1-A imports ``control.deploy.deploy``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# ``python -m modal deploy -m control.modal_app``
_DEPLOY_ARGV = [sys.executable, "-m", "modal", "deploy", "-m", "control.modal_app"]


def _console_dist() -> Path:
    override = os.environ.get("SBX_CONSOLE_DIST")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "console" / "dist"


def deploy() -> None:
    """Deploy the control-plane Modal App. Does not parse process argv.

    SOR-266: the image ships ``console/dist`` and serves it at "/" — the
    build artifact must exist before ``modal deploy`` runs. Fail loudly
    rather than let the deploy produce a root that has no V2 console.
    """
    dist = _console_dist()
    if not (dist / "index.html").is_file():
        raise SystemExit(
            "console build artifact missing: "
            f"{dist} (no index.html).\n"
            "Build it first: `npm --prefix console ci && npm --prefix console "
            "run build` — or use `sbx deploy`, which builds it as a pipeline "
            "step. SBX_CONSOLE_DIST may point at a prebuilt output."
        )
    subprocess.check_call(_DEPLOY_ARGV)


def main() -> None:
    deploy()


if __name__ == "__main__":
    main()
