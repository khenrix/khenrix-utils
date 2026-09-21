#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib.sh"
"$LAB_ROOT/scripts/controller-build.sh"
evidence_dir=$(new_evidence_dir pier-preflight)
chmod 0700 "$evidence_dir"
trap 'status=$?; trap - EXIT; finalize_evidence "$evidence_dir" || status=70; exit "$status"' EXIT
export DOCKER_HOST
DOCKER_HOST=$(docker_host_for_lab)
export DOCKER_HOST
record_run_inputs "$evidence_dir" config/pier-preflight.json
controller_image_id=$(docker image inspect maka-lab-controller:0.2.0-dev.44.20260920 --format '{{.Id}}')
egress_image_id=$(docker image inspect maka-eval-egress-proxy:12.2.3 --format '{{.Id}}')
docker image inspect "$controller_image_id" > "$evidence_dir/controller-image.json"
docker image inspect "$egress_image_id" > "$evidence_dir/egress-image.json"
docker run --rm \
  --ulimit core=0 \
  --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
  --mount "type=bind,source=$LAB_ROOT,target=$LAB_ROOT" \
  --workdir "$LAB_ROOT" \
  --env "MAKA_LAB_ROOT=$LAB_ROOT" \
  --env "MAKA_EVIDENCE_DIR=$evidence_dir" \
  "$controller_image_id" \
  bash controller/run-pier-preflight-inside.sh
printf '%s\n' "$evidence_dir"
