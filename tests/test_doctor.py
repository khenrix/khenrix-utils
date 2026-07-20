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

def test_json_output_is_parseable():
    r = run("--json", "--profile", "portable")
    json.loads(r.stdout)


def test_every_check_has_a_status():
    data = json.loads(run("--json", "--profile", "portable").stdout)
    assert data["checks"], "no checks ran"
    for c in data["checks"]:
        assert c["status"] in ("PASS", "FAIL", "SKIP"), c
        assert c["name"] and c["detail"]


def test_portable_profile_skips_not_fails_hardware_checks():
    data = json.loads(run("--json", "--profile", "portable").stdout)
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

def test_mcp_check_rejects_an_initialize_only_response(tmp_path):
    """The initialize reply advertises `"tools":{"listChanged":true}` in its
    capabilities. A substring search for '"tools"' therefore passes even when
    tools/list never answered -- this test pins the parsed behaviour."""
    ps = r"""#!/bin/sh
cat > /dev/null
echo '{"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{"listChanged":true}},"serverInfo":{"name":"chrome_devtools"}},"jsonrpc":"2.0","id":1}'
exit 0
"""
    home = fake_home(tmp_path, ps)
    r = run("--json", "--profile", "full", "--only", "mcp-chrome-devtools",
            env={"HOME": str(home), "DOCTOR_NPX": str(home / ".local" / "bin" / "powershell.exe")})
    c = checks_of(r)["mcp-chrome-devtools"]
    assert c["status"] == "FAIL", c


def test_mcp_check_passes_on_a_non_empty_tools_list(tmp_path):
    ps = r"""#!/bin/sh
cat > /dev/null
echo '{"result":{"capabilities":{"tools":{}}},"jsonrpc":"2.0","id":1}'
echo '{"result":{"tools":[{"name":"click"},{"name":"navigate_page"}]},"jsonrpc":"2.0","id":2}'
exit 0
"""
    home = fake_home(tmp_path, ps)
    r = run("--json", "--profile", "full", "--only", "mcp-chrome-devtools",
            env={"HOME": str(home), "DOCTOR_NPX": str(home / ".local" / "bin" / "powershell.exe")})
    c = checks_of(r)["mcp-chrome-devtools"]
    assert c["status"] == "PASS", c
    assert "2" in c["detail"]


def test_mcp_check_fails_on_an_empty_tools_list(tmp_path):
    ps = r"""#!/bin/sh
cat > /dev/null
echo '{"result":{"tools":[]},"jsonrpc":"2.0","id":2}'
exit 0
"""
    home = fake_home(tmp_path, ps)
    r = run("--json", "--profile", "full", "--only", "mcp-chrome-devtools",
            env={"HOME": str(home), "DOCTOR_NPX": str(home / ".local" / "bin" / "powershell.exe")})
    c = checks_of(r)["mcp-chrome-devtools"]
    assert c["status"] == "FAIL", c


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
