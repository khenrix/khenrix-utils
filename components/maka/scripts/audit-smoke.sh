#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib.sh"

profile=$(maka_profile_root)
before=absent
test ! -e "$profile" || before=$(python "$LAB_ROOT/interactive/maka_profile.py" digest "$profile")

evidence_dir=$(new_evidence_dir audit-smoke)
trap 'status=$?; trap - EXIT; finalize_evidence "$evidence_dir" || status=70; exit "$status"' EXIT
package_root=$(maka_install_root)
node "$LAB_ROOT/scripts/synthetic-import.mjs" "$package_root" "$evidence_dir"
isolated_home="$evidence_dir/isolated-home"
isolated_xdg="$isolated_home/.config"
mkdir -p "$isolated_home" "$isolated_xdg"
HOME="$isolated_home" XDG_CONFIG_HOME="$isolated_xdg" \
  node "$package_root/dist/cli.js" --version > "$evidence_dir/maka-version.txt"
HOME="$isolated_home" XDG_CONFIG_HOME="$isolated_xdg" \
  node "$package_root/dist/cli.js" --help > "$evidence_dir/maka-help.txt"

after=absent
test ! -e "$profile" || after=$(python "$LAB_ROOT/interactive/maka_profile.py" digest "$profile")
test "$before" = "$after"
printf '{"normalProfileBefore":"%s","normalProfileAfter":"%s","unchanged":true}\n' "$before" "$after" > "$evidence_dir/normal-profile-check.json"
printf '%s\n' "$evidence_dir"
