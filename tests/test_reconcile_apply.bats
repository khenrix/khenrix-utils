#!/usr/bin/env bats

setup() { :; }

@test "reconcile --all --apply does not silently force apply=False" {
  run grep -n "apply=False" "$BATS_TEST_DIRNAME/../scripts/lib/reconcile.py"
  # the --all branch must not hardcode apply=False
  [[ "$output" != *"for c in CLIS"* ]]
  run python3 -c "
import ast,sys
src=open('$BATS_TEST_DIRNAME/../scripts/lib/reconcile.py').read()
assert 'reconcile(c, caps, apply=False' not in src, 'CLIS loop still hardcodes apply=False'
print('ok')"
  [ "$status" -eq 0 ]
}

@test "mcp_merge.py exposes --apply" {
  run python3 "$BATS_TEST_DIRNAME/../scripts/lib/mcp_merge.py" --help
  [[ "$output" == *"--apply"* ]]
}
