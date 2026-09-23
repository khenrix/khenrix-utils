#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib.sh"

expected_maka=0.2.0-dev.47.20260922
expected_node=v24.16.0
expected_python='Python 3.12.14'
expected_uv=0.12.15

test "$(node --version)" = "$expected_node"
test "$(python --version)" = "$expected_python"
test "$(uv --version | awk '{print $2}')" = "$expected_uv"

package_root="$HOME/.local/share/khenrix-utils/maka/runtime/package"
test "$(jq -r .version "$package_root/package.json")" = "$expected_maka"
wrapper="$HOME/.local/bin/maka"
test -x "$wrapper"
grep -F 'unset OPENAI_API_KEY ANTHROPIC_API_KEY' "$wrapper" >/dev/null
grep -F 'python "$maka_lab_root/interactive/maka_auth_mode.py" show' "$wrapper" >/dev/null
grep -F 'python "$maka_lab_root/interactive/maka_auth_mode.py" relay-ready' "$wrapper" >/dev/null
grep -F 'configure_maka_subscription.mjs' "$wrapper" >/dev/null
grep -F 'caller_working_directory=$PWD' "$wrapper" >/dev/null
grep -F 'unset XDG_CONFIG_HOME XDG_DATA_HOME' "$wrapper" >/dev/null
grep -F 'export HOME="$account_root" USER="$account_name" LOGNAME="$account_name"' "$wrapper" >/dev/null
grep -F 'exec "$mise_bin" -C "$maka_lab_root" exec' "$wrapper" >/dev/null
grep -F 'cd "$1" && shift && package=$1 && shift && exec node "$package/dist/cli.js" "$@"' "$wrapper" >/dev/null
if grep -F -- '--purge-credentials' "$wrapper" >/dev/null; then
  echo 'Ordinary Maka wrapper unexpectedly requests credential purge' >&2
  exit 1
fi

# Parse the four rendered assignments without sourcing the wrapper.
python - "$wrapper" "$LAB_ROOT" "$package_root" <<'PY'
import os
import pathlib
import shlex
import sys

wrapper = pathlib.Path(sys.argv[1])
expected_root = pathlib.Path(sys.argv[2])
expected_package = pathlib.Path(sys.argv[3])
wanted = {"account_root", "mise_bin", "maka_lab_root", "maka_package_root"}
values = {}
for line in wrapper.read_text(encoding="utf-8").splitlines():
    name, separator, raw = line.partition("=")
    if separator and name in wanted:
        parsed = shlex.split(raw, posix=True)
        if len(parsed) != 1 or name in values:
            raise SystemExit("Maka wrapper assignment is invalid")
        values[name] = parsed[0]
if set(values) != wanted:
    raise SystemExit("Maka wrapper assignment is missing")
if pathlib.Path(values["account_root"]) != pathlib.Path.home():
    raise SystemExit("Maka wrapper account root is invalid")
if pathlib.Path(values["maka_lab_root"]) != expected_root:
    raise SystemExit("Maka wrapper component root is invalid")
if pathlib.Path(values["maka_package_root"]) != expected_package:
    raise SystemExit("Maka wrapper package root is invalid")
mise = pathlib.Path(values["mise_bin"])
allowed = {
    pathlib.Path.home() / ".local/bin/mise",
    pathlib.Path("/opt/homebrew/bin/mise"),
    pathlib.Path("/usr/local/bin/mise"),
}
if mise not in allowed or not mise.is_file() or not os.access(mise, os.X_OK):
    raise SystemExit("Maka wrapper mise path is invalid")
PY

auth_mode=$(python "$LAB_ROOT/interactive/maka_auth_mode.py" show)
case "$auth_mode" in
  api-key-relay)
    /usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
      /usr/bin/python3 -I -S -B "$LAB_ROOT/scripts/harden_python_runtime.py" verify >/dev/null
    python "$LAB_ROOT/interactive/maka_auth_mode.py" relay-ready >/dev/null
    ;;
  chatgpt-subscription) ;;
  *) echo 'Unsupported Maka auth mode' >&2; exit 1 ;;
esac

if update_output=$("$wrapper" update 2>&1); then
  echo 'Pinned wrapper unexpectedly allowed maka update' >&2
  exit 1
fi
grep -F 'maka update is disabled; update the pinned khenrix-utils component instead' \
  <<<"$update_output" >/dev/null
test "$("$wrapper" --version)" = "$expected_maka"
"$wrapper" --help | grep -F 'Maka terminal UI' >/dev/null
"$wrapper" run --help | grep -F -- '--yolo' >/dev/null

normal_profile=$(maka_profile_root)
python "$LAB_ROOT/interactive/maka_profile.py" check-private "$normal_profile"
node "$LAB_ROOT/interactive/inspect_maka_runtime.mjs" \
  --mode "$auth_mode" \
  --package-root "$package_root" >/dev/null

printf 'Maka auth OK: %s, route %s, Node %s, Python 3.12.14, uv %s\n' \
  "$expected_maka" "$auth_mode" "${expected_node#v}" "$expected_uv"
