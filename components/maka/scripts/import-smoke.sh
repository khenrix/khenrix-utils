#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib.sh"

evidence_dir=$(new_evidence_dir import-smoke)
trap 'status=$?; trap - EXIT; finalize_evidence "$evidence_dir" || status=70; exit "$status"' EXIT
package_root=$(maka_install_root)
node "$LAB_ROOT/scripts/synthetic-import.mjs" "$package_root" "$evidence_dir"
printf '%s\n' "$evidence_dir"
