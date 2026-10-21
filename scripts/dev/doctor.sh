#!/usr/bin/env bash
# Observe development readiness. No installation, authentication, file edits,
# network requests, service starts, database mutations, or credential reads.
set -uo pipefail

SBX_DOCTOR_REPO=${SBX_REPO_DIR:-"$HOME/Developer/sbx-agent"}
SBX_DOCTOR_FAILURES=0
export PATH="$HOME/.local/bin:$PATH"

if [[ ${1:-} == --help ]]; then
  printf 'Usage: bash sbx-environment-doctor.sh\n'
  printf 'Optional: SBX_REPO_DIR=/an/existing/checkout\n'
  printf 'Exit 0 means required tools and installed project dependencies were found.\n'
  printf 'This does not run tests or validate subscription access.\n'
  exit 0
fi
if (( $# )); then
  printf 'Unknown argument. Use --help.\n' >&2
  exit 2
fi

sbx_doctor_version() {
  local sbx_doctor_level=$1 sbx_doctor_name=$2
  shift 2
  if ! command -v "$sbx_doctor_name" >/dev/null 2>&1; then
    printf '%s MISSING: %s\n' "$sbx_doctor_level" "$sbx_doctor_name"
    if [[ $sbx_doctor_level == REQUIRED ]]; then
      SBX_DOCTOR_FAILURES=$((SBX_DOCTOR_FAILURES + 1))
    fi
    return
  fi
  local sbx_doctor_result
  if sbx_doctor_result=$("$sbx_doctor_name" "$@" 2>&1); then
    printf 'FOUND: %s — %s\n' "$sbx_doctor_name" "${sbx_doctor_result%%$'\n'*}"
  else
    printf '%s UNUSABLE: %s\n' "$sbx_doctor_level" "$sbx_doctor_name"
    if [[ $sbx_doctor_level == REQUIRED ]]; then
      SBX_DOCTOR_FAILURES=$((SBX_DOCTOR_FAILURES + 1))
    fi
  fi
}

printf 'Platform: %s %s\n' "$(uname -s)" "$(uname -m)"
if [[ $(uname -s) != Linux ]]; then
  printf 'PLATFORM: full SBX runtime tests require Linux (/proc and prctl); run them on Ubuntu.\n'
fi
printf 'Workspace: %s\n' "$SBX_DOCTOR_REPO"
sbx_doctor_version REQUIRED git --version
sbx_doctor_version REQUIRED uv --version
sbx_doctor_version REQUIRED node --version
sbx_doctor_version REQUIRED npm --version
sbx_doctor_version REQUIRED make --version
sbx_doctor_version REQUIRED rg --version
sbx_doctor_version REQUIRED codex --version
sbx_doctor_version REQUIRED claude --version
sbx_doctor_version OPTIONAL gh --version
sbx_doctor_version OPTIONAL tmux -V
sbx_doctor_version OPTIONAL docker --version

if command -v node >/dev/null 2>&1; then
  SBX_DOCTOR_NODE_MAJOR=$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || printf 0)
  if [[ $SBX_DOCTOR_NODE_MAJOR =~ ^[0-9]+$ ]] && (( SBX_DOCTOR_NODE_MAJOR >= 22 )); then
    printf 'PASS: Node.js meets the 22+ requirement.\n'
  else
    printf 'REQUIRED: Node.js 22 or newer.\n'
    SBX_DOCTOR_FAILURES=$((SBX_DOCTOR_FAILURES + 1))
  fi
fi
if command -v uv >/dev/null 2>&1 && uv python find 3.12 >/dev/null 2>&1; then
  printf 'PASS: Python 3.12 is installed.\n'
else
  printf 'REQUIRED MISSING: Python 3.12.\n'
  SBX_DOCTOR_FAILURES=$((SBX_DOCTOR_FAILURES + 1))
fi

SBX_DOCTOR_PG_BIN=
if command -v pg_config >/dev/null 2>&1; then
  SBX_DOCTOR_PG_BIN=$(pg_config --bindir 2>/dev/null || true)
fi
if [[ -z $SBX_DOCTOR_PG_BIN || ! -x $SBX_DOCTOR_PG_BIN/initdb ]]; then
  for SBX_DOCTOR_CANDIDATE in /usr/lib/postgresql/*/bin /opt/homebrew/opt/postgresql@*/bin /usr/local/opt/postgresql@*/bin; do
    if [[ -x $SBX_DOCTOR_CANDIDATE/initdb && -x $SBX_DOCTOR_CANDIDATE/pg_ctl ]]; then
      SBX_DOCTOR_PG_BIN=$SBX_DOCTOR_CANDIDATE
    fi
  done
fi
if [[ -n $SBX_DOCTOR_PG_BIN && -x $SBX_DOCTOR_PG_BIN/postgres && -x $SBX_DOCTOR_PG_BIN/initdb && -x $SBX_DOCTOR_PG_BIN/pg_ctl ]]; then
  printf 'FOUND: %s\n' "$("$SBX_DOCTOR_PG_BIN/postgres" --version)"
  printf 'PostgreSQL binaries: %s\n' "$SBX_DOCTOR_PG_BIN"
else
  printf 'REQUIRED MISSING: PostgreSQL server binaries.\n'
  SBX_DOCTOR_FAILURES=$((SBX_DOCTOR_FAILURES + 1))
fi

if [[ -d $SBX_DOCTOR_REPO ]] && command -v git >/dev/null 2>&1 \
  && git --no-optional-locks -C "$SBX_DOCTOR_REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  printf 'Branch: %s\n' "$(git --no-optional-locks -C "$SBX_DOCTOR_REPO" branch --show-current)"
  printf 'Commit: %s\n' "$(git --no-optional-locks -C "$SBX_DOCTOR_REPO" rev-parse HEAD)"
  if [[ -z $(git --no-optional-locks -C "$SBX_DOCTOR_REPO" status --porcelain) ]]; then
    printf 'Working tree: clean.\n'
  else
    printf 'Working tree: contains local work; nothing was changed.\n'
  fi
else
  printf 'REQUIRED MISSING: SBX Git worktree.\n'
  SBX_DOCTOR_FAILURES=$((SBX_DOCTOR_FAILURES + 1))
fi

if [[ -x $SBX_DOCTOR_REPO/.venv/bin/python ]] \
  && PYTHONDONTWRITEBYTECODE=1 "$SBX_DOCTOR_REPO/.venv/bin/python" -c 'import fastapi, httpx, psycopg, pytest, ruff; import sys; assert sys.version_info[:2] == (3, 12)' >/dev/null 2>&1; then
  printf 'PASS: project Python 3.12 environment and key runtime/test dependencies import.\n'
else
  printf 'REQUIRED MISSING: usable project Python 3.12 environment.\n'
  SBX_DOCTOR_FAILURES=$((SBX_DOCTOR_FAILURES + 1))
fi
for SBX_DOCTOR_AREA in console docs-site; do
  if [[ -d $SBX_DOCTOR_REPO/$SBX_DOCTOR_AREA/node_modules ]]; then
    printf 'FOUND: %s/node_modules (lockfile freshness is not checked here).\n' "$SBX_DOCTOR_AREA"
  else
    printf 'REQUIRED MISSING: %s/node_modules.\n' "$SBX_DOCTOR_AREA"
    SBX_DOCTOR_FAILURES=$((SBX_DOCTOR_FAILURES + 1))
  fi
done
if [[ -d $HOME/.cache/ms-playwright || -d $HOME/Library/Caches/ms-playwright ]]; then
  printf 'FOUND: Playwright browser cache (browser execution is not tested here).\n'
else
  printf 'OPTIONAL MISSING: Playwright browser cache.\n'
fi

printf 'Subscription auth, GitHub permissions and Docker daemon access were not inspected.\n'
printf 'No test suite, product service or cloud task was started.\n'
if (( SBX_DOCTOR_FAILURES )); then
  printf 'NOT READY: %s required checks are missing or unusable.\n' "$SBX_DOCTOR_FAILURES"
  exit 1
fi
printf 'READY: required environment components were found; run the project verification before accepting changes.\n'
