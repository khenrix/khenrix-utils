#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib.sh"

package_root=$(maka_install_root)
KHENRIX_MAKA_PACKAGE_ROOT="$package_root" node --test \
  "$LAB_ROOT/interactive/test_configure_maka_openai_relay.mjs" \
  "$LAB_ROOT/interactive/test_configure_maka_subscription.mjs" \
  "$LAB_ROOT/interactive/test_maka_judge.mjs" \
  "$LAB_ROOT/interactive/test_maka_skill_routing.mjs" \
  "$LAB_ROOT/controller/test_harbor_judge.mjs"
node "$LAB_ROOT/interactive/inspect_maka_runtime.mjs" --self-test
(
  cd "$LAB_ROOT/interactive"
  python -m unittest discover -p 'test_*.py'
)
(
  cd "$LAB_ROOT/scripts"
  python -m unittest test_component_management.py test_maka_gpt6_compat.py test_relay_tier_status.py test_harbor_judge_gate.py test_harbor_judge_input.py test_harbor_judge_audit.py test_runtime_manifest_verify.py
)
(
  cd "$LAB_ROOT/controller/egress-overlay"
  python -m unittest test_judge_boundary.py
)
python "$LAB_ROOT/controller/test_secret_broker.py"
python "$LAB_ROOT/controller/test_maka_hosted_onboarding_compat.py" "$package_root"
python "$LAB_ROOT/controller/test_maka_harbor_judge_compat.py" "$package_root"
python "$LAB_ROOT/controller/test_undici_h2_close_compat.py" "$package_root"
request=$(mktemp)
trap 'rm -f "$request"' EXIT
node "$LAB_ROOT/controller/provider-request-contract.mjs" "$package_root" "$request"
python "$LAB_ROOT/controller/assert-provider-body.py" "$request"
rm -f "$request"
trap - EXIT
"$LAB_ROOT/scripts/audit-smoke.sh" >/dev/null
"$LAB_ROOT/scripts/import-smoke.sh" >/dev/null
printf 'Maka portable tests OK: %s\n' "$(jq -r .version "$package_root/package.json")"
