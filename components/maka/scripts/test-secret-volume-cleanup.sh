#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib.sh"
export DOCKER_HOST
DOCKER_HOST=$(docker_host_for_lab)
export DOCKER_HOST

suffix="$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
secret_volume="maka-cleanup-test-$suffix"
container="maka-cleanup-test-$suffix"
scratch=$(mktemp -d)
fallback_cleanup() {
  trap - EXIT
  chmod 0700 "$scratch" 2>/dev/null || true
  docker container rm -f "$container" >/dev/null 2>&1 || true
  docker volume rm "$secret_volume" >/dev/null 2>&1 || true
  python3 - "$scratch" <<'PY'
from pathlib import Path
import shutil
import sys
shutil.rmtree(Path(sys.argv[1]), ignore_errors=True)
PY
}
trap fallback_cleanup EXIT

docker volume create "$secret_volume" >/dev/null
docker run -d --name "$container" --ulimit core=0 \
  --mount "type=volume,source=$secret_volume,target=/run/maka-secret" \
  --entrypoint sh maka-lab-controller:0.2.0-dev.47.20260922 \
  -c 'sleep 300' >/dev/null

# Model an unavailable evidence location. Cleanup must not read or write it.
chmod 000 "$scratch"
cleanup_secret_volume "$secret_volume"
test -n "$SECRET_CLEANUP_CONTAINERS_BEFORE"
test -z "$SECRET_CLEANUP_CONTAINERS_AFTER"
test "$SECRET_CLEANUP_VOLUME_REMOVED" = true
test "$SECRET_CLEANUP_OK" = true
test -z "$(docker container ls -aq --filter "name=^/${container}$")"
! docker volume inspect "$secret_volume" >/dev/null 2>&1

chmod 0700 "$scratch"
python3 - "$scratch" <<'PY'
from pathlib import Path
import shutil
import sys
shutil.rmtree(Path(sys.argv[1]))
PY
trap - EXIT
printf '%s\n' 'Secret volume cleanup survived an unavailable evidence directory'
