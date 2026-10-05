#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib.sh"

# The full lab doctor includes the lightweight interactive/auth checks, then
# starts the dedicated Colima VM for the evaluation-only boundary checks.
"$LAB_ROOT/scripts/auth-doctor.sh" >/dev/null
"$LAB_ROOT/scripts/colima-start.sh" >/dev/null
assert_maka_daemon_core_limits >/dev/null

expected_maka=0.2.0-dev.62.20260928
test "$(docker --version | sed -E 's/.*version ([0-9.]+),.*/\1/')" = 29.8.1
test "$(docker-compose version --short)" = 5.5.1
test "$(docker buildx version | awk '{print $2}')" = v0.37.1
test "$(shasum -a 256 "$LAB_ROOT/benchmark/fix-git.bundle" | awk '{print $1}')" \
  = "$(jq -r .terminalBench.sourceBundleSha256 "$LAB_ROOT/provenance.json")"
ensure_benchmark_repository >/dev/null

package_root=$(maka_install_root)
jq -e '.maka.integrity == "sha512-s8SDp0RCo2B4f2wDrwxPa8HL3m4aby+JBKF+Ctq6vtNhjJTQgq6LRvT3aEeQ4e8TBI0mb3JO6oSA8l/uvGyqRQ=="' "$LAB_ROOT/provenance.json" >/dev/null
jq -e '.maka.apacheCommit == "2f32205528a4436783c800715261d4b6e286023d"' "$LAB_ROOT/provenance.json" >/dev/null
jq -e '.maka.compatibilityOverlays == [{
  "id":"relay-root-virtiofs-v2",
  "target":"node_modules/@maka/eval/harbor/relay_agent.py",
  "baseSha256":"045c2774a37bc014232d5d9a2f2008f40ec4291d36c39ab632ebf3698ef0789f",
  "patchedSha256":"6214ed402d2d8345b20b625739b9a08905116bfdd944775938d5d78054033621"
}, {
  "id":"eval-openai-onboarding-v2",
  "targets":[{
    "target":"node_modules/@maka/runtime/dist/model-fetcher.js",
    "baseSha256":"044173c4dfcfc40c371038fda6880941533c1a13b54119248f97a496ab4d3aba",
    "patchedSha256":"427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7"
  }, {
    "target":"node_modules/@maka/eval/dist/maka-subject.js",
    "baseSha256":"84258ed57890af57d88b917007e52c53ac71a69836c9b72bd7a7d9239f0a320c",
    "patchedSha256":"f47c1278ed25434f2a1a012d26ed285eb930e737acbc182cbdcda6b914887775"
  }]
}, {
  "id":"harbor-judge-sol61-v1",
  "targets":[{
    "target":"node_modules/@maka/runtime/dist/model-fetcher.js",
    "baseSha256":"427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7",
    "patchedSha256":"7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5"
  }, {
    "target":"node_modules/@maka/core/dist/model-metadata.js",
    "baseSha256":"454ae64aa3a2dee4fe93d30f3d547e361b6b30f2c26987c69f0a8ab6981cd2cf",
    "patchedSha256":"9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7"
  }]
}, {
  "id":"undici-h2-close-v2",
  "target":"node_modules/@maka/runtime/node_modules/undici/lib/dispatcher/client-h2.js",
  "baseSha256":"611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7",
  "patchedSha256":"32c2b747c1c1c9c7405e60dc70145f67f9b8f96db0b6264f55e1f0400a375dc4"
}]' "$LAB_ROOT/provenance.json" >/dev/null
jq -e '.egressProxy.openaiEndpoint == "POST https://api.openai.com/v1/responses"' "$LAB_ROOT/provenance.json" >/dev/null
python "$LAB_ROOT/controller/test_maka_hosted_onboarding_compat.py" "$package_root"
python "$LAB_ROOT/controller/test_maka_harbor_judge_compat.py" "$package_root"
python "$LAB_ROOT/controller/test_undici_h2_close_compat.py" "$package_root"
grep -F 'version = "0.2.0-dev.62.20260928"' "$LAB_ROOT/mise.lock" >/dev/null
grep -F 'version = "24.16.0"' "$LAB_ROOT/mise.lock" >/dev/null
grep -F 'version = "0.37.1"' "$LAB_ROOT/mise.lock" >/dev/null
maka eval --help | grep -F 'maka eval run' >/dev/null

printf 'Maka full lab OK: %s, Docker 29.8.1, Compose 5.5.1, Buildx 0.37.1\n' "$expected_maka"
