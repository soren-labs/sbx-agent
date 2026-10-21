#!/usr/bin/env bash
# Prepare the SBX Ubuntu checkout after install-system.sh has completed.
# User-level only. Never resets an existing checkout or touches CLI credentials.
set -euo pipefail
umask 077

SBX_SETUP_BASE=c0aed37d731121a39c7c41eb4ba47229d8fbe081
SBX_SETUP_BRANCH=setup/oss-environment-20261021
SBX_SETUP_REMOTE=https://github.com/soren-labs/sbx-agent.git
SBX_SETUP_REPO=${SBX_REPO_DIR:-"$HOME/Developer/sbx-agent"}
SBX_SETUP_CACHE="$HOME/.cache/sbx-setup"
SBX_SETUP_BUNDLE="$SBX_SETUP_CACHE/oss-environment.bundle"
SBX_SETUP_STAGE=

if [[ ${1:-} == --help ]]; then
  cat <<'HELP'
Usage: bash ubuntu-bootstrap-after-system.sh

Run as the ordinary Ubuntu user, after install-system.sh succeeds.
Default checkout: ~/Developer/sbx-agent
Optional override: SBX_REPO_DIR=/an/owned/path

A new checkout starts at c0aed37d731121a39c7c41eb4ba47229d8fbe081,
plus the verified local Mac handoff bundle when present, on
setup/oss-environment-20261021. An existing checkout is preserved:
no fetch/pull, reset, stash, branch switch, credential changes or deployment.
Existing work must be clean and descend from the required baseline.

This installs locked Python/Console/Docs dependencies, then runs make lint
and make test with the repository's temporary PostgreSQL test harness.
It does not start the product service or run cloud-provider acceptance.
HELP
  exit 0
fi
if (( $# )); then
  printf 'Unknown argument. Use --help.\n' >&2
  exit 2
fi
if [[ $(uname -s) != Linux || $EUID -eq 0 ]]; then
  printf 'Run this script as the ordinary Ubuntu user, without sudo.\n' >&2
  exit 2
fi

export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$SBX_SETUP_CACHE/logs"
exec 9>"$SBX_SETUP_CACHE/after-system.lock"
if ! flock -n 9; then
  printf 'Another SBX environment preparation is already running.\n' >&2
  exit 3
fi
SBX_SETUP_LOG="$SBX_SETUP_CACHE/logs/after-system-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
exec > >(tee -a "$SBX_SETUP_LOG") 2>&1

sbx_setup_finished() {
  SBX_SETUP_EXIT=$?
  trap - EXIT
  if (( SBX_SETUP_EXIT != 0 )); then
    printf '\nEnvironment preparation stopped with exit code %s.\n' "$SBX_SETUP_EXIT"
    if [[ -n $SBX_SETUP_STAGE && -d $SBX_SETUP_STAGE ]]; then
      printf 'The unfinished, separate clone was preserved at: %s\n' "$SBX_SETUP_STAGE"
    fi
    printf 'Existing source files and authentication files were not reset.\n'
  fi
  printf 'Log: %s\n' "$SBX_SETUP_LOG"
  exit "$SBX_SETUP_EXIT"
}
trap sbx_setup_finished EXIT

SBX_SETUP_MISSING=0
for SBX_SETUP_TOOL in git git-lfs curl rg make gcc g++ pkg-config tmux jq shellcheck \
  python3 uv node npm psql pg_config docker; do
  if ! command -v "$SBX_SETUP_TOOL" >/dev/null 2>&1; then
    printf 'Missing prerequisite: %s\n' "$SBX_SETUP_TOOL"
    SBX_SETUP_MISSING=1
  fi
done
if (( SBX_SETUP_MISSING )); then
  printf 'Complete ~/.cache/sbx-setup/install-system.sh, then run this script again.\n'
  exit 20
fi
if ! docker compose version >/dev/null 2>&1; then
  printf 'Docker Compose is missing. Complete the system package script first.\n'
  exit 20
fi
SBX_SETUP_NODE_MAJOR=$(node -p 'Number(process.versions.node.split(".")[0])')
if (( SBX_SETUP_NODE_MAJOR < 22 )); then
  printf 'Node.js 22 or newer is required; current: %s\n' "$(node --version)"
  exit 20
fi

SBX_SETUP_PG_BIN=$(pg_config --bindir)
if [[ ! -x $SBX_SETUP_PG_BIN/initdb || ! -x $SBX_SETUP_PG_BIN/pg_ctl || ! -x $SBX_SETUP_PG_BIN/postgres ]]; then
  printf 'PostgreSQL server binaries are missing; client-only installation is insufficient.\n'
  exit 20
fi
printf 'Node: %s\n' "$(node --version)"
printf 'uv: %s\n' "$(uv --version)"
"$SBX_SETUP_PG_BIN/postgres" --version

if [[ ! -e $SBX_SETUP_REPO ]]; then
  SBX_SETUP_PARENT=$(dirname "$SBX_SETUP_REPO")
  mkdir -p "$SBX_SETUP_PARENT"
  SBX_SETUP_STAGE=$(mktemp -d "$SBX_SETUP_PARENT/.sbx-clone.XXXXXX")
  git clone --no-checkout --no-tags "$SBX_SETUP_REMOTE" "$SBX_SETUP_STAGE"
  git -C "$SBX_SETUP_STAGE" cat-file -e "$SBX_SETUP_BASE^{commit}"
  SBX_SETUP_START=$SBX_SETUP_BASE
  if [[ -f $SBX_SETUP_BUNDLE ]]; then
    git -C "$SBX_SETUP_STAGE" bundle verify "$SBX_SETUP_BUNDLE"
    git -C "$SBX_SETUP_STAGE" fetch "$SBX_SETUP_BUNDLE" "refs/heads/$SBX_SETUP_BRANCH"
    SBX_SETUP_START=$(git -C "$SBX_SETUP_STAGE" rev-parse FETCH_HEAD)
    git -C "$SBX_SETUP_STAGE" merge-base --is-ancestor "$SBX_SETUP_BASE" "$SBX_SETUP_START"
    printf 'Including the local Mac environment handoff from the verified Git bundle.\n'
  fi
  git -C "$SBX_SETUP_STAGE" switch --create "$SBX_SETUP_BRANCH" "$SBX_SETUP_START"
  # Do not overwrite a directory that appeared while the clone was being made.
  mv -T --no-clobber "$SBX_SETUP_STAGE" "$SBX_SETUP_REPO"
  if [[ -d $SBX_SETUP_STAGE ]]; then
    printf 'The destination appeared during cloning; both directories were preserved.\n'
    exit 21
  fi
  SBX_SETUP_STAGE=
  printf 'Created checkout: %s\n' "$SBX_SETUP_REPO"
else
  if [[ ! -d $SBX_SETUP_REPO ]] || ! git -C "$SBX_SETUP_REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    printf 'Destination exists but is not a Git worktree. It was left unchanged: %s\n' "$SBX_SETUP_REPO"
    exit 21
  fi
  SBX_SETUP_TOP=$(git -C "$SBX_SETUP_REPO" rev-parse --show-toplevel)
  if [[ $(realpath "$SBX_SETUP_TOP") != "$(realpath "$SBX_SETUP_REPO")" ]]; then
    printf 'Destination is nested inside another Git worktree. It was left unchanged.\n'
    exit 21
  fi
  SBX_SETUP_ORIGIN=$(git -C "$SBX_SETUP_REPO" config --get remote.origin.url || true)
  case "$SBX_SETUP_ORIGIN" in
    https://github.com/soren-labs/sbx-agent|https://github.com/soren-labs/sbx-agent.git|git@github.com:soren-labs/sbx-agent.git|ssh://git@github.com/soren-labs/sbx-agent.git) ;;
    *)
      printf 'Existing checkout has a different or unrecognized origin. Origin was not printed or changed.\n'
      exit 21
      ;;
  esac
  unset SBX_SETUP_ORIGIN
  if [[ -n $(git --no-optional-locks -C "$SBX_SETUP_REPO" status --porcelain) ]]; then
    printf 'Existing checkout has uncommitted work. No checkout changes or dependency installation were made.\n'
    printf 'Preserve and reconcile that work before rerunning environment preparation.\n'
    exit 21
  fi
  if ! git -C "$SBX_SETUP_REPO" merge-base --is-ancestor "$SBX_SETUP_BASE" HEAD; then
    printf 'Existing checkout does not descend from the required baseline. It was not updated or reset.\n'
    exit 21
  fi
  printf 'Using existing checkout without changing its branch or source files.\n'
fi

cd "$SBX_SETUP_REPO"
test -f AGENTS.md
test -f uv.lock
test -f console/package-lock.json
test -f docs-site/package-lock.json
printf 'Branch: %s\n' "$(git branch --show-current)"
printf 'Commit: %s\n' "$(git rev-parse HEAD)"
printf 'Repository rules: %s/AGENTS.md (read before feature work).\n' "$SBX_SETUP_REPO"

uv sync --frozen --python 3.12
npm --prefix console ci --no-audit --no-fund
npm --prefix docs-site ci --no-audit --no-fund

# Prevent a caller's dev database DSN from redirecting baseline tests.
# tests/conftest.py additionally strips ambient provider credentials and
# isolates HOME/XDG for the product's test runtimes.
env -u SBX_DATABASE_URL -u SBX_TEST_DATABASE_URL make lint
env -u SBX_DATABASE_URL -u SBX_TEST_DATABASE_URL make test

if [[ -n $(git --no-optional-locks status --porcelain) ]]; then
  printf 'Verification left tracked or untracked source changes. They were preserved for inspection.\n'
  exit 22
fi
printf '\nPASS: locked dependencies, lint and backend tests completed.\n'
printf 'Ubuntu workspace: %s\n' "$SBX_SETUP_REPO"
printf 'No product service or cloud acceptance was started.\n'
