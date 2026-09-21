#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib.sh"

# The full lab doctor includes the lightweight interactive/auth checks, then
# starts the dedicated Colima VM for the evaluation-only boundary checks.
"$LAB_ROOT/scripts/auth-doctor.sh" >/dev/null
"$LAB_ROOT/scripts/colima-start.sh" >/dev/null
assert_maka_daemon_core_limits >/dev/null

expected_maka=0.2.0-dev.44.20260920
test "$(docker --version | sed -E 's/.*version ([0-9.]+),.*/\1/')" = 29.8.1
test "$(docker-compose version --short)" = 5.5.1
test "$(docker buildx version | awk '{print $2}')" = v0.37.1
test "$(shasum -a 256 "$LAB_ROOT/benchmark/fix-git.bundle" | awk '{print $1}')" \
  = "$(jq -r .terminalBench.sourceBundleSha256 "$LAB_ROOT/provenance.json")"
ensure_benchmark_repository >/dev/null

package_root=$(maka_install_root)
jq -e '.maka.integrity == "sha512-qAopTJ3cjacJzqayStz2hQ62AdOj6b4iP0lD8ddqlpb516qZY4lZNExZnezfNbP7GR8xXZY5pfPMkKJ1l2LQog=="' "$LAB_ROOT/provenance.json" >/dev/null
jq -e '.maka.apacheCommit == "feb9cf22fa216ce499860ad7cbcd59c32d28aa97"' "$LAB_ROOT/provenance.json" >/dev/null
jq -e '.maka.compatibilityOverlays == [{
  "id":"relay-root-virtiofs-v2",
  "target":"node_modules/@maka/eval/harbor/relay_agent.py",
  "baseSha256":"c8654a17bd9ecefac2ce183c3fcc238fc142d9e2a47d703541c9cfc9a29fe3ee",
  "patchedSha256":"8761f73c2940365ca8a5861a9057a62f0ea2de6276b393512e50f72cb66e3bd0"
}, {
  "id":"eval-openai-onboarding-v2",
  "targets":[{
    "target":"node_modules/@maka/runtime/dist/model-fetcher.js",
    "baseSha256":"2df217533e6aa47524fa4027493bfba721fa744b36612b34df0ea4015efe2af5",
    "patchedSha256":"82d4fdb90a8c3794a745f2e39410f19970c7a8afc679e14589244992ac50e56c"
  }, {
    "target":"node_modules/@maka/eval/dist/maka-subject.js",
    "baseSha256":"20b5044736ed951b7b7a3a1c93cda168d04b3f6b84dcbc6aa7564fe61e641923",
    "patchedSha256":"2f39e06a20291b7d2759d5bd9d54e912c3d32817c15f74140ef9cf62b51ec8f3"
  }]
}]' "$LAB_ROOT/provenance.json" >/dev/null
jq -e '.egressProxy.openaiEndpoint == "POST https://api.openai.com/v1/responses"' "$LAB_ROOT/provenance.json" >/dev/null
python "$LAB_ROOT/controller/test_maka_hosted_onboarding_compat.py" "$package_root"
grep -F 'version = "0.2.0-dev.44.20260920"' "$LAB_ROOT/mise.lock" >/dev/null
grep -F 'version = "24.16.0"' "$LAB_ROOT/mise.lock" >/dev/null
grep -F 'version = "0.37.1"' "$LAB_ROOT/mise.lock" >/dev/null
maka eval --help | grep -F 'maka eval run' >/dev/null

printf 'Maka full lab OK: %s, Docker 29.8.1, Compose 5.5.1, Buildx 0.37.1\n' "$expected_maka"
