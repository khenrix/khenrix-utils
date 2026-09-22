"""Optional Linux host smoke for the hostile `/proc/[pid]/stat` process-name shape.

This is intentionally outside pytest's `test_*.py` collection pattern. Run it on a Linux
host with:

    mise exec -- python tests/forge_proc_host_smoke.py

The ordinary journal suite uses a deterministic synthetic stat file for parser correctness.
This smoke only proves that a real Linux process can produce that hostile shape.
"""
import ast
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if not sys.platform.startswith("linux"):
        raise SystemExit("this optional host smoke requires Linux /proc")

    with tempfile.TemporaryDirectory() as directory:
        link = Path(directory) / "we) ird"
        link.symlink_to(sys.executable)
        prog = (
            "import pathlib, sys;"
            f"sys.path.insert(0, {str(ROOT / 'shared' / 'lib')!r});"
            "from forge import journal;"
            "raw = pathlib.Path('/proc/self/stat').read_text();"
            "naive = raw.split()[2:];"
            "print(raw[raw.index('('):raw.rindex(')') + 1]);"
            "print(journal._read_proc_process_start());"
            "print(naive[19] if len(naive) > 19 else 'short')"
        )
        result = subprocess.run([str(link), "-c", prog], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        comm, parsed, naive = result.stdout.splitlines()
        assert comm == "(we) ird)", comm
        value, source = ast.literal_eval(parsed)
        assert source == "proc" and value.isdigit(), parsed
        assert naive != value, "the naive split happened to agree, so this proves nothing"


if __name__ == "__main__":
    main()
