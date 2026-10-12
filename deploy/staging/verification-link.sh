#!/usr/bin/env bash
# Print the newest email-verification link stored for one address. Runs on the
# staging host as root. Staging has no outbound mail provider by default, so a
# person who registers at the staging Console cannot receive their link by
# email; the operator runs this for that one address and hands the link over
# out of band. The link is single-use. Nothing else from the outbox is printed.
set -euo pipefail

EMAIL=${1:?usage: verification-link.sh <email>}

docker compose -p sbx-staging exec -T sbx python - "$EMAIL" <<'PY'
import json
import sys
from pathlib import Path

wanted = sys.argv[1].strip().lower()
for path in sorted(Path("/var/lib/sbx/mail").glob("*.json"), reverse=True):
    mail = json.loads(path.read_text())
    if mail["to"].lower() == wanted and "token=" in mail["body"]:
        print(mail["body"][mail["body"].index("http") :].strip())
        break
else:
    sys.exit(f"no verification mail stored for {wanted}")
PY
