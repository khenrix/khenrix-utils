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

@test "MCP drift is classified identically for claude, codex and agy" {
  # Regression: reconcile reported a false "MATCH" for the claude backend when an
  # MCP server's live command/args genuinely differed from capabilities.toml, while
  # codex and agy correctly reported UPDATE for the SAME difference. Two causes:
  #   1. mcp_drift() gated the command/args comparison on `if cli in ("codex","agy")`,
  #      so claude was skipped outright and always fell through to `return None`.
  #   2. claude_mcp_current() scraped `claude mcp list` into {"endpoint": "<text>"},
  #      so no command/args fields existed to compare even if the gate had allowed it.
  # This drives the REAL loaders against fixture configs in a temp HOME and asserts
  # all four statuses per backend -- table-driven so a regression in any one backend
  # (not just claude) fails loudly and names itself.
  cat > "$BATS_TEST_TMPDIR/drift.py" <<'PY'
import json, os, sys
from pathlib import Path

LIB, HOME = sys.argv[1], Path(sys.argv[2])
HOME.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(HOME)
# No PATH: `claude mcp list` must not run. The on-disk user-scope config is the
# structured source of truth, and the test stays hermetic.
os.environ["PATH"] = str(HOME / "no-such-bin")
sys.path.insert(0, LIB)
import reconcile

PS = "/opt/powershell.exe"
BASE = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
# The exact shape of the reported bug: 5 args, differing only at index 4.
DECLARED = "$n = (Get-Command npx.cmd -EA SilentlyContinue).Source"
LIVE_OLD = '$env:Path = "C:\\Program Files\\nodejs;" + $env:Path; & "C:\\Program Files\\nodejs\\npx.cmd"'
URL = "https://mcp.context7.com/mcp"

caps = {"mcp_servers": {
            "chrome-devtools": {"transport": "stdio", "command": PS, "args": BASE + [DECLARED]},
            "linkedin":        {"transport": "stdio", "command": "uvx", "args": ["mcp-server-linkedin@latest"]},
            "context7":        {"transport": "http", "url": URL},
            "absent-server":   {"transport": "stdio", "command": "uvx", "args": ["nope@latest"]},
        },
        "docs_mcp": {}}

live = {
    "chrome-devtools": {"command": PS, "args": BASE + [LIVE_OLD]},          # -> UPDATE
    "linkedin":        {"command": "uvx", "args": ["mcp-server-linkedin@latest"]},  # -> MATCH
    "context7":        {"url": URL},                                       # -> MATCH
    "machine-extra":   {"command": "uvx", "args": ["something-local"]},     # -> EXTRA
    # "absent-server" deliberately not installed                           # -> ADD
    }   # NOTE: indented -- a `}` in column 0 would end the @test block early
        # in tests/bats-fallback.sh (see the header note).

def write_claude(entries):
    servers = {n: ({"type": "http", "url": e["url"]} if "url" in e
                   else {"type": "stdio", "command": e["command"], "args": e["args"]})
               for n, e in entries.items()}
    (HOME / ".claude.json").write_text(json.dumps({"mcpServers": servers}, indent=2))

def lit(s):
    # TOML multi-line literal string: no escape processing, so backslashes and
    # double quotes in the powershell snippet survive verbatim.
    assert "'''" not in s
    return "'''" + s + "'''"

def write_codex(entries):
    p = HOME / ".codex" / "config.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for n, e in entries.items():
        out.append("[mcp_servers.%s]" % n)
        if "url" in e:
            out.append("url = " + lit(e["url"]))
        else:
            out.append("command = " + lit(e["command"]))
            out.append("args = [" + ", ".join(lit(a) for a in e["args"]) + "]")
        out.append("")
    p.write_text("\n".join(out))

def write_agy(entries):
    p = HOME / ".gemini" / "config" / "mcp_config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    servers = {n: ({"httpUrl": e["url"]} if "url" in e
                   else {"command": e["command"], "args": e["args"]})
               for n, e in entries.items()}
    p.write_text(json.dumps({"mcpServers": servers}, indent=2))

writers = {"claude": write_claude, "codex": write_codex, "agy": write_agy}
assert set(writers) == set(reconcile.CLIS), ("backend added without drift coverage", reconcile.CLIS)

fail = []
for cli in reconcile.CLIS:
    writers[cli](live)
    desired = reconcile.desired_mcp(caps, cli)
    cur = reconcile.mcp_current(cli)
    rows, extras = reconcile.classify_mcp(desired, cur)
    status = {n: s for n, s, _ in rows}
    for name, expected in (("chrome-devtools", "UPDATE"),   # the false-MATCH bug
                           ("linkedin", "MATCH"),
                           ("context7", "MATCH"),
                           ("absent-server", "ADD")):
        got = status.get(name)
        if got != expected:
            fail.append("%s: %s expected %s, got %s" % (cli, name, expected, got))
    if "machine-extra" not in extras:
        fail.append("%s: undeclared 'machine-extra' should be EXTRA, extras=%s" % (cli, extras))
    for declared in caps["mcp_servers"]:
        if declared in extras:
            fail.append("%s: declared '%s' must never be EXTRA" % (cli, declared))

if fail:
    for f in fail:
        print("FAIL " + f)
    sys.exit(1)
print("DRIFT-OK")
PY
  run python3 "$BATS_TEST_TMPDIR/drift.py" "$BATS_TEST_DIRNAME/../scripts/lib" "$BATS_TEST_TMPDIR/home"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRIFT-OK"* ]]
}

@test "a live entry missing the declared field is drift, not a match (all backends)" {
  # Same defect class, latent in codex/agy too: the old comparison guarded on
  # `wc and hc` / `want and have`, so a live entry that had NO command (or NO url)
  # where one was declared silently reported MATCH -- i.e. an installed server of
  # the wrong transport read as in-sync. Absence of a declared field is drift.
  cat > "$BATS_TEST_TMPDIR/missing.py" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import reconcile

stdio = {"transport": "stdio", "command": "uvx", "args": ["mcp-server-linkedin@latest"]}
http = {"transport": "http", "url": "https://mcp.context7.com/mcp"}

cases = [
    ("stdio declared, live entry has no command", stdio, {"args": ["mcp-server-linkedin@latest"]}, True),
    ("stdio declared, live entry is empty",       stdio, {}, True),
    ("http declared, live entry has no url",      http,  {"command": "uvx"}, True),
    ("http declared, live entry is empty",        http,  {}, True),
    ("stdio declared, live entry equal",          stdio, {"command": "uvx", "args": ["mcp-server-linkedin@latest"]}, False),
    ("http declared, live url equal (url key)",   http,  {"url": http["url"]}, False),
    ("http declared, live url equal (httpUrl)",   http,  {"httpUrl": http["url"]}, False),
    ("http declared, live httpUrl differs",       http,  {"httpUrl": "https://elsewhere.example/mcp"}, True),
]
fail = []
for label, spec, cur, want_drift in cases:
    got = reconcile.mcp_drift(spec, cur) is not None
    if got != want_drift:
        fail.append("%s: expected drift=%s, got drift=%s" % (label, want_drift, got))

# mcp_drift must not be able to special-case a backend: it takes no cli argument.
import inspect
params = list(inspect.signature(reconcile.mcp_drift).parameters)
if "cli" in params:
    fail.append("mcp_drift regained a `cli` parameter (%s) -- per-backend skips become possible again" % params)

if fail:
    for f in fail:
        print("FAIL " + f)
    sys.exit(1)
print("MISSING-FIELD-OK")
PY
  run python3 "$BATS_TEST_TMPDIR/missing.py" "$BATS_TEST_DIRNAME/../scripts/lib"
  [ "$status" -eq 0 ]
  [[ "$output" == *"MISSING-FIELD-OK"* ]]
}

@test "reconcile --all without --apply writes nothing (read-only guarantee)" {
  py=$(command -v python3)
  home="$BATS_TEST_TMPDIR/ro-home"
  mkdir -p "$home/.codex" "$home/.gemini/config" "$home/.claude"

  cat > "$home/.claude.json" <<'JSON'
{"mcpServers": {"chrome-devtools": {"type": "stdio", "command": "/opt/powershell.exe", "args": ["-NoProfile", "STALE"]}}}
JSON
  cat > "$home/.gemini/config/mcp_config.json" <<'JSON'
{"mcpServers": {"chrome-devtools": {"command": "/opt/powershell.exe", "args": ["-NoProfile", "STALE"]}}}
JSON
  cat > "$home/.codex/config.toml" <<'TOML'
[mcp_servers.chrome-devtools]
command = "/opt/powershell.exe"
args = ["-NoProfile", "STALE"]
TOML

  snap() { find "$1" -type f -exec md5sum {} + 2>/dev/null | sort; }
  before=$(snap "$home")

  # PATH stripped so no real CLI can run and write into the fixture HOME --
  # anything that changes must therefore be reconcile.py's own doing.
  run env HOME="$home" PATH="$home/no-such-bin" "$py" \
      "$BATS_TEST_DIRNAME/../scripts/lib/reconcile.py" --all
  [ "$status" -eq 0 ]

  after=$(snap "$home")
  [ "$before" = "$after" ]

  # and it must have reported the drift it saw rather than staying silent
  [[ "$output" == *"UPDATE"* ]]
  # no backup files -- those are only ever written on an apply path
  [ -z "$(find "$home" -name '*.khenrix-backup' -print -quit)" ]
}

@test "claude mcp list diagnostics are not scraped as EXTRA servers" {
  # `claude mcp list` prints an "MCP config diagnostics" section whose lines also
  # contain ": " ("Location: …", "For help configuring MCP servers, see: …"). The
  # scraper took every such line as a server name, so reconcile reported EXTRA rows
  # for servers that do not exist.
  mkdir -p "$BATS_TEST_TMPDIR/bin" "$BATS_TEST_TMPDIR/diag-home"
  cat > "$BATS_TEST_TMPDIR/bin/claude" <<'SH'
#!/usr/bin/env bash
cat <<'OUT'
Checking MCP server health…

context7: https://mcp.context7.com/mcp (HTTP) - ok
plugin:playwright:playwright: npx @playwright/mcp@latest - ok

MCP config diagnostics

For help configuring MCP servers, see: https://code.claude.com/docs/en/mcp

[Contains warnings] User config (available in all your projects)
Location: /home/u/.claude.json
 [Warning] [slack] mcpServers.slack: Missing environment variables: SLACK_MCP_XOXC_TOKEN
OUT
SH
  chmod +x "$BATS_TEST_TMPDIR/bin/claude"

  cat > "$BATS_TEST_TMPDIR/diag.py" <<'PY'
import os, sys
from pathlib import Path
LIB, HOME, BIN = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
os.environ["HOME"] = str(HOME)
os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, LIB)
import reconcile

names = set(reconcile.claude_mcp_current())
expected = {"context7", "plugin:playwright:playwright"}
junk = {n for n in names if n not in expected}
if names != expected:
    print("FAIL names=%r  unexpected=%r" % (sorted(names), sorted(junk)))
    sys.exit(1)
print("DIAG-OK")
PY
  run python3 "$BATS_TEST_TMPDIR/diag.py" "$BATS_TEST_DIRNAME/../scripts/lib" \
      "$BATS_TEST_TMPDIR/diag-home" "$BATS_TEST_TMPDIR/bin"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DIAG-OK"* ]]
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
