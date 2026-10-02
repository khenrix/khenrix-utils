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
compatibility_overlays=relay-root-virtiofs-v2,eval-openai-onboarding-v2
model_fetcher_sha256=427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7
model_metadata_sha256=
undici_h2_base_sha256=
undici_h2_sha256=
case "${MAKA_EVAL_RUNTIME_VARIANT:-}" in
  '') ;;
  harbor-judge-sol61-v1|harbor-judge-sol61-h2-v1)
    compatibility_overlay_bundle=$compatibility_overlay_bundle-harbor-judge-sol61-v1
    compatibility_overlays=$compatibility_overlays,harbor-judge-sol61-v1
    model_fetcher_sha256=7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5
    model_metadata_sha256=9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7
    if test "$MAKA_EVAL_RUNTIME_VARIANT" = harbor-judge-sol61-h2-v1; then
      compatibility_overlay_bundle=$compatibility_overlay_bundle-undici-h2-close-v1
      compatibility_overlays=$compatibility_overlays,undici-h2-close-v1
      undici_h2_base_sha256=611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7
      undici_h2_sha256=521fef59ccb84909a3c68d52c15e972f61836e0d386093d5d4f4e64ba7d85087
    fi
    ;;
  *) echo 'Unknown Maka eval runtime variant' >&2; exit 64 ;;
esac
version_root="$runtime_parent/maka-0.2.0-dev.62.20260928-node-24.16.0-$compatibility_overlay_bundle"
expected_complete=$(printf '%s\n' \
  'maka=0.2.0-dev.62.20260928' \
  'node=24.16.0' \
  "compatibility-overlay-bundle=$compatibility_overlay_bundle" \
  "compatibility-overlays=$compatibility_overlays" \
  'relay-agent-sha256=6214ed402d2d8345b20b625739b9a08905116bfdd944775938d5d78054033621' \
  "model-fetcher-sha256=$model_fetcher_sha256" \
  'maka-subject-sha256=f47c1278ed25434f2a1a012d26ed285eb930e737acbc182cbdcda6b914887775'
  if test -n "$model_metadata_sha256"; then
    printf '%s\n' "model-metadata-sha256=$model_metadata_sha256"
  fi
  if test -n "$undici_h2_sha256"; then
    printf '%s\n' "undici-h2-base-sha256=$undici_h2_base_sha256" "undici-h2-sha256=$undici_h2_sha256"
  fi)
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
  test "$(jq -r .version "$staging/maka-agent/package.json")" = 0.2.0-dev.62.20260928
  /opt/venvs/harbor-0.20.0/bin/python \
    /opt/maka-lab/controller/apply_maka_compat.py "$staging/maka-agent"
  /opt/venvs/harbor-0.20.0/bin/python \
    /opt/maka-lab/controller/apply_maka_hosted_onboarding_compat.py "$staging/maka-agent"
  if test -n "$model_metadata_sha256"; then
    /opt/venvs/harbor-0.20.0/bin/python \
      /opt/maka-lab/controller/apply_maka_harbor_judge_compat.py "$staging/maka-agent"
    test "$(sha256sum "$staging/maka-agent/node_modules/@maka/core/dist/model-metadata.js" | awk '{print $1}')" \
      = "$model_metadata_sha256"
  fi
  if test -n "$undici_h2_sha256"; then
    /opt/venvs/harbor-0.20.0/bin/python \
      /opt/maka-lab/controller/apply_undici_h2_close_compat.py "$staging/maka-agent"
    test "$(sha256sum "$staging/maka-agent/node_modules/@maka/runtime/node_modules/undici/lib/dispatcher/client-h2.js" | awk '{print $1}')" \
      = "$undici_h2_sha256"
  fi
  test "$(sha256sum "$staging/maka-agent/node_modules/@maka/eval/harbor/relay_agent.py" | awk '{print $1}')" \
    = 6214ed402d2d8345b20b625739b9a08905116bfdd944775938d5d78054033621
  test "$(sha256sum "$staging/maka-agent/node_modules/@maka/runtime/dist/model-fetcher.js" | awk '{print $1}')" \
    = "$model_fetcher_sha256"
  test "$(sha256sum "$staging/maka-agent/node_modules/@maka/eval/dist/maka-subject.js" | awk '{print $1}')" \
    = f47c1278ed25434f2a1a012d26ed285eb930e737acbc182cbdcda6b914887775
  printf '%s\n' "$expected_complete" > "$staging/.complete"
  (
    cd "$staging"
    /opt/venvs/harbor-0.20.0/bin/python /opt/maka-lab/controller/runtime_manifest_verify.py \
      "$staging" /usr/bin/sha256sum > "$manifest_name"
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
  /opt/venvs/harbor-0.20.0/bin/python /opt/maka-lab/controller/runtime_manifest_verify.py \
    "$version_root" /usr/bin/sha256sum > "$verification_manifest"
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
test "$(cat "$version_root/.complete")" = "$expected_complete"
test "$(sha256sum "$version_root/maka-agent/node_modules/@maka/eval/harbor/relay_agent.py" | awk '{print $1}')" \
  = 6214ed402d2d8345b20b625739b9a08905116bfdd944775938d5d78054033621
test "$(sha256sum "$version_root/maka-agent/node_modules/@maka/runtime/dist/model-fetcher.js" | awk '{print $1}')" \
  = "$model_fetcher_sha256"
test "$(sha256sum "$version_root/maka-agent/node_modules/@maka/eval/dist/maka-subject.js" | awk '{print $1}')" \
  = f47c1278ed25434f2a1a012d26ed285eb930e737acbc182cbdcda6b914887775
if test -n "$model_metadata_sha256"; then
  test "$(sha256sum "$version_root/maka-agent/node_modules/@maka/core/dist/model-metadata.js" | awk '{print $1}')" \
    = "$model_metadata_sha256"
fi
if test -n "$undici_h2_sha256"; then
  test "$(sha256sum "$version_root/maka-agent/node_modules/@maka/runtime/node_modules/undici/lib/dispatcher/client-h2.js" | awk '{print $1}')" \
    = "$undici_h2_sha256"
fi
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
