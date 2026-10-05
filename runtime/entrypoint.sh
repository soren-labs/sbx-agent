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
# Sandbox HOME is fixed at $SBX_WORK/home (credential-restore root). Image
# env already pins the same value; export here too so every entrypoint child
# sees the contract layout regardless of image env.
export HOME="${SBX_WORK}/home"
export PYTHONPATH="${PYTHONPATH:-/opt/sbx}"

mkdir -p "${SBX_WORK}/inbox" "${SBX_WORK}/turns" "${SBX_WORK}/home"

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
