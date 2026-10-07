#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib.sh"

mode=broad
baseline_root=
mode_set=false
while test "$#" -gt 0; do
  case "$1" in
    --native|--broad)
      test "$mode_set" = false || exit 64
      mode=${1#--}
      mode_set=true
      shift
      ;;
    --baseline-package-root)
      test "$#" -ge 2 && test -z "$baseline_root" || exit 64
      baseline_root=$2
      shift 2
      ;;
    *) echo 'Usage: test.sh [--native | --broad --baseline-package-root PATH]' >&2; exit 64 ;;
  esac
done
if test "$mode" = native; then
  test -z "$baseline_root" || exit 64
else
  if test -z "$baseline_root"; then
    echo 'Broad tests require an explicit unpatched .62 baseline package root' >&2
    exit 78
  fi
  python - "$LAB_ROOT" "$baseline_root" <<'PY'
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / "controller"))
import apply_maka_compat as relay
import apply_maka_hosted_onboarding_compat as onboarding
import apply_undici_h2_close_compat as h2

try:
    package = pathlib.Path(sys.argv[2])
    if package.is_symlink() or not package.is_dir():
        raise ValueError
    package = package.resolve(strict=True)
    descriptor_path = package / "package.json"
    if descriptor_path.is_symlink() or not descriptor_path.is_file():
        raise ValueError
    descriptor = json.loads(descriptor_path.read_text())
    if (descriptor.get("name"), descriptor.get("version")) != (
        "maka-agent", "0.2.0-dev.62.20260928"
    ):
        raise ValueError
    sources = (
        (relay.RELAY_PATH, relay.BASE_SHA256),
        (onboarding.MODEL_FETCHER_PATH, onboarding.MODEL_FETCHER_BASE_SHA256),
        (onboarding.MAKA_SUBJECT_PATH, onboarding.MAKA_SUBJECT_BASE_SHA256),
        (pathlib.Path("node_modules/@maka/core/dist/model-metadata.js"),
         "454ae64aa3a2dee4fe93d30f3d547e361b6b30f2c26987c69f0a8ab6981cd2cf"),
        (h2.TARGET, h2.BASE_SHA256),
    )
    for relative, expected in sources:
        target = package / relative
        if (target.is_symlink() or not target.is_file()
                or not target.resolve(strict=True).is_relative_to(package)
                or hashlib.sha256(target.read_bytes()).hexdigest() != expected):
            raise ValueError
except (OSError, ValueError, TypeError, AttributeError):
    print("Broad tests require an exact unpatched .62 baseline package", file=sys.stderr)
    raise SystemExit(78)
PY
fi

package_root=$(maka_install_root)
export KHENRIX_MAKA_PACKAGE_ROOT="$package_root"
KHENRIX_MAKA_PACKAGE_ROOT="$package_root" node --test \
  "$LAB_ROOT/interactive/test_configure_maka_openai_relay.mjs" \
  "$LAB_ROOT/interactive/test_configure_maka_subscription.mjs" \
  "$LAB_ROOT/interactive/test_maka_judge.mjs" \
  "$LAB_ROOT/interactive/test_maka_skill_routing.mjs" \
  "$LAB_ROOT/controller/test_harbor_judge.mjs" \
  "$LAB_ROOT/test_native_capture_runtime.mjs"
node "$LAB_ROOT/interactive/inspect_maka_runtime.mjs" --self-test
(
  cd "$LAB_ROOT"
  python -m unittest test_native_capture_join
)
(
  cd "$LAB_ROOT/interactive"
  python -m unittest discover -p 'test_*.py'
)
(
  cd "$LAB_ROOT/scripts"
  python -m unittest test_component_management.py test_maka_gpt6_compat.py test_maka_test_gate.py test_relay_tier_status.py test_harbor_judge_gate.py test_harbor_judge_input.py test_harbor_judge_audit.py test_runtime_manifest_verify.py
)
(
  cd "$LAB_ROOT/controller/egress-overlay"
  python -m unittest test_judge_boundary.py
)
python "$LAB_ROOT/controller/test_secret_broker.py"
request=$(mktemp)
trap 'rm -f "$request"' EXIT
node "$LAB_ROOT/controller/provider-request-contract.mjs" "$package_root" "$request"
python "$LAB_ROOT/controller/assert-provider-body.py" "$request"
if test "$mode" = broad; then
  KHENRIX_MAKA_PACKAGE_ROOT="$baseline_root" node --test "$LAB_ROOT/controller/test_harbor_runtime_baseline.mjs"
  python "$LAB_ROOT/controller/test_maka_hosted_onboarding_compat.py" "$baseline_root"
  python "$LAB_ROOT/controller/test_maka_harbor_judge_compat.py" "$baseline_root"
  python "$LAB_ROOT/controller/test_undici_h2_close_compat.py" "$baseline_root"
  node "$LAB_ROOT/controller/provider-request-contract.mjs" "$baseline_root" "$request"
  python "$LAB_ROOT/controller/assert-provider-body.py" "$request"
fi
rm -f "$request"
trap - EXIT
if test "$mode" = native; then
  smoke_root=$(mktemp -d)
  trap 'rm -rf "$smoke_root"' EXIT
  node "$LAB_ROOT/scripts/synthetic-import.mjs" "$package_root" "$smoke_root"
  mkdir -p "$smoke_root/isolated-home/.config"
  HOME="$smoke_root/isolated-home" XDG_CONFIG_HOME="$smoke_root/isolated-home/.config" \
    node "$package_root/dist/cli.js" --version >/dev/null
  HOME="$smoke_root/isolated-home" XDG_CONFIG_HOME="$smoke_root/isolated-home/.config" \
    node "$package_root/dist/cli.js" --help >/dev/null
  rm -rf "$smoke_root"
  trap - EXIT
else
  "$LAB_ROOT/scripts/audit-smoke.sh" >/dev/null
  "$LAB_ROOT/scripts/import-smoke.sh" >/dev/null
fi
printf 'Maka %s regression tests OK: %s\n' "$mode" "$(jq -r .version "$package_root/package.json")"
