#!/usr/bin/env bats
#
# BEHAVIOURAL, not source-text, checks (finding: the previous version of this
# file asserted the literal string `reconcile(c, caps, apply=False` was absent
# from reconcile.py, and that `--apply` appeared in mcp_merge.py --help. Both
# assertions pass vacuously if the same bug is reintroduced in a different
# code shape -- e.g. `broken_flag = False; reconcile(c, caps, apply=broken_flag,
# ...)`, or an `--apply` flag that argparse declares but main() never wires to
# a write. Empirically confirmed: both reintroduced bugs left the old suite at
# "2 tests, 0 failures". These tests instead exercise the actual behaviour the
# fix guarantees: apply/update_drift really propagate to every CLI, and
# `--apply` really writes (while its absence really doesn't).
#
# NOTE: closing braces of @test blocks must stay at column 0 -- tests/bats-fallback.sh
# relies on that to run this file when the real bats binary is unavailable.

setup() { :; }

@test "reconcile --all propagates apply/update_drift per CLI (not hardcoded)" {
  run python3 -c "
import sys
sys.path.insert(0, '$BATS_TEST_DIRNAME/../scripts/lib')
import reconcile

calls = []
def fake_reconcile(cli, caps, apply, update_drift):
    calls.append((cli, apply, update_drift))
reconcile.reconcile = fake_reconcile

def invoke(argv):
    calls.clear()
    reconcile.main(argv)
    return list(calls)

r = invoke(['--all', '--apply'])
assert set(c for c, _, _ in r) == set(reconcile.CLIS), ('missing CLIs', r)
assert all(a is True for _, a, _ in r), ('--all --apply must set apply=True for every CLI', r)

r = invoke(['--all'])
assert set(c for c, _, _ in r) == set(reconcile.CLIS), ('missing CLIs', r)
assert all(a is False for _, a, _ in r), ('--all without --apply must stay apply=False', r)

r = invoke(['--status', '--apply', '--all'])
assert all(a is False for _, a, _ in r), ('--status must force apply=False even with --apply', r)

r = invoke(['--all', '--apply', '--update-drift'])
assert all(u is True for _, _, u in r), ('--update-drift must propagate too', r)

print('OK')
"
  [ "$status" -eq 0 ]
  [[ "$output" == *"OK"* ]]
}

@test "mcp_merge.py --apply writes the merge; without --apply the file is untouched" {
  dir="$BATS_TEST_TMPDIR/mcp"
  mkdir -p "$dir"
  cat > "$dir/config.json" <<'JSON'
{"mcpServers": {"a": {"command": "x"}}, "theme": "dark"}
JSON
  cat > "$dir/additions.json" <<'JSON'
{"mcpServers": {"b": {"command": "y"}}}
JSON

  before_sum=$(md5sum "$dir/config.json" | awk '{print $1}')

  # dry run (no --apply): must print the merge but leave the file byte-identical
  run python3 "$BATS_TEST_DIRNAME/../scripts/lib/mcp_merge.py" "$dir/config.json" "$dir/additions.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"b"'* ]]
  after_dry_sum=$(md5sum "$dir/config.json" | awk '{print $1}')
  [ "$before_sum" = "$after_dry_sum" ]

  # --apply: must actually write the merged result back to the file
  run python3 "$BATS_TEST_DIRNAME/../scripts/lib/mcp_merge.py" --apply "$dir/config.json" "$dir/additions.json"
  [ "$status" -eq 0 ]

  run python3 -c "
import json
d = json.load(open('$dir/config.json'))
assert d['mcpServers']['a']['command'] == 'x', d
assert d['mcpServers']['b']['command'] == 'y', d
assert d.get('theme') == 'dark', d
print('MERGED-OK')
"
  [ "$status" -eq 0 ]
  [[ "$output" == *"MERGED-OK"* ]]
}
