#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
npm --prefix console ci
VITE_HOSTED=1 VITE_API_MODE=http VITE_API_BASE=https://api.sbx-agent.com \
  VITE_BUILD_SHA="$(git rev-parse HEAD)" npm --prefix console run build
# Review console/dist before separately deploying deploy/hosted/wrangler.toml.
