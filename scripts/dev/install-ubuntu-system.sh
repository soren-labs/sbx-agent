#!/usr/bin/env bash
set -euo pipefail
# SBX workstation prerequisites only: no dist-upgrade, reboot, credentials or sudoers changes.
sudo -v
sudo apt-get update
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  git git-lfs gh curl ca-certificates ripgrep build-essential pkg-config tmux jq \
  zip unzip xz-utils shellcheck python3-venv python3-dev libpq-dev \
  postgresql postgresql-client docker.io docker-compose-v2 uidmap dbus-user-session slirp4netns fuse-overlayfs
printf '\nSBX system prerequisites installed. No user group or sudo policy was changed.\n'
printf 'Docker group access, if desired, is a separate explicit setup choice.\n'
