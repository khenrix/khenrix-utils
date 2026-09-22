#!/usr/bin/env bats
# Optional WSL/Windows integration witness. The certifying suite uses hermetic
# PowerShell fixtures; this test belongs outside it because the required host
# boundary does not exist on macOS or ordinary Linux.

setup() {
  export TIER0="$BATS_TEST_DIRNAME/../scripts/bootstrap-tier0.sh"
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"
  export BIN="$HOME/.local/bin"
}

@test "REAL powershell.exe: the provisioned shim launches and delivers the URL" {
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
