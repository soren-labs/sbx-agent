"""Modal deploy entry for ``make deploy`` / ``runtime.image.invoke_control_deploy``.

Do not route deploy through ``control.app.main`` — that argparse CLI eats
pytest arguments (SOR-47). WP1-A imports ``control.deploy.deploy``.
"""

from __future__ import annotations

import subprocess
import sys

# ``python -m modal deploy -m control.modal_app``
_DEPLOY_ARGV = [sys.executable, "-m", "modal", "deploy", "-m", "control.modal_app"]


def deploy() -> None:
    """Deploy the control-plane Modal App. Does not parse process argv."""
    subprocess.check_call(_DEPLOY_ARGV)


def main() -> None:
    deploy()


if __name__ == "__main__":
    main()
