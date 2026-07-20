#!/usr/bin/env bats
#
# Behavioural tests for scripts/bootstrap-tier0.sh.
#
# These drive the REAL script against a fake $HOME and a fake powershell.exe.
# Nothing here asserts on source text: every check either inspects a file the
# script actually created, or EXECUTES a shim the script generated and asserts
# on what that shim did. The windows-chrome tests in particular run the
# generated shim end to end and decode the PowerShell payload it produced, so
# they fail if the argument-passing is broken -- which is exactly how the
# hand-written predecessor was broken (see the $args note in the shim header).
#
# NOTE: closing braces of @test blocks must stay at column 0 -- tests/bats-fallback.sh
# relies on that to run this file when the real bats binary is unavailable.

setup() {
  export TIER0="$BATS_TEST_DIRNAME/../scripts/bootstrap-tier0.sh"
  # A fake $HOME with NO .local/bin: dry-run must not even create the directory.
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"
  export BIN="$HOME/.local/bin"
  export FAKE_PS_LOG="$BATS_TEST_TMPDIR/ps.log"
  : > "$FAKE_PS_LOG"
  unset FAKE_NODE_PATH FAKE_CHROME_PATH WINDOWS_CHROME_PATH
  unset FAKE_PS_BROKEN FAKE_LAUNCH_FAIL FAKE_WIN_EXISTING
  unset FAKE_PROGRAMFILES FAKE_PROGRAMFILESX86 FAKE_LOCALAPPDATA
  unset KHENRIX_TIER0_PROC_VERSION

  # Point the absolute-interop fallback at nothing. Without this, any test whose
  # fake shim is deliberately broken would silently fall through to the
  # developer's REAL powershell.exe and stop testing its own fixture.
  export KHENRIX_TIER0_WIN_PS="$BATS_TEST_TMPDIR/no-such-powershell.exe"

  # Stub the apt-managed binaries onto PATH so the apt branch is decided by the
  # FIXTURE, not by the host. Without this the suite is hostage to whatever the
  # developer's machine happens to have -- this one is missing `unzip`, so every
  # test would drive the real sudo path and fail on a password prompt.
  : "${ORIG_PATH:=$PATH}"
  export ORIG_PATH
  export STUB="$BATS_TEST_TMPDIR/stub"
  mkdir -p "$STUB"
  for b in git curl jq unzip; do
    printf '#!/bin/sh\nexit 0\n' > "$STUB/$b"
    chmod +x "$STUB/$b"
  done
  export PATH="$STUB:$ORIG_PATH"
}

# A powershell.exe stand-in for the LOGIC-BRANCH tests. It is fast and total,
# and it is NOT sufficient on its own -- see the REAL-interop test at the bottom
# of this file, which drives the actual powershell.exe. A fake that inspects
# command text cannot observe an AV refusing CreateProcess, a quoting bug, or
# anything else that happens before PowerShell runs. That blind spot is exactly
# how a shim that could not launch anything shipped green.
#
# Fixture knobs:
#   FAKE_PS_BROKEN      executable, exits 0, echoes nothing (a DEAD shim)
#   FAKE_NODE_PATH      what `Get-Command node.exe` resolves to
#   FAKE_CHROME_PATH    what `Get-Command chrome.exe` resolves to (PATH hit)
#   FAKE_PROGRAMFILES / FAKE_PROGRAMFILESX86 / FAKE_LOCALAPPDATA
#                       Windows env values used to expand the FALLBACK candidates
#   FAKE_WIN_EXISTING   newline-separated paths that `Test-Path` should accept
#   FAKE_LAUNCH_FAIL    make Start-Process fail
plant_fake_ps() {
  mkdir -p "$BIN"
  cat > "$BIN/powershell.exe" <<'PSEOF'
#!/usr/bin/env bash
# Real powershell.exe DRAINS stdin even with -Command; a probe that forgets
# `< /dev/null` therefore eats its caller's stdin. Model that, or the guard
# against it is untestable.
cat > /dev/null 2>/dev/null

cmd=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-Command" ]; then cmd="$2"; fi
  shift
done

# A shim that is present and executable but reaches nothing.
[ -z "${FAKE_PS_BROKEN:-}" ] || exit 0

case "$cmd" in
  "Write-Output "*)
    printf '%s\r\n' "${cmd#Write-Output }"
    ;;
  *"Get-Command node.exe"*)
    [ -n "${FAKE_NODE_PATH:-}" ] || exit 4
    printf '%s\r\n' "$FAKE_NODE_PATH"
    ;;
  *"Get-Command chrome.exe"*)
    if [ -n "${FAKE_CHROME_PATH:-}" ]; then
      printf '%s\r\n' "$FAKE_CHROME_PATH"
      exit 0
    fi
    # Emulate the FALLBACK list the way PowerShell would: read the candidates
    # the script actually declares, expand the Windows env placeholders, and
    # take the first one the fixture says exists. Nothing here hardcodes the
    # expected paths, so a wrong candidate in the script cannot pass.
    hit=""
    while IFS= read -r cand; do
      p=$cand
      p=${p//'${env:ProgramFiles(x86)}'/${FAKE_PROGRAMFILESX86:-}}
      p=${p//'$env:ProgramFiles'/${FAKE_PROGRAMFILES:-}}
      p=${p//'$env:LOCALAPPDATA'/${FAKE_LOCALAPPDATA:-}}
      if printf '%s\n' "${FAKE_WIN_EXISTING:-}" | grep -qxF "$p"; then
        hit="$p"; break
      fi
    done < <(printf '%s\n' "$cmd" | grep -o '"[^"]*chrome\.exe"' | sed 's/^"//; s/"$//')
    [ -n "$hit" ] || exit 1
    printf '%s\r\n' "$hit"
    ;;
  *Start-Process*)
    [ -z "${FAKE_LAUNCH_FAIL:-}" ] || exit 1
    # The values arrive in the ENVIRONMENT (via WSLENV), not in the command
    # text. That is the whole point of the fix: a base64 literal next to
    # Start-Process is a fileless-PowerShell signature and the AV on this fleet
    # refuses the process outright. Read them where they now travel.
    {
      printf 'ARG=%s\n' "${WINCHROME_EXE:-<unset>}"
      printf 'ARG=%s\n' "${WINCHROME_URL:-<unset>}"
    } >> "${FAKE_PS_LOG:-/dev/null}"
    ;;
esac
exit 0
PSEOF
  chmod +x "$BIN/powershell.exe"
}

# ---------------------------------------------------------------- dry-run ---

@test "--dry-run mutates nothing at all -- not even mkdir" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  rm -rf "$BIN"
  run "$TIER0" --dry-run
  [ ! -e "$BIN" ]
  [[ "$output" == *"DRY:"* ]]
}

@test "--dry-run leaves an existing windows-chrome byte-identical" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  echo "SENTINEL-DO-NOT-TOUCH" > "$BIN/windows-chrome"
  before=$(cat "$BIN/windows-chrome")
  run "$TIER0" --dry-run
  [ "$(cat "$BIN/windows-chrome")" = "$before" ]
}

# ------------------------------------------------------------ provisioning ---

@test "a real run provisions both bridges as executables" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  rm -f "$BIN/powershell.exe"
  run "$TIER0"
  [ -x "$BIN/powershell.exe" ]
  [ -x "$BIN/windows-chrome" ]
}

@test "an existing powershell.exe shim is preserved, not clobbered" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  before=$(cat "$BIN/powershell.exe")
  run "$TIER0"
  [ "$(cat "$BIN/powershell.exe")" = "$before" ]
}

@test "re-running is idempotent: the second pass rewrites nothing" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  stamp_before=$(stat -c %Y.%N "$BIN/windows-chrome")
  run "$TIER0"
  stamp_after=$(stat -c %Y.%N "$BIN/windows-chrome")
  [ "$stamp_before" = "$stamp_after" ]
  [[ "$output" == *"up to date"* ]]
}

@test "a stale hand-written windows-chrome IS replaced" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  printf '#!/bin/sh\n# the old broken one\nexit 0\n' > "$BIN/windows-chrome"
  chmod +x "$BIN/windows-chrome"
  run "$TIER0"
  [[ "$(cat "$BIN/windows-chrome")" != *"the old broken one"* ]]
  grep -q WSLENV "$BIN/windows-chrome"
}

# ---------------------------------------------------- interop verification ---

@test "an executable but DEAD powershell.exe shim is replaced, not trusted" {
  # The old code skipped on the executable bit alone. A shim can be executable
  # and reach nothing, and when it is preferred over the absolute interop path
  # it poisons every probe made through it.
  plant_fake_ps
  export FAKE_PS_BROKEN=1
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  before=$(cat "$BIN/powershell.exe")
  run "$TIER0"
  [[ "$output" == *"does NOT round-trip a nonce"* ]]
  [ "$(cat "$BIN/powershell.exe")" != "$before" ]
}

@test "broken interop is reported AS interop, not as a missing Windows Node" {
  # The misdiagnosis this guards against: a dead shim made the node probe come
  # back empty, so Tier 0 told the user to `winget install` Node they already
  # had -- and nothing ever re-examined the shim, so it said that forever.
  # --dry-run keeps the broken fixture in place (nothing is rewritten), which
  # is what leaves the run with no working interop at all.
  plant_fake_ps
  export FAKE_PS_BROKEN=1
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  run "$TIER0" --dry-run
  [ "$status" -eq 1 ]
  [[ "$output" == *"MISSING: Windows PowerShell interop"* ]]
  [[ "$output" != *"MISSING: Windows-side Node.js"* ]]
}

# ------------------------------------------------------------ windows-node ---

@test "missing Windows-side Node is reported with winget guidance and exit 1" {
  plant_fake_ps
  unset FAKE_NODE_PATH
  run "$TIER0"
  [ "$status" -eq 1 ]
  [[ "$output" == *"MISSING: Windows-side Node.js"* ]]
  [[ "$output" == *"winget install OpenJS.NodeJS.LTS"* ]]
}

@test "present Windows-side Node reports its path and exits 0" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  run "$TIER0"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ok: Windows node at C:\\Program Files\\nodejs\\node.exe"* ]]
}

@test "the Windows-node report says WSL node does not satisfy it" {
  plant_fake_ps
  unset FAKE_NODE_PATH
  run "$TIER0"
  # NOT a bare *"WSL"* match: the "== WSL Windows bridges ==" header supplies
  # that substring, so the loose assertion passed even with the explanation
  # deleted. Match the explanation itself.
  [[ "$output" == *"cannot use WSL's node"* ]]
}

# ---------------------------------------------------------- windows-chrome ---

@test "generated windows-chrome uses the Chrome that Get-Command resolves" {
  # Renamed: this asserts only that whatever PowerShell RESOLVES is the thing
  # launched. It proves nothing about the fallback list -- all three fallback
  # paths could be garbage and this would still pass. The three tests below are
  # the ones that actually exercise them.
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  export FAKE_CHROME_PATH='C:\Users\sb2\AppData\Local\Google\Chrome\Application\chrome.exe'
  run "$BIN/windows-chrome" "https://example.com/?a=1&b=2"
  [ "$status" -eq 0 ]
  grep -qF 'ARG=C:\Users\sb2\AppData\Local\Google\Chrome\Application\chrome.exe' "$FAKE_PS_LOG"
}

# The fallback list is only reached when Get-Command misses, which the fake
# used always to answer -- so replacing all three candidates with garbage left
# the suite green. Each test below unsets FAKE_CHROME_PATH (forcing the miss)
# and makes exactly ONE candidate resolvable, so a wrong path in any single
# slot turns exactly one test red.
setup_fallback_env() {
  export FAKE_PROGRAMFILES='C:\Program Files'
  export FAKE_PROGRAMFILESX86='C:\Program Files (x86)'
  export FAKE_LOCALAPPDATA='C:\Users\sb2\AppData\Local'
  unset FAKE_CHROME_PATH
}

@test "chrome FALLBACK 1: Program Files, when Chrome is not on PATH" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  setup_fallback_env
  export FAKE_WIN_EXISTING='C:\Program Files\Google\Chrome\Application\chrome.exe'
  run "$BIN/windows-chrome" "https://example.com/pf"
  [ "$status" -eq 0 ]
  grep -qF 'ARG=C:\Program Files\Google\Chrome\Application\chrome.exe' "$FAKE_PS_LOG"
}

@test "chrome FALLBACK 2: Program Files (x86)" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  setup_fallback_env
  export FAKE_WIN_EXISTING='C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
  run "$BIN/windows-chrome" "https://example.com/x86"
  [ "$status" -eq 0 ]
  grep -qF 'ARG=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe' "$FAKE_PS_LOG"
}

@test "chrome FALLBACK 3: LOCALAPPDATA -- the real per-user Surface Book 2 case" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  setup_fallback_env
  export FAKE_WIN_EXISTING='C:\Users\sb2\AppData\Local\Google\Chrome\Application\chrome.exe'
  run "$BIN/windows-chrome" "https://example.com/peruser"
  [ "$status" -eq 0 ]
  grep -qF 'ARG=C:\Users\sb2\AppData\Local\Google\Chrome\Application\chrome.exe' "$FAKE_PS_LOG"
}

@test "chrome resolution prefers PATH over the fallback candidates" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  setup_fallback_env
  export FAKE_WIN_EXISTING='C:\Program Files\Google\Chrome\Application\chrome.exe'
  export FAKE_CHROME_PATH='D:\onpath\chrome.exe'
  run "$BIN/windows-chrome" "https://example.com/pathwins"
  [ "$status" -eq 0 ]
  grep -qF 'ARG=D:\onpath\chrome.exe' "$FAKE_PS_LOG"
  ! grep -qF 'ARG=C:\Program Files\Google\Chrome\Application\chrome.exe' "$FAKE_PS_LOG"
}

# ------------------------------------------------------- URL allowlisting ---

@test "windows-chrome REFUSES an argument that is not http(s)" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  export FAKE_CHROME_PATH='C:\Program Files\Google\Chrome\Application\chrome.exe'
  run "$BIN/windows-chrome" "file:///C:/Windows/System32/calc.exe"
  [ "$status" -ne 0 ]
  [[ "$output" == *"non-http(s)"* ]]
  [ ! -s "$FAKE_PS_LOG" ]
}

@test "windows-chrome ACCEPTS an uppercase scheme and preserves path case" {
  # URI schemes are case-insensitive (RFC 3986 3.1), so HTTPS:// is a valid URL.
  # The match used to be case-sensitive and refused it. The path/query are NOT
  # case-insensitive, so the URL must still arrive with its case intact --
  # lowercasing the whole argument would "fix" the scheme and break the URL.
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  export FAKE_CHROME_PATH='C:\Program Files\Google\Chrome\Application\chrome.exe'
  run "$BIN/windows-chrome" "HTTPS://example.com/MixedCase?Q=AbC"
  [ "$status" -eq 0 ]
  [[ "$output" != *"non-http(s)"* ]]
  grep -qF 'ARG=HTTPS://example.com/MixedCase?Q=AbC' "$FAKE_PS_LOG"
}

@test "windows-chrome REFUSES a mixed-case non-http(s) scheme" {
  # The case-insensitive match must not become a hole: FiLe:// is still refused.
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  export FAKE_CHROME_PATH='C:\Program Files\Google\Chrome\Application\chrome.exe'
  run "$BIN/windows-chrome" "FiLe:///C:/Windows/System32/calc.exe"
  [ "$status" -ne 0 ]
  [[ "$output" == *"non-http(s)"* ]]
  [ ! -s "$FAKE_PS_LOG" ]
}

@test "windows-chrome REFUSES a URL carrying extra Chrome switches" {
  # Start-Process appends ArgumentList to the target's command line, and
  # Chrome's parser splits it on whitespace -- so a space turns the remainder
  # into switches. This shim is vercel's BROWSER, so the URL comes from an MCP
  # server and is untrusted.
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  export FAKE_CHROME_PATH='C:\Program Files\Google\Chrome\Application\chrome.exe'
  run "$BIN/windows-chrome" 'https://x.com/q?x=1 --load-extension=C:\evil'
  [ "$status" -ne 0 ]
  [[ "$output" == *"switches"* ]]
  [ ! -s "$FAKE_PS_LOG" ]
}

@test "a rejected URL does not stop the remaining URLs, and rc is still 1" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  export FAKE_CHROME_PATH='C:\Program Files\Google\Chrome\Application\chrome.exe'
  run "$BIN/windows-chrome" "ftp://evil.example/x" "https://good.example/ok"
  [ "$status" -eq 1 ]
  grep -qF 'ARG=https://good.example/ok' "$FAKE_PS_LOG"
}

@test "a FAILED launch propagates rc=1 out of windows-chrome" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  export FAKE_CHROME_PATH='C:\Program Files\Google\Chrome\Application\chrome.exe'
  export FAKE_LAUNCH_FAIL=1
  run "$BIN/windows-chrome" "https://example.com/boom"
  [ "$status" -eq 1 ]
  [[ "$output" == *"failed to launch"* ]]
}

@test "windows-chrome errors when the PowerShell shim is missing" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  rm -f "$BIN/powershell.exe"
  run "$BIN/windows-chrome" "https://example.com"
  [ "$status" -ne 0 ]
  [[ "$output" == *"no PowerShell interop shim"* ]]
}

@test "generated windows-chrome passes the URL through INTACT" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  export FAKE_CHROME_PATH='C:\Program Files\Google\Chrome\Application\chrome.exe'
  run "$BIN/windows-chrome" "https://example.com/?a=1&b=2"
  [ "$status" -eq 0 ]
  grep -qF 'ARG=https://example.com/?a=1&b=2' "$FAKE_PS_LOG"
}

@test "generated windows-chrome SURFACES an error when Chrome is absent" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  unset FAKE_CHROME_PATH
  run "$BIN/windows-chrome" "https://example.com"
  [ "$status" -ne 0 ]
  [[ "$output" == *"not found"* ]]
}

@test "generated windows-chrome honours WINDOWS_CHROME_PATH" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  unset FAKE_CHROME_PATH
  export WINDOWS_CHROME_PATH='D:\portable\chrome.exe'
  run "$BIN/windows-chrome" "https://example.com"
  [ "$status" -eq 0 ]
  grep -qF 'ARG=D:\portable\chrome.exe' "$FAKE_PS_LOG"
}

@test "generated windows-chrome contains no hardcoded home directory" {
  # Behavioural, not a grep for the string: the shim was just RUN above under a
  # $HOME that is not /home/khenrix, and it found its PowerShell shim anyway.
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  export FAKE_CHROME_PATH='C:\Program Files\Google\Chrome\Application\chrome.exe'
  run env HOME="$HOME" "$BIN/windows-chrome" "https://portability.test"
  [ "$status" -eq 0 ]
  grep -qF 'ARG=https://portability.test' "$FAKE_PS_LOG"
}

@test "generated windows-chrome with no arguments is a no-op that succeeds" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  run "$BIN/windows-chrome"
  [ "$status" -eq 0 ]
}

# ------------------------------------------------------------- portability ---

@test "off WSL the Windows bridges are skipped and the run still succeeds" {
  echo "Linux version 6.6.0 (gcc)" > "$BATS_TEST_TMPDIR/proc-version"
  export KHENRIX_TIER0_PROC_VERSION="$BATS_TEST_TMPDIR/proc-version"
  run "$TIER0"
  [ "$status" -eq 0 ]
  [[ "$output" == *"not WSL"* ]]
  [ ! -e "$BIN/windows-chrome" ]
}

# --------------------------------------------------------------------- apt ---

@test "apt is skipped when the base packages are already present" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  run "$TIER0" --dry-run
  [[ "$output" != *"apt-get install"* ]]
}

@test "apt install is planned when a base package is missing" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  # jq lives in ~/.local/bin on this machine; a PATH without it makes jq absent.
  run env PATH=/usr/bin:/bin "$TIER0" --dry-run
  [[ "$output" == *"apt-get install"* ]]
  [[ "$output" == *"DRY:"* ]]
}

@test "as a NON-root user apt goes through sudo" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  run env PATH=/usr/bin:/bin "$TIER0" --dry-run
  [[ "$output" == *"sudo apt-get install"* ]]
}

@test "as root apt runs directly -- a bare container has no sudo" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  idstub="$BATS_TEST_TMPDIR/idstub"
  mkdir -p "$idstub"
  printf '#!/bin/sh\necho 0\n' > "$idstub/id"
  chmod +x "$idstub/id"
  run env PATH="$idstub:/usr/bin:/bin" "$TIER0" --dry-run
  [[ "$output" == *"DRY:  apt-get install"* ]]
  [[ "$output" != *"sudo apt-get"* ]]
}

# ------------------------------------------------------------------ stdin ---

@test "tier0 does not swallow its caller's stdin" {
  # powershell.exe drains stdin even with -Command. A probe that omits
  # `< /dev/null` eats whatever is feeding the script -- observed live: it ate a
  # test runner's work list and 17 of 18 tests silently never ran.
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  leftover=$(printf 'LINE1\nLINE2\n' | { "$TIER0" >/dev/null 2>&1; cat; })
  [ "$leftover" = "$(printf 'LINE1\nLINE2\n')" ]
}

# ------------------------------------------------------------------- args ---

@test "an unknown argument is an ERROR, not a silent real run" {
  # --dryrun is a plausible typo for --dry-run. A script that ships a dry run
  # must not provision the machine because a flag was misspelt.
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  rm -rf "$BIN"
  run "$TIER0" --dryrun
  [ "$status" -eq 2 ]
  [[ "$output" == *"unknown argument"* ]]
  [ ! -e "$BIN" ]
}

@test "--help prints usage and provisions nothing" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  rm -rf "$BIN"
  run "$TIER0" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"Usage:"* ]]
  [[ "$output" == *"--dry-run"* ]]
  [ ! -e "$BIN" ]
}

# --------------------------------------------------------- REAL interop -----

@test "REAL powershell.exe: the provisioned shim launches and delivers the URL" {
  # NO FAKE ANYWHERE IN THIS TEST. Every other test above mocks powershell.exe,
  # and mocks are structurally incapable of catching the defect this one exists
  # for: the AV on this fleet refuses `FromBase64String` + `Start-Process` on a
  # single command line as a fileless-PowerShell signature, and it refuses it at
  # CreateProcess time -- PowerShell never starts. A fake that greps the command
  # text sees a well-formed script and reports success. So does any test that
  # swaps Write-Output in for Start-Process, because that substitution removes
  # the very token pair that triggers the block.
  #
  # A recorder .cmd stands in for chrome.exe so no browser opens: it appends its
  # arguments to a file, which is then asserted to contain the nonce intact.
  real_ps=/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe
  [ -x "$real_ps" ] || skip "no Windows interop on this host ($real_ps absent)"
  command -v wslpath >/dev/null 2>&1 || skip "wslpath absent; cannot stage a recorder"

  mkdir -p "$BIN"
  printf '#!/usr/bin/env bash\nset -euo pipefail\nexec /init %s "$@"\n' "$real_ps" \
    > "$BIN/powershell.exe"
  chmod +x "$BIN/powershell.exe"
  "$BIN/powershell.exe" -NoProfile -Command 'Write-Output PONG' </dev/null 2>/dev/null \
    | grep -q PONG || skip "real powershell.exe does not round-trip here"

  unset KHENRIX_TIER0_WIN_PS
  "$TIER0" >/dev/null 2>&1 || true
  [ -x "$BIN/windows-chrome" ]

  wtmp=$("$BIN/powershell.exe" -NoProfile -Command \
         'Write-Output ([IO.Path]::GetTempPath())' </dev/null 2>/dev/null \
         | tr -d '\r' | head -n1)
  [ -n "$wtmp" ] || skip "could not read the Windows temp path"
  utmp=$(wslpath -u "$wtmp")
  [ -d "$utmp" ] || skip "Windows temp $utmp not visible from WSL"

  tok="khenrix-bats-$$-${RANDOM}"
  rec="$utmp/$tok.cmd"; log="$utmp/$tok.txt"
  printf '@echo off\r\nsetlocal EnableDelayedExpansion\r\nset "ARGS=%%*"\r\n>>"%%~dp0%s.txt" echo RAW=[!ARGS!]\r\n' \
    "$tok" > "$rec"

  url="https://khenrix-bats.invalid/$tok"
  rc=0
  WINDOWS_CHROME_PATH="$(wslpath -w "$rec")" "$BIN/windows-chrome" "$url" || rc=$?
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    [ -s "$log" ] && break
    sleep 0.25
  done
  got=$(tr -d '\r' < "$log" 2>/dev/null || true)
  rm -f "$rec" "$log"

  [ "$rc" -eq 0 ]
  [[ "$got" == *"$url"* ]]
}
