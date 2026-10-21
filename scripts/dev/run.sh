#!/usr/bin/env bash
# Run a development command from this checkout with user tools and PostgreSQL
# on PATH. It does not read local secrets, start services or change auth policy.
set -euo pipefail
SBX_DEV_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
export PATH="$HOME/.local/bin:$PATH"
for SBX_DEV_PG_BIN in /opt/homebrew/opt/postgresql@17/bin /usr/local/opt/postgresql@17/bin; do
  if [[ -x $SBX_DEV_PG_BIN/initdb && -x $SBX_DEV_PG_BIN/pg_ctl ]]; then
    export PATH="$SBX_DEV_PG_BIN:$PATH"
    break
  fi
done
if (( $# == 0 )); then
  printf 'Usage: bash scripts/dev/run.sh COMMAND [ARGUMENT ...]\n' >&2
  printf 'Full backend/runtime tests require Ubuntu; macOS is the coordination/client machine.\n' >&2
  exit 2
fi
cd "$SBX_DEV_ROOT"
exec "$@"
