#!/bin/sh
set -eu

ROOT=/opt/sbx-agent

command -v docker >/dev/null 2>&1 || {
  echo "Docker Engine is required on the production host" >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose plugin is required on the production host" >&2
  exit 1
}
command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required to bootstrap production secrets" >&2
  exit 1
}

install -d -m 0755 "$ROOT"

if [ ! -f "$ROOT/.env" ]; then
  umask 077
  postgres_password=$(openssl rand -hex 32)
  vault_key=$(openssl rand -base64 32 | tr -d '\n')
  runtime_key=$(openssl rand -hex 32)
  cat > "$ROOT/.env" <<EOF
SBX_POSTGRES_PASSWORD=$postgres_password
SBX_VAULT_KEYS=k1:$vault_key
SBX_RUNTIME_MASTER_KEY=$runtime_key
SBX_PUBLIC_URL=https://sbx-agent.com
SBX_ALLOWED_ORIGINS=https://sbx-agent.com
SBX_COOKIE_SECURE=1
SBX_EXECUTORS=modal
SBX_WORKER_THREADS=4
EOF
  chmod 0600 "$ROOT/.env"
  echo "Created $ROOT/.env with host-local production secrets"
else
  chmod 0600 "$ROOT/.env"
fi
