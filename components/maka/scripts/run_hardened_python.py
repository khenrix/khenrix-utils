#!/usr/bin/env python3
"""Verify pinned Python, then enter one fixed secret-bearing Maka script."""

from __future__ import annotations

import contextlib
import os
import pathlib
import pwd
import stat
import sys

SCRIPTS_ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_ROOT))

from harden_python_runtime import RuntimeHardeningError, verify_runtime  # noqa: E402
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_ROOT))


def main(arguments: list[str]) -> int:
    entries = {
        "relay-installer": SCRIPTS_ROOT.parent / "interactive" / "install_maka_openai_relay.py",
        "key-export": SCRIPTS_ROOT / "keychain-openai-key.py",
    }
    if not arguments or arguments[0] not in entries:
        print("Unsupported hardened Python entry point.", file=sys.stderr)
        return 64
    account = pwd.getpwuid(os.getuid())
    script = entries[arguments[0]]
    try:
        metadata = script.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
        ):
            raise RuntimeHardeningError("Maka hardened Python entry point is unsafe")
        python = verify_runtime()
    except (OSError, RuntimeHardeningError) as error:
        print(str(error), file=sys.stderr)
        return 78
    environment = {
        "HOME": account.pw_dir,
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }
    os.execve(
        python,
        [str(python), "-I", "-S", "-B", str(script), *arguments[1:]],
        environment,
    )
    return 71


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
