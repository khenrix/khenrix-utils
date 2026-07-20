#!/usr/bin/env python3
"""doctor.py — behavioural capability checks for the khenrix machine profile.

khenrix-utils encodes *configuration*; it never verified *capability*. Three
things worked on the reference machine and silently died on a second one (the
chrome-devtools MCP, clipboard image paste, the Windows-bridge shims) because
nothing ever asserted they actually functioned. This is the verifier.

NOT named `verify`: `make verify` already exists (Makefile:48) and validates
manifests/skills.
NOT in scripts/lib/: LIB_SCRIPTS membership pulls every skill into the
eval-receipt closure and fires `make precommit` across ~10 skills x 3 providers.

DESIGN RULE — assert ROUND-TRIPS, never presence.
    On the reference machine a `command -v` probe for xclip returns a path and
    exits 0 while `dpkg -l xclip` shows the package is absent: ~/.local/bin/xclip
    is a hand-written bash shim. A presence check would have certified the second
    machine healthy while image paste was dead. That IS the bug. Every check
    below either completes a round trip or reads a real artefact off disk;
    `shutil.which` appears only to LOCATE a candidate that is then interrogated,
    never as the assertion itself.

READ-ONLY, with one documented exception: `clipboard-image-roundtrip` must set
the clipboard to prove the round trip, which destroys whatever the user had
copied. It is tagged `invasive`, and `--skip-invasive` opts out of it.

TWO INDEPENDENT AXES — do not conflate them:

    APPLICABILITY (`--profile`)
        full      every check
        portable  skips only checks tagged `hardware`: host/hardware-specific
                  ones that cannot meaningfully run elsewhere. Inapplicable
                  checks SKIP; they never FAIL — a tool that cries wolf trains
                  people to ignore it.

    COST / DESTRUCTIVENESS (`--skip-invasive`, `--skip-network`)
        Opt-out flags, off by default, orthogonal to the profile.

    The profile must NOT gate the clipboard or MCP checks. `portable` is the
    profile someone reaches for on the second machine, and the clipboard round
    trip and the chrome-devtools MCP are two of the three capabilities that
    silently died there — the exact failures this script was written to catch.
    Skipping them under `portable` would hand that machine a clean report while
    both were still dead. Destructiveness is a separate concern with a separate
    flag, so anyone mid-copy-paste opts out explicitly and knowingly.

Every subprocess call goes through sh(), which refuses an unbounded timeout: a
doctor that hangs is worse than one that fails.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

try:                                  # stdlib since 3.11; the doctor must still
    import tomllib                    # import on an older interpreter, so a
except ImportError:                   # missing tomllib costs one config source,
    tomllib = None                    # not the whole script.

CHECKS: list[dict] = []

PS_SHIM = ".local/bin/powershell.exe"


def check(name, hardware=False, wsl_only=False, network=False, invasive=False):
    def deco(fn):
        CHECKS.append({"name": name, "fn": fn, "hardware": hardware,
                       "wsl_only": wsl_only, "network": network,
                       "invasive": invasive})
        return fn
    return deco


def sh(cmd, timeout=30, input=None, **kw):
    """The only subprocess entry point. An unbounded timeout is a bug, not a default."""
    if timeout is None:
        raise ValueError("sh() requires a bounded timeout")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          input=input, **kw)


def win_cwd():
    """Working directory for WINDOWS-side subprocesses only.

    PowerShell warns and misbehaves when launched from a UNC working directory
    (which is what WSL paths look like from the Windows side), so interop calls
    run from /mnt/c. Pure-Linux subprocesses — ldconfig, a version manager's
    --version — have no such problem and keep the caller's cwd; relocating them
    would be an unexplained global side effect."""
    return "/mnt/c" if os.path.isdir("/mnt/c") else None


def is_wsl():
    p = Path("/proc/version")
    return p.exists() and "microsoft" in p.read_text().lower()


def ps_shim():
    return Path.home() / PS_SHIM


# Locating node.exe on the Windows side is the slowest thing the doctor does
# (a 90s worst case), and two checks need it: `windows-node` and
# `mcp-chrome-devtools`. The result is memoised per shim path so a full run
# pays for it once.
#
# This does NOT couple the two checks. The cache is only ever a shortcut:
# windows_node_source() probes on its own whenever the cache is cold, so the MCP
# check still establishes what it needs when `windows-node` was skipped, was
# excluded by --only, or never ran at all.
_WIN_NODE_SOURCE: dict[str, str] = {}


def windows_node_source(ps, timeout=90):
    """(path_to_node.exe, None) or (None, reason). Memoised; probes if cold."""
    key = str(ps)
    if key in _WIN_NODE_SOURCE:
        return _WIN_NODE_SOURCE[key], None
    try:
        r = sh([str(ps), "-NoProfile", "-Command",
                '$s=(Get-Command node.exe -EA SilentlyContinue).Source; '
                'if(-not $s){ exit 4 }; Write-Output ("PATH=" + $s)'],
               timeout=timeout, cwd=win_cwd())
    except subprocess.TimeoutExpired:
        return None, f"Windows-side node probe timed out after {timeout}s"
    if "PATH=" not in (r.stdout or ""):
        return None, "no Windows-side node found"
    return remember_windows_node(ps, r.stdout), None


def remember_windows_node(ps, stdout):
    """Cache the node.exe path out of any probe output carrying `PATH=`."""
    src = stdout.split("PATH=", 1)[1].splitlines()[0].strip()
    _WIN_NODE_SOURCE[str(ps)] = src
    return src


# --- tiny stdlib PNG codec (no Pillow; stdlib-only is a hard constraint) ---

def solid_png(w, h, rgb):
    """Encode a solid-colour truecolour PNG."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def png_header(blob):
    """(width, height) from a PNG's IHDR, or None if it is not a PNG."""
    if not blob.startswith(b"\x89PNG\r\n\x1a\n") or blob[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", blob[16:24])


# --- Windows bridge -------------------------------------------------------

@check("windows-interop", wsl_only=True)
def _interop():
    """Round trip: hand PowerShell a fresh nonce and require it back. Exit 0
    alone proves nothing — a stub that returns success without echoing has not
    reached Windows."""
    ps = ps_shim()
    if not ps.exists():
        return "FAIL", f"missing shim {ps} — Tier 0 must provision it"
    nonce = "khenrix-doctor-" + secrets.token_hex(8)
    try:
        r = sh([str(ps), "-NoProfile", "-Command", f"Write-Output {nonce}"],
               timeout=60, cwd=win_cwd())
    except subprocess.TimeoutExpired:
        return "FAIL", "powershell.exe shim did not respond within 60s"
    if r.returncode != 0:
        return "FAIL", (f"shim present but non-functional: rc={r.returncode} "
                        f"{(r.stderr or '').strip()[:120]}")
    if nonce not in r.stdout:
        return "FAIL", ("shim exited 0 but did not echo the nonce — no round trip "
                        f"to Windows (stdout: {r.stdout.strip()[:80]!r})")
    return "PASS", "powershell.exe shim round-trips a nonce"


@check("windows-node", wsl_only=True)
def _win_node():
    """The chrome-devtools MCP runs on the WINDOWS side. Windows-side Node is
    the prerequisite nothing installs, nothing checks and no doc mentions — the
    most likely second-machine failure cause. WSL's own node does NOT satisfy it.

    Behavioural: locate node.exe, then make it EVALUATE an expression. A path
    string proves the file is on PATH, not that it runs."""
    ps = ps_shim()
    if not ps.exists():
        return "FAIL", "no powershell shim; cannot probe Windows-side node"
    script = ('$s=(Get-Command node.exe -EA SilentlyContinue).Source; '
              'if(-not $s){ exit 4 }; '
              'Write-Output ("PATH=" + $s); '
              '& $s -e "process.stdout.write(\'EVAL=\'+String(6*7)+\' \'+process.version)"')
    try:
        r = sh([str(ps), "-NoProfile", "-Command", script], timeout=90, cwd=win_cwd())
    except subprocess.TimeoutExpired:
        return "FAIL", "Windows-side node probe timed out after 90s"
    out = r.stdout or ""
    if "PATH=" not in out:
        return "FAIL", ("Windows-side Node NOT FOUND. Install it on Windows "
                        "(winget install OpenJS.NodeJS.LTS) — WSL's node does NOT "
                        "satisfy this; the chrome-devtools MCP spawns npx on the "
                        "Windows side.")
    # Locating and evaluating happen in one PowerShell round trip; hand the
    # located path to the shared cache so the MCP check need not re-probe.
    src = remember_windows_node(ps, out)
    if "EVAL=42 " not in out:
        return "FAIL", (f"node.exe found at {src} but did not execute "
                        f"(rc={r.returncode}) — it is on PATH yet not runnable: "
                        f"{(r.stderr or '').strip()[:120]}")
    ver = out.split("EVAL=42 ", 1)[1].split()[0]
    return "PASS", f"Windows node {ver} evaluated an expression ({src})"


@check("windows-chrome", wsl_only=True)
def _win_chrome():
    """Behavioural-ish: locate chrome.exe, then read the version resource out of
    the binary itself. Launching Chrome would mutate the user's session, so
    reading its VersionInfo is the strongest non-invasive proof it is a real
    executable rather than a stale PATH entry."""
    ps = ps_shim()
    if not ps.exists():
        return "FAIL", "no powershell shim; cannot probe Windows-side chrome"
    script = (
        '$c=(Get-Command chrome.exe -EA SilentlyContinue).Source; '
        'if(-not $c){ foreach($p in @('
        '"$env:ProgramFiles\\Google\\Chrome\\Application\\chrome.exe",'
        '"${env:ProgramFiles(x86)}\\Google\\Chrome\\Application\\chrome.exe",'
        '"$env:LOCALAPPDATA\\Google\\Chrome\\Application\\chrome.exe"'
        ')){ if(Test-Path $p){ $c=$p; break } } }; '
        'if(-not $c){ exit 4 }; '
        'Write-Output ("CHROME={0}|{1}" -f $c,(Get-Item $c).VersionInfo.ProductVersion)')
    try:
        r = sh([str(ps), "-NoProfile", "-Command", script], timeout=90, cwd=win_cwd())
    except subprocess.TimeoutExpired:
        return "FAIL", "Windows-side chrome probe timed out after 90s"
    out = r.stdout or ""
    if "CHROME=" not in out:
        return "FAIL", ("Chrome not found on the Windows side "
                        "(checked PATH, Program Files, Program Files (x86), per-user)")
    payload = out.split("CHROME=", 1)[1].splitlines()[0].strip()
    path, _, ver = payload.partition("|")
    if not ver:
        return "FAIL", f"chrome.exe at {path} has no version resource — not a real binary"
    return "PASS", f"chrome {ver} at {path}"


@check("windows-chrome-shim", wsl_only=True)
def _win_chrome_shim():
    """Behavioural: make ~/.local/bin/windows-chrome actually LAUNCH something,
    and require the URL to arrive intact.

    This is a different assertion from `windows-chrome`, which reads the Chrome
    binary's VersionInfo. That proves the BROWSER exists; it says nothing about
    the shim that is supposed to drive it. The gap is not hypothetical: the shim
    shipped in a state where `powershell.exe` refused to start at all (the AV on
    this fleet blocks `FromBase64String` + `Start-Process` on one command line as
    a fileless-PowerShell signature), and because every check only ever asked
    "does Chrome exist?", that answered yes throughout.

    Non-invasive, and NOT profile-gated -- the second machine is exactly where a
    dead BROWSER hook goes unnoticed. Instead of opening a browser,
    WINDOWS_CHROME_PATH is pointed at a recorder .cmd in the Windows temp
    directory that appends its arguments to a file. The shim launches the
    recorder, the nonce is read back, both files are removed. ~1s, no window.

    A mock cannot replace this. The failure it exists to catch happens at
    CreateProcess time, before PowerShell runs, so anything that pattern-matches
    the command text instead of executing it will report success."""
    shim = Path.home() / ".local" / "bin" / "windows-chrome"
    if not shim.exists():
        return "FAIL", f"missing shim {shim} — Tier 0 must provision it"
    ps = ps_shim()
    if not ps.exists():
        return "FAIL", f"no powershell shim at {ps}; cannot stage a recorder"

    try:
        r = sh([str(ps), "-NoProfile", "-Command",
                "Write-Output ([IO.Path]::GetTempPath())"],
               timeout=60, cwd=win_cwd())
    except subprocess.TimeoutExpired:
        return "FAIL", "could not read the Windows temp path within 60s"
    win_tmp = (r.stdout or "").replace("\r", "").strip().splitlines()
    if r.returncode != 0 or not win_tmp or not win_tmp[0].strip():
        return "FAIL", ("could not locate the Windows temp directory "
                        f"(rc={r.returncode}) — interop is broken; see windows-interop")
    try:
        u = sh(["wslpath", "-u", win_tmp[0].strip()], timeout=15)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "SKIP", "wslpath unavailable; cannot stage a Windows-visible recorder"
    tmp_dir = Path((u.stdout or "").strip())
    if not tmp_dir.is_dir():
        return "SKIP", f"Windows temp {tmp_dir} not visible from WSL"

    token = "khenrix-doctor-" + secrets.token_hex(8)
    rec, log = tmp_dir / f"{token}.cmd", tmp_dir / f"{token}.txt"
    nonce_url = f"https://khenrix-doctor.invalid/{token}"
    # Delayed expansion keeps cmd.exe from re-parsing the recorded value. The
    # nonce carries no '&' on purpose: `cmd /c` splits its own command line on
    # it, which is a property of batch recorders, not of the shim.
    rec.write_text("@echo off\r\n"
                   "setlocal EnableDelayedExpansion\r\n"
                   'set "ARGS=%*"\r\n'
                   f'>>"%~dp0{token}.txt" echo RAW=[!ARGS!]\r\n')
    try:
        try:
            w = sh(["wslpath", "-w", str(rec)], timeout=15)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return "SKIP", "wslpath unavailable; cannot address the recorder"
        win_rec = (w.stdout or "").strip()
        if not win_rec:
            return "FAIL", f"wslpath could not translate {rec} for Windows"

        env = dict(os.environ, WINDOWS_CHROME_PATH=win_rec)
        try:
            got = sh([str(shim), nonce_url], timeout=90, env=env)
        except subprocess.TimeoutExpired:
            return "FAIL", "windows-chrome did not return within 90s"
        if got.returncode != 0:
            return "FAIL", (f"windows-chrome exited {got.returncode} launching a "
                            f"recorder: {(got.stderr or '').strip()[:200]} — the shim "
                            "cannot launch anything; Chrome existing is irrelevant")

        # Start-Process is asynchronous (correct for a browser), so the recorder
        # lands shortly after the shim returns.
        recorded = ""
        for _ in range(40):
            if log.exists() and log.stat().st_size > 0:
                recorded = log.read_text(errors="replace")
                break
            time.sleep(0.25)
    finally:
        for f in (rec, log):
            try:
                f.unlink()
            except OSError:
                pass

    if not recorded.strip():
        return "FAIL", ("windows-chrome exited 0 but the recorder was never "
                        "invoked — nothing was launched (exit 0 alone proves "
                        "nothing; that is how a dead shim survived)")
    if nonce_url not in recorded:
        return "FAIL", ("the recorder ran but the URL did not arrive intact: "
                        f"{recorded.strip()[:160]!r} (expected {nonce_url})")
    return "PASS", f"windows-chrome launched a recorder and delivered {nonce_url} intact"


# --- Clipboard ------------------------------------------------------------

@check("clipboard-image-roundtrip", wsl_only=True, invasive=True)
def _clipboard():
    """Behavioural: put a randomly sized, randomly coloured PNG on the Windows
    clipboard, read it back, and require the SAME image — dimensions and first
    pixel. Randomising defeats the stale-clipboard false pass, where the
    clipboard still holds an older image and a naive "did we get an image?"
    check goes green while the write path is dead.

    DESTRUCTIVE: this overwrites whatever the user had copied. That is why the
    check is tagged `invasive` and why --skip-invasive exists. It runs by
    default under EVERY profile: image paste is one of the capabilities that
    silently died on the second machine, so the profile people run there must
    exercise it. Opting out is a deliberate act, not a side effect.
    """
    ps = ps_shim()
    if not ps.exists():
        return "SKIP", "no interop shim; clipboard round trip needs Windows"
    w, h = random.randint(8, 64), random.randint(8, 64)
    rgb = tuple(random.randint(0, 255) for _ in range(3))
    src = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(solid_png(w, h, rgb))
            src = f.name
        try:
            win = sh(["wslpath", "-w", src], timeout=15).stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return "SKIP", "wslpath unavailable; cannot hand a path to Windows"
        if not win:
            return "FAIL", f"wslpath could not translate {src} for Windows"

        set_script = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
                      f"$i=[System.Drawing.Image]::FromFile('{win}'); "
                      "[System.Windows.Forms.Clipboard]::SetImage($i); $i.Dispose()")
        get_script = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
                      "$i=[System.Windows.Forms.Clipboard]::GetImage(); "
                      "if($null -eq $i){ exit 3 }; "
                      "$b=[System.Drawing.Bitmap]$i; $p=$b.GetPixel(0,0); "
                      "Write-Output ('{0} {1} {2} {3} {4}' -f "
                      "$b.Width,$b.Height,$p.R,$p.G,$p.B); "
                      "$ms=New-Object IO.MemoryStream; "
                      "$i.Save($ms,[System.Drawing.Imaging.ImageFormat]::Png); "
                      "Write-Output ([Convert]::ToBase64String($ms.ToArray()))")
        try:
            set_r = sh([str(ps), "-NoProfile", "-Sta", "-Command", set_script],
                       timeout=90, cwd=win_cwd())
            if set_r.returncode != 0:
                return "FAIL", (f"could not put an image on the clipboard "
                                f"(rc={set_r.returncode}): {(set_r.stderr or '').strip()[:150]}")
            get_r = sh([str(ps), "-NoProfile", "-Sta", "-Command", get_script],
                       timeout=90, cwd=win_cwd())
        except subprocess.TimeoutExpired:
            return "FAIL", "clipboard round trip timed out after 90s"
    finally:
        if src:
            try:
                os.unlink(src)
            except OSError:
                pass

    if get_r.returncode == 3:
        return "FAIL", ("clipboard held no image after SetImage — the Windows "
                        "interop clipboard is broken; image paste will not work")
    if get_r.returncode != 0:
        return "FAIL", (f"clipboard read failed rc={get_r.returncode}: "
                        f"{(get_r.stderr or '').strip()[:150]}")

    lines = [l for l in (get_r.stdout or "").splitlines() if l.strip()]
    if len(lines) < 2:
        return "FAIL", f"malformed clipboard read-back: {get_r.stdout.strip()[:120]!r}"
    try:
        got = tuple(int(x) for x in lines[0].split())
    except ValueError:
        return "FAIL", f"clipboard read-back header not numeric: {lines[0][:80]!r}"
    want = (w, h) + rgb
    if got != want:
        return "FAIL", (f"clipboard returned an image, but not the image we set "
                        f"(mismatch: got {got}, expected {want}) — the write half "
                        f"of the round trip is dead and a stale image was read back")
    try:
        blob = base64.b64decode("".join(lines[1:]))
    except Exception as e:
        return "FAIL", f"clipboard payload not base64: {e}"
    if png_header(blob) is None:
        return "FAIL", (f"clipboard returned non-PNG bytes ({blob[:8]!r}) — "
                        f"likely a BMP shim intercept")
    return "PASS", f"{w}x{h} RGB{rgb} image round-tripped as PNG ({len(blob)} bytes)"


@check("clipboard-no-shim-intercept", wsl_only=True)
def _no_shim():
    """Claude Code dispatches wl-paste -> xclip -> powershell. A hand-written
    script named wl-paste/xclip sits at the FRONT of that || chain and preempts
    the maintained upstream path — and, being a script, it reports success from
    `command -v` whether or not the real package is installed.

    PATH resolution is used only to find which file would actually be executed;
    the assertion is on the file's contents (a `#!` script is a shim, an ELF
    binary is the real tool)."""
    bad = []
    for name in ("wl-paste", "wl-copy", "xclip", "xsel"):
        p = shutil.which(name)
        if not p:
            continue
        f = Path(p)
        try:
            head = f.read_bytes()[:2]
        except OSError:
            continue
        if head == b"#!":
            bad.append(f"{name} -> {p}")
    if bad:
        return "FAIL", ("script shim(s) intercepting the clipboard dispatch chain: "
                        + "; ".join(bad)
                        + " — these preempt the maintained path and mask a missing "
                          "package; remove them and install the real tool (Task 3)")
    return "PASS", "no script shims on the clipboard dispatch path"


# --- MCP ------------------------------------------------------------------

def mcp_tools_over_stdio(cmd, timeout, cwd=None, env=None):
    """Speak MCP over stdio; return (tools, error) — exactly one is None.

    Note the trap this avoids: the `initialize` reply advertises
    `"capabilities":{"tools":{"listChanged":true}}`, so a substring search for
    '"tools"' passes even when tools/list never answered. The response is parsed
    and matched on the request id.

    Shared by every MCP check on purpose. Two copies of this parser would mean
    the trap could be closed in one and left open in the other.

    `notifications/initialized` is REQUIRED, not ceremony. The spec says the
    client sends it after `initialize`, and a strict server rejects everything
    until it arrives: the 1Password MCP answers tools/list with
    `ExpectedInitializedNotification` and nothing else, which reads exactly like
    a dead server. chrome-devtools happens to be lenient, so omitting it looked
    correct for as long as chrome-devtools was the only server probed."""
    payload = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "khenrix-doctor", "version": "1"}}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
    ]) + "\n"
    try:
        r = sh(cmd, input=payload, timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        return None, f"did not respond within {timeout}s"
    except OSError as e:
        return None, f"could not be launched ({type(e).__name__}: {e})"

    tools = None
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == 2 and isinstance(msg.get("result"), dict):
            tools = msg["result"].get("tools")
    if tools is None:
        return None, ("no tools/list response. "
                      f"stderr: {(r.stderr or '').strip()[:200]}")
    if not isinstance(tools, list) or not tools:
        return None, "answered tools/list with an empty tool set"
    return tools, None


@check("mcp-chrome-devtools", wsl_only=True, network=True)
def _mcp_chrome():
    """Behavioural: speak MCP over stdio and require a non-empty tools LIST.

    There is deliberately NO environment override for the command. A hook that
    let $SOMETHING replace the MCP invocation would make this check trivially
    passable, which is a hole in the one tool whose entire job is honest
    verification. The tests drive it through a fake powershell.exe instead."""
    ps = ps_shim()
    if not ps.exists():
        return "SKIP", "no interop shim; the MCP runs on the Windows side"
    src, err = windows_node_source(ps)
    if src is None:
        return "FAIL", (f"{err}, so npx cannot run the MCP "
                        "(see the windows-node check)")
    node_dir = src.rsplit("\\", 1)[0]
    cmd = [str(ps), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
           f'& "{node_dir}\\npx.cmd" -y chrome-devtools-mcp@latest']

    tools, err = mcp_tools_over_stdio(cmd, timeout=180, cwd=win_cwd())
    if err:
        return "FAIL", f"chrome-devtools MCP {err}"
    return "PASS", f"chrome-devtools MCP returned {len(tools)} tools over stdio"


# --- GPU ------------------------------------------------------------------

@check("cuda-stub-not-shadowed", hardware=True, wsl_only=True)
def _cuda():
    """WSL ships a libcuda stub in /usr/lib/wsl/lib that forwards to the Windows
    driver. Installing the `cuda` or `cuda-drivers` metapackage drops a Linux
    driver into /usr/lib/x86_64-linux-gnu that shadows it and breaks GPU access.
    Install `cuda-toolkit-N-N` instead."""
    if not Path("/dev/dxg").exists():
        return "SKIP", "no /dev/dxg — no WSL GPU passthrough on this machine"
    try:
        r = sh(["ldconfig", "-p"], timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "SKIP", "ldconfig unavailable"
    lines = [l for l in (r.stdout or "").splitlines() if "libcuda.so" in l]
    if not lines:
        return "SKIP", "libcuda not registered with ldconfig (CUDA not installed yet)"
    shadowed = [l.strip() for l in lines if "/usr/lib/x86_64-linux-gnu/libcuda" in l]
    if shadowed:
        return "FAIL", ("a Linux NVIDIA driver shadows the WSL stub "
                        f"({shadowed[0]}) — you installed cuda/cuda-drivers instead "
                        "of cuda-toolkit-N-N. Remove them.")
    if not any("/usr/lib/wsl/lib" in l for l in lines):
        return "FAIL", f"libcuda does not resolve into /usr/lib/wsl/lib: {lines[:2]}"
    return "PASS", "libcuda resolves to /usr/lib/wsl/lib (WSL stub intact)"


# --- Toolchain ------------------------------------------------------------

# A manager is ACTIVE when its shim/bin directory participates in PATH
# resolution — that is what makes two managers fight over `node`. An install
# directory sitting on disk is NOT activity: this machine has a full ~/.nvm
# (three node versions) that no rc file ever sources, so it resolves nothing.
# Failing on its mere presence would be the same presence-vs-behaviour mistake
# this whole script exists to prevent.
MANAGER_PATH_MARKERS = {
    "asdf": ("/.asdf/shims", "/.asdf/bin"),
    "mise": ("/mise/shims", "/.mise/shims"),
    "nvm": ("/.nvm/versions/",),
}
MANAGER_INSTALL_DIRS = {"asdf": ".asdf", "mise": ".local/share/mise", "nvm": ".nvm"}


@check("no-dual-version-managers")
def _dual():
    """Two version managers on PATH fight over shims and make `node --version`
    depend on shell startup order.

    Behavioural: resolution is decided by PATH, so PATH is what gets inspected —
    not `which asdf`, and not whether an install directory exists on disk."""
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e]
    active, dormant = [], []
    for name, markers in MANAGER_PATH_MARKERS.items():
        on_path = any(m in e for e in entries for m in markers)
        if on_path:
            label = name
            p = shutil.which(name)
            if p:
                try:
                    r = sh([p, "--version"], timeout=20)
                    if r.returncode == 0 and (r.stdout or r.stderr).strip():
                        v = (r.stdout or r.stderr).strip().splitlines()[0][:24]
                        label = f"{name} ({v})"
                except (subprocess.TimeoutExpired, OSError):
                    pass
            active.append(label)
        elif (Path.home() / MANAGER_INSTALL_DIRS[name]).exists():
            dormant.append(name)
    note = f"; installed but not on PATH, so inert: {', '.join(dormant)}" if dormant else ""
    if len(active) > 1:
        return "FAIL", ("multiple version managers own PATH: " + ", ".join(active)
                        + " — they fight over shims; resolve before trusting any "
                          "tool version" + note)
    return "PASS", f"version managers on PATH: {active[0] if active else 'none'}{note}"


# --- Secrets --------------------------------------------------------------
#
# A shell rc file is the worst place a credential can sit: world-readable to
# every process the user runs, copied wholesale into backups and tarballs, and
# pasted into agent transcripts. Three live secrets sat in ~/.bashrc on this
# machine indefinitely — a Supabase secret key, a database password and a Google
# Places API key — as literal `export VAR="value"` lines. Nothing ever looked; a
# grep found them by accident during unrelated work.
#
# DESIGN RULE — A LOW FALSE-POSITIVE RATE BEATS EXHAUSTIVE DETECTION.
#     A check that cries wolf trains people to ignore it, which is strictly
#     worse than no check. So there are exactly two high-confidence rules:
#       1. the VALUE carries a known credential SHAPE (sb_secret_, AIza, ...)
#       2. the NAME says credential AND the value is a plausible literal
#     Everything house-style.md actually prescribes — ${VAR}, $(op read ...),
#     op://Private/x/y, a path, an empty value — is the CORRECT pattern and
#     must PASS. Rule 2 in particular is gated hard: TOKENIZERS_PARALLELISM
#     matches /TOKEN/ and is not a secret.
#
# SECOND DESIGN RULE — NEVER PRINT A MATCHED VALUE.
#     Reporting a secret to prove a secret is exposed spreads it further: into
#     terminal scrollback, CI logs, `doctor --json` output and the next agent
#     transcript. The detail line carries the file, the line number and the
#     variable NAME. That is enough to act on and harmless to paste.
#
# Not `hardware` (so it runs under --profile portable — nothing here is
# host-specific) and not `invasive` (it only reads).

RC_FILES = (".bashrc", ".bash_profile", ".profile", ".zshrc", ".bash_aliases")

# Each shape is a literal prefix plus ENOUGH trailing credential-shaped
# characters that ordinary prose and paths cannot reach it: "/opt/sk-tools"
# carries `sk-` but only five trailing characters, so it does not match.
SECRET_SHAPES = (
    ("Supabase secret key", re.compile(r"sb_secret_[A-Za-z0-9_\-]{16,}")),
    ("Google API key", re.compile(r"AIza[A-Za-z0-9_\-]{30,}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("GitHub token", re.compile(r"ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("OpenAI-style key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("AWS access key id", re.compile(r"AKIA[A-Z0-9]{16}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

SECRETISH_NAME = re.compile(r"SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|CREDENTIAL")
# A _FILE/_PATH/_DIR variable names WHERE the credential lives, never the
# credential. Excluding the suffix keeps the common correct pattern quiet.
LOCATION_NAME = re.compile(r"_(FILE|PATH|DIR|DIRECTORY|URL)$")

EXPORT_RE = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
SOURCE_RE = re.compile(r"^\s*(?:source|\.)\s+(\S+)")

# Values that match a secretish NAME but are plainly configuration, not
# credentials. TOKENIZERS_PARALLELISM=false is the canonical example.
NON_SECRET_LITERALS = frozenset(
    {"true", "false", "yes", "no", "on", "off", "none", "null", "nil",
     "unset", "default", "auto", "disabled", "enabled"})


def rc_lines(path):
    """Text lines of an rc file, or [] if it cannot be read. Read-only."""
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def rc_value(raw):
    """The literal value from the right-hand side of an `export NAME=<raw>`.

    Quoted values win over the rest of the line so a trailing `# comment` is
    not mistaken for part of the value."""
    raw = raw.strip()
    if not raw:
        return ""
    m = re.match(r'"((?:[^"\\]|\\.)*)"', raw)
    if m:
        return m.group(1)
    m = re.match(r"'([^']*)'", raw)
    if m:
        return m.group(1)
    return raw.split()[0].split("#", 1)[0]


def is_reference(value):
    """True when the value POINTS AT a secret instead of being one.

    `${BAR}`, `$(op read ...)`, `op://Private/x/y`, a path and an empty string
    are all the pattern house-style.md prescribes. Flagging them would make the
    check fire on correctly-migrated rc files, i.e. permanently."""
    v = value.strip()
    if not v:
        return True
    if "op://" in v:
        return True
    if "$" in v or "`" in v:
        return True
    if v.startswith(("/", "~")):
        return True
    return False


def plausible_literal_secret(value):
    """Rule-2 gate: could this literal plausibly BE a credential?

    Rule 1 (shape) is self-evidencing; rule 2 has only the variable's name to go
    on, so the value must clear a bar. Credentials are long, are not booleans or
    version numbers, and contain no whitespace."""
    v = value.strip()
    if len(v) < 8:
        return False
    if v.lower() in NON_SECRET_LITERALS:
        return False
    if any(ch.isspace() for ch in v):
        return False
    if not any(ch.isalnum() for ch in v):
        return False
    if v.replace(".", "").replace("-", "").replace("_", "").isdigit():
        return False
    return True


def rc_targets(home):
    """rc files to scan: the usual five, plus whatever they `source`, ONE level
    deep. Deeper nesting is rare and every level multiplies the chance of
    resolving the wrong file. An unresolvable expansion (`. "$ASDF_DIR/asdf.sh"`)
    is skipped rather than guessed at — scanning the wrong file is worse than
    not scanning."""
    seen, targets = set(), []

    def add(p):
        try:
            rp = p.resolve()
        except OSError:
            return
        if rp in seen or not rp.is_file():
            return
        seen.add(rp)
        targets.append(p)

    for name in RC_FILES:
        add(home / name)
    for p in list(targets):          # snapshot: sourced files are not re-scanned
        for line in rc_lines(p):
            m = SOURCE_RE.match(line)
            if not m:
                continue
            raw = m.group(1).strip().strip("\"'")
            if raw.startswith("~/"):
                raw = str(home) + raw[1:]
            elif raw.startswith("$HOME/"):
                raw = str(home) + raw[5:]
            if "$" in raw:
                continue
            cand = Path(raw)
            add(cand if cand.is_absolute() else home / raw)
    return targets


def scan_rc_file(path, home=None):
    """[(display, lineno, varname, why)] for one rc file. NEVER the value."""
    home = home or Path.home()
    try:
        display = "~/" + str(path.relative_to(home))
    except ValueError:
        display = str(path)
    found = []
    for n, line in enumerate(rc_lines(path), 1):
        m = EXPORT_RE.match(line)
        if not m:
            continue
        name, value = m.group(1), rc_value(m.group(2))
        shape = next((label for label, rx in SECRET_SHAPES if rx.search(value)), None)
        if shape:
            found.append((display, n, name, shape))
            continue
        if is_reference(value):
            continue
        if (SECRETISH_NAME.search(name) and not LOCATION_NAME.search(name)
                and plausible_literal_secret(value)):
            found.append((display, n, name, "credential-named variable holding a literal"))
    return found


@check("no-plaintext-secrets-in-shell-rc")
def _rc_secrets():
    home = Path.home()
    targets = rc_targets(home)
    if not targets:
        return "SKIP", "no shell rc files found under $HOME"
    found = [f for p in targets for f in scan_rc_file(p, home)]
    if not found:
        return "PASS", (f"no exported literal secrets in {len(targets)} shell rc "
                        f"file(s); references (${{VAR}}, op://, $(...)) are the "
                        f"correct pattern and pass")
    where = "; ".join(f"{d}:{n} {name} [{why}]" for d, n, name, why in found)
    return "FAIL", (
        f"{len(found)} exported literal secret(s) in shell rc files: {where} "
        "— VALUES WITHHELD DELIBERATELY (printing one to prove it is exposed "
        "spreads it into scrollback, logs and transcripts). A shell rc is "
        "world-readable to every process you run and is copied into backups, "
        "tarballs and agent transcripts. Move each into 1Password and export a "
        'REFERENCE instead: export NAME="op://Private/<item>/credential", then '
        "run the consumer under `op run -- <cmd>`; see the secrets section of "
        "house-style.md. scripts/migrate-secrets-to-1password.sh performs the "
        "move transactionally. Rotate anything that was exposed — it has been "
        "readable for as long as it has been on disk.")


# --- 1Password ------------------------------------------------------------
#
# THE QUESTION THIS ASKS is "can this machine resolve a 1Password reference at
# all, and by WHICH path?" — deliberately not "is the CLI authenticated?".
# Driving 1Password through the MCP alone is a legitimate configuration, and a
# check that failed it would be crying wolf at a working machine.
#
# THE WINDOWS/WSL BOUNDARY — the trap that cost real time here.
#     The desktop app's "Integrate with 1Password CLI" exposes its auth socket
#     to WINDOWS processes only. `op` installed inside WSL is a LINUX binary and
#     cannot reach it, so it reports "No accounts configured for use with
#     1Password CLI" with desktop integration fully enabled and healthy. That
#     reads like a broken setup; it is a boundary. Whenever the CLI is unusable
#     AND is a Linux binary under WSL, the detail says so outright — a FAIL that
#     sends the reader back to re-toggle a setting that was never the problem is
#     worse than no FAIL at all.
#
# THE MCP IS A SEPARATE PATH, NOT A SUBSTITUTE. `op run --` and `op read` are
# CLI features; the MCP manages Developer Environments. A machine whose MCP
# works and whose CLI has no account still cannot resolve an op:// reference,
# so the MCP-only PASS says so rather than implying everything is fine.
#
# Same defect class as the Windows-side-Node prerequisite: an undocumented
# requirement that fails silently and confusingly on a second machine.
#
# Not `hardware` — nothing here is host-specific, and `portable` is the profile
# reached for on the second machine. Not `invasive` — it reads config and, at
# most, starts an MCP that exists to be started. Not `network` — `op account
# list` reads on-disk config and the MCP is a locally installed executable.

# Where each CLI keeps its MCP registry. Top-level servers only: a per-project
# override is not the machine's 1Password capability.
OP_MCP_CONFIGS = (
    ("claude", ".claude.json", "json", "mcpServers"),
    ("codex", ".codex/config.toml", "toml", "mcp_servers"),
    ("agy", ".gemini/config/mcp_config.json", "json", "mcpServers"),
)

OP_MCP_NAME = re.compile(r"1password|onepassword", re.I)

# Tight on purpose: the MCP is a local executable, so a slow answer is a broken
# answer, and a doctor that hangs is worse than one that fails.
OP_MCP_TIMEOUT = 30
OP_CLI_TIMEOUT = 20

WSL_BOUNDARY = (
    "WSL BOUNDARY, NOT A BROKEN SETUP: this `op` is a Linux binary, and the "
    "1Password desktop app's \"Integrate with 1Password CLI\" exposes its auth "
    "socket to WINDOWS processes only — desktop integration can be fully "
    "enabled and this `op` still sees no account. Give WSL's own op its own "
    "auth: `op account add` (prompts for the master password; works in a Linux "
    "shell) or a service account via OP_SERVICE_ACCOUNT_TOKEN."
)


def expand_config_value(value):
    """`${HOME}`/`~` in a config command survive into the live registry on some
    CLIs and are pre-expanded on others; the doctor must launch either."""
    return os.path.expandvars(os.path.expanduser(str(value)))


def onepassword_mcp_entries(home):
    """[(labels, argv, env)] — every DISTINCT configured 1Password MCP command.

    Deduplicated by argv: all three CLIs point at the same executable on this
    machine, and launching it three times proves nothing the first launch did
    not while tripling the worst-case runtime."""
    found = {}
    for cli, rel, fmt, key in OP_MCP_CONFIGS:
        path = home / rel
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if fmt == "toml" and tomllib is None:
            continue
        try:
            data = tomllib.loads(text) if fmt == "toml" else json.loads(text)
        except (ValueError, TypeError):
            continue
        servers = data.get(key) if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            continue
        for name, spec in servers.items():
            if not OP_MCP_NAME.search(str(name)) or not isinstance(spec, dict):
                continue
            cmd = spec.get("command")
            if not cmd:
                continue
            argv = tuple([expand_config_value(cmd)]
                         + [expand_config_value(a) for a in (spec.get("args") or [])])
            entry = found.setdefault(argv, {"labels": [], "env": {}})
            entry["labels"].append(f"{cli}:{name}")
            for k, v in (spec.get("env") or {}).items():
                entry["env"][str(k)] = expand_config_value(v)
    return [(v["labels"], list(argv), v["env"]) for argv, v in found.items()]


def is_elf(path):
    """True when `path` is a Linux ELF executable. READ, not inferred from the
    name: an op.exe reached through interop is a PE, and which one is on PATH is
    the entire substance of the boundary note."""
    try:
        return Path(path).read_bytes()[:4] == b"\x7fELF"
    except OSError:
        return False


def op_cli_status(op, wsl=None, timeout=OP_CLI_TIMEOUT):
    """(usable, detail) for the `op` CLI.

    USABLE MEANS AUTHENTICATED. `op` on PATH is exactly the presence check this
    file exists to refuse: an unauthenticated `op` resolves no reference at all,
    and certifying it would repeat the xclip-shim mistake in a new place."""
    if wsl is None:
        wsl = is_wsl()
    if not op:
        return False, "`op` CLI: not installed (not on PATH)"
    try:
        r = sh([op, "account", "list", "--format=json"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"`op` CLI at {op}: `op account list` did not return within {timeout}s"
    except OSError as e:
        return False, f"`op` CLI at {op}: could not be executed ({type(e).__name__})"
    try:
        accounts = json.loads((r.stdout or "").strip() or "[]")
    except ValueError:
        accounts = None
    if r.returncode == 0 and isinstance(accounts, list) and accounts:
        # The COUNT only. Account emails and sign-in URLs are the user's, and
        # this detail is printed, JSON-dumped, logged and pasted into agent
        # transcripts — same rule as the rc-secrets check.
        return True, f"`op` CLI at {op} is authenticated ({len(accounts)} account(s))"
    if os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        # A service account authenticates by token, so `op account list` stays
        # EMPTY while the CLI works perfectly. Probed only when the variable is
        # set — its value is never read, and `op whoami` cannot prompt, so this
        # never turns the doctor into an interactive auth attempt.
        try:
            w = sh([op, "whoami", "--format=json"], timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            w = None
        if w is not None and w.returncode == 0 and (w.stdout or "").strip():
            return True, f"`op` CLI at {op} is authenticated via OP_SERVICE_ACCOUNT_TOKEN"
        return False, (f"`op` CLI at {op}: OP_SERVICE_ACCOUNT_TOKEN is set but "
                       "`op whoami` did not accept it")
    detail = (f"`op` CLI at {op}: installed but NO account is configured, so it "
              "cannot resolve an op:// reference — present is not usable")
    if wsl and is_elf(op):
        detail += ". " + WSL_BOUNDARY
    return False, detail


@check("onepassword-usable")
def _onepassword():
    home = Path.home()
    op = shutil.which("op")
    entries = onepassword_mcp_entries(home)
    if not op and not entries:
        return "SKIP", ("no `op` CLI on PATH and no 1Password MCP configured in "
                        "any CLI — 1Password is optional and this machine does "
                        "not use it")

    cli_ok, cli_detail = op_cli_status(op)

    mcp_ok, mcp_detail = False, "1Password MCP: not configured in any CLI"
    failures = []
    for labels, argv, env in entries:
        label = ", ".join(labels)
        # Only a Windows-side launcher wants /mnt/c; relocating a pure-Linux
        # subprocess would be an unexplained side effect (see win_cwd()).
        cwd = win_cwd() if "powershell.exe" in argv[0] else None
        tools, err = mcp_tools_over_stdio(
            argv, timeout=OP_MCP_TIMEOUT, cwd=cwd,
            env=dict(os.environ, **env) if env else None)
        if err:
            failures.append(f"1Password MCP ({label}) {err}")
            continue
        mcp_ok = True
        mcp_detail = (f"1Password MCP ({label}) launched and answered tools/list "
                      f"with {len(tools)} tools")
        break
    if entries and not mcp_ok:
        mcp_detail = "; ".join(failures)

    if cli_ok and mcp_ok:
        return "PASS", ("op:// resolvable via the `op` CLI, and the MCP answers too "
                        f"— {cli_detail}; {mcp_detail}")
    if cli_ok:
        return "PASS", f"op:// resolvable via the `op` CLI — {cli_detail} ({mcp_detail})"
    if mcp_ok:
        return "PASS", (f"1Password reachable via the MCP only — {mcp_detail}. NOTE: "
                        "`op run --` / `op read` are CLI features the MCP does not "
                        "provide, so op:// references cannot be resolved here until "
                        f"the CLI has its own auth. {cli_detail}")
    return "FAIL", ("1Password is configured here but NO path to it works. "
                    f"Tried: {cli_detail} | {mcp_detail}")


# --- runner ---------------------------------------------------------------

def run_checks(profile="full", only=None, wsl=None, skip_invasive=False,
               skip_network=False):
    """Return (results, failed_count). `only` restricts to named checks.

    The gates are independent by design: `profile` answers "does this check
    APPLY to this machine?", while skip_invasive/skip_network answer "do I want
    to pay this check's cost right now?". Only `hardware` is profile-gated —
    see the module docstring for why `invasive` and `network` must not be."""
    if wsl is None:
        wsl = is_wsl()
    selected = [c for c in CHECKS if only is None or c["name"] in only]
    results, failed = [], 0
    for c in selected:
        if c["wsl_only"] and not wsl:
            status, detail = "SKIP", "not running under WSL"
        elif profile == "portable" and c["hardware"]:
            status, detail = "SKIP", "hardware-specific check skipped in portable profile"
        elif skip_invasive and c["invasive"]:
            status, detail = "SKIP", "invasive check skipped by --skip-invasive"
        elif skip_network and c["network"]:
            status, detail = "SKIP", "network check skipped by --skip-network"
        else:
            try:
                status, detail = c["fn"]()
            except Exception as e:
                status, detail = "FAIL", f"check raised {type(e).__name__}: {e}"
        if status == "FAIL":
            failed += 1
        results.append({"name": c["name"], "status": status, "detail": detail,
                        "hardware": c["hardware"], "network": c["network"],
                        "invasive": c["invasive"]})
    return results, failed


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Behavioural capability checks (round trips, not presence).",
        epilog="--profile controls APPLICABILITY (does this check make sense on this "
               "machine?). --skip-invasive/--skip-network control COST (do I want to "
               "pay for it right now?). They are independent: the clipboard and MCP "
               "checks run under every profile, because those are exactly the "
               "capabilities that silently break on a second machine.")
    ap.add_argument("--profile", choices=["full", "portable"], default="full",
                    help="portable skips host/hardware-specific checks that cannot "
                         "meaningfully run elsewhere; it does NOT skip invasive or "
                         "networked ones (default: full)")
    ap.add_argument("--skip-invasive", action="store_true",
                    help="skip checks that mutate user state — currently the clipboard "
                         "round trip, which overwrites whatever you had copied")
    ap.add_argument("--skip-network", action="store_true",
                    help="skip checks that need the network — the chrome-devtools MCP "
                         "check fetches the server from npm")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", help="comma-separated check names to run")
    ap.add_argument("--list", action="store_true", help="list check names and exit")
    ap.add_argument("--scan-rc", metavar="FILE",
                    help="scan ONE rc file and print 'lineno<TAB>NAME<TAB>why' per "
                         "exported literal secret, then exit. Values are never "
                         "printed. This is the seam scripts/migrate-secrets-to-"
                         "1password.sh drives, so the migration and the check can "
                         "never disagree about what counts as a secret")
    args = ap.parse_args(argv)

    if args.scan_rc:
        p = Path(args.scan_rc).expanduser()
        if not p.is_file():
            print(f"not a readable file: {p}", file=sys.stderr)
            return 2
        for _, n, name, why in scan_rc_file(p):
            print(f"{n}\t{name}\t{why}")
        return 0

    if args.list:
        print("tags: hardware -> skipped by --profile portable; "
              "invasive -> skipped by --skip-invasive; "
              "network -> skipped by --skip-network\n")
        for c in CHECKS:
            tags = ",".join(t for t in ("hardware", "network", "invasive", "wsl_only")
                            if c[t]) or "-"
            print(f"{c['name']:<32} {tags}")
        return 0

    only = None
    if args.only:
        only = [n.strip() for n in args.only.split(",") if n.strip()]
        known = {c["name"] for c in CHECKS}
        unknown = [n for n in only if n not in known]
        if unknown:
            print(f"unknown check(s): {', '.join(unknown)}", file=sys.stderr)
            return 2

    results, failed = run_checks(profile=args.profile, only=only,
                                 skip_invasive=args.skip_invasive,
                                 skip_network=args.skip_network)

    if args.json:
        print(json.dumps({"profile": args.profile, "wsl": is_wsl(),
                          "skip_invasive": args.skip_invasive,
                          "skip_network": args.skip_network,
                          "checks": results}, indent=2))
    else:
        for r in results:
            mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[r["status"]]
            print(f"[{mark}] {r['name']:<30} {r['detail']}")
        n = len(results)
        print(f"\n{n} checks, {failed} failed, "
              f"{sum(1 for r in results if r['status'] == 'SKIP')} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
