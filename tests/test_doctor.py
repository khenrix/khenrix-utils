"""Behavioural tests for scripts/doctor.py.

These tests drive the doctor as a real process against real fixtures on disk
(fake $HOME trees, fake $PATH entries, a fake powershell.exe that emulates the
Windows clipboard). They assert on OBSERVED STATUS, not on source text --
the one source-text assertion below is an explicit lint guard and is labelled
as such.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCTOR = ROOT / "scripts" / "doctor.py"

sys.path.insert(0, str(ROOT / "scripts"))
import doctor  # noqa: E402


# --- helpers --------------------------------------------------------------

def run(*args, env=None, timeout=300):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, str(DOCTOR), *args],
                          capture_output=True, text=True, env=e, timeout=timeout)


def checks_of(r):
    return {c["name"]: c for c in json.loads(r.stdout)["checks"]}


def fake_home(tmp_path, ps_body=None):
    """Build a $HOME whose .local/bin optionally holds a fake powershell.exe."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    if ps_body is not None:
        p = home / ".local" / "bin" / "powershell.exe"
        p.write_text(ps_body)
        p.chmod(0o755)
    return home


def bin_dir(tmp_path, name, body=None, copy_of=None):
    """Build a $PATH entry containing one command: either a script or a real binary."""
    d = tmp_path / f"bin-{name}"
    d.mkdir(exist_ok=True)
    target = d / name
    if copy_of is not None:
        target.write_bytes(pathlib.Path(copy_of).read_bytes())
    else:
        target.write_text(body)
    target.chmod(0o755)
    return d


# A powershell.exe stand-in that satisfies the interop round-trip: it echoes
# back whatever `Write-Output <token>` asks for.
PS_ECHO = """#!/usr/bin/env bash
cmd=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-Command" ]; then cmd="$2"; fi
  shift
done
case "$cmd" in
  "Write-Output "*) echo "${cmd#Write-Output }" ;;
esac
exit 0
"""

PS_BROKEN = """#!/usr/bin/env bash
echo "boom" >&2
exit 1
"""


# --- contract tests from the task brief -----------------------------------

# The whole-suite tests below pass --skip-invasive --skip-network deliberately.
# Those are now the ONLY way to keep the clipboard and the npm-fetching MCP
# check out of a run: the profile no longer does it (see the portable-profile
# tests further down, which is the entire point). Without the flags these tests
# would clobber the developer's clipboard and spend a minute on npx.
FAST = ("--skip-invasive", "--skip-network")


def test_json_output_is_parseable():
    r = run("--json", "--profile", "portable", *FAST)
    json.loads(r.stdout)


def test_every_check_has_a_status():
    data = json.loads(run("--json", "--profile", "portable", *FAST).stdout)
    assert data["checks"], "no checks ran"
    for c in data["checks"]:
        assert c["status"] in ("PASS", "FAIL", "SKIP"), c
        assert c["name"] and c["detail"]


def test_portable_profile_skips_not_fails_hardware_checks():
    data = json.loads(run("--json", "--profile", "portable", *FAST).stdout)
    hw = [c for c in data["checks"] if c.get("hardware")]
    assert hw, "no hardware-tagged checks exist to exercise the portable profile"
    assert all(c["status"] != "FAIL" for c in hw), "hardware check FAILed in portable profile"


def test_no_presence_based_clipboard_check():
    """LINT GUARD (source text, not behaviour) -- kept because the task brief
    specifies it. The real coverage is test_clipboard_* below."""
    src = DOCTOR.read_text()
    assert "command -v xclip" not in src
    assert 'shutil.which("xclip")' not in src, "presence check -- must assert a round-trip"


# --- runner behaviour -----------------------------------------------------

@pytest.fixture
def registry():
    """Let a test add temporary checks without leaking into other tests."""
    saved = list(doctor.CHECKS)
    yield doctor.CHECKS
    doctor.CHECKS[:] = saved


def only(reg, *names):
    return doctor.run_checks(profile="full", only=list(names), wsl=True)


def test_check_that_raises_is_reported_as_FAIL_not_a_crash(registry):
    @doctor.check("boom-test")
    def _boom():
        raise RuntimeError("kaboom")

    results, failed = only(registry, "boom-test")
    assert failed == 1
    assert results[0]["status"] == "FAIL"
    assert "kaboom" in results[0]["detail"]


def test_hardware_check_runs_in_full_and_skips_in_portable(registry):
    @doctor.check("hw-test", hardware=True)
    def _hw():
        return "FAIL", "hardware genuinely broken"

    full, failed = doctor.run_checks(profile="full", only=["hw-test"], wsl=True)
    assert full[0]["status"] == "FAIL" and failed == 1

    port, failed = doctor.run_checks(profile="portable", only=["hw-test"], wsl=True)
    assert port[0]["status"] == "SKIP" and failed == 0


def test_invasive_and_network_are_not_profile_driven(registry):
    """The two axes must stay independent.

    REGRESSION. `portable` used to skip `invasive` and `network` too, which
    meant the profile someone naturally reaches for on a second machine silently
    skipped the clipboard round trip and the chrome-devtools MCP -- two of the
    three capabilities that silently died on that machine. The doctor handed it
    a clean report while both were dead. Profile answers "does this apply here";
    the skip flags answer "do I want to pay for it now"."""
    @doctor.check("inv-test", invasive=True)
    def _i():
        return "PASS", "invasive check ran"

    @doctor.check("net-test", network=True)
    def _n():
        return "PASS", "network check ran"

    names = ["inv-test", "net-test"]
    port, _ = doctor.run_checks(profile="portable", only=names, wsl=True)
    assert [c["status"] for c in port] == ["PASS", "PASS"], (
        "portable must not skip invasive/network checks: %r" % (port,))

    off, _ = doctor.run_checks(profile="full", only=names, wsl=True,
                               skip_invasive=True, skip_network=True)
    assert [c["status"] for c in off] == ["SKIP", "SKIP"], off

    # ...and each flag skips only its own tag.
    only_inv, _ = doctor.run_checks(profile="full", only=names, wsl=True,
                                    skip_invasive=True)
    assert [c["status"] for c in only_inv] == ["SKIP", "PASS"], only_inv


def test_wsl_only_check_skips_off_wsl(registry):
    @doctor.check("wsl-test", wsl_only=True)
    def _w():
        return "FAIL", "should never run off wsl"

    results, failed = doctor.run_checks(profile="full", only=["wsl-test"], wsl=False)
    assert results[0]["status"] == "SKIP" and failed == 0


def test_exit_code_is_1_only_when_something_fails(tmp_path):
    shim = bin_dir(tmp_path, "xclip", body="#!/bin/sh\nexit 0\n")
    bad = run("--json", "--profile", "portable", "--only", "clipboard-no-shim-intercept",
              env={"PATH": f"{shim}:{os.environ['PATH']}"})
    assert bad.returncode == 1, bad.stdout

    good = run("--json", "--profile", "portable", "--only", "clipboard-no-shim-intercept",
               env={"PATH": str(tmp_path / "empty")})
    assert good.returncode == 0, good.stdout


def test_sh_refuses_an_unbounded_timeout():
    with pytest.raises(ValueError):
        doctor.sh(["true"], timeout=None)


def test_sh_runs_linux_subprocesses_in_the_callers_directory():
    """sh() used to force cwd=/mnt/c on EVERY subprocess, including pure-Linux
    ones like ldconfig. Relocating a process the caller never asked to relocate
    silently changes what its relative paths mean."""
    r = doctor.sh(["pwd"], timeout=10)
    assert r.stdout.strip() == os.getcwd()


def test_windows_interop_subprocesses_run_from_a_windows_safe_directory(tmp_path):
    """The other half: PowerShell warns and misbehaves from a UNC working
    directory, so interop calls DO still run from /mnt/c. That is now win_cwd()'s
    job rather than a global default in sh()."""
    if not os.path.isdir("/mnt/c"):
        pytest.skip("no /mnt/c on this host")
    ps = """#!/usr/bin/env bash
pwd > "$DOCTOR_TEST_PWD"
cmd=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-Command" ]; then cmd="$2"; fi
  shift
done
case "$cmd" in
  "Write-Output "*) echo "${cmd#Write-Output }" ;;
esac
exit 0
"""
    out = tmp_path / "interop-pwd.txt"
    r = run("--json", "--profile", "portable", "--only", "windows-interop",
            env={"HOME": str(fake_home(tmp_path, ps)), "DOCTOR_TEST_PWD": str(out)})
    assert checks_of(r)["windows-interop"]["status"] == "PASS", r.stdout
    assert out.read_text().strip() == "/mnt/c"


def test_a_hanging_subprocess_becomes_FAIL_rather_than_hanging(registry):
    @doctor.check("hang-test")
    def _hang():
        doctor.sh(["sleep", "30"], timeout=1)
        return "PASS", "unreachable"

    results, failed = only(registry, "hang-test")
    assert results[0]["status"] == "FAIL"
    assert "Timeout" in results[0]["detail"] or "timed out" in results[0]["detail"].lower()


# --- clipboard-no-shim-intercept -----------------------------------------

def test_script_shim_on_the_clipboard_path_is_detected(tmp_path):
    shim = bin_dir(tmp_path, "xclip", body="#!/usr/bin/env bash\nexit 0\n")
    r = run("--json", "--profile", "portable", "--only", "clipboard-no-shim-intercept",
            env={"PATH": f"{shim}:{os.environ['PATH']}"})
    c = checks_of(r)["clipboard-no-shim-intercept"]
    assert c["status"] == "FAIL", c
    assert "xclip" in c["detail"]


def test_real_binary_on_the_clipboard_path_is_accepted(tmp_path):
    real = bin_dir(tmp_path, "xclip", copy_of="/bin/true")
    r = run("--json", "--profile", "portable", "--only", "clipboard-no-shim-intercept",
            env={"PATH": str(real)})
    c = checks_of(r)["clipboard-no-shim-intercept"]
    assert c["status"] == "PASS", c


def test_wl_paste_shim_is_detected_too(tmp_path):
    shim = bin_dir(tmp_path, "wl-paste", body="#!/bin/sh\nexit 0\n")
    r = run("--json", "--profile", "portable", "--only", "clipboard-no-shim-intercept",
            env={"PATH": f"{shim}:{os.environ['PATH']}"})
    c = checks_of(r)["clipboard-no-shim-intercept"]
    assert c["status"] == "FAIL", c
    assert "wl-paste" in c["detail"]


# --- windows-interop ------------------------------------------------------

def test_interop_fails_when_the_shim_is_absent(tmp_path):
    r = run("--json", "--profile", "portable", "--only", "windows-interop",
            env={"HOME": str(fake_home(tmp_path))})
    c = checks_of(r)["windows-interop"]
    assert c["status"] == "FAIL" and "missing" in c["detail"]


def test_interop_fails_when_the_shim_is_present_but_broken(tmp_path):
    r = run("--json", "--profile", "portable", "--only", "windows-interop",
            env={"HOME": str(fake_home(tmp_path, PS_BROKEN))})
    c = checks_of(r)["windows-interop"]
    assert c["status"] == "FAIL", c
    assert "non-functional" in c["detail"]


def test_interop_passes_only_on_a_real_round_trip(tmp_path):
    r = run("--json", "--profile", "portable", "--only", "windows-interop",
            env={"HOME": str(fake_home(tmp_path, PS_ECHO))})
    c = checks_of(r)["windows-interop"]
    assert c["status"] == "PASS", c


def test_interop_fails_when_the_shim_exits_0_but_echoes_nothing(tmp_path):
    """A shim that succeeds without returning the nonce has not round-tripped."""
    silent = "#!/bin/sh\nexit 0\n"
    r = run("--json", "--profile", "portable", "--only", "windows-interop",
            env={"HOME": str(fake_home(tmp_path, silent))})
    c = checks_of(r)["windows-interop"]
    assert c["status"] == "FAIL", c


# --- windows-node ---------------------------------------------------------

PS_NO_NODE = """#!/usr/bin/env bash
exit 0
"""


def test_windows_node_fails_with_actionable_message_when_absent(tmp_path):
    r = run("--json", "--profile", "portable", "--only", "windows-node",
            env={"HOME": str(fake_home(tmp_path, PS_NO_NODE))})
    c = checks_of(r)["windows-node"]
    assert c["status"] == "FAIL", c
    assert "WSL" in c["detail"], "must say WSL's node does not satisfy this"


def test_windows_node_fails_when_located_but_not_executable(tmp_path):
    """Locating node.exe is not enough -- it must actually evaluate an expression."""
    ps = """#!/usr/bin/env bash
cmd=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-Command" ]; then cmd="$2"; fi
  shift
done
case "$cmd" in
  *"Get-Command node.exe"*) echo 'PATH=C:\\Program Files\\nodejs\\node.exe' ;;
  *) exit 1 ;;
esac
exit 0
"""
    r = run("--json", "--profile", "portable", "--only", "windows-node",
            env={"HOME": str(fake_home(tmp_path, ps))})
    c = checks_of(r)["windows-node"]
    assert c["status"] == "FAIL", c
    assert "not runnable" in c["detail"], "must distinguish 'on PATH' from 'runs'"


# --- windows-chrome -------------------------------------------------------

def _chrome_ps(output):
    return f"""#!/usr/bin/env bash
cmd=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-Command" ]; then cmd="$2"; fi
  shift
done
case "$cmd" in
  *"chrome.exe"*) {output} ;;
esac
exit 0
"""


def test_windows_chrome_fails_when_not_installed(tmp_path):
    r = run("--json", "--profile", "portable", "--only", "windows-chrome",
            env={"HOME": str(fake_home(tmp_path, _chrome_ps("exit 4")))})
    c = checks_of(r)["windows-chrome"]
    assert c["status"] == "FAIL", c
    assert "not found" in c["detail"].lower()


def test_windows_chrome_passes_when_the_binary_reports_a_version(tmp_path):
    ps = _chrome_ps("echo 'CHROME=C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe|150.0.1'")
    r = run("--json", "--profile", "portable", "--only", "windows-chrome",
            env={"HOME": str(fake_home(tmp_path, ps))})
    c = checks_of(r)["windows-chrome"]
    assert c["status"] == "PASS", c
    assert "150.0.1" in c["detail"]


def test_windows_chrome_fails_when_the_path_has_no_version_resource(tmp_path):
    """A path on PATH is not proof of an executable; the version resource is
    read out of the binary itself."""
    ps = _chrome_ps("echo 'CHROME=C:\\bogus\\chrome.exe|'")
    r = run("--json", "--profile", "portable", "--only", "windows-chrome",
            env={"HOME": str(fake_home(tmp_path, ps))})
    c = checks_of(r)["windows-chrome"]
    assert c["status"] == "FAIL", c
    assert "version" in c["detail"].lower()


# --- windows-chrome-shim --------------------------------------------------
#
# `windows-chrome` above asserts the BROWSER exists. These assert the SHIM can
# drive it -- a different claim, and the one that was never made. The shim
# spent its whole life unable to launch anything (the AV refuses
# FromBase64String + Start-Process on one command line) while `windows-chrome`
# reported PASS the entire time, because Chrome did exist.

PS_TEMPPATH = """#!/usr/bin/env bash
cmd=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-Command" ]; then cmd="$2"; fi
  shift
done
case "$cmd" in
  *GetTempPath*) echo "$FAKE_WIN_TMP" ;;
esac
exit 0
"""

# wslpath stand-in: the fake "Windows temp" IS a real Linux directory here, so
# both translations are the identity.
WSLPATH_IDENTITY = """#!/usr/bin/env bash
shift
printf '%s\\n' "$1"
"""


def _shim_home(tmp_path, chrome_body):
    """A $HOME carrying a temp-path-aware powershell.exe and a windows-chrome."""
    home = fake_home(tmp_path, PS_TEMPPATH)
    wc = home / ".local" / "bin" / "windows-chrome"
    wc.write_text(chrome_body)
    wc.chmod(0o755)
    return home


def _run_shim_check(tmp_path, home, extra_env=None):
    wsl = bin_dir(tmp_path, "wslpath", body=WSLPATH_IDENTITY)
    wintmp = tmp_path / "wintmp"
    wintmp.mkdir(exist_ok=True)
    env = {"HOME": str(home), "FAKE_WIN_TMP": str(wintmp),
           "PATH": f"{wsl}:{os.environ['PATH']}"}
    env.update(extra_env or {})
    r = run("--json", "--profile", "portable", "--only", "windows-chrome-shim", env=env)
    return checks_of(r)["windows-chrome-shim"]


# A windows-chrome that behaves: it "launches" the recorder by appending the URL
# to the log the recorder would have written.
WC_GOOD = """#!/usr/bin/env bash
log="${WINDOWS_CHROME_PATH%.cmd}.txt"
printf 'RAW=["%s"]\\n' "$1" >> "$log"
exit 0
"""

# The real C1 failure: powershell.exe is refused at CreateProcess time, so the
# shim exits non-zero having launched nothing.
WC_AV_BLOCKED = """#!/usr/bin/env bash
echo "powershell.exe: Invalid argument" >&2
echo "windows-chrome: failed to launch $1" >&2
exit 1
"""

# Exits 0, launches nothing. Exit 0 alone is not evidence.
WC_SILENT = """#!/usr/bin/env bash
exit 0
"""

WC_MANGLES = """#!/usr/bin/env bash
log="${WINDOWS_CHROME_PATH%.cmd}.txt"
printf 'RAW=[https://wrong.example/lost]\\n' >> "$log"
exit 0
"""


def test_chrome_shim_passes_when_the_url_arrives_intact(tmp_path):
    c = _run_shim_check(tmp_path, _shim_home(tmp_path, WC_GOOD))
    assert c["status"] == "PASS", c


def test_chrome_shim_fails_when_the_shim_is_absent(tmp_path):
    home = fake_home(tmp_path, PS_TEMPPATH)
    c = _run_shim_check(tmp_path, home)
    assert c["status"] == "FAIL", c
    assert "missing shim" in c["detail"] and "Tier 0" in c["detail"]


def test_chrome_shim_fails_when_powershell_is_refused_at_launch(tmp_path):
    """The C1 regression: an AV blocks the CreateProcess, so nothing launches.
    This is the case every mocked test was structurally unable to see."""
    c = _run_shim_check(tmp_path, _shim_home(tmp_path, WC_AV_BLOCKED))
    assert c["status"] == "FAIL", c
    assert "cannot launch anything" in c["detail"]


def test_chrome_shim_fails_when_it_exits_0_having_launched_nothing(tmp_path):
    c = _run_shim_check(tmp_path, _shim_home(tmp_path, WC_SILENT))
    assert c["status"] == "FAIL", c
    assert "never invoked" in c["detail"]


def test_chrome_shim_fails_when_the_url_does_not_arrive_intact(tmp_path):
    c = _run_shim_check(tmp_path, _shim_home(tmp_path, WC_MANGLES))
    assert c["status"] == "FAIL", c
    assert "intact" in c["detail"]


def test_chrome_shim_check_runs_under_the_portable_profile(tmp_path):
    """The Surface Book 2 is exactly where a dead BROWSER hook goes unnoticed,
    and `portable` is the profile reached for on the second machine."""
    c = _run_shim_check(tmp_path, _shim_home(tmp_path, WC_GOOD))
    assert c["status"] == "PASS", c
    assert not c["hardware"], "must not be hardware-gated out of portable"


def test_chrome_shim_cleans_up_its_recorder(tmp_path):
    """Read-only apart from its own temp files — nothing may be left behind."""
    wintmp = tmp_path / "wintmp"
    _run_shim_check(tmp_path, _shim_home(tmp_path, WC_GOOD))
    assert list(wintmp.iterdir()) == [], f"left files behind: {list(wintmp.iterdir())}"


# --- clipboard-image-roundtrip -------------------------------------------

# A powershell.exe stand-in emulating the Windows clipboard: SetImage records
# the PNG the doctor wrote; GetImage replays its real dimensions and first
# pixel. STALE=1 makes GetImage return a different image instead, which is
# exactly the false-pass a naive "did we get *an* image back" check misses.
PS_CLIPBOARD = r"""#!/usr/bin/env python3
import base64, os, re, struct, subprocess, sys, zlib

STATE = os.environ["DOCTOR_TEST_STATE"]
cmd = ""
a = sys.argv[1:]
for i, v in enumerate(a):
    if v == "-Command" and i + 1 < len(a):
        cmd = a[i + 1]

def decode(path):
    raw = open(path, "rb").read()
    i, w, h, idat = 8, 0, 0, b""
    while i < len(raw):
        n = struct.unpack(">I", raw[i:i+4])[0]
        typ = raw[i+4:i+8]
        data = raw[i+8:i+8+n]
        if typ == b"IHDR":
            w, h = struct.unpack(">II", data[:8])
        elif typ == b"IDAT":
            idat += data
        i += 12 + n
    px = zlib.decompress(idat)[1:4]
    return w, h, px[0], px[1], px[2], raw

if "SetImage" in cmd:
    m = re.search(r"FromFile\('([^']+)'\)", cmd)
    win = m.group(1)
    lin = subprocess.run(["wslpath", "-u", win], capture_output=True, text=True).stdout.strip()
    open(STATE, "w").write(lin)
    sys.exit(0)

if "GetImage" in cmd:
    if os.environ.get("DOCTOR_TEST_STALE"):
        # a leftover image from before the doctor ran
        stale = os.environ["DOCTOR_TEST_STALE"]
        w, h, r, g, b, raw = decode(stale)
    else:
        w, h, r, g, b, raw = decode(open(STATE).read())
    print(f"{w} {h} {r} {g} {b}")
    print(base64.b64encode(raw).decode())
    sys.exit(0)

sys.exit(0)
"""


def _clip_env(tmp_path, stale=None):
    env = {
        "HOME": str(fake_home(tmp_path, PS_CLIPBOARD)),
        "DOCTOR_TEST_STATE": str(tmp_path / "clipboard.state"),
    }
    if stale:
        env["DOCTOR_TEST_STALE"] = str(stale)
    return env


def test_clipboard_round_trip_passes_when_the_image_comes_back_intact(tmp_path):
    r = run("--json", "--profile", "full", "--only", "clipboard-image-roundtrip",
            env=_clip_env(tmp_path))
    c = checks_of(r)["clipboard-image-roundtrip"]
    assert c["status"] == "PASS", c


def test_clipboard_round_trip_fails_on_a_stale_image(tmp_path):
    """The clipboard returns *an* image, but not the one we put there."""
    stale = tmp_path / "stale.png"
    stale.write_bytes(doctor.solid_png(3, 5, (1, 2, 3)))
    r = run("--json", "--profile", "full", "--only", "clipboard-image-roundtrip",
            env=_clip_env(tmp_path, stale=stale))
    c = checks_of(r)["clipboard-image-roundtrip"]
    assert c["status"] == "FAIL", c
    assert "not the image" in c["detail"] or "mismatch" in c["detail"].lower()


def test_clipboard_round_trip_fails_when_clipboard_is_empty(tmp_path):
    empty = """#!/bin/sh
case "$*" in *GetImage*) exit 3 ;; esac
exit 0
"""
    r = run("--json", "--profile", "full", "--only", "clipboard-image-roundtrip",
            env={"HOME": str(fake_home(tmp_path, empty))})
    c = checks_of(r)["clipboard-image-roundtrip"]
    assert c["status"] == "FAIL", c
    assert "no image" in c["detail"].lower()


def test_solid_png_is_a_decodable_png():
    blob = doctor.solid_png(4, 6, (10, 20, 30))
    assert blob.startswith(b"\x89PNG\r\n\x1a\n")
    assert doctor.png_header(blob) == (4, 6)


# --- mcp-chrome-devtools --------------------------------------------------

def _mcp_ps(*responses):
    """A powershell.exe stand-in for the MCP check.

    It plays both halves the doctor asks for: the Windows-node locate probe,
    then the MCP server itself when npx is invoked. This replaced a DOCTOR_NPX
    environment hook that let any caller substitute the whole MCP command --
    a trivially-passable back door in the one tool whose job is honest
    verification.

    It is also a stricter fixture than the hook was: the npx branch matches on
    `nodejs\\npx.cmd`, so it only answers if the doctor correctly derived the
    node DIRECTORY from the located node.exe path -- logic the hook bypassed
    entirely.
    """
    # bash, not sh: dash's builtin echo expands backslash escapes, which would
    # mangle 'C:\\Program Files\\nodejs\\node.exe' into three lines at the \\n.
    body = "\n".join("echo " + shlex.quote(r) for r in responses)
    return f"""#!/usr/bin/env bash
cmd=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-Command" ]; then cmd="$2"; fi
  shift
done
case "$cmd" in
  *"Get-Command node.exe"*) echo 'PATH=C:\\Program Files\\nodejs\\node.exe' ;;
  *"nodejs\\npx.cmd"*)
    cat > /dev/null
{body}
    ;;
esac
exit 0
"""


INIT_ONLY = ('{"result":{"protocolVersion":"2024-11-05","capabilities":'
             '{"tools":{"listChanged":true}},"serverInfo":{"name":"chrome_devtools"}},'
             '"jsonrpc":"2.0","id":1}')
INIT_OK = '{"result":{"capabilities":{"tools":{}}},"jsonrpc":"2.0","id":1}'
TOOLS_TWO = ('{"result":{"tools":[{"name":"click"},{"name":"navigate_page"}]},'
             '"jsonrpc":"2.0","id":2}')
TOOLS_EMPTY = '{"result":{"tools":[]},"jsonrpc":"2.0","id":2}'


def test_mcp_check_rejects_an_initialize_only_response(tmp_path):
    """The initialize reply advertises `"tools":{"listChanged":true}` in its
    capabilities. A substring search for '"tools"' therefore passes even when
    tools/list never answered -- this test pins the parsed behaviour."""
    r = run("--json", "--profile", "full", "--only", "mcp-chrome-devtools",
            env={"HOME": str(fake_home(tmp_path, _mcp_ps(INIT_ONLY)))})
    c = checks_of(r)["mcp-chrome-devtools"]
    assert c["status"] == "FAIL", c


def test_mcp_check_passes_on_a_non_empty_tools_list(tmp_path):
    r = run("--json", "--profile", "full", "--only", "mcp-chrome-devtools",
            env={"HOME": str(fake_home(tmp_path, _mcp_ps(INIT_OK, TOOLS_TWO)))})
    c = checks_of(r)["mcp-chrome-devtools"]
    assert c["status"] == "PASS", c
    assert "2" in c["detail"]


def test_mcp_check_fails_on_an_empty_tools_list(tmp_path):
    r = run("--json", "--profile", "full", "--only", "mcp-chrome-devtools",
            env={"HOME": str(fake_home(tmp_path, _mcp_ps(TOOLS_EMPTY)))})
    c = checks_of(r)["mcp-chrome-devtools"]
    assert c["status"] == "FAIL", c


def test_mcp_check_fails_when_windows_node_is_absent(tmp_path):
    """No Windows node means npx cannot run the MCP at all. The message must
    point at the windows-node check rather than blaming the MCP."""
    r = run("--json", "--profile", "full", "--only", "mcp-chrome-devtools",
            env={"HOME": str(fake_home(tmp_path, PS_NO_NODE))})
    c = checks_of(r)["mcp-chrome-devtools"]
    assert c["status"] == "FAIL", c
    assert "windows-node" in c["detail"], c


def test_no_environment_override_can_replace_the_mcp_command(tmp_path):
    """INTEGRITY GUARD. A hook that let an env var stand in for the whole MCP
    invocation would make the check pass by simply exporting a variable. The
    doctor must ignore any such variable and still talk to the real command."""
    home = fake_home(tmp_path, PS_NO_NODE)
    always_ok = bin_dir(tmp_path, "fake-mcp", body=(
        "#!/bin/sh\ncat > /dev/null\n"
        'echo \'{"result":{"tools":[{"name":"click"}]},"jsonrpc":"2.0","id":2}\'\n'))
    r = run("--json", "--profile", "full", "--only", "mcp-chrome-devtools",
            env={"HOME": str(home), "DOCTOR_NPX": str(always_ok / "fake-mcp")})
    c = checks_of(r)["mcp-chrome-devtools"]
    assert c["status"] == "FAIL", "DOCTOR_NPX made a dead MCP report healthy"


# --- profile vs. invasiveness, on the REAL named checks --------------------
#
# The tests above use synthetic checks to pin the runner's gating logic. These
# pin the tagging of the three checks the doctor was actually written for, end
# to end through the CLI. Both halves are needed: correct gating applied to
# mis-tagged checks would still hand the second machine a clean report.

def test_portable_runs_the_clipboard_check(tmp_path):
    """REGRESSION: image paste is one of the capabilities that silently died on
    the second machine. `portable` used to SKIP this."""
    r = run("--json", "--profile", "portable", "--only", "clipboard-image-roundtrip",
            env=_clip_env(tmp_path))
    c = checks_of(r)["clipboard-image-roundtrip"]
    assert c["status"] == "PASS", c


def test_portable_runs_the_mcp_check(tmp_path):
    """REGRESSION: the chrome-devtools MCP is another. `portable` used to SKIP it."""
    r = run("--json", "--profile", "portable", "--only", "mcp-chrome-devtools",
            env={"HOME": str(fake_home(tmp_path, _mcp_ps(INIT_OK, TOOLS_TWO)))})
    c = checks_of(r)["mcp-chrome-devtools"]
    assert c["status"] == "PASS", c


def test_skip_invasive_skips_the_clipboard_check(tmp_path):
    """The opt-out exists so nobody mid-copy-paste loses their clipboard. It is
    a flag, not a profile: destructiveness and applicability are separate."""
    r = run("--json", "--profile", "full", "--skip-invasive",
            "--only", "clipboard-image-roundtrip", env=_clip_env(tmp_path))
    c = checks_of(r)["clipboard-image-roundtrip"]
    assert c["status"] == "SKIP", c
    assert "invasive" in c["detail"]


def test_skip_network_skips_the_mcp_check(tmp_path):
    r = run("--json", "--profile", "full", "--skip-network",
            "--only", "mcp-chrome-devtools",
            env={"HOME": str(fake_home(tmp_path, _mcp_ps(INIT_OK, TOOLS_TWO)))})
    c = checks_of(r)["mcp-chrome-devtools"]
    assert c["status"] == "SKIP", c
    assert "network" in c["detail"]


def test_skip_invasive_leaves_the_mcp_check_running(tmp_path):
    """Each flag is narrow: opting out of clipboard destruction must not also
    silently drop the MCP check."""
    r = run("--json", "--profile", "portable", "--skip-invasive",
            "--only", "mcp-chrome-devtools",
            env={"HOME": str(fake_home(tmp_path, _mcp_ps(INIT_OK, TOOLS_TWO)))})
    c = checks_of(r)["mcp-chrome-devtools"]
    assert c["status"] == "PASS", c


# --- cuda -----------------------------------------------------------------

def test_cuda_fails_when_a_linux_driver_shadows_the_wsl_stub(tmp_path, registry):
    ldconfig = bin_dir(tmp_path, "ldconfig", body=(
        "#!/bin/sh\n"
        "echo '\tlibcuda.so.1 (libc6,x86-64) => /usr/lib/x86_64-linux-gnu/libcuda.so.1'\n"
    ))
    r = run("--json", "--profile", "full", "--only", "cuda-stub-not-shadowed",
            env={"PATH": f"{ldconfig}:{os.environ['PATH']}"})
    c = checks_of(r)["cuda-stub-not-shadowed"]
    if c["status"] == "SKIP":
        pytest.skip("no /dev/dxg on this host")
    assert c["status"] == "FAIL", c
    assert "shadow" in c["detail"].lower()


def test_cuda_passes_when_the_stub_resolves_into_wsl_lib(tmp_path):
    ldconfig = bin_dir(tmp_path, "ldconfig", body=(
        "#!/bin/sh\n"
        "echo '\tlibcuda.so.1 (libc6,x86-64) => /usr/lib/wsl/lib/libcuda.so.1'\n"
    ))
    r = run("--json", "--profile", "full", "--only", "cuda-stub-not-shadowed",
            env={"PATH": f"{ldconfig}:{os.environ['PATH']}"})
    c = checks_of(r)["cuda-stub-not-shadowed"]
    if c["status"] == "SKIP":
        pytest.skip("no /dev/dxg on this host")
    assert c["status"] == "PASS", c


# --- version managers -----------------------------------------------------

def _vm_env(tmp_path, path_dirs, installs=()):
    home = tmp_path / "home"
    for rel in installs:
        (home / rel).mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    dirs = []
    for rel in path_dirs:
        d = home / rel
        d.mkdir(parents=True, exist_ok=True)
        dirs.append(str(d))
    return {"HOME": str(home), "PATH": os.pathsep.join(dirs)}


def test_two_managers_owning_the_path_is_a_conflict(tmp_path):
    env = _vm_env(tmp_path, [".asdf/shims", ".local/share/mise/shims"])
    r = run("--json", "--profile", "portable", "--only", "no-dual-version-managers",
            env=env)
    c = checks_of(r)["no-dual-version-managers"]
    assert c["status"] == "FAIL", c
    assert "asdf" in c["detail"] and "mise" in c["detail"]


def test_a_dormant_manager_install_is_not_a_conflict(tmp_path):
    """Regression: this machine has a full ~/.nvm (three node versions) that no
    rc file sources. It resolves nothing, so it is not a second active manager.
    Failing on its presence would be the very bug this script exists to catch."""
    env = _vm_env(tmp_path, [".asdf/shims"], installs=[".nvm"])
    r = run("--json", "--profile", "portable", "--only", "no-dual-version-managers",
            env=env)
    c = checks_of(r)["no-dual-version-managers"]
    assert c["status"] == "PASS", c
    assert "nvm" in c["detail"] and "inert" in c["detail"]


def test_one_manager_on_the_path_is_fine(tmp_path):
    env = _vm_env(tmp_path, [".asdf/shims"])
    r = run("--json", "--profile", "portable", "--only", "no-dual-version-managers",
            env=env)
    c = checks_of(r)["no-dual-version-managers"]
    assert c["status"] == "PASS", c


# --- no-plaintext-secrets-in-shell-rc -------------------------------------
#
# Three live credentials sat in ~/.bashrc on this machine indefinitely as
# literal `export VAR="value"` lines. These fixtures drive the check through the
# CLI against fake $HOME trees.
#
# The fixture values below are FAKE but SHAPED like the real thing -- that is
# the whole point, since shape is what rule 1 matches on. They are also what the
# leak tests search the output for.

RC_CHECK = "no-plaintext-secrets-in-shell-rc"

FAKE_GOOGLE = "AIzaSyD" + "9tQ3vBn7Kx2mLp0RfZs4WjH6cVaE1uYgT"   # AIza + 33
FAKE_SUPABASE = "sb_secret_" + "7Kx2mLp0RfZs4WjH6cVaE1uYgT"
FAKE_GITHUB = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_PASSWORD = "correct-horse-battery-staple-99"

# Every literal that must never reach the doctor's output.
ALL_FAKE_VALUES = (FAKE_GOOGLE, FAKE_SUPABASE, FAKE_GITHUB, FAKE_PASSWORD)


def rc_home(tmp_path, **files):
    """A $HOME containing the named rc files with the given contents."""
    home = tmp_path / "rc-home"
    home.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (home / name.replace("__", ".")).write_text(body)
    return home


def rc_check(tmp_path, home):
    r = run("--json", "--profile", "portable", "--only", RC_CHECK,
            env={"HOME": str(home)})
    return checks_of(r)[RC_CHECK], r.stdout


# A clean rc: ordinary configuration, nothing credential-shaped.
CLEAN_RC = """\
# ordinary shell setup
export EDITOR="vim"
export LESS="-R -F -X"
export MAKEFLAGS="-j8"
alias ll='ls -la'
"""

# Every pattern house-style.md actually prescribes. All of these are CORRECT and
# must pass -- a check that flags the fix it recommends is worse than useless,
# because it can never be satisfied.
REFERENCE_RC = """\
export EXPENSES_DB_PASSWORD="op://Private/expenses/db-password"
export SUPABASE_SECRET_KEY="${SUPABASE_SECRET_KEY}"
export GOOGLE_PLACES_API_KEY="$(op read op://Private/places/credential)"
export GITHUB_TOKEN="$GH_TOKEN_FROM_KEYCHAIN"
export SOME_API_KEY=""
export AWS_CREDENTIAL_FILE="$HOME/.aws/credentials"
export NPM_TOKEN_PATH=~/.npm-token
export OPENAI_API_KEY=`cat /run/secrets/openai`
"""


def test_clean_rc_files_pass(tmp_path):
    c, _ = rc_check(tmp_path, rc_home(tmp_path, __bashrc=CLEAN_RC))
    assert c["status"] == "PASS", c


def test_a_literal_secret_in_bashrc_is_reported(tmp_path):
    """The actual incident: an exported literal credential nobody ever looked at."""
    home = rc_home(tmp_path, __bashrc=CLEAN_RC + (
        f'export EXPENSES_SUPABASE_SECRET_KEY="{FAKE_SUPABASE}"\n'))
    c, _ = rc_check(tmp_path, home)
    assert c["status"] == "FAIL", c
    assert "EXPENSES_SUPABASE_SECRET_KEY" in c["detail"]
    # Derived, not hardcoded: an edit to CLEAN_RC must not silently stop this
    # from asserting the line number.
    lineno = CLEAN_RC.count("\n") + 1
    assert f".bashrc:{lineno}" in c["detail"], (
        "must report the line number: %r" % c["detail"])


def test_references_are_not_flagged(tmp_path):
    """${VAR}, $(op read ...), op://, backticks, empty values and paths are the
    CORRECT pattern. Flagging them would make the check unsatisfiable and it
    would be ignored -- the failure mode that matters most here."""
    c, _ = rc_check(tmp_path, rc_home(tmp_path, __bashrc=REFERENCE_RC))
    assert c["status"] == "PASS", c


def test_secret_shaped_value_under_an_innocuous_name_is_caught(tmp_path):
    """Rule 1 is name-independent. `MAPS_BACKEND` says nothing, but an
    AIza-shaped 37-character literal is a Google API key whatever it is called."""
    home = rc_home(tmp_path, __bashrc=f'export MAPS_BACKEND="{FAKE_GOOGLE}"\n')
    c, _ = rc_check(tmp_path, home)
    assert c["status"] == "FAIL", c
    assert "MAPS_BACKEND" in c["detail"]


def test_no_matched_value_appears_anywhere_in_the_output(tmp_path):
    """THE POINT OF THE CHECK. Printing a secret to prove it is exposed spreads
    it into scrollback, CI logs, --json output and the next agent transcript.
    Asserted against the WHOLE stdout, not just the detail field."""
    home = rc_home(tmp_path, __bashrc=(
        f'export EXPENSES_SUPABASE_SECRET_KEY="{FAKE_SUPABASE}"\n'
        f'export GOOGLE_PLACES_API_KEY="{FAKE_GOOGLE}"\n'
        f'export EXPENSES_DB_PASSWORD="{FAKE_PASSWORD}"\n'
        f'export MAPS_BACKEND="{FAKE_GITHUB}"\n'))
    c, stdout = rc_check(tmp_path, home)
    assert c["status"] == "FAIL", c
    for value in ALL_FAKE_VALUES:
        assert value not in stdout, f"doctor leaked a secret value: {value[:6]}..."
    # ...and not in the human-readable rendering either.
    plain = run("--profile", "portable", "--only", RC_CHECK,
                env={"HOME": str(home)}).stdout
    for value in ALL_FAKE_VALUES:
        assert value not in plain, f"doctor leaked a secret value in text mode"
    # It must still be actionable: every offending NAME is named.
    for name in ("EXPENSES_SUPABASE_SECRET_KEY", "GOOGLE_PLACES_API_KEY",
                 "EXPENSES_DB_PASSWORD", "MAPS_BACKEND"):
        assert name in c["detail"], name


def test_a_credential_named_variable_with_a_literal_is_flagged(tmp_path):
    """Rule 2: the value has no recognisable shape (a database password is just
    a string), so the NAME is the only signal."""
    home = rc_home(tmp_path, __bashrc=f'export EXPENSES_DB_PASSWORD="{FAKE_PASSWORD}"\n')
    c, _ = rc_check(tmp_path, home)
    assert c["status"] == "FAIL", c
    assert "EXPENSES_DB_PASSWORD" in c["detail"]


def test_config_flags_that_merely_match_the_name_pattern_are_not_secrets(tmp_path):
    """FALSE-POSITIVE GUARD. TOKENIZERS_PARALLELISM matches /TOKEN/ and is a
    HuggingFace setting; so do a boolean, a numeric and a _FILE/_PATH variable.
    A check that fires on these gets ignored, and an ignored check is worse than
    no check."""
    home = rc_home(tmp_path, __bashrc="""\
export TOKENIZERS_PARALLELISM=false
export VAULT_TOKEN_TTL=3600
export SECRET_SERVICE_ENABLED=true
export API_KEY_FILE=/etc/keys/api.key
export GOOGLE_APPLICATION_CREDENTIALS_PATH=/etc/gcp.json
export DB_PASSWORD_FILE=/run/secrets/db
export CREDENTIAL_DIR=/var/lib/creds
""")
    c, _ = rc_check(tmp_path, home)
    assert c["status"] == "PASS", c


def test_other_rc_files_are_scanned_not_just_bashrc(tmp_path):
    home = rc_home(tmp_path,
                   __bashrc=CLEAN_RC,
                   __zshrc=f'export SLACK_TOKEN="xoxb-{FAKE_GITHUB[4:]}"\n')
    c, _ = rc_check(tmp_path, home)
    assert c["status"] == "FAIL", c
    assert ".zshrc" in c["detail"], c


def test_a_sourced_file_one_level_deep_is_scanned(tmp_path):
    """A secret hidden one `source` away is exactly as exposed. `.bashrc` here
    is clean; only the sourced file is dirty."""
    home = rc_home(tmp_path,
                   __bashrc=CLEAN_RC + 'source "$HOME/.secrets.env"\n',
                   __secrets__env=f'export STRIPE_API_KEY="{FAKE_GOOGLE}"\n')
    c, stdout = rc_check(tmp_path, home)
    assert c["status"] == "FAIL", c
    assert "STRIPE_API_KEY" in c["detail"] and ".secrets.env" in c["detail"], c
    assert FAKE_GOOGLE not in stdout


def test_an_unresolvable_source_target_does_not_crash_the_check(tmp_path):
    """`. "$ASDF_DIR/asdf.sh"` cannot be resolved without running the shell.
    Guessing at it could scan the wrong file; the check skips it and still
    reports on everything it CAN read."""
    home = rc_home(tmp_path, __bashrc=(
        '. "$ASDF_DIR/asdf.sh"\n'
        'source /nonexistent/nowhere.sh\n'
        f'export EXPENSES_DB_PASSWORD="{FAKE_PASSWORD}"\n'))
    c, _ = rc_check(tmp_path, home)
    assert c["status"] == "FAIL", c
    assert "EXPENSES_DB_PASSWORD" in c["detail"]


def test_the_failure_is_actionable_and_points_at_the_op_reference_pattern(tmp_path):
    """A FAIL that does not say what to do instead gets silenced, not fixed."""
    home = rc_home(tmp_path, __bashrc=f'export GOOGLE_PLACES_API_KEY="{FAKE_GOOGLE}"\n')
    c, _ = rc_check(tmp_path, home)
    assert c["status"] == "FAIL", c
    assert "op://" in c["detail"], "must point at the op:// reference pattern"
    assert "house-style.md" in c["detail"]
    assert "migrate-secrets-to-1password.sh" in c["detail"]


def test_the_check_runs_under_the_portable_profile_and_is_not_invasive(tmp_path):
    """Nothing here is host-specific, so `portable` -- the profile reached for on
    a second machine -- must actually RUN it, not skip it. And it only reads, so
    --skip-invasive must not drop it either."""
    home = rc_home(tmp_path, __bashrc=f'export GOOGLE_PLACES_API_KEY="{FAKE_GOOGLE}"\n')
    c, _ = rc_check(tmp_path, home)
    assert c["status"] == "FAIL", "portable must run this check, not skip it: %r" % c
    assert not c["hardware"], "must not be hardware-gated out of portable"
    assert not c["invasive"], "the check only reads rc files"

    r = run("--json", "--profile", "portable", "--skip-invasive", "--skip-network",
            "--only", RC_CHECK, env={"HOME": str(home)})
    assert checks_of(r)[RC_CHECK]["status"] == "FAIL", "skip flags must not drop it"


def test_the_check_does_not_modify_the_rc_files_it_reads(tmp_path):
    """Read-only is a hard constraint on the whole doctor."""
    body = f'export GOOGLE_PLACES_API_KEY="{FAKE_GOOGLE}"\n'
    home = rc_home(tmp_path, __bashrc=body)
    before = (home / ".bashrc").read_bytes()
    rc_check(tmp_path, home)
    assert (home / ".bashrc").read_bytes() == before


def test_check_skips_when_there_are_no_rc_files_at_all(tmp_path):
    """Inapplicable must SKIP, never FAIL -- a tool that cries wolf is ignored."""
    empty = tmp_path / "empty-home"
    empty.mkdir()
    c, _ = rc_check(tmp_path, empty)
    assert c["status"] == "SKIP", c


# --- onepassword-usable ---------------------------------------------------
#
# The question is "can this machine resolve a 1Password reference at all, and by
# WHICH path?" -- not "is the CLI authenticated?", because driving 1Password
# through the MCP alone is a legitimate configuration.
#
# Everything here runs against fake `op` binaries and fake MCP servers in temp
# HOMEs. The real 1Password state is never touched and nothing ever attempts to
# authenticate.

OP_CHECK = "onepassword-usable"

# `op account list --format=json` on an authenticated CLI.
OP_AUTHED = """#!/bin/sh
case "$*" in
  *"account list"*) echo '[{"url":"example.1password.com","user_uuid":"UUUU"}]' ;;
esac
exit 0
"""

# The measured state on this machine: `op` runs, and reports no accounts.
OP_NO_ACCOUNT = """#!/bin/sh
case "$*" in
  *"account list"*) echo '[]' ;;
esac
exit 0
"""

# A service account authenticates by token, so `account list` stays empty while
# `whoami` works. Without this branch the doctor would FAIL a working CLI.
OP_SERVICE_ACCOUNT = """#!/bin/sh
case "$*" in
  *"account list"*) echo '[]' ;;
  *whoami*) echo '{"URL":"example.1password.com","ServiceAccountType":"USER"}' ;;
esac
exit 0
"""

# A SPEC-STRICT MCP server: it refuses tools/list until it has seen
# `notifications/initialized`, which is what the real 1Password MCP does
# (`ExpectedInitializedNotification`). The order of the cases matters --
# "notifications/initialized" also contains "initialize".
MCP_STRICT = """#!/bin/sh
saw_init=0
while IFS= read -r line; do
  case "$line" in
    *notifications/initialized*) saw_init=1 ;;
    *initialize*) echo '{"jsonrpc":"2.0","id":1,"result":{"capabilities":{"tools":{}}}}' ;;
    *tools/list*)
      if [ "$saw_init" = 1 ]; then
        echo '{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"list_environments"},{"name":"authenticate"}]}}'
      else
        echo "Error: ExpectedInitializedNotification" >&2
      fi
      ;;
  esac
done
exit 0
"""

# Configured, launches, answers nothing -- a dead MCP.
MCP_DEAD = """#!/bin/sh
cat > /dev/null
echo "1password-mcp: cannot reach the desktop app" >&2
exit 1
"""


def op_home(tmp_path, mcp_command=None, cli="claude"):
    """A $HOME whose CLI config optionally registers a 1Password MCP."""
    home = tmp_path / "op-home"
    home.mkdir(parents=True, exist_ok=True)
    if mcp_command is None:
        return home
    entry = {"type": "stdio", "command": str(mcp_command), "args": []}
    if cli == "claude":
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"1password": entry}}))
    elif cli == "codex":
        (home / ".codex").mkdir(exist_ok=True)
        (home / ".codex" / "config.toml").write_text(
            '[mcp_servers.1password]\n'
            f'command = "{mcp_command}"\nargs = []\n')
    elif cli == "agy":
        (home / ".gemini" / "config").mkdir(parents=True, exist_ok=True)
        (home / ".gemini" / "config" / "mcp_config.json").write_text(
            json.dumps({"mcpServers": {"1password": entry}}))
    return home


def op_bin(tmp_path, body=None, copy_of=None):
    return bin_dir(tmp_path, "op", body=body, copy_of=copy_of)


def op_check(tmp_path, home, path_dirs=(), env=None):
    """Run onepassword-usable against a fake HOME and a PATH we fully control.

    PATH is REPLACED, never prepended: the real `op` lives in ~/.local/bin on
    this machine and would otherwise be found and interrogated.
    OP_SERVICE_ACCOUNT_TOKEN is cleared for the same reason -- a variable set in
    the developer's shell must not change what these tests assert."""
    e = {"HOME": str(home),
         "PATH": os.pathsep.join([str(d) for d in path_dirs] or [str(tmp_path / "empty")]),
         "OP_SERVICE_ACCOUNT_TOKEN": ""}
    e.update(env or {})
    r = run("--json", "--profile", "portable", "--only", OP_CHECK, env=e)
    return checks_of(r)[OP_CHECK]


def test_onepassword_skips_when_neither_cli_nor_mcp_is_configured(tmp_path):
    """1Password is optional. Inapplicable must SKIP, never FAIL."""
    c = op_check(tmp_path, op_home(tmp_path))
    assert c["status"] == "SKIP", c
    assert "optional" in c["detail"]


def test_onepassword_passes_naming_the_cli_when_the_cli_is_authenticated(tmp_path):
    c = op_check(tmp_path, op_home(tmp_path),
                 path_dirs=[op_bin(tmp_path, body=OP_AUTHED)])
    assert c["status"] == "PASS", c
    assert "via the `op` CLI" in c["detail"], "PASS must say WHICH path works"
    assert "1 account" in c["detail"], c


def test_op_merely_on_path_but_unauthenticated_is_not_usable(tmp_path):
    """THE POINT OF THIS CHECK. An `op` on PATH with no account resolves no
    reference at all; certifying it would repeat the xclip-shim mistake."""
    c = op_check(tmp_path, op_home(tmp_path),
                 path_dirs=[op_bin(tmp_path, body=OP_NO_ACCOUNT)])
    assert c["status"] == "FAIL", c
    assert "present is not usable" in c["detail"], c


def test_onepassword_passes_naming_the_mcp_when_only_the_mcp_works(tmp_path):
    """An unauthenticated CLI plus a working MCP is a WORKING configuration.
    Failing it would be crying wolf at a machine that is fine."""
    mcp = tmp_path / "mcp-strict.sh"
    mcp.write_text(MCP_STRICT)
    mcp.chmod(0o755)
    c = op_check(tmp_path, op_home(tmp_path, mcp_command=mcp),
                 path_dirs=[op_bin(tmp_path, body=OP_NO_ACCOUNT)])
    assert c["status"] == "PASS", c
    assert "via the MCP only" in c["detail"], "PASS must say WHICH path works"
    assert "2 tools" in c["detail"], c
    # ...and must not imply everything is fine: op run / op read still cannot work.
    assert "op run" in c["detail"] and "op read" in c["detail"], c


def test_the_mcp_probe_sends_the_initialized_notification(tmp_path):
    """REGRESSION. The MCP spec requires `notifications/initialized` after
    `initialize`, and a strict server answers tools/list with
    ExpectedInitializedNotification until it arrives -- which is indistinguishable
    from a dead server. The real 1Password MCP is strict; chrome-devtools is
    lenient, so omitting the notification looked correct for as long as
    chrome-devtools was the only server ever probed. MCP_STRICT only answers
    tools/list once it has seen the notification."""
    mcp = tmp_path / "mcp-strict.sh"
    mcp.write_text(MCP_STRICT)
    mcp.chmod(0o755)
    c = op_check(tmp_path, op_home(tmp_path, mcp_command=mcp))
    assert c["status"] == "PASS", c
    assert "2 tools" in c["detail"], c


def test_onepassword_fails_when_both_are_configured_and_neither_works(tmp_path):
    """FAIL only when 1Password is clearly INTENDED and no path works, and the
    detail must name every path tried and what each did."""
    mcp = tmp_path / "mcp-dead.sh"
    mcp.write_text(MCP_DEAD)
    mcp.chmod(0o755)
    c = op_check(tmp_path, op_home(tmp_path, mcp_command=mcp),
                 path_dirs=[op_bin(tmp_path, body=OP_NO_ACCOUNT)])
    assert c["status"] == "FAIL", c
    assert "`op` CLI" in c["detail"], "must name the CLI path it tried"
    assert "1Password MCP (claude:1password)" in c["detail"], "must name the MCP entry"
    assert "no tools/list response" in c["detail"], "must say what the MCP did"


@pytest.mark.skipif(not doctor.is_wsl(), reason="the boundary note only applies under WSL")
def test_the_failure_states_the_wsl_boundary_end_to_end(tmp_path):
    """An ELF `op` under WSL with no account is the measured state on this
    machine. Without the explanation the reader concludes their 1Password
    desktop integration is broken and goes off re-toggling a setting that was
    never the problem."""
    mcp = tmp_path / "mcp-dead.sh"
    mcp.write_text(MCP_DEAD)
    mcp.chmod(0o755)
    c = op_check(tmp_path, op_home(tmp_path, mcp_command=mcp),
                 path_dirs=[op_bin(tmp_path, copy_of="/bin/true")])
    assert c["status"] == "FAIL", c
    assert "WSL BOUNDARY" in c["detail"], c
    assert "WINDOWS processes only" in c["detail"], c


# The boundary logic itself is exercised in-process so it is asserted on EVERY
# host, not only on the WSL machine that happens to have written it.

def test_boundary_note_is_attached_for_a_linux_op_under_wsl(tmp_path):
    op = op_bin(tmp_path, copy_of="/bin/true") / "op"
    usable, detail = doctor.op_cli_status(str(op), wsl=True)
    assert usable is False, detail
    assert "WSL BOUNDARY" in detail
    assert "op account add" in detail, "must say how to authenticate WSL's own op"
    assert "OP_SERVICE_ACCOUNT_TOKEN" in detail, "must give the second way too"


def test_boundary_note_is_not_attached_off_wsl(tmp_path):
    """Off WSL there is no Windows desktop app to blame; the note would be noise."""
    op = op_bin(tmp_path, copy_of="/bin/true") / "op"
    usable, detail = doctor.op_cli_status(str(op), wsl=False)
    assert usable is False, detail
    assert "WSL BOUNDARY" not in detail, detail


def test_a_service_account_token_counts_as_an_authenticated_cli(tmp_path):
    """`op account list` is EMPTY under a service account, so account-list alone
    would report a working CLI as unusable."""
    c = op_check(tmp_path, op_home(tmp_path),
                 path_dirs=[op_bin(tmp_path, body=OP_SERVICE_ACCOUNT)],
                 env={"OP_SERVICE_ACCOUNT_TOKEN": "fake-not-a-real-token"})
    assert c["status"] == "PASS", c
    assert "via the `op` CLI" in c["detail"], c
    assert "OP_SERVICE_ACCOUNT_TOKEN" in c["detail"], c


def test_no_token_value_appears_in_the_output(tmp_path):
    """Same rule as the rc-secrets check: never print the credential."""
    token = "ops_" + "A1b2C3d4E5f6G7h8I9j0"
    c = op_check(tmp_path, op_home(tmp_path),
                 path_dirs=[op_bin(tmp_path, body=OP_SERVICE_ACCOUNT)],
                 env={"OP_SERVICE_ACCOUNT_TOKEN": token})
    assert c["status"] == "PASS", c
    assert token not in json.dumps(c), "doctor leaked the service-account token"


def test_a_codex_toml_mcp_entry_is_discovered_too(tmp_path):
    """Each CLI keeps its registry in a different place and format. A 1Password
    MCP configured only in Codex is still a working path on this machine."""
    mcp = tmp_path / "mcp-strict.sh"
    mcp.write_text(MCP_STRICT)
    mcp.chmod(0o755)
    c = op_check(tmp_path, op_home(tmp_path, mcp_command=mcp, cli="codex"))
    assert c["status"] == "PASS", c
    assert "codex:1password" in c["detail"], c


def test_an_agy_json_mcp_entry_is_discovered_too(tmp_path):
    mcp = tmp_path / "mcp-strict.sh"
    mcp.write_text(MCP_STRICT)
    mcp.chmod(0o755)
    c = op_check(tmp_path, op_home(tmp_path, mcp_command=mcp, cli="agy"))
    assert c["status"] == "PASS", c
    assert "agy:1password" in c["detail"], c


def test_onepassword_runs_under_portable_and_is_not_invasive(tmp_path):
    """Host-independent, and it only reads -- so `portable` must RUN it and no
    skip flag may drop it."""
    c = op_check(tmp_path, op_home(tmp_path),
                 path_dirs=[op_bin(tmp_path, body=OP_NO_ACCOUNT)])
    assert c["status"] == "FAIL", "portable must run this check, not skip it: %r" % c
    assert not c["hardware"], "must not be hardware-gated out of portable"
    assert not c["invasive"], "the check only reads"

    r = run("--json", "--profile", "portable", "--skip-invasive", "--skip-network",
            "--only", OP_CHECK,
            env={"HOME": str(op_home(tmp_path)),
                 "PATH": str(op_bin(tmp_path, body=OP_NO_ACCOUNT)),
                 "OP_SERVICE_ACCOUNT_TOKEN": ""})
    assert checks_of(r)[OP_CHECK]["status"] == "FAIL", "skip flags must not drop it"


# --- the --scan-rc seam the migration script drives ------------------------
#
# scripts/migrate-secrets-to-1password.sh rewrites rc lines based on this
# output. If it disagreed with the check, the two failure modes are a check that
# can never go green and a rewrite of a line that was never a secret.

def test_scan_rc_reports_line_and_name_without_the_value(tmp_path):
    rc = tmp_path / "fixture.bashrc"
    rc.write_text('export EDITOR="vim"\n'
                  f'export GOOGLE_PLACES_API_KEY="{FAKE_GOOGLE}"\n')
    r = run("--scan-rc", str(rc))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("2\tGOOGLE_PLACES_API_KEY\t"), r.stdout
    assert FAKE_GOOGLE not in r.stdout


def test_scan_rc_and_the_check_agree_on_what_is_a_secret(tmp_path):
    """One detector, not two."""
    body = REFERENCE_RC + f'export EXPENSES_DB_PASSWORD="{FAKE_PASSWORD}"\n'
    home = rc_home(tmp_path, __bashrc=body)
    c, _ = rc_check(tmp_path, home)
    scanned = [l.split("\t")[1]
               for l in run("--scan-rc", str(home / ".bashrc")).stdout.splitlines() if l]
    assert scanned == ["EXPENSES_DB_PASSWORD"], scanned
    assert c["status"] == "FAIL" and "EXPENSES_DB_PASSWORD" in c["detail"]


def test_scan_rc_exits_2_on_a_missing_file(tmp_path):
    r = run("--scan-rc", str(tmp_path / "nope"))
    assert r.returncode == 2, r.stdout
