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
