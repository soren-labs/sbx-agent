#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
npm --prefix console ci
VITE_HOSTED=1 VITE_API_BASE=https://api.sbx-agent.com npm --prefix console run build
# Review console/dist before separately deploying deploy/hosted/wrangler.toml.
