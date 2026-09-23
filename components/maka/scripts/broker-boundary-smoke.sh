#!/usr/bin/env bash
set -euo pipefail
ulimit -S -c 0
ulimit -H -c 0
test "$(ulimit -S -c)" = 0
test "$(ulimit -H -c)" = 0
source "$(dirname "$0")/lib.sh"
"$LAB_ROOT/scripts/controller-build.sh"
evidence_dir=$(new_evidence_dir broker-boundary)
chmod 0700 "$evidence_dir"
trap 'status=$?; trap - EXIT; finalize_evidence "$evidence_dir" || status=70; exit "$status"' EXIT
export DOCKER_HOST
DOCKER_HOST=$(docker_host_for_lab)
export DOCKER_HOST
record_run_inputs "$evidence_dir" config/harbor-fix-git.json
controller_image_id=$(docker image inspect maka-lab-controller:0.2.0-dev.47.20260922 --format '{{.Id}}')
python3 "$LAB_ROOT/scripts/broker-boundary-smoke.py" "$controller_image_id" "$evidence_dir" "$LAB_ROOT"
runtime_root="$LAB_ROOT/runtime/linux-amd64/maka-0.2.0-dev.47.20260922-node-24.16.0-relay-root-virtiofs-v2-eval-openai-onboarding-v2"
cp "$runtime_root/RUNTIME_SHA256SUMS" "$evidence_dir/runtime-file-SHA256SUMS"
cp "$runtime_root/RUNTIME_SYMLINKS" "$evidence_dir/runtime-symlinks.txt"
printf '%s\n' "$evidence_dir"
