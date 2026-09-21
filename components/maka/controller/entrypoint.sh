#!/usr/bin/env bash
set -euo pipefail
ulimit -c 0
test "$(ulimit -c)" = 0
test "$(ulimit -H -c)" = 0

test -n "${MAKA_LAB_ROOT:-}" || { echo 'MAKA_LAB_ROOT is required' >&2; exit 64; }
command_cwd=$PWD
case "$MAKA_LAB_ROOT" in
  /*) ;;
  *) echo 'MAKA_LAB_ROOT must be absolute' >&2; exit 64 ;;
esac

runtime_parent="$MAKA_LAB_ROOT/runtime/linux-amd64"
compatibility_overlay_bundle=relay-root-virtiofs-v2-eval-openai-onboarding-v2
version_root="$runtime_parent/maka-0.2.0-dev.44.20260920-node-24.16.0-$compatibility_overlay_bundle"
manifest_name=RUNTIME_SHA256SUMS
symlink_manifest_name=RUNTIME_SYMLINKS
staging=
verification_manifest=
verification_symlinks=
cleanup_temporary() {
  if test -n "$staging" && test -d "$staging"; then
    chmod -R u+w "$staging" 2>/dev/null || true
    find "$staging" -depth -delete 2>/dev/null || true
  fi
  test -z "$verification_manifest" || rm -f "$verification_manifest"
  test -z "$verification_symlinks" || rm -f "$verification_symlinks"
}
trap cleanup_temporary EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
install -d -m 0755 "$runtime_parent"
command -v flock >/dev/null
exec 9>"$runtime_parent/.runtime.lock"
flock -w 120 9
while IFS= read -r -d '' stale_staging; do
  test "$(dirname "$stale_staging")" = "$runtime_parent"
  case "$(basename "$stale_staging")" in
    .staging-[A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9]) ;;
    *) echo "Refusing unexpected runtime staging path: $stale_staging" >&2; exit 70 ;;
  esac
  chmod -R u+w "$stale_staging" 2>/dev/null || true
  find "$stale_staging" -depth -delete
done < <(find "$runtime_parent" -mindepth 1 -maxdepth 1 -type d -name '.staging-*' -print0)
if test ! -f "$version_root/.complete"; then
  if test -e "$version_root"; then
    echo "Incomplete immutable runtime exists: $version_root" >&2
    exit 70
  fi
  staging=$(mktemp -d "$runtime_parent/.staging-XXXXXX")
  node_root=$(mise -C /opt/maka-lab where node@24.16.0)
  install_root=$(cd "$(dirname "$(mise -C /opt/maka-lab which maka)")/../.." && pwd)
  package_json=$(find "$install_root" -type f -path '*/node_modules/maka-agent/package.json' -print -quit)
  test -n "$package_json"
  package_root=$(dirname "$package_json")
  mkdir -p "$staging/node" "$staging/maka-agent"
  cp -a --no-preserve=ownership "$node_root/." "$staging/node/"
  cp -a --no-preserve=ownership "$package_root/." "$staging/maka-agent/"
  test "$("$staging/node/bin/node" --version)" = v24.16.0
  test "$(jq -r .version "$staging/maka-agent/package.json")" = 0.2.0-dev.44.20260920
  /opt/venvs/harbor-0.20.0/bin/python \
    /opt/maka-lab/controller/apply_maka_compat.py "$staging/maka-agent"
  /opt/venvs/harbor-0.20.0/bin/python \
    /opt/maka-lab/controller/apply_maka_hosted_onboarding_compat.py "$staging/maka-agent"
  test "$(sha256sum "$staging/maka-agent/node_modules/@maka/eval/harbor/relay_agent.py" | awk '{print $1}')" \
    = 8761f73c2940365ca8a5861a9057a62f0ea2de6276b393512e50f72cb66e3bd0
  test "$(sha256sum "$staging/maka-agent/node_modules/@maka/runtime/dist/model-fetcher.js" | awk '{print $1}')" \
    = 82d4fdb90a8c3794a745f2e39410f19970c7a8afc679e14589244992ac50e56c
  test "$(sha256sum "$staging/maka-agent/node_modules/@maka/eval/dist/maka-subject.js" | awk '{print $1}')" \
    = 2f39e06a20291b7d2759d5bd9d54e912c3d32817c15f74140ef9cf62b51ec8f3
  printf '%s\n' \
    'maka=0.2.0-dev.44.20260920' \
    'node=24.16.0' \
    'compatibility-overlay-bundle=relay-root-virtiofs-v2-eval-openai-onboarding-v2' \
    'compatibility-overlays=relay-root-virtiofs-v2,eval-openai-onboarding-v2' \
    'relay-agent-sha256=8761f73c2940365ca8a5861a9057a62f0ea2de6276b393512e50f72cb66e3bd0' \
    'model-fetcher-sha256=82d4fdb90a8c3794a745f2e39410f19970c7a8afc679e14589244992ac50e56c' \
    'maka-subject-sha256=2f39e06a20291b7d2759d5bd9d54e912c3d32817c15f74140ef9cf62b51ec8f3' \
    > "$staging/.complete"
  (
    cd "$staging"
    find . -type f ! -name "$manifest_name" ! -name "$symlink_manifest_name" -print0 \
      | LC_ALL=C sort -z \
      | xargs -0 sha256sum > "$manifest_name"
    find . -type l -printf '%p -> %l\n' | LC_ALL=C sort > "$symlink_manifest_name"
  )
  chmod -R a-w "$staging"
  mv "$staging" "$version_root"
  staging=
fi

verification_manifest=$(mktemp)
verification_symlinks=$(mktemp)
(
  cd "$version_root"
  find . -type f ! -name "$manifest_name" ! -name "$symlink_manifest_name" -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 sha256sum > "$verification_manifest"
  find . -type l -printf '%p -> %l\n' | LC_ALL=C sort > "$verification_symlinks"
)
cmp -s "$version_root/$manifest_name" "$verification_manifest" || {
  echo "Immutable Maka runtime failed full-tree verification: $version_root" >&2
  exit 70
}
cmp -s "$version_root/$symlink_manifest_name" "$verification_symlinks" || {
  echo "Immutable Maka runtime failed symlink verification: $version_root" >&2
  exit 70
}
test "$(cat "$version_root/.complete")" = 'maka=0.2.0-dev.44.20260920
node=24.16.0
compatibility-overlay-bundle=relay-root-virtiofs-v2-eval-openai-onboarding-v2
compatibility-overlays=relay-root-virtiofs-v2,eval-openai-onboarding-v2
relay-agent-sha256=8761f73c2940365ca8a5861a9057a62f0ea2de6276b393512e50f72cb66e3bd0
model-fetcher-sha256=82d4fdb90a8c3794a745f2e39410f19970c7a8afc679e14589244992ac50e56c
maka-subject-sha256=2f39e06a20291b7d2759d5bd9d54e912c3d32817c15f74140ef9cf62b51ec8f3'
test "$(sha256sum "$version_root/maka-agent/node_modules/@maka/eval/harbor/relay_agent.py" | awk '{print $1}')" \
  = 8761f73c2940365ca8a5861a9057a62f0ea2de6276b393512e50f72cb66e3bd0
test "$(sha256sum "$version_root/maka-agent/node_modules/@maka/runtime/dist/model-fetcher.js" | awk '{print $1}')" \
  = 82d4fdb90a8c3794a745f2e39410f19970c7a8afc679e14589244992ac50e56c
test "$(sha256sum "$version_root/maka-agent/node_modules/@maka/eval/dist/maka-subject.js" | awk '{print $1}')" \
  = 2f39e06a20291b7d2759d5bd9d54e912c3d32817c15f74140ef9cf62b51ec8f3
rm -f "$verification_manifest" "$verification_symlinks"
verification_manifest=
verification_symlinks=
flock -u 9
exec 9>&-
trap - EXIT INT TERM

export MAKA_EVAL_MAKA_BUNDLE_PATH="$version_root/maka-agent"
export MAKA_EVAL_NODE_TOOLCHAIN_PATH="$version_root/node"
export MAKA_EVAL_RUNTIME_ROOT="$version_root"
export MAKA_EVAL_EGRESS_OVERLAY_PATH="$MAKA_LAB_ROOT/controller/egress-overlay"
export MAKA_EVAL_HARBOR_PYTHON=/opt/venvs/harbor-0.20.0/bin/python
export MAKA_EVAL_PIER_PYTHON=/opt/venvs/pier-0.3.0/bin/python
exec mise -C /opt/maka-lab exec -- sh -c '
  command_cwd=$1
  lab_root=$2
  shift 2
  cd "$command_cwd"
  exec env MAKA_LAB_ROOT="$lab_root" "$@"
' maka-entrypoint "$command_cwd" "$MAKA_LAB_ROOT" "$@"
