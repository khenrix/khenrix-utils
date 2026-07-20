#!/usr/bin/env bash
# bootstrap-tier0.sh -- UNAUTHENTICATED prerequisite provisioning.
#
# Runs on a BARE distro. No credentials, no marketplaces, no plugins, no CLIs.
# Tier 1 (scripts/bootstrap-machine.sh) assumes this has already succeeded.
#
# WHY TWO TIERS. bootstrap-machine.sh runs under `set -euo pipefail` and opens by
# probing seven binaries (claude, codex, agy, uv, gh, node, git) that a fresh
# distro does not have. "Fail hard on a missing prerequisite" and "validate on a
# bare distro" cannot both hold in one script: the hard failure aborts before
# anything useful runs. So they are split by AUTHENTICATION, which is the real
# seam:
#     Tier 0 (here)  unauthenticated, tolerant, PROVISIONS prerequisites.
#                    `set -u -o pipefail` but deliberately NOT -e: the job is to
#                    report EVERY missing prerequisite in one pass, not to abort
#                    on the first.
#     Tier 1         authenticated, fails hard, assumes Tier 0 succeeded.
#
# WHAT THIS OWNS. capabilities.toml references ~/.local/bin/powershell.exe and
# ~/.local/bin/windows-chrome, and docs/machine-setup.md lists them as manual
# prerequisites -- but nothing created them. On a second machine the
# chrome-devtools MCP was configured and dead for exactly that reason. Tier 0
# creates them, and reports the one prerequisite WSL cannot install for you:
# Windows-side Node.
#
# Idempotent (check-before-act; a re-run rewrites nothing that is already
# correct) and `--dry-run` mutates nothing whatsoever.
set -uo pipefail

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

FAIL=0

run() {
  if [ "$DRY" = 1 ]; then echo "DRY:  $*"; return 0; fi
  echo "RUN:  $*"
  if ! "$@"; then
    echo "  FAILED: $*" >&2
    FAIL=1
    return 1
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }

BIN="$HOME/.local/bin"

# Seam for the portability test only. WSL detection is a file read; pointing it
# at a fixture is the only way to exercise the native-Linux branch from WSL.
PROC_VERSION="${KHENRIX_TIER0_PROC_VERSION:-/proc/version}"

# The canonical Windows-side PowerShell, reachable without the shim and without
# WSL's appendWindowsPath. Used to PROBE before the shim exists, so a dry run on
# a bare distro still reports the Windows prerequisites accurately.
WIN_PS='/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe'

is_wsl() { grep -qi microsoft "$PROC_VERSION" 2>/dev/null; }

# Install $1 from stdin, 0755, only when the content differs.
#
# The two bridges are treated ASYMMETRICALLY on purpose -- see the call sites:
# powershell.exe is left alone if it already exists (any working variant is
# fine, and it may be legitimately customised), whereas windows-chrome is OWNED
# by this script because the copy already deployed on this fleet is known
# broken and must be replaced, not preserved.
install_file() {
  local dest="$1" tmp rc=0
  tmp="$(mktemp)" || { echo "  FAILED: mktemp" >&2; FAIL=1; return 1; }
  cat > "$tmp"
  if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
    rm -f "$tmp"
    echo "SKIP: $dest up to date"
    return 0
  fi
  if [ "$DRY" = 1 ]; then
    rm -f "$tmp"
    echo "DRY:  write $dest"
    return 0
  fi
  install -m 0755 "$tmp" "$dest" || rc=$?
  rm -f "$tmp"
  if [ "$rc" -ne 0 ]; then
    echo "  FAILED: write $dest" >&2
    FAIL=1
    return 1
  fi
  echo "RUN:  wrote $dest"
}

# Run a PowerShell command through the shim if it exists, else through the
# absolute interop path. PowerShell misbehaves from a UNC working directory
# (which is what a WSL path looks like from Windows), so interop runs from /mnt/c.
#
# `< /dev/null` is load-bearing: powershell.exe DRAINS stdin even with -Command.
# A probe that swallows its caller's stdin corrupts whatever is feeding this
# script (observed: it ate a test runner's work list). A probe reads nothing.
ps_probe() {
  local ps="$BIN/powershell.exe"
  [ -x "$ps" ] || ps="$WIN_PS"
  [ -e "$ps" ] || return 127
  ( cd /mnt/c 2>/dev/null || true; "$ps" -NoProfile -Command "$1" ) \
    < /dev/null 2>/dev/null | tr -d '\r'
}

# ---------------------------------------------------------------------------

if [ "$DRY" = 1 ]; then
  [ -d "$BIN" ] || echo "DRY:  mkdir -p $BIN"
else
  mkdir -p "$BIN" || { echo "  FAILED: mkdir -p $BIN" >&2; FAIL=1; }
fi

echo "== apt prerequisites =="
# Check-before-act: on a machine that already has these, Tier 0 must not invoke
# sudo at all. ca-certificates ships no binary, so it is probed by its cert bundle.
APT_PKGS=(git curl jq unzip ca-certificates)
missing=()
for b in git curl jq unzip; do have "$b" || missing+=("$b"); done
[ -e /etc/ssl/certs/ca-certificates.crt ] || missing+=(ca-certificates)
if [ "${#missing[@]}" -eq 0 ]; then
  echo "SKIP: apt prerequisites already present (${APT_PKGS[*]})"
else
  echo "  missing: ${missing[*]}"
  if [ "$(id -u)" -eq 0 ]; then
    run apt-get update -qq
    run apt-get install -y --no-install-recommends "${APT_PKGS[@]}"
  else
    run sudo apt-get update -qq
    run sudo apt-get install -y --no-install-recommends "${APT_PKGS[@]}"
  fi
fi

echo "== WSL Windows bridges =="
if is_wsl; then
  # powershell.exe shim: absolute path via /init so it resolves regardless of
  # appendWindowsPath, and regardless of what PATH the CLI hands an MCP server.
  # PRESERVED if already present: it is a one-liner, any working variant is fine.
  if [ -x "$BIN/powershell.exe" ]; then
    echo "SKIP: powershell.exe shim present"
  else
    install_file "$BIN/powershell.exe" <<'EOF'
#!/usr/bin/env bash
# Provisioned by khenrix-utils scripts/bootstrap-tier0.sh.
set -euo pipefail
exec /init /mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe "$@"
EOF
  fi

  # windows-chrome is OWNED by Tier 0 (replaced unless byte-identical): the
  # hand-written copy deployed on this fleet has three defects, all fixed below.
  install_file "$BIN/windows-chrome" <<'EOF'
#!/usr/bin/env bash
# windows-chrome -- open URLs in the WINDOWS Chrome from inside WSL.
# Provisioned by khenrix-utils scripts/bootstrap-tier0.sh -- do not hand-edit.
#
# Four defects in the hand-written predecessor are fixed here:
#
#   1. ARGUMENTS NEVER ARRIVED. It passed the URL as a trailing word after
#      `-Command <script>` and read it back as $args[0]. powershell.exe does
#      NOT populate $args in that form -- verified on this machine: $args.Count
#      is 0 and the trailing words are appended to the script text and executed.
#      So $args[0] was always empty and Start-Process got an empty ArgumentList.
#      Values are now embedded as base64 literals inside the script itself.
#      base64 is [A-Za-z0-9+/=] only, so it cannot break out of the quoting.
#   2. $HOME was hardcoded to /home/khenrix.
#   3. Chrome's path was hardcoded to the Program Files install, so a per-user
#      (LOCALAPPDATA) or winget install failed permanently. It is RESOLVED now.
#   4. Every failure was invisible: the launch ran `>/dev/null 2>&1 &`, which
#      discarded both streams AND the exit status. Errors now reach stderr and
#      the exit status.
set -euo pipefail

[ $# -eq 0 ] && exit 0

PS="$HOME/.local/bin/powershell.exe"
[ -x "$PS" ] || { echo "windows-chrome: no PowerShell interop shim at $PS" >&2; exit 1; }

b64() { printf '%s' "$1" | base64 -w0; }

CHROME="${WINDOWS_CHROME_PATH:-}"
if [ -z "$CHROME" ]; then
  CHROME=$("$PS" -NoProfile -Command '
    $c = (Get-Command chrome.exe -EA SilentlyContinue).Source
    if (-not $c) { $c = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                          "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                          "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") |
                   Where-Object { Test-Path $_ } | Select-Object -First 1 }
    if ($c) { $c } else { exit 1 }' < /dev/null 2>/dev/null | tr -d '\r' | head -n1) || CHROME=""
fi
if [ -z "$CHROME" ]; then
  echo "windows-chrome: Chrome not found on the Windows side (checked PATH," \
       "Program Files, Program Files (x86), LOCALAPPDATA)." \
       "Set WINDOWS_CHROME_PATH to override." >&2
  exit 1
fi

cenc=$(b64 "$CHROME")
rc=0
for url in "$@"; do
  uenc=$(b64 "$url")
  "$PS" -NoProfile -Command "
    \$u = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$uenc'));
    \$c = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$cenc'));
    try { Start-Process -FilePath \$c -ArgumentList \$u -ErrorAction Stop }
    catch { Write-Error \$_; exit 1 }" < /dev/null \
    || { echo "windows-chrome: failed to launch $url" >&2; rc=1; }
done
exit "$rc"
EOF

  echo "== Windows-side prerequisites (NOT installable from WSL) =="
  WNODE=$(ps_probe '(Get-Command node.exe -EA SilentlyContinue).Source' | head -n1)
  if [ -z "$WNODE" ]; then
    echo "  MISSING: Windows-side Node.js -- the chrome-devtools MCP runs on the"
    echo "           WINDOWS side and cannot use WSL's node. On Windows run:"
    echo "             winget install OpenJS.NodeJS.LTS"
    FAIL=1
  else
    echo "  ok: Windows node at $WNODE"
  fi
else
  echo "SKIP: not WSL -- no Windows bridges needed"
fi

echo "== Tier 0 done (dry-run=$DRY, missing-prereqs=$FAIL) =="
exit "$FAIL"
