#!/usr/bin/env bash
# Sandbox entrypoint: layout under $SBX_WORK, then stay up.
# idle_timeout / timeout / cpu / memory / workdir / tags / secrets come from
# the control plane (Sandbox.create), not from this script.
#
# This process is container PID 1. Linux ignores the default SIGTERM action on
# PID 1, so we must not `exec sleep` (sleep installs no handler and would hang).
# Stay in bash, trap TERM/INT, run the keep-alive as a child.
set -euo pipefail

export SBX_WORK="${SBX_WORK:-/work}"
export CODEX_HOME="${CODEX_HOME:-${SBX_WORK}/.codex}"
export PYTHONPATH="${PYTHONPATH:-/opt/sbx}"

# SOR-74: agent CLIs must never see Desktop/ACP auth bridges or Devin API-key
# env — auth comes only from the restored credential blob ($HOME/credentials).
unset ACP_BACKEND DEVIN_API_KEY DEVIN_V3_API_KEY DEVIN_LEGACY_API_KEY DEVIN_ORG_ID

mkdir -p "${SBX_WORK}/inbox" "${SBX_WORK}/turns" "${SBX_WORK}/home" "${CODEX_HOME}"

child=""
shutdown() {
  trap - TERM INT
  if [[ -n "${child}" ]]; then
    kill -TERM "${child}" 2>/dev/null || true
    wait "${child}" 2>/dev/null || true
  fi
  exit 0
}
trap shutdown TERM INT

if [[ $# -eq 0 ]]; then
  set -- sleep infinity
fi

"$@" &
child=$!
set +e
wait "${child}"
status=$?
set -e
exit "${status}"
