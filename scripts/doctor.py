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
copied. It is tagged `invasive` and therefore runs only under `--profile full`.

Profiles:
    full      every check
    portable  skips hardware-, network- and clipboard-destroying checks, so the
              same doctor reports meaningfully on a machine where they do not
              apply. Inapplicable checks SKIP; they never FAIL — a tool that
              cries wolf trains people to ignore it.

Every subprocess call goes through sh(), which refuses an unbounded timeout: a
doctor that hangs is worse than one that fails.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

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
    cwd = kw.pop("cwd", None)
    if cwd is None and os.path.isdir("/mnt/c"):
        cwd = "/mnt/c"          # keep the Windows side off a UNC working dir
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          input=input, cwd=cwd, **kw)


def is_wsl():
    p = Path("/proc/version")
    return p.exists() and "microsoft" in p.read_text().lower()


def ps_shim():
    return Path.home() / PS_SHIM


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
        r = sh([str(ps), "-NoProfile", "-Command", f"Write-Output {nonce}"], timeout=60)
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
        r = sh([str(ps), "-NoProfile", "-Command", script], timeout=90)
    except subprocess.TimeoutExpired:
        return "FAIL", "Windows-side node probe timed out after 90s"
    out = r.stdout or ""
    if "PATH=" not in out:
        return "FAIL", ("Windows-side Node NOT FOUND. Install it on Windows "
                        "(winget install OpenJS.NodeJS.LTS) — WSL's node does NOT "
                        "satisfy this; the chrome-devtools MCP spawns npx on the "
                        "Windows side.")
    src = out.split("PATH=", 1)[1].splitlines()[0].strip()
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
        r = sh([str(ps), "-NoProfile", "-Command", script], timeout=90)
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


# --- Clipboard ------------------------------------------------------------

@check("clipboard-image-roundtrip", wsl_only=True, invasive=True)
def _clipboard():
    """Behavioural: put a randomly sized, randomly coloured PNG on the Windows
    clipboard, read it back, and require the SAME image — dimensions and first
    pixel. Randomising defeats the stale-clipboard false pass, where the
    clipboard still holds an older image and a naive "did we get an image?"
    check goes green while the write path is dead.

    DESTRUCTIVE: this overwrites whatever the user had copied. That is why the
    check is tagged `invasive` and runs only under --profile full.
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
            set_r = sh([str(ps), "-NoProfile", "-Sta", "-Command", set_script], timeout=90)
            if set_r.returncode != 0:
                return "FAIL", (f"could not put an image on the clipboard "
                                f"(rc={set_r.returncode}): {(set_r.stderr or '').strip()[:150]}")
            get_r = sh([str(ps), "-NoProfile", "-Sta", "-Command", get_script], timeout=90)
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

@check("mcp-chrome-devtools", wsl_only=True, network=True)
def _mcp_chrome():
    """Behavioural: speak MCP over stdio and require a non-empty tools LIST.

    Note the trap this avoids: the `initialize` reply advertises
    `"capabilities":{"tools":{"listChanged":true}}`, so a substring search for
    '"tools"' passes even when tools/list never answered. The response is parsed
    and matched on the request id."""
    npx_override = os.environ.get("DOCTOR_NPX")
    if npx_override:
        cmd = [npx_override]
    else:
        ps = ps_shim()
        if not ps.exists():
            return "SKIP", "no interop shim; the MCP runs on the Windows side"
        try:
            probe = sh([str(ps), "-NoProfile", "-Command",
                        '$s=(Get-Command node.exe -EA SilentlyContinue).Source; '
                        'if(-not $s){ exit 4 }; Write-Output ("PATH=" + $s)'], timeout=90)
        except subprocess.TimeoutExpired:
            return "FAIL", "could not locate Windows node within 90s"
        if "PATH=" not in (probe.stdout or ""):
            return "FAIL", ("no Windows-side node, so npx cannot run the MCP "
                            "(see the windows-node check)")
        node_dir = probe.stdout.split("PATH=", 1)[1].splitlines()[0].strip().rsplit("\\", 1)[0]
        cmd = [str(ps), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
               f'& "{node_dir}\\npx.cmd" -y chrome-devtools-mcp@latest']

    payload = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "khenrix-doctor", "version": "1"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
    ]) + "\n"
    try:
        r = sh(cmd, input=payload, timeout=180)
    except subprocess.TimeoutExpired:
        return "FAIL", "chrome-devtools MCP did not respond within 180s"

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
        return "FAIL", ("no tools/list response from the chrome-devtools MCP. "
                        f"stderr: {(r.stderr or '').strip()[:200]}")
    if not isinstance(tools, list) or not tools:
        return "FAIL", "chrome-devtools MCP answered tools/list with an empty tool set"
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


# --- runner ---------------------------------------------------------------

def run_checks(profile="full", only=None, wsl=None):
    """Return (results, failed_count). `only` restricts to named checks."""
    if wsl is None:
        wsl = is_wsl()
    selected = [c for c in CHECKS if only is None or c["name"] in only]
    results, failed = [], 0
    for c in selected:
        if c["wsl_only"] and not wsl:
            status, detail = "SKIP", "not running under WSL"
        elif profile == "portable" and c["hardware"]:
            status, detail = "SKIP", "hardware check skipped in portable profile"
        elif profile == "portable" and c["network"]:
            status, detail = "SKIP", "network check skipped in portable profile"
        elif profile == "portable" and c["invasive"]:
            status, detail = "SKIP", ("clipboard-destroying check skipped in portable "
                                      "profile; run --profile full to exercise it")
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
        description="Behavioural capability checks (round trips, not presence).")
    ap.add_argument("--profile", choices=["full", "portable"], default="full",
                    help="portable skips hardware, network and clipboard-destroying checks")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", help="comma-separated check names to run")
    ap.add_argument("--list", action="store_true", help="list check names and exit")
    args = ap.parse_args(argv)

    if args.list:
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

    results, failed = run_checks(profile=args.profile, only=only)

    if args.json:
        print(json.dumps({"profile": args.profile, "wsl": is_wsl(),
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
