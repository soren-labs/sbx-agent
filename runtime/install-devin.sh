#!/usr/bin/env bash
# Install the pinned standalone Devin CLI bundle (SOR-74).
#
# Mirrors the layout of https://cli.devin.ai/install.sh without running it:
# the bundle is unpacked under /opt/devin/cli/_versions/<ver>/, `current` is
# linked to it, and `devin` is linked onto PATH via /usr/local/bin. We do not
# run `devin setup` (interactive auth/MCP wizard) — auth comes only from the
# restored SBX_ACCOUNT_CREDENTIAL blob at runtime.
#
# Required env (rendered from runtime/packages.txt by image.py):
#   SBX_DEVIN_VERSION            e.g. 3000.10.21
#   SBX_DEVIN_SHA256_X86_64      bundle sha256 for x86_64-unknown-linux
#   SBX_DEVIN_SHA256_AARCH64     bundle sha256 for aarch64-unknown-linux
# Optional:
#   SBX_DEVIN_BASE_URL           default https://static.devin.ai/cli
set -euo pipefail

VER="${SBX_DEVIN_VERSION:?SBX_DEVIN_VERSION is required}"
BASE="${SBX_DEVIN_BASE_URL:-https://static.devin.ai/cli}"

case "$(uname -m)" in
  x86_64)
    TARGET="x86_64-unknown-linux"
    SHA="${SBX_DEVIN_SHA256_X86_64:?SBX_DEVIN_SHA256_X86_64 is required}"
    ;;
  aarch64 | arm64)
    TARGET="aarch64-unknown-linux"
    SHA="${SBX_DEVIN_SHA256_AARCH64:?SBX_DEVIN_SHA256_AARCH64 is required}"
    ;;
  *)
    echo "install-devin.sh: unsupported arch $(uname -m)" >&2
    exit 1
    ;;
esac

DEST="/opt/devin/cli/_versions/${VER}"
TMP="/tmp/devin-cli-${VER}.tar.gz"

mkdir -p "${DEST}"
curl -fsSL "${BASE}/${VER}/devin-${VER}-${TARGET}.tar.gz" -o "${TMP}"
echo "${SHA}  ${TMP}" | sha256sum -c -
tar xzf "${TMP}" -C "${DEST}"
rm -f "${TMP}"

# Same marker the official installer writes for curl|bash installs.
echo "curl-bash" > "${DEST}/distribution"

ln -sfn "${VER}" /opt/devin/cli/_versions/current
mkdir -p /usr/local/bin
ln -sfn /opt/devin/cli/_versions/current/bin/devin /usr/local/bin/devin

devin --version
