#!/usr/bin/env bash
# Pull-based rollout for the staging host. Runs from a systemd timer as root.
#
# The desired release is whatever image the registry tag `staging-current`
# points at. Only the protected staging rollout job on GitHub Actions moves
# that tag. This host therefore needs no inbound access from GitHub and
# GitHub holds no credential for this host: the host reads a public tag and
# rolls itself with deploy.sh (digest-pinned, readiness-checked, with rollback).
set -euo pipefail

ROOT=/opt/sbx-staging
REPOSITORY=soren-labs/sbx-agent
TAG=${SBX_STAGING_TRACK_TAG:-staging-current}

cd "$ROOT"

digest=$(
  python3 - "$REPOSITORY" "$TAG" <<'PY'
import json
import sys
import urllib.error
import urllib.request

repository, tag = sys.argv[1:3]
with urllib.request.urlopen(
    f"https://ghcr.io/token?scope=repository:{repository}:pull", timeout=20
) as response:
    token = json.load(response)["token"]
request = urllib.request.Request(
    f"https://ghcr.io/v2/{repository}/manifests/{tag}",
    method="HEAD",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.oci.image.manifest.v1+json, "
        "application/vnd.oci.image.index.v1+json, "
        "application/vnd.docker.distribution.manifest.v2+json",
    },
)
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        print(response.headers.get("Docker-Content-Digest", ""))
except urllib.error.HTTPError as exc:
    if exc.code != 404:
        raise
PY
)

if [ -z "$digest" ]; then
  exit 0 # no desired release published yet
fi
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "Unexpected digest for $TAG: $digest" >&2
  exit 1
}

image="ghcr.io/$REPOSITORY@$digest"
if [ -f release.env.last-good ] && grep -qxF "SBX_IMAGE=$image" release.env.last-good; then
  exit 0 # already running the desired release
fi
if [ -f reconcile.failed ] && grep -qxF "$image" reconcile.failed; then
  exit 0 # this release already failed here; wait for a new one instead of looping
fi

# A rollout started by an operator holds the same lock deploy.sh takes; try again later.
flock -n "$ROOT/.deploy.lock" true || exit 0

docker pull --quiet "$image" >/dev/null
revision=$(docker image inspect "$image" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')

echo "Rolling staging to $image (revision $revision)"
if ./deploy.sh "$image" "$revision"; then
  rm -f reconcile.failed
else
  echo "$image" > reconcile.failed
  exit 1
fi
