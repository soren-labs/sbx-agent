#!/usr/bin/env bash
# B2 acceptance: an isolated SBX control plane on this machine (e.g. the Mac) runs real work
# on a resident Linux machine (e.g. ubuntu-dev) over SSH. No Modal, no inference provider,
# no subscription and no production resource is used.
#
#   scripts/acceptance/b2_resident_mac.sh [--fake-turn] [--keep]
#
# Environment (defaults in brackets):
#   SBX_B2_SSH_ALIAS     [ubuntu-dev]  your existing, verified SSH alias for the machine
#   SBX_B2_REMOTE_ROOT   [/home/dev/Developer/sbx-b2-20261021]  SBX checkout on the machine,
#                        at the same commit as this checkout (the runtime digest is enforced)
#   SBX_B2_REMOTE_PYTHON [/home/dev/Developer/sbx-agent/.venv/bin/python]
#   SBX_B2_REMOTE_STATE  [.local/state/sbx-resident-acceptance]  under the remote HOME
#   SBX_B2_REMOTE_PATH   [/usr/local/bin:/usr/bin:/bin]
#   SBX_B2_OUT           [./b2-acceptance-<timestamp>]  evidence + server log
#   SBX_B2_LINK_TARGET_PORT [22]  machine-side sshd port the Mac-side link forwards to
#   SBX_B2_SSH_CONFIG    [unset]  harness-only `ssh -F` file (a self-test alias); never used by SBX
#
# --fake-turn adds Turns whose OpenCode CLI is the repository's deterministic FAKE stand-in
# (labelled FAKE in the output): Turn success, restart survival and cancel on the resident
# runtime. It is not subscription evidence. --keep leaves the temp dir and remote state.
#
# The alias is used (1) once to read its explicit parameters (`ssh -G`) and verified host key
# from your known_hosts, (2) for a Mac-side link forward used to cut/restore the network
# path, and (3) by the harness to collect evidence. SBX itself never reads ssh_config: it gets
# explicit host/user/port/key/known_hosts from `sbx server machine add`.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ALIAS="${SBX_B2_SSH_ALIAS:-ubuntu-dev}"
REMOTE_ROOT="${SBX_B2_REMOTE_ROOT:-/home/dev/Developer/sbx-b2-20261021}"
REMOTE_PYTHON="${SBX_B2_REMOTE_PYTHON:-/home/dev/Developer/sbx-agent/.venv/bin/python}"
REMOTE_STATE_REL="${SBX_B2_REMOTE_STATE:-.local/state/sbx-resident-acceptance}"
REMOTE_PATH="${SBX_B2_REMOTE_PATH:-/usr/local/bin:/usr/bin:/bin}"
OUT="${SBX_B2_OUT:-$PWD/b2-acceptance-$(date +%Y%m%d-%H%M%S)}"
PY="$REPO/.venv/bin/python"
SBX="$REPO/.venv/bin/sbx"
FAKE=0
KEEP=0
for arg in "$@"; do
  case "$arg" in
    --fake-turn) FAKE=1 ;;
    --keep) KEEP=1 ;;
    *) echo "usage: $0 [--fake-turn] [--keep]" >&2; exit 2 ;;
  esac
done

log() { printf '== %s\n' "$*"; }
die() { printf 'FAIL %s\n' "$*"; exit 1; }
free_port() { "$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }

ORIG_HOME="$HOME"
# Every harness ssh call (never SBX's own) goes through this, with the operator's config.
hssh() {
  if [ -n "${SBX_B2_SSH_CONFIG:-}" ]; then
    HOME="$ORIG_HOME" ssh -F "$SBX_B2_SSH_CONFIG" -T -o BatchMode=yes "$@"
  else
    HOME="$ORIG_HOME" ssh -T -o BatchMode=yes "$@"
  fi
}
export SBX_B2_ORIG_HOME="$ORIG_HOME"
# Short path: the private PostgreSQL socket must fit macOS's 104-byte limit.
WORK="$(mktemp -d /tmp/sbx-b2.XXXXXX)"
chmod 700 "$WORK"
mkdir -p "$OUT"

# ---------------------------------------------------------------------------- preflight
[ -x "$SBX" ] || die "run 'uv sync --frozen --python 3.12' in $REPO first"
command -v ssh-keygen >/dev/null || die "ssh-keygen is required"
hssh "$ALIAS" true || die "ssh $ALIAS must work non-interactively"
LOCAL_HEAD="$(git -C "$REPO" rev-parse HEAD)"
REMOTE_HEAD="$(hssh "$ALIAS" "git -C '$REMOTE_ROOT' rev-parse HEAD")"
REMOTE_DIRTY="$(hssh "$ALIAS" "git -C '$REMOTE_ROOT' status --porcelain | wc -l" | tr -d ' ')"
log "local HEAD $LOCAL_HEAD; remote $REMOTE_ROOT HEAD $REMOTE_HEAD ($REMOTE_DIRTY changed files)"
[ "$LOCAL_HEAD" = "$REMOTE_HEAD" ] || log "WARNING: heads differ; SBX refuses a differing runtime digest"
REMOTE_HOME="$(hssh "$ALIAS" 'printf %s "$HOME"')"
REMOTE_STATE="$REMOTE_HOME/$REMOTE_STATE_REL"
KILL="$(hssh "$ALIAS" "grep -hs '^KillUserProcesses' /etc/systemd/logind.conf /etc/systemd/logind.conf.d/*.conf || true")"
log "remote logind KillUserProcesses: ${KILL:-default (no)}"
case "$KILL" in *yes*) die "KillUserProcesses=yes ends runtimes with the SSH session; see docs" ;; esac
if [ -n "$(hssh "$ALIAS" "pgrep -f '[$(printf %s "$REMOTE_STATE" | cut -c1)]$(printf %s "$REMOTE_STATE" | cut -c2-)' || true")" ]; then
  die "processes from an earlier run still use $REMOTE_STATE on the machine"
fi

# ------------------------------------------------------- explicit SSH params, verified key
G="$(hssh -G "$ALIAS")"
field() { printf '%s\n' "$G" | awk -v k="$1" '$1==k{print $2; exit}'; }
SSH_HOST="$(field hostname)"
SSH_USER="$(field user)"
SSH_PORT="$(field port)"
KEY_ALIAS="$(field hostkeyalias)"
[ -z "$(field proxyjump)$(field proxycommand)" ] || die "$ALIAS uses a proxy; register it through a link instead"
IDENTITY=""
for f in $(printf '%s\n' "$G" | awk '$1=="identityfile"{print $2}'); do
  f="${f/#\~/$ORIG_HOME}"
  if [ -f "$f" ]; then IDENTITY="$f"; break; fi
done
[ -n "$IDENTITY" ] || die "no identity file of $ALIAS exists"
LOOKUP="${KEY_ALIAS:-$SSH_HOST}"
[ "$SSH_PORT" = "22" ] || LOOKUP="[$LOOKUP]:$SSH_PORT"
KEYS="$WORK/verified-keys"
: > "$KEYS"
for f in $(printf '%s\n' "$G" | awk '$1=="userknownhostsfile"{for(i=2;i<=NF;i++)print $i}'); do
  f="${f/#\~/$ORIG_HOME}"
  [ -f "$f" ] || continue
  ssh-keygen -F "$LOOKUP" -f "$f" | awk '!/^#/ && !/^@/ && NF>=3 {print $2, $3}' >> "$KEYS" || true
done
[ -s "$KEYS" ] || die "no verified host key for $LOOKUP in your known_hosts (SBX never trusts on first use)"
LINK_PORT="$(free_port)"
CLOSED_PORT="$(free_port)"
KH="$WORK/known_hosts"
entry() { if [ "$2" = "22" ]; then printf '%s' "$1"; else printf '[%s]:%s' "$1" "$2"; fi; }
while read -r type key; do
  printf '%s %s %s\n' "$(entry "$SSH_HOST" "$SSH_PORT")" "$type" "$key"
  printf '[127.0.0.1]:%s %s %s\n' "$LINK_PORT" "$type" "$key"
  printf '[127.0.0.1]:%s %s %s\n' "$CLOSED_PORT" "$type" "$key"
done < "$KEYS" > "$KH"
chmod 600 "$KH"
log "machine $SSH_USER@$SSH_HOST:$SSH_PORT, key $IDENTITY, host key copied from your known_hosts"

# Mac-side link to the machine's sshd, so the harness can cut and restore the network path.
cat > "$WORK/link.sh" <<LINK
#!/usr/bin/env bash
set -u
case "\$1" in
  up)
    HOME="$ORIG_HOME" ssh ${SBX_B2_SSH_CONFIG:+-F "$SBX_B2_SSH_CONFIG"} -o BatchMode=yes -o ExitOnForwardFailure=yes -N \\
      -L "127.0.0.1:$LINK_PORT:127.0.0.1:${SBX_B2_LINK_TARGET_PORT:-22}" "$ALIAS" \\
      < /dev/null > /dev/null 2>> "$WORK/link.err" &
    echo \$! > "$WORK/link.pid"
    for _ in \$(seq 1 50); do
      "$PY" -c 'import socket,sys;socket.create_connection(("127.0.0.1",$LINK_PORT),1)' 2>/dev/null && exit 0
      sleep 0.2
    done
    exit 1 ;;
  down)
    [ -f "$WORK/link.pid" ] && kill "\$(cat "$WORK/link.pid")" 2>/dev/null
    rm -f "$WORK/link.pid"; sleep 1 ;;
esac
LINK
chmod 700 "$WORK/link.sh"

# ------------------------------------------------------------- isolated control plane
export HOME="$WORK/home" XDG_CONFIG_HOME="$WORK/config" XDG_DATA_HOME="$WORK/data"
export XDG_STATE_HOME="$WORK/state" XDG_CACHE_HOME="$WORK/cache"
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME"
# Server/client settings come only from this run; the harness's own SBX_B2_* stay.
for v in $(env | awk -F= '/^SBX_/ && !/^SBX_B2_/ {print $1}'); do unset "$v"; done
PORT="$(free_port)"
export SBX_BASE_URL="http://127.0.0.1:$PORT"
# The FAKE validation-only inference endpoint is on 127.0.0.1 over http, which SBX accepts only
# with this documented operator setting (this isolated instance only).
export SBX_INFERENCE_ALLOW_PRIVATE_URLS=1

cleanup() {
  set +e
  log "cleanup"
  "$SBX" server logs --lines 400 > "$OUT/b2-server.log" 2>&1
  for m in ubuntu-dev ubuntu-link ubuntu-offline ubuntu-fake; do
    "$SBX" server machine remove "$m" > /dev/null 2>&1
  done
  "$SBX" server stop > /dev/null 2>&1
  "$WORK/link.sh" down
  if [ -f "$WORK/fake-inference.pid" ]; then
    kill "$(cat "$WORK/fake-inference.pid")" 2>/dev/null
    cp "$WORK/fake-inference.requests" "$OUT/fake-inference-requests.txt" 2>/dev/null
  fi
  if [ "$KEEP" = 0 ]; then
    if left="$(hssh "$ALIAS" "pgrep -af '[$(printf %s "$REMOTE_STATE" | cut -c1)]$(printf %s "$REMOTE_STATE" | cut -c2-)'; [ \$? -le 1 ]")"; then
      if [ -z "$left" ]; then
        hssh "$ALIAS" "rm -rf '$REMOTE_STATE'" && log "remote state removed; no process left"
      else
        log "LEFTOVER remote processes (state kept; stop them with the agent's terminate verb): $left"
      fi
    else
      log "could not check the machine for leftovers; remote state kept at $REMOTE_STATE"
    fi
    rm -rf "$WORK"
  else
    log "kept $WORK and $REMOTE_STATE"
  fi
}
trap cleanup EXIT

# FAKE inference endpoint (validation only, see fake_inference.py); stopped in cleanup.
"$PY" "$REPO/scripts/acceptance/fake_inference.py" "$WORK/fake-inference.port" \
  "$WORK/fake-inference.requests" < /dev/null > /dev/null 2>> "$WORK/fake-inference.err" &
echo $! > "$WORK/fake-inference.pid"
for _ in $(seq 1 50); do [ -s "$WORK/fake-inference.port" ] && break; sleep 0.1; done
[ -s "$WORK/fake-inference.port" ] || die "the fake inference endpoint did not start"
FAKE_INFERENCE="http://127.0.0.1:$(cat "$WORK/fake-inference.port")/v1"

"$SBX" server init --executors resident --port "$PORT" > "$OUT/server-init.json"
"$SBX" server start > "$OUT/server-start.json"
EMAIL="b2-acceptance@example.test"
PASSWORD="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(18))')"
printf '%s\n' "$PASSWORD" | "$SBX" auth register --email "$EMAIL" > /dev/null
TOKEN="$("$SBX" server outbox --to "$EMAIL" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["body"].split("token=")[1].split()[0])')"
printf '%s\n' "$TOKEN" | "$SBX" auth verify > /dev/null
printf '%s\n' "$PASSWORD" | "$SBX" auth login --email "$EMAIL" > /dev/null
# The resolver needs one inference Connection to create a Session. It points at the FAKE
# validation-only endpoint above with a REDACTED key; the driver waits until SBX's own
# validation reports it ready and pins it explicitly. No model call is made in B2.
printf 'REDACTED-fake-key\n' | "$SBX" connections add inference_api \
  --endpoint "openai_chat=$FAKE_INFERENCE" --model fake-validation-only > "$OUT/connection.json" \
  || die "adding the fake inference Connection failed: $(cat "$OUT/connection.json")"
CONNECTION_ID="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["id"])' "$OUT/connection.json")"
log "control plane at $SBX_BASE_URL (isolated in $WORK)"

add() {
  local name="$1" host="$2" port="$3"
  shift 3
  "$SBX" server machine add "$name" --owner-email "$EMAIL" --host "$host" --port "$port" \
    --user "$SSH_USER" --identity-file "$IDENTITY" --known-hosts-file "$KH" \
    --remote-python "$REMOTE_PYTHON" --remote-root "$REMOTE_ROOT" \
    --state-root "~/$REMOTE_STATE_REL" --remote-path "$REMOTE_PATH" "$@"
}
add ubuntu-dev "$SSH_HOST" "$SSH_PORT" > "$OUT/machine-ubuntu-dev.json" \
  || die "registering ubuntu-dev failed: $(cat "$OUT/machine-ubuntu-dev.json")"
"$WORK/link.sh" up || die "the link forward did not start"
add ubuntu-link 127.0.0.1 "$LINK_PORT" > "$OUT/machine-ubuntu-link.json" \
  || die "registering ubuntu-link failed: $(cat "$OUT/machine-ubuntu-link.json")"
add ubuntu-offline 127.0.0.1 "$CLOSED_PORT" --no-probe > "$OUT/machine-ubuntu-offline.json"
if [ "$FAKE" = 1 ]; then
  add ubuntu-fake "$SSH_HOST" "$SSH_PORT" \
    --cli-override "OPENCODE_BIN=$REMOTE_ROOT/tests/fakes/official/opencode_cli.py" \
    > "$OUT/machine-ubuntu-fake.json"
fi
log "machines registered; running the scenario"

DRIVER_ARGS=(--alias "$ALIAS" --remote-state "$REMOTE_STATE" --link "$WORK/link.sh"
  --sbx "$SBX" --evidence "$OUT/b2-evidence.json" --inference-connection "$CONNECTION_ID"
  --secret-shaped "$IDENTITY" "$KH" "$SSH_HOST" "$REMOTE_PYTHON" "$REMOTE_ROOT")
[ "$FAKE" = 1 ] && DRIVER_ARGS+=(--fake-turn)
set +e
"$PY" "$REPO/scripts/acceptance/b2_resident_driver.py" "${DRIVER_ARGS[@]}" | tee "$OUT/b2-checks.txt"
STATUS=${PIPESTATUS[0]}
set -e
log "evidence in $OUT (exit $STATUS)"
exit "$STATUS"
