#!/usr/bin/env bash
# Roll the staging backend to one image. Runs on the staging host as root:
#
#   deploy.sh <image-ref> <git-sha>
#
# <image-ref> must be digest-pinned (name@sha256:...) so the deployed artifact
# is exactly the one built and identified on GitHub Actions. The database
# volume is preserved; a dump is taken before migrations. If the new container
# does not become ready, the last known-good image is restored.
set -euo pipefail

ROOT=/opt/sbx-staging
CONTAINER=sbx-staging-sbx-1
KEEP_BACKUPS=5

IMAGE=${1:?usage: deploy.sh <image-ref> <git-sha>}
GIT_SHA=${2:?usage: deploy.sh <image-ref> <git-sha>}

case "$IMAGE" in
  *@sha256:*) ;;
  *)
    # A tag can move between build and deploy. The override exists only for
    # rehearsing this script with an image loaded directly onto the host.
    if [ "${SBX_STAGING_ALLOW_UNPINNED:-0}" != "1" ]; then
      echo "Refusing image without a digest: $IMAGE" >&2
      exit 1
    fi
    ;;
esac
[[ "$GIT_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "git-sha must be a full 40-character commit SHA" >&2
  exit 1
}

cd "$ROOT"
[ -f .env ] || {
  echo "$ROOT/.env is missing; run bootstrap.sh first" >&2
  exit 1
}

# One deployment at a time on the host, independent of the caller.
exec 9> "$ROOT/.deploy.lock"
flock -n 9 || {
  echo "Another staging deployment is in progress" >&2
  exit 1
}

compose() {
  docker compose --env-file .env --env-file release.env -f compose.yml "$@" </dev/null
}

write_release() {
  printf 'SBX_IMAGE=%s\nSBX_GIT_SHA=%s\n' "$1" "$2" > release.env
}

ready() {
  for _ in $(seq 1 "$1"); do
    if curl -fsS http://127.0.0.1:8000/readyz >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

rollback() {
  echo "Staging deployment failed: $1" >&2
  compose logs --tail=150 sbx >&2 || true
  if [ -f release.env.last-good ]; then
    cp release.env.last-good release.env
    compose up -d --force-recreate sbx || true
    if ready 30; then
      echo "Restored last known-good release: $(grep '^SBX_GIT_SHA=' release.env)" >&2
    else
      echo "Rollback did not become ready; staging needs manual attention" >&2
    fi
  fi
  exit 1
}

write_release "$IMAGE" "$GIT_SHA"

if compose ps -q postgres | grep -q .; then
  compose exec -T postgres pg_dump -U sbx sbx | gzip > "backups/pre-${GIT_SHA}.sql.gz"
  # shellcheck disable=SC2012
  ls -1t backups/pre-*.sql.gz | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
fi

if [ "${SBX_STAGING_ALLOW_UNPINNED:-0}" = "1" ]; then
  compose pull postgres
else
  compose pull
fi
compose up -d postgres

pg_ready=0
for _ in $(seq 1 30); do
  if compose exec -T postgres pg_isready -U sbx -d sbx >/dev/null 2>&1; then
    pg_ready=1
    break
  fi
  sleep 2
done
[ "$pg_ready" -eq 1 ] || rollback "PostgreSQL did not become ready"

compose run --rm --no-deps -T sbx python -m control.composition migrate || rollback "migration failed"
compose up -d --force-recreate sbx

ready 45 || rollback "control plane did not become ready"

actual_image=$(docker inspect "$CONTAINER" --format '{{.Config.Image}}')
[ "$actual_image" = "$IMAGE" ] || rollback "image mismatch: expected=$IMAGE actual=$actual_image"

actual_sha=$(docker inspect "$CONTAINER" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
[ "$actual_sha" = "$GIT_SHA" ] || rollback "revision mismatch: expected=$GIT_SHA actual=$actual_sha"

cp release.env release.env.last-good
docker image prune -af >/dev/null || true
echo "Staging is ready on $IMAGE (revision $GIT_SHA)"
