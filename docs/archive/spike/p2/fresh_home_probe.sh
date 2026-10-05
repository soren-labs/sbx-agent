#!/usr/bin/env bash
# SOR-73 (P2.1-S0) — Devin CLI standalone auth on a fresh HOME/XDG (clean-room probe).
#
# Verifies the minimum credential file set for `devin auth status` + a
# non-interactive `devin -p` turn under a scrubbed environment: no ACP_BACKEND,
# no DEVIN_*/WINDSURF_* env vars, no inherited config. Compares the credential
# file hash before/after to detect CLI-side rewrites.
#
# Never prints credential values — only a truncated sha256 prefix.
#
# Run:  bash spike/p2/fresh_home_probe.sh
# Env:  DEVIN_BIN (default devin), DEVIN_SPIKE_MODEL (default swe-2-high),
#       DEVIN_SPIKE_TIMEOUT (default 300), SKIP_SMOKE=1 to skip the `-p` turn.
set -u

DEVIN_BIN="${DEVIN_BIN:-devin}"
MODEL="${DEVIN_SPIKE_MODEL:-swe-2-high}"
TIMEOUT="${DEVIN_SPIKE_TIMEOUT:-300}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$HERE/out"
mkdir -p "$OUT_DIR"

CRED_SRC="${XDG_DATA_HOME:-$HOME/.local/share}/devin/credentials.toml"
if [ ! -f "$CRED_SRC" ]; then
  echo "FATAL: credentials.toml not found at $CRED_SRC" >&2
  exit 2
fi

FRESH="$(mktemp -d /tmp/devin-fresh.XXXXXX)"
trap 'rm -rf "$FRESH"' EXIT
mkdir -p "$FRESH/.local/share/devin" "$FRESH/ws"
cp "$CRED_SRC" "$FRESH/.local/share/devin/credentials.toml"
chmod 600 "$FRESH/.local/share/devin/credentials.toml"

hash16() { sha256sum "$1" | cut -c1-16; }
json_bool() { [ "$1" = "true" ] && echo true || echo false; }

H_BEFORE="$(hash16 "$FRESH/.local/share/devin/credentials.toml")"

echo "== 1) devin auth status, scrubbed env (fresh HOME) =="
AUTH_OUT="$(env -i PATH="$PATH" HOME="$FRESH" LANG=C.UTF-8 TERM=dumb \
  "$DEVIN_BIN" auth status 2>&1)"
AUTH_OK=false; grep -q "Logged in" <<<"$AUTH_OUT" && AUTH_OK=true
TIER="$(grep -oE 'Tier: +[A-Za-z ]+' <<<"$AUTH_OUT" | head -1 | tr -s ' ')"
echo "   logged_in=$AUTH_OK  ${TIER:-tier=unknown}"

echo "== 2) negative control: ACP_BACKEND=windsurf leaked into env =="
ACP_OUT="$(env -i PATH="$PATH" HOME="$FRESH" LANG=C.UTF-8 TERM=dumb \
  ACP_BACKEND=windsurf "$DEVIN_BIN" auth status 2>&1)"
ACP_BROKEN=false; grep -q "Not logged in" <<<"$ACP_OUT" && ACP_BROKEN=true
echo "   acp_backend_breaks_auth=$ACP_BROKEN"

echo "== 3) devin -p smoke (model=$MODEL, stdin=/dev/null) =="
if [ "${SKIP_SMOKE:-0}" = "1" ]; then
  P_RC=-1; PONG=skipped
else
  cd "$FRESH/ws"
  P_OUT="$(timeout "$TIMEOUT" env -i PATH="$PATH" HOME="$FRESH" LANG=C.UTF-8 TERM=dumb \
    "$DEVIN_BIN" -p "Reply with exactly: PONG" --model "$MODEL" \
    --respect-workspace-trust false </dev/null 2>&1)"
  P_RC=$?
  PONG=false; grep -q "PONG" <<<"$P_OUT" && PONG=true
fi
echo "   rc=$P_RC pong=$PONG"

H_AFTER="$(hash16 "$FRESH/.local/share/devin/credentials.toml")"
CRED_CHANGED=false; [ "$H_BEFORE" != "$H_AFTER" ] && CRED_CHANGED=true
echo "== 4) credential hash =="
echo "   before=$H_BEFORE after=$H_AFTER changed=$CRED_CHANGED"

PASS=false
if [ "$AUTH_OK" = "true" ] && { [ "$PONG" = "true" ] || [ "$PONG" = "skipped" ]; }; then
  PASS=true
fi

cat > "$OUT_DIR/fresh_home.json" <<EOF
{
  "probe": "fresh_home",
  "devin_version": "$("$DEVIN_BIN" --version 2>/dev/null | head -1)",
  "cred_source": "\$XDG_DATA_HOME/devin/credentials.toml or ~/.local/share/devin/credentials.toml",
  "min_files": [".local/share/devin/credentials.toml"],
  "auth_logged_in": $(json_bool "$AUTH_OK"),
  "tier": "${TIER:-unknown}",
  "acp_backend_breaks_auth": $(json_bool "$ACP_BROKEN"),
  "smoke": {"rc": $P_RC, "pong_seen": $(json_bool "$PONG"), "model": "$MODEL"},
  "cred_hash16_before": "$H_BEFORE",
  "cred_hash16_after": "$H_AFTER",
  "cred_hash_changed": $(json_bool "$CRED_CHANGED"),
  "verdict": $(json_bool "$PASS")
}
EOF

echo "wrote $OUT_DIR/fresh_home.json"
[ "$PASS" = "true" ]
