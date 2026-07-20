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

usage() {
  cat <<'USAGE'
bootstrap-tier0.sh -- unauthenticated prerequisite provisioning (Tier 0).

Usage: bootstrap-tier0.sh [--dry-run] [--help]

  --dry-run   Print the plan and mutate NOTHING (not even mkdir).
  --help      This text.

Provisions: the apt base (git curl jq unzip ca-certificates), and on WSL the
Windows bridges ~/.local/bin/powershell.exe and ~/.local/bin/windows-chrome.
Reports -- never installs -- the Windows-side prerequisites WSL cannot provide.

Exit 0 when every prerequisite is satisfied, 1 when one is missing or a step
failed, 2 on a usage error.
USAGE
}

DRY=0
# Unknown flags must NOT fall through to a real run. `--dryrun` is a plausible
# typo for `--dry-run`, and silently provisioning the machine because a flag was
# misspelt defeats the entire point of shipping a dry run.
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "bootstrap-tier0.sh: unknown argument: $1" >&2
       echo "Try --help." >&2
       exit 2 ;;
  esac
  shift
done

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
#
# Seam for the tests only, same category as PROC_VERSION above: without it the
# interop tests would silently fall through to the developer's REAL PowerShell
# and stop testing the fixture they set up.
WIN_PS="${KHENRIX_TIER0_WIN_PS:-/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe}"

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

# Does this powershell.exe candidate actually REACH WINDOWS? Nonce round trip:
# exit 0 proves nothing, because a stub that succeeds without echoing has not
# reached anything.
#
# The executable bit is NOT a substitute for this. An executable-but-broken shim
# used to be trusted on the strength of `-x` alone, and because it was preferred
# over the absolute interop path it poisoned every probe downstream: the
# Windows-node probe came back empty and Tier 0 told the user to install Node
# they already had. Nothing ever re-examined the shim, so that misdiagnosis was
# permanent. Trust is now earned per run, by behaviour.
ps_functional() {
  local ps="$1" nonce out
  [ -e "$ps" ] || return 1
  nonce="khenrix-tier0-$$-${RANDOM}"
  out=$( ( cd /mnt/c 2>/dev/null || true
           timeout 60 "$ps" -NoProfile -Command "Write-Output $nonce" ) \
         < /dev/null 2>/dev/null | tr -d '\r' )
  case "$out" in *"$nonce"*) return 0 ;; *) return 1 ;; esac
}

# The interop binary to probe WITH: the shim when it functions, else the
# absolute Windows path, else nothing. Memoised -- the round trip costs ~0.5s
# and several callers want it.
PS_BEST=""
PS_BEST_SET=0
ps_best() {
  if [ "$PS_BEST_SET" = 0 ]; then
    PS_BEST_SET=1
    if ps_functional "$BIN/powershell.exe"; then
      PS_BEST="$BIN/powershell.exe"
    elif ps_functional "$WIN_PS"; then
      PS_BEST="$WIN_PS"
    fi
  fi
  [ -n "$PS_BEST" ] || return 127
  printf '%s' "$PS_BEST"
}

# Run a PowerShell command through whichever interop path actually works.
# PowerShell misbehaves from a UNC working directory (which is what a WSL path
# looks like from Windows), so interop runs from /mnt/c.
#
# `< /dev/null` is load-bearing: powershell.exe DRAINS stdin even with -Command.
# A probe that swallows its caller's stdin corrupts whatever is feeding this
# script (observed: it ate a test runner's work list). A probe reads nothing.
ps_probe() {
  local ps
  ps="$(ps_best)" || return 127
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
  #
  # PRESERVED only if it FUNCTIONS. Any working variant is fine and may be
  # legitimately customised -- but "present and executable" was never evidence
  # of "working", and treating it as such kept a dead shim alive indefinitely
  # while misattributing its failures to a missing Windows-side Node.
  if [ -x "$BIN/powershell.exe" ] && ps_functional "$BIN/powershell.exe"; then
    echo "SKIP: powershell.exe shim present and round-trips a nonce"
  else
    if [ -e "$BIN/powershell.exe" ]; then
      echo "  NOTE: the existing powershell.exe shim does NOT round-trip a nonce"
      echo "        -- it is broken, not customised. Replacing it."
      PS_BEST=""; PS_BEST_SET=0   # the verdict was about the OLD file
    fi
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
# Defects in the hand-written predecessor that are fixed here:
#
#   1. ARGUMENTS NEVER ARRIVED. It passed the URL as a trailing word after
#      `-Command <script>` and read it back as $args[0]. powershell.exe does
#      NOT populate $args in that form -- verified on this machine: $args.Count
#      is 0 and the trailing words are appended to the script text and executed.
#      So $args[0] was always empty and Start-Process got an empty ArgumentList.
#   2. $HOME was hardcoded to /home/khenrix.
#   3. Chrome's path was hardcoded to the Program Files install, so a per-user
#      (LOCALAPPDATA) or winget install failed permanently. It is RESOLVED now.
#   4. Every failure was invisible: the launch ran `>/dev/null 2>&1 &`, which
#      discarded both streams AND the exit status. Errors now reach stderr and
#      the exit status.
#
# VALUES TRAVEL IN THE ENVIRONMENT, NEVER ON THE COMMAND LINE.
#
# The obvious fix for (1) is to embed the values as base64 literals in the
# script text. It does not work on this fleet, and the failure is not subtle:
#
#     FromBase64String alone         -> ok
#     Start-Process alone            -> ok
#     BOTH on one command line       -> powershell.exe: Invalid argument
#
# `FromBase64String` next to `Start-Process` is a textbook fileless-PowerShell
# signature, and the resident AV (Bitdefender here -- Defender is off, so this
# is not an ASR rule) refuses it at CreateProcess time. PowerShell never starts,
# so nothing inside the script can compensate. WSLENV hands the values across
# the boundary out of band instead: no literal in the command line, no
# signature, and no quoting surface to inject into.
#
# Note that any test which substitutes an echo for Start-Process removes the
# very token pair that triggers the block, and will pass on a host where the
# real thing is refused.
set -euo pipefail

[ $# -eq 0 ] && exit 0

PS="$HOME/.local/bin/powershell.exe"
[ -x "$PS" ] || { echo "windows-chrome: no PowerShell interop shim at $PS" >&2; exit 1; }

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

rc=0
for url in "$@"; do
  # SCHEME ALLOWLIST. This shim is vercel's BROWSER, so its argument arrives
  # from an MCP server -- it is untrusted input, not something a human typed.
  #
  # `Start-Process -ArgumentList $u` appends $u to the target's command line
  # UNQUOTED, and Chrome's parser then splits it on whitespace. Measured
  # against a recorder:
  #     IN  : https://x.com/q?x=1 --load-extension=C:\evil
  #     OUT : RAW=[https://x.com/q?x=1 --load-extension=C:\evil]
  # -- so --load-extension becomes a switch, not part of the URL. A real URL
  # never contains a raw space or a double quote (both must be percent-encoded),
  # so rejecting them costs nothing and closes the split.
  case "$url" in
    http://*|https://*) ;;
    *) echo "windows-chrome: refusing to open a non-http(s) argument: $url" >&2
       rc=1; continue ;;
  esac
  case "$url" in
    *[[:space:]]*|*'"'*)
       echo "windows-chrome: refusing a URL containing whitespace or a quote --" \
            "Chrome would parse the remainder as command-line switches: $url" >&2
       rc=1; continue ;;
  esac
  # Belt and braces: the allowlist above already forbids the characters that
  # would split, and quoting the ArgumentList means one argument reaches Chrome
  # even if it did not.
  WINCHROME_URL="$url" WINCHROME_EXE="$CHROME" WSLENV='WINCHROME_URL:WINCHROME_EXE' \
  "$PS" -NoProfile -Command '
    try {
      Start-Process -FilePath $env:WINCHROME_EXE `
                    -ArgumentList ([char]34 + $env:WINCHROME_URL + [char]34) `
                    -ErrorAction Stop
    } catch { Write-Error $_; exit 1 }' < /dev/null \
    || { echo "windows-chrome: failed to launch $url" >&2; rc=1; }
done
exit "$rc"
EOF

  echo "== Windows-side prerequisites (NOT installable from WSL) =="
  # Interop is reported SEPARATELY from what it is used to probe. Folding the
  # two together is how a broken shim came out as "install Node" -- advice for
  # software the user already had, with the actual fault invisible.
  if ! ps_best >/dev/null; then
    echo "  MISSING: Windows PowerShell interop -- nothing round-trips a nonce"
    echo "           (tried $BIN/powershell.exe and $WIN_PS)."
    echo "           The Windows-side prerequisites CANNOT be probed until this"
    echo "           works. This is NOT a report that they are absent."
    FAIL=1
  else
    WNODE=$(ps_probe '(Get-Command node.exe -EA SilentlyContinue).Source' | head -n1)
    if [ -z "$WNODE" ]; then
      echo "  MISSING: Windows-side Node.js -- the chrome-devtools MCP runs on the"
      echo "           WINDOWS side and cannot use WSL's node. On Windows run:"
      echo "             winget install OpenJS.NodeJS.LTS"
      FAIL=1
    else
      echo "  ok: Windows node at $WNODE"
    fi
  fi
else
  echo "SKIP: not WSL -- no Windows bridges needed"
fi

echo "== Tier 0 done (dry-run=$DRY, missing-prereqs=$FAIL) =="
exit "$FAIL"
