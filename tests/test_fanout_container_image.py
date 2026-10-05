"""Execute the image tool-link block without Docker or provider calls."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


def test_image_python_names_execute_same_interpreter(tmp_path):
    dockerfile = (Path(__file__).resolve().parents[1] /
                  "components/fanout-container/Dockerfile").read_text()
    # Execute the actual linking block; only redirect its output directory.
    block = dockerfile.split("RUN mise install", 1)[1].split("&& test", 1)[0]
    tools = tmp_path / "tools"
    output = tmp_path / "bin"
    tools.mkdir()
    output.mkdir()
    interpreter = str(Path(sys.executable).resolve(strict=True))
    mise = tools / "mise"
    mise.write_text("#!/bin/sh\nif [ \"$1\" = which ]; then printf '%s\\n' "
                    + shlex.quote(interpreter) + "; else exit 1; fi\n")
    mise.chmod(0o700)
    environment = dict(os.environ, PATH=str(tools) + os.pathsep + os.defpath)
    command = "true " + block.replace("/usr/local/bin", str(output))
    subprocess.run(("/bin/sh", "-ec", command), env=environment,
                   check=True, capture_output=True, timeout=5)
    python = output / "python"
    python3 = output / "python3"
    assert python.samefile(python3)
    code = "import json,sys; print(json.dumps([sys.executable, list(sys.version_info[:3])]))"
    observations = [subprocess.run((str(path), "-I", "-B", "-c", code),
                                  check=True, capture_output=True, text=True, timeout=5)
                    for path in (python, python3)]
    for observation in observations:
        executable, version = json.loads(observation.stdout)
        assert Path(executable).samefile(interpreter)
        assert version == list(sys.version_info[:3])
    check = ("from pathlib import Path; import stat, sys; p = Path('README.md'); "
             "expected = b'synthetic input\\nverified marker\\n'; "
             "sys.exit(0 if stat.S_ISREG(p.lstat().st_mode) "
             "and p.read_bytes() == expected else 1)")
    readme = tmp_path / "README.md"
    expected = b"synthetic input\nverified marker\n"

    def run_check():
        return subprocess.run((str(python3), "-I", "-B", "-c", check),
                              cwd=tmp_path, capture_output=True, timeout=5).returncode

    readme.write_bytes(expected)
    assert run_check() == 0
    for invalid in (expected[:-1], b"synthetic input\n", expected + b"extra\n"):
        readme.write_bytes(invalid)
        assert run_check() == 1
    readme.unlink()
    target = tmp_path / "target"
    target.write_bytes(expected)
    readme.symlink_to(target)
    assert run_check() == 1
    readme.unlink()
    readme.mkdir()
    assert run_check() == 1
