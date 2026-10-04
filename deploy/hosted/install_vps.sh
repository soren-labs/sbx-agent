#!/usr/bin/env bash
# Run as root on the existing VPS after uploading the current source and secrets.
set -euo pipefail
[[ $(id -u) == 0 ]]
[[ $(uname -m) == aarch64 ]]
[[ -f /etc/sbx-hosted.env ]]
[[ $(stat -c %a /etc/sbx-hosted.env) == 600 ]]
id sbx >/dev/null
install -d -m 700 -o sbx -g sbx /var/lib/sbx-hosted
# The existing PostgreSQL database/role and tunnel are preserved.
runuser -u sbx -- psql -d sbx -Atc 'SELECT current_user' >/dev/null
if ! command -v node >/dev/null || [[ $(node --version) != v22.* ]]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
  apt-get install -y nodejs >/dev/null
fi
if [[ ! -x /usr/local/bin/codex ]] || \
   [[ $(/usr/local/bin/codex --version) != 'codex-cli 0.159.2' ]]; then
  npm install -g --prefix /usr/local @openai/codex@0.159.2 >/dev/null
fi
cd /opt/sbx-browser
runuser -u sbx -- env HOME=/var/lib/sbx-hosted UV_CACHE_DIR=/var/lib/sbx-hosted/uv-cache uv sync --frozen --no-dev
install -m 644 deploy/hosted/sbx-hosted.service /etc/systemd/system/sbx-hosted.service
systemctl daemon-reload
systemctl enable sbx-hosted.service
systemctl restart sbx-hosted.service
# The existing sbx-cloudflared connector already targets localhost:8000.
systemctl is-active sbx-cloudflared.service >/dev/null
for attempt in $(seq 1 60); do
  if curl --fail --silent http://127.0.0.1:8000/hosted/health >/dev/null; then
    exit 0
  fi
  sleep 1
done
exit 1
