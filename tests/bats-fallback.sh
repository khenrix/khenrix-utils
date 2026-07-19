#!/usr/bin/env bash
# bats-fallback.sh -- run a .bats file WITHOUT the bats binary.
#
# `bats` cannot be installed on this machine (sudo requires a password that is
# not available non-interactively). Rather than hand-copying each @test body
# into a parallel script -- which silently drifts from the real suite -- this
# harness TRANSLATES tests/test_repo_sweep.bats itself, so the assertions run
# here are byte-identical to the ones bats will run once it is installed.
#
# Reproduced bats semantics:
#   * fresh $BATS_TEST_TMPDIR per test, setup() re-run before each
#   * `run cmd` populates $output (stdout+stderr merged) and $status, and never
#     itself fails the test
#   * the test body runs under `set -e`: any failing command fails the test
#
# Requires: @test blocks closed by a `}` at column 0.
#
# Usage: tests/bats-fallback.sh [path/to/file.bats]
set -uo pipefail

BATS_FILE="${1:-$(dirname "$(readlink -f "$0")")/test_repo_sweep.bats}"
[ -f "$BATS_FILE" ] || { echo "no such .bats file: $BATS_FILE" >&2; exit 2; }

export BATS_TEST_DIRNAME
BATS_TEST_DIRNAME=$(dirname "$(readlink -f "$BATS_FILE")")

GEN=$(mktemp); NAMES=$(mktemp)
trap 'rm -f "$GEN" "$NAMES"' EXIT

awk -v names="$NAMES" '
  /^@test[ \t]/ {
    match($0, /"[^"]*"/)
    n++
    print substr($0, RSTART+1, RLENGTH-2) > names
    print "bats_test_" n "() { ( set -e"
    intest = 1
    next
  }
  intest && /^}[ \t]*$/ { print "); }"; intest = 0; next }
  { print }
' "$BATS_FILE" > "$GEN"

run() {
  local rc=0
  output=$("$@" 2>&1) || rc=$?
  status=$rc
  return 0
}

# shellcheck disable=SC1090
source "$GEN"

PASS=0; FAIL=0; FAILED=()
i=0
while IFS= read -r name; do
  i=$((i+1))
  BATS_TEST_TMPDIR=$(mktemp -d); export BATS_TEST_TMPDIR
  setup   # must run in THIS shell: it exports FIXT/SWEEP the body relies on
  # NOT `if "bats_test_$i"; then` -- calling it in a condition context makes
  # bash suppress errexit for the whole call INCLUDING the body's own subshell,
  # so every assertion but the last would be silently ignored. Verified: with
  # that form, deleting `f+=(no-remote)` from the script left the suite green.
  "bats_test_$i"; rc=$?
  if [ "$rc" -eq 0 ]; then
    PASS=$((PASS+1)); echo "ok $i - $name"
  else
    FAIL=$((FAIL+1)); FAILED+=("$name"); echo "not ok $i - $name"
    echo "  --- last \$output ---"; printf '%s\n' "${output:-}" | sed 's/^/  /'
    echo "  --- last \$status: ${status:-?} ---"
  fi
  chmod -R u+rwX "$BATS_TEST_TMPDIR" 2>/dev/null
  rm -rf "$BATS_TEST_TMPDIR"
done < "$NAMES"

echo
echo "$((PASS+FAIL)) tests, $FAIL failures"
[ "$FAIL" -eq 0 ] || { printf 'failed: %s\n' "${FAILED[@]}"; exit 1; }
exit 0
