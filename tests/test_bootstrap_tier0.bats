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
  unset KHENRIX_TIER0_PROC_VERSION

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

# A powershell.exe stand-in. It answers the two probes tier0/windows-chrome make
# and, for Start-Process, DECODES the base64 literals out of the script it was
# handed and logs them -- so a test can assert the URL and the Chrome path
# arrived intact rather than merely that something was invoked.
plant_fake_ps() {
  mkdir -p "$BIN"
  cat > "$BIN/powershell.exe" <<'PSEOF'
#!/usr/bin/env bash
cmd=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-Command" ]; then cmd="$2"; fi
  shift
done
case "$cmd" in
  *"Get-Command node.exe"*)
    [ -n "${FAKE_NODE_PATH:-}" ] || exit 4
    printf '%s\r\n' "$FAKE_NODE_PATH"
    ;;
  *"Get-Command chrome.exe"*)
    [ -n "${FAKE_CHROME_PATH:-}" ] || exit 1
    printf '%s\r\n' "$FAKE_CHROME_PATH"
    ;;
  *Start-Process*)
    printf '%s\n' "$cmd" \
      | grep -o "FromBase64String('[A-Za-z0-9+/=]*')" \
      | sed "s/^FromBase64String('//; s/')\$//" \
      | while read -r b; do
          printf 'ARG=%s\n' "$(printf '%s' "$b" | base64 -d)"
        done >> "${FAKE_PS_LOG:-/dev/null}"
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
  grep -q FromBase64String "$BIN/windows-chrome"
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

@test "generated windows-chrome resolves a PER-USER Chrome install" {
  plant_fake_ps
  export FAKE_NODE_PATH='C:\Program Files\nodejs\node.exe'
  "$TIER0" >/dev/null 2>&1
  export FAKE_CHROME_PATH='C:\Users\sb2\AppData\Local\Google\Chrome\Application\chrome.exe'
  run "$BIN/windows-chrome" "https://example.com/?a=1&b=2"
  [ "$status" -eq 0 ]
  grep -qF 'ARG=C:\Users\sb2\AppData\Local\Google\Chrome\Application\chrome.exe' "$FAKE_PS_LOG"
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
