#!/bin/sh
# One-time (idempotent) preparation of the dedicated staging host. Run as root.
# Installs Docker Engine + Compose from the distribution, adds a small swap
# file as an out-of-memory safety margin, and generates host-local staging
# secrets. It never reads or copies anything from production.
set -eu

ROOT=/opt/sbx-staging
SWAP_FILE=/swapfile
SWAP_MB=${SBX_STAGING_SWAP_MB:-1024}
PUBLIC_URL=${SBX_STAGING_PUBLIC_URL:-https://staging.sbx-agent.com}

[ "$(id -u)" -eq 0 ] || {
  echo "bootstrap.sh must run as root" >&2
  exit 1
}

# Production lives in /opt/sbx-agent. Refuse to turn a production host into
# a staging host, or to run two stacks that both claim loopback port 8000.
if [ -e /opt/sbx-agent ]; then
  echo "/opt/sbx-agent exists: this looks like a production host; refusing to bootstrap staging here" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends docker.io docker-compose-v2 >/dev/null
fi
systemctl enable --now docker >/dev/null
command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required to generate staging secrets" >&2
  exit 1
}

if [ "$SWAP_MB" -gt 0 ] && ! swapon --show=NAME --noheadings | grep -q .; then
  if [ ! -f "$SWAP_FILE" ]; then
    fallocate -l "${SWAP_MB}M" "$SWAP_FILE"
    chmod 0600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null
  fi
  swapon "$SWAP_FILE"
  grep -q "^$SWAP_FILE " /etc/fstab || echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
  echo "Enabled ${SWAP_MB} MiB swap at $SWAP_FILE"
fi
# Swap is a safety margin, not working memory.
printf 'vm.swappiness=10\n' > /etc/sysctl.d/60-sbx-staging.conf
sysctl -q -p /etc/sysctl.d/60-sbx-staging.conf

install -d -m 0755 "$ROOT"
install -d -m 0700 "$ROOT/backups"

if [ ! -f "$ROOT/.env" ]; then
  umask 077
  postgres_password=$(openssl rand -hex 32)
  vault_key=$(openssl rand -base64 32 | tr -d '\n')
  runtime_key=$(openssl rand -hex 32)
  cat > "$ROOT/.env" <<EOF
SBX_POSTGRES_PASSWORD=$postgres_password
SBX_VAULT_KEYS=stg1:$vault_key
SBX_RUNTIME_MASTER_KEY=$runtime_key
SBX_PUBLIC_URL=$PUBLIC_URL
SBX_ALLOWED_ORIGINS=$PUBLIC_URL
SBX_COOKIE_SECURE=1
SBX_EXECUTORS=modal
SBX_WORKER_THREADS=2
EOF
  chmod 0600 "$ROOT/.env"
  echo "Created $ROOT/.env with host-local staging secrets"
else
  chmod 0600 "$ROOT/.env"
fi

# Pull-based rollout: installed when the unit files were uploaded next to this
# script. The timer only acts once a `staging-current` image has been published.
here=$(dirname "$0")
if [ -f "$here/reconcile.sh" ] && [ -f "$here/sbx-staging-reconcile.timer" ]; then
  install -m 0755 "$here/reconcile.sh" "$ROOT/reconcile.sh"
  install -m 0644 "$here/sbx-staging-reconcile.service" /etc/systemd/system/sbx-staging-reconcile.service
  install -m 0644 "$here/sbx-staging-reconcile.timer" /etc/systemd/system/sbx-staging-reconcile.timer
  systemctl daemon-reload
  systemctl enable --now sbx-staging-reconcile.timer >/dev/null
fi
