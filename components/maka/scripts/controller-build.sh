#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib.sh"
benchmark_repository=$(ensure_benchmark_repository)
"$LAB_ROOT/scripts/colima-start.sh"
export DOCKER_HOST
DOCKER_HOST=$(docker_host_for_lab)
export DOCKER_HOST
docker build --platform linux/amd64 -f "$LAB_ROOT/controller/Dockerfile" \
  -t maka-lab-controller:0.2.0-dev.44.20260920 "$LAB_ROOT"
test "$(docker image inspect maka-lab-controller:0.2.0-dev.44.20260920 --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = feb9cf22fa216ce499860ad7cbcd59c32d28aa97
test "$(docker image inspect maka-lab-controller:0.2.0-dev.44.20260920 --format '{{index .Config.Labels "io.maka.eval.compatibility-overlay"}}')" = relay-root-virtiofs-v2-eval-openai-onboarding-v2
test "$(docker image inspect maka-lab-controller:0.2.0-dev.44.20260920 --format '{{index .Config.Labels "io.maka.eval.model-fetcher-sha256"}}')" = 82d4fdb90a8c3794a745f2e39410f19970c7a8afc679e14589244992ac50e56c
test "$(docker image inspect maka-lab-controller:0.2.0-dev.44.20260920 --format '{{index .Config.Labels "io.maka.eval.maka-subject-sha256"}}')" = 2f39e06a20291b7d2759d5bd9d54e912c3d32817c15f74140ef9cf62b51ec8f3
stage_probe="$LAB_ROOT/runtime/linux-amd64/.staging-Zz09Yx"
cleanup_stage_probe() {
  if test -d "$stage_probe"; then
    chmod -R u+w "$stage_probe" 2>/dev/null || true
    find "$stage_probe" -depth -delete 2>/dev/null || true
  fi
}
trap cleanup_stage_probe EXIT
install -d -m 0755 "$stage_probe"
printf '%s\n' stale > "$stage_probe/marker"
test "$(docker run --rm --ulimit core=0 \
  --mount "type=bind,source=$LAB_ROOT,target=$LAB_ROOT" \
  --workdir "$LAB_ROOT" \
  --env "MAKA_LAB_ROOT=$LAB_ROOT" \
  maka-lab-controller:0.2.0-dev.44.20260920 pwd)" = "$LAB_ROOT"
test ! -e "$stage_probe"
runtime_root="$LAB_ROOT/runtime/linux-amd64/maka-0.2.0-dev.44.20260920-node-24.16.0-relay-root-virtiofs-v2-eval-openai-onboarding-v2"
runtime_path_proof=$(docker run --rm --network none --ulimit core=0 \
  --entrypoint "$runtime_root/node/bin/node" \
  --mount "type=bind,source=$LAB_ROOT,target=$LAB_ROOT,readonly" \
  --workdir "$LAB_ROOT" \
  maka-lab-controller:0.2.0-dev.44.20260920 \
  controller/assert-eval-runtime-path.mjs \
  "$runtime_root" "$runtime_root/maka-agent")
jq -e '
  .schema == "maka-eval-runtime-path-v3"
  and .cli == "maka-agent/dist/cli.js"
  and .evalEntry == "maka-agent/node_modules/@maka/eval/dist/index.js"
  and .harborRelayRoot == "maka-agent/node_modules/@maka/eval/harbor"
  and .relayAgent == "maka-agent/node_modules/@maka/eval/harbor/relay_agent.py"
  and .relayAgentSha256 == "8761f73c2940365ca8a5861a9057a62f0ea2de6276b393512e50f72cb66e3bd0"
  and .modelFetcher == "maka-agent/node_modules/@maka/runtime/dist/model-fetcher.js"
  and .modelFetcherSha256 == "82d4fdb90a8c3794a745f2e39410f19970c7a8afc679e14589244992ac50e56c"
  and .makaSubject == "maka-agent/node_modules/@maka/eval/dist/maka-subject.js"
  and .makaSubjectSha256 == "2f39e06a20291b7d2759d5bd9d54e912c3d32817c15f74140ef9cf62b51ec8f3"
  and .stagedRuntime == true
' <<< "$runtime_path_proof" >/dev/null
trap - EXIT
docker run --rm --entrypoint /opt/venvs/harbor-0.20.0/bin/python \
  --mount "type=bind,source=$LAB_ROOT,target=$LAB_ROOT,readonly" \
  --workdir "$LAB_ROOT" \
  maka-lab-controller:0.2.0-dev.44.20260920 controller/assert-secret-mounts.py >/dev/null
install -d -m 0700 "$LAB_ROOT/controller/.build"
bundle_probe_dir=$(mktemp -d "$LAB_ROOT/controller/.build/benchmark-bundle-XXXXXX")
cleanup_bundle_probe() {
  python3 - "$bundle_probe_dir" <<'PY'
from pathlib import Path
import shutil
import sys
candidate = Path(sys.argv[1])
shutil.rmtree(candidate)
if candidate.exists() or candidate.is_symlink():
    raise SystemExit("benchmark bundle probe cleanup failed")
PY
}
trap cleanup_bundle_probe EXIT
bundle_probe="$bundle_probe_dir/fix-git.bundle"
git -C "$benchmark_repository" bundle create "$bundle_probe" HEAD refs/heads/main
chmod 0600 "$bundle_probe"
git -C "$benchmark_repository" bundle verify "$bundle_probe" >/dev/null
bundle_probe_sha256=$(shasum -a 256 "$bundle_probe" | awk '{print $1}')
test "$(docker run --rm --network none --entrypoint bash \
  --mount "type=bind,source=$LAB_ROOT,target=$LAB_ROOT,readonly" \
  --workdir "$LAB_ROOT" \
  --env "MAKA_LAB_ROOT=$LAB_ROOT" \
  --env "MAKA_BENCHMARK_BUNDLE_PATH=$bundle_probe" \
  --env "MAKA_BENCHMARK_BUNDLE_SHA256=$bundle_probe_sha256" \
  maka-lab-controller:0.2.0-dev.44.20260920 -c \
  'set -euo pipefail
   source controller/configure-benchmark-git.sh
   test "$benchmark_repository" = "$benchmark_native_root/fix-git-repo"
   test "$(jq -r .benchmark.config.repository "$benchmark_runtime_config")" = "file://$benchmark_repository"
   test ! -e /root/.gitconfig
   test "$(env -i HOME=/root PATH="$PATH" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git ls-remote "file://$benchmark_repository" | awk '\''$2 == "HEAD" { print $1 }'\'')" = cd855d42cdea03da2d781ae1dbc89f74b39a4491
   clone=$(mktemp -d /tmp/maka-safe-directory-clone-XXXXXX)
   trap '\''find "$clone" -depth -delete; find "$benchmark_native_root" -depth -delete'\'' EXIT
   env -i HOME=/root PATH="$PATH" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git -c init.defaultBranch=main clone --quiet --no-checkout "file://$benchmark_repository" "$clone"
   git -C "$clone" rev-parse HEAD')" \
  = cd855d42cdea03da2d781ae1dbc89f74b39a4491
cleanup_bundle_probe
trap - EXIT
"$LAB_ROOT/scripts/test-secret-volume-cleanup.sh" >/dev/null

mkdir -p "$LAB_ROOT/controller/.build"
egress_root=$(mktemp -d "$LAB_ROOT/controller/.build/egress-XXXXXX")
cleanup_egress_context() {
  python3 - "$egress_root" <<'PY'
from pathlib import Path
import shutil
import sys
shutil.rmtree(Path(sys.argv[1]), ignore_errors=True)
PY
}
trap cleanup_egress_context EXIT
cp "$LAB_ROOT/requirements/egress-proxy.lock" "$egress_root/egress-proxy.lock"
cp "$LAB_ROOT/controller/egress-overlay/egress_filter.py" "$egress_root/egress_filter.py"
cp "$LAB_ROOT/controller/egress-overlay/entrypoint.sh" "$egress_root/entrypoint.sh"
cp "$LAB_ROOT/controller/egress-overlay/keyed_mitmdump.py" "$egress_root/keyed_mitmdump.py"
cp "$LAB_ROOT/controller/egress-overlay/test_egress_injector.py" "$egress_root/test_egress_injector.py"
cp "$LAB_ROOT/controller/egress-overlay/test_egress_integration.py" "$egress_root/test_egress_integration.py"
cp "$LAB_ROOT/controller/egress-overlay/test_egress_undici_h2.py" "$egress_root/test_egress_undici_h2.py"
cp "$LAB_ROOT/controller/egress-overlay/test_egress_undici_h2_client.mjs" "$egress_root/test_egress_undici_h2_client.mjs"
docker build --pull --platform linux/amd64 -f "$LAB_ROOT/controller/egress-proxy.Dockerfile" \
  -t maka-eval-egress-proxy:12.2.3 "$egress_root"
test "$(docker image inspect maka-eval-egress-proxy:12.2.3 --format '{{index .Config.Labels "io.maka.lab.source-revision"}}')" = feb9cf22fa216ce499860ad7cbcd59c32d28aa97
expected_filter_hash=$(shasum -a 256 "$LAB_ROOT/controller/egress-overlay/egress_filter.py" | awk '{print $1}')
actual_filter_hash=$(docker run --rm --entrypoint sha256sum maka-eval-egress-proxy:12.2.3 /opt/maka-eval/egress_filter.py | awk '{print $1}')
test "$expected_filter_hash" = "$actual_filter_hash"
for overlay_file in \
  entrypoint.sh \
  keyed_mitmdump.py \
  test_egress_injector.py \
  test_egress_integration.py \
  test_egress_undici_h2.py \
  test_egress_undici_h2_client.mjs; do
  expected_overlay_hash=$(shasum -a 256 "$LAB_ROOT/controller/egress-overlay/$overlay_file" | awk '{print $1}')
  actual_overlay_hash=$(docker run --rm --entrypoint sha256sum maka-eval-egress-proxy:12.2.3 "/opt/maka-eval/$overlay_file" | awk '{print $1}')
  test "$expected_overlay_hash" = "$actual_overlay_hash"
done
docker run --rm --entrypoint python maka-eval-egress-proxy:12.2.3 /opt/maka-eval/test_egress_injector.py >/dev/null
docker run --rm --entrypoint python \
  --ulimit core=0 \
  --add-host api.openai.com:127.0.0.1 \
  --add-host attacker.test:127.0.0.1 \
  maka-eval-egress-proxy:12.2.3 /opt/maka-eval/test_egress_integration.py >/dev/null
docker run --rm --entrypoint python \
  --ulimit core=0 \
  --add-host api.openai.com:127.0.0.1 \
  --mount "type=bind,source=$runtime_root/maka-agent,target=/opt/maka-agent,readonly" \
  --mount "type=bind,source=$runtime_root/node,target=/opt/maka-node-toolchain,readonly" \
  maka-eval-egress-proxy:12.2.3 /opt/maka-eval/test_egress_undici_h2.py >/dev/null

docker pull alexgshaw/fix-git@sha256:389b9c8247610c2c5be080b1ac00429007c2c69bf57f7f26c79f0f75ba2d5c74 >/dev/null
docker image inspect alexgshaw/fix-git@sha256:389b9c8247610c2c5be080b1ac00429007c2c69bf57f7f26c79f0f75ba2d5c74 >/dev/null
"$LAB_ROOT/scripts/relay-artifact-setup-smoke.sh"

cleanup_egress_context
trap - EXIT
