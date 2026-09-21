#!/usr/bin/env bash
set -euo pipefail
umask 077
ulimit -S -c 0
ulimit -H -c 0
test "$(ulimit -S -c)" = 0
test "$(ulimit -H -c)" = 0
source "$(dirname "$0")/lib.sh"
"$LAB_ROOT/scripts/controller-build.sh"
evidence_dir=$(new_evidence_dir harbor-fix-git)
chmod 0700 "$evidence_dir"
secret_volume="maka-eval-secret-$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
volume_created=false
benchmark_bundle_dir=
cleanup_benchmark_bundle() {
  test -z "$benchmark_bundle_dir" && return 0
  if ! python3 - "$benchmark_bundle_dir" "$LAB_ROOT/controller/.build" <<'PY'
from pathlib import Path
import shutil
import sys

candidate = Path(sys.argv[1])
parent = Path(sys.argv[2]).resolve()
if candidate.parent.resolve() != parent or not candidate.name.startswith("benchmark-bundle-"):
    raise SystemExit("refusing to remove an unexpected benchmark bundle directory")
suffix = candidate.name.removeprefix("benchmark-bundle-")
if len(suffix) != 6 or not suffix.isalnum():
    raise SystemExit("refusing to remove an unexpected benchmark bundle directory")
shutil.rmtree(candidate)
PY
  then
    return 1
  fi
  benchmark_bundle_dir=
}
find "$LAB_ROOT" -type f -name core -print \
  | sed "s#^$LAB_ROOT/##" \
  > "$evidence_dir/core-files-before.txt"
test ! -s "$evidence_dir/core-files-before.txt" || { echo 'Existing core file under the lab root blocks credentialed evaluation' >&2; exit 70; }
finish() {
  run_status=$?
  status=$run_status
  trap - EXIT INT TERM
  set +e

  # Remove every process and volume that can hold the credential before any
  # evidence operation. Evidence may be absent, full, or deliberately made
  # unwritable by the evaluated task.
  if test "$volume_created" = true; then
    if cleanup_secret_volume "$secret_volume"; then
      volume_created=false
    else
      status=70
    fi
  else
    SECRET_CLEANUP_CONTAINERS_BEFORE=""
    SECRET_CLEANUP_CONTAINERS_AFTER=""
    SECRET_CLEANUP_VOLUME_REMOVED=true
    SECRET_CLEANUP_OK=true
  fi
  cleanup_benchmark_bundle || status=70
  daemon_limits_after=$(assert_maka_daemon_core_limits 2>&1)
  daemon_limits_status=$?
  test "$daemon_limits_status" -eq 0 || status=70

  if test "$run_status" -eq 79 || test "$run_status" -eq 80; then
    suspect_evidence_dir=$evidence_dir
    if python3 - "$suspect_evidence_dir" "$LAB_ROOT/evidence/runs" <<'PY'
import os
from pathlib import Path
import shutil
import sys

candidate = Path(os.path.abspath(sys.argv[1]))
base = Path(sys.argv[2]).resolve()
if candidate.parent != base or candidate == base:
    raise SystemExit("refusing to quarantine a path outside the Maka run root")
def retry(function, path, _error):
    os.chmod(path, 0o700 if os.path.isdir(path) else 0o600, follow_symlinks=False)
    function(path)
if candidate.is_symlink():
    candidate.unlink()
elif candidate.exists():
    shutil.rmtree(candidate, onerror=retry)
candidate.mkdir(mode=0o700)
PY
    then
      evidence_dir=$suspect_evidence_dir
      if ! printf '%s\n' 'The broker could not complete its retained-evidence scan; the original run directory was deleted and recreated.' \
        > "$evidence_dir/credential-scan-failure.txt"; then
        status=70
      fi
      record_run_inputs "$evidence_dir" config/harbor-fix-git.json || status=70
    else
      evidence_dir=$(new_evidence_dir harbor-quarantine-failure)
      chmod 0700 "$evidence_dir" || status=70
      if ! printf '%s\n' "Failed to remove suspect run directory: $suspect_evidence_dir" \
        > "$evidence_dir/credential-scan-failure.txt"; then
        status=70
      fi
      status=70
    fi
  fi

  if ! printf '%s\n' "$SECRET_CLEANUP_CONTAINERS_BEFORE" | sed '/^$/d' \
    > "$evidence_dir/secret-volume-containers-before-cleanup.txt"; then
    status=70
  fi
  if ! printf '%s\n' "$SECRET_CLEANUP_CONTAINERS_AFTER" | sed '/^$/d' \
    > "$evidence_dir/secret-volume-containers-after-cleanup.txt"; then
    status=70
  fi
  if ! printf '%s\n' "$daemon_limits_after" > "$evidence_dir/daemon-core-limits-after.txt"; then
    status=70
  fi
  if ! printf '{"ok":%s,"containersBefore":%d,"containersAfter":%d,"volumeRemoved":%s}\n' \
    "$SECRET_CLEANUP_OK" \
    "$(printf '%s\n' "$SECRET_CLEANUP_CONTAINERS_BEFORE" | sed '/^$/d' | wc -l | tr -d ' ')" \
    "$(printf '%s\n' "$SECRET_CLEANUP_CONTAINERS_AFTER" | sed '/^$/d' | wc -l | tr -d ' ')" \
    "$SECRET_CLEANUP_VOLUME_REMOVED" \
    > "$evidence_dir/secret-cleanup-result.json"; then
    status=70
  fi
  if test "$SECRET_CLEANUP_OK" != true || test -n "$SECRET_CLEANUP_CONTAINERS_AFTER" || test "$SECRET_CLEANUP_VOLUME_REMOVED" != true; then
    if ! printf '%s\n' 'Secret-bearing Docker resource cleanup was incomplete' \
      > "$evidence_dir/cleanup-error.txt"; then
      status=70
    fi
    status=70
  fi
  core_files_after=$(find "$LAB_ROOT" -type f -name core -print | sed "s#^$LAB_ROOT/##")
  core_scan_status=$?
  test "$core_scan_status" -eq 0 || status=70
  if ! printf '%s\n' "$core_files_after" | sed '/^$/d' > "$evidence_dir/core-files-after.txt"; then
    status=70
  fi
  if test -n "$core_files_after"; then
    printf '%s\n' 'A core file appeared under the lab root' >> "$evidence_dir/cleanup-error.txt" || status=70
    status=70
  fi
  finalize_evidence "$evidence_dir" || status=70
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export DOCKER_HOST
DOCKER_HOST=$(docker_host_for_lab)
export DOCKER_HOST
record_run_inputs "$evidence_dir" config/harbor-fix-git.json
benchmark_repository=$(ensure_benchmark_repository)
test "$(git -C "$benchmark_repository" rev-parse HEAD)" = cd855d42cdea03da2d781ae1dbc89f74b39a4491
test "$(git -C "$benchmark_repository" rev-parse 'HEAD^{tree}')" = 43a3b7807bc2dc02c200f12b3575ae966e511c19
test "$(git -C "$benchmark_repository" write-tree)" = 43a3b7807bc2dc02c200f12b3575ae966e511c19
test "$(git -C "$benchmark_repository" rev-parse HEAD:tasks/fix-git)" = 08b47ce8eebc1d6e43b814bba691d6d559d7c5de
test -z "$(git -C "$benchmark_repository" status --porcelain=v1 --untracked-files=all)"
test "$(sed -n 's/^docker_image = "\(.*\)"/\1/p' "$benchmark_repository/tasks/fix-git/task.toml")" \
  = alexgshaw/fix-git@sha256:389b9c8247610c2c5be080b1ac00429007c2c69bf57f7f26c79f0f75ba2d5c74
install -d -m 0700 "$LAB_ROOT/controller/.build"
benchmark_bundle_dir=$(mktemp -d "$LAB_ROOT/controller/.build/benchmark-bundle-XXXXXX")
benchmark_bundle="$benchmark_bundle_dir/fix-git.bundle"
git -C "$benchmark_repository" bundle create "$benchmark_bundle" HEAD refs/heads/main
chmod 0600 "$benchmark_bundle"
git -C "$benchmark_repository" bundle verify "$benchmark_bundle" >/dev/null
test "$(git bundle list-heads "$benchmark_bundle" | LC_ALL=C sort)" = "$(printf '%s\n%s' \
  'cd855d42cdea03da2d781ae1dbc89f74b39a4491 HEAD' \
  'cd855d42cdea03da2d781ae1dbc89f74b39a4491 refs/heads/main')"
benchmark_bundle_sha256=$(shasum -a 256 "$benchmark_bundle" | awk '{print $1}')
printf '%s  %s\n' "$benchmark_bundle_sha256" fix-git.bundle \
  > "$evidence_dir/benchmark-bundle-SHA256SUM"
assert_maka_daemon_core_limits > "$evidence_dir/daemon-core-limits-before.txt"
controller_image_id=$(docker image inspect maka-lab-controller:0.2.0-dev.44.20260920 --format '{{.Id}}')
egress_image_id=$(docker image inspect maka-eval-egress-proxy:12.2.3 --format '{{.Id}}')
docker run --rm --ulimit core=0 --entrypoint cat "$controller_image_id" /proc/sys/kernel/core_pattern \
  > "$evidence_dir/core-pattern.txt"
test "$(cat "$evidence_dir/core-pattern.txt")" = core
docker image inspect "$controller_image_id" > "$evidence_dir/controller-image.json"
docker image inspect "$egress_image_id" > "$evidence_dir/egress-image.json"
docker image inspect alexgshaw/fix-git@sha256:389b9c8247610c2c5be080b1ac00429007c2c69bf57f7f26c79f0f75ba2d5c74 > "$evidence_dir/task-image.json"
docker volume create \
  --label io.maka.lab.secret-broker=true \
  --label "io.maka.lab.run=$secret_volume" \
  "$secret_volume" >/dev/null
volume_created=true

/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  /usr/bin/python3 -I -S -B "$LAB_ROOT/scripts/harden_python_runtime.py" verify >/dev/null
set +e
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  /usr/bin/python3 -I -S -B "$LAB_ROOT/scripts/run_hardened_python.py" key-export | docker run --rm -i \
  --ulimit core=0 \
  --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
  --mount "type=bind,source=$LAB_ROOT,target=$LAB_ROOT" \
  --mount "type=volume,source=$secret_volume,target=/run/maka-secret" \
  --workdir "$LAB_ROOT" \
  --env "MAKA_LAB_ROOT=$LAB_ROOT" \
  --env "MAKA_BENCHMARK_BUNDLE_PATH=$benchmark_bundle" \
  --env "MAKA_BENCHMARK_BUNDLE_SHA256=$benchmark_bundle_sha256" \
  --env "MAKA_EVIDENCE_DIR=$evidence_dir" \
  --env "MAKA_EGRESS_IMAGE_ID=$egress_image_id" \
  --env "MAKA_EVAL_SECRET_VOLUME=$secret_volume" \
  "$controller_image_id" \
  python controller/secret-broker-exec.py bash controller/run-harbor-inside.sh
status=$?
if { test "$status" -eq 79 || test "$status" -eq 80; } && ! test -d "$evidence_dir"; then
  install -d -m 0700 "$evidence_dir" || true
  printf '%s\n' 'The broker removed this run directory after its retained-evidence scan failed; host cleanup will recreate sanitized failure evidence.' \
    > "$evidence_dir/credential-scan-failure.txt" || true
fi
printf '{"controllerExitCode":%d}\n' "$status" > "$evidence_dir/controller-result.json" || true
printf '%s\n' "$evidence_dir" || true
exit "$status"
