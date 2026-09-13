#!/usr/bin/env bash
# Sandbox entrypoint: layout under $SBX_WORK, then stay up.
# idle_timeout / timeout / cpu / memory / workdir / tags / secrets come from
# the control plane (Sandbox.create), not from this script.
set -euo pipefail

export SBX_WORK="${SBX_WORK:-/work}"
export CODEX_HOME="${CODEX_HOME:-${SBX_WORK}/.codex}"

mkdir -p "${SBX_WORK}/inbox" "${SBX_WORK}/turns" "${CODEX_HOME}"

if [[ $# -eq 0 ]]; then
  set -- sleep infinity
fi

# Replace this process so SIGTERM reaches the keep-alive (or the command
# Modal / docker passed). Modal Functions also require `exec "$@"`.
exec "$@"
