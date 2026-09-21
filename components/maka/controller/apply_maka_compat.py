#!/usr/bin/env python3
"""Apply the lab's hash-pinned Harbor RelayAgent compatibility overlay."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


OVERLAY_ID = "relay-root-virtiofs-v2"
RELAY_PATH = Path("node_modules/@maka/eval/harbor/relay_agent.py")
BASE_SHA256 = "c8654a17bd9ecefac2ce183c3fcc238fc142d9e2a47d703541c9cfc9a29fe3ee"
PATCHED_SHA256 = "8761f73c2940365ca8a5861a9057a62f0ea2de6276b393512e50f72cb66e3bd0"

IMPORTS_OLD = b'''if framework == "harbor":\n    from harbor.agents.base import BaseAgent\nelse:\n    from pier.agents.base import BaseAgent\n'''

IMPORTS_NEW = b'''if framework == "harbor":\n    from harbor.agents.base import BaseAgent\n    from harbor.constants import MAIN_SERVICE_NAME\n    from harbor.environments.docker.docker import DockerEnvironment\n    from harbor.environments.docker.docker_unix import UnixOps\nelse:\n    from pier.agents.base import BaseAgent\n'''

SETUP_OLD = b'''        prepared = await environment.exec(\n            "mkdir -p /logs/agent /logs/artifacts && "\n            f"chown {owner} /logs/agent /logs/artifacts && "\n            "chmod 700 /logs/agent /logs/artifacts",\n            user="root",\n        )\n'''

SETUP_NEW = b'''        owner_change = (\n            ""\n            if owner == "0:0"\n            else f"chown {owner} /logs/agent /logs/artifacts && "\n        )\n        prepared = await environment.exec(\n            "mkdir -p /logs/agent /logs/artifacts && "\n            f"{owner_change}chmod 700 /logs/agent /logs/artifacts && "\n            "test -w /logs/agent && test -w /logs/artifacts",\n            user="root",\n        )\n'''

PERSIST_CALL_OLD = b"await _persist_subject_outputs(environment, result)"
PERSIST_CALL_NEW = b"await _persist_subject_outputs(environment, result, self._subject_owner)"
PERSIST_CALL_COUNT = 4

PERSIST_OLD = b'''async def _persist_subject_outputs(environment: Any, result: Any) -> None:\n    with tempfile.TemporaryDirectory() as directory:\n        root = Path(directory)\n        stdout = root / "stdout"\n        stderr = root / "stderr"\n        stdout.write_text(str(getattr(result, "stdout", "") or ""), encoding="utf-8")\n        stderr.write_text(str(getattr(result, "stderr", "") or ""), encoding="utf-8")\n        prepared = await environment.exec("mkdir -p /logs/artifacts && chmod 700 /logs/artifacts")\n        if prepared.return_code != 0:\n            raise RuntimeError("Maka Eval could not prepare subject artifact output")\n        await environment.upload_file(stdout, SUBJECT_STDOUT_PATH)\n        await environment.upload_file(stderr, SUBJECT_STDERR_PATH)\n'''

PERSIST_NEW = b'''ROOT_ARTIFACT_WRITE_SCRIPT = """set -euo pipefail
target=$1
case "$target" in
  /logs/artifacts/maka-subject.stdout.txt|/logs/artifacts/maka-subject.stderr.txt) ;;
  *) exit 64 ;;
esac
umask 077
temporary=$(mktemp /logs/artifacts/.maka-subject-output.XXXXXX)
trap 'rm -f -- "$temporary"' EXIT
test "$(stat -c %a -- "$temporary")" = 600
cat > "$temporary"
test -f "$temporary" && test ! -L "$temporary"
chmod 600 "$temporary"
mv -fT -- "$temporary" "$target"
trap - EXIT
test -f "$target" && test ! -L "$target" && test -w "$target"
"""


async def _persist_root_harbor_artifact(
    environment: Any, payload: bytes, target: str
) -> None:
    if target not in {SUBJECT_STDOUT_PATH, SUBJECT_STDERR_PATH}:
        raise RuntimeError("Maka Eval refused an unexpected subject artifact path")
    written = await environment._run_docker_compose_command(
        [
            "exec",
            "-T",
            "-u",
            "root",
            MAIN_SERVICE_NAME,
            "bash",
            "-c",
            ROOT_ARTIFACT_WRITE_SCRIPT,
            "maka-artifact-writer",
            target,
        ],
        check=False,
        stdin_data=payload,
    )
    if written.return_code != 0:
        raise RuntimeError("Maka Eval could not persist root subject artifact output")


async def _persist_subject_outputs(
    environment: Any, result: Any, owner: str | None
) -> None:
    stdout_text = str(getattr(result, "stdout", "") or "")
    stderr_text = str(getattr(result, "stderr", "") or "")
    prepared = await environment.exec(
        "mkdir -p /logs/artifacts && chmod 700 /logs/artifacts && "
        "test -w /logs/artifacts"
    )
    if prepared.return_code != 0:
        raise RuntimeError("Maka Eval could not prepare subject artifact output")
    if (
        owner == "0:0"
        and framework == "harbor"
        and type(environment) is DockerEnvironment
        and type(environment._platform) is UnixOps
    ):
        await _persist_root_harbor_artifact(
            environment, stdout_text.encode("utf-8"), SUBJECT_STDOUT_PATH
        )
        await _persist_root_harbor_artifact(
            environment, stderr_text.encode("utf-8"), SUBJECT_STDERR_PATH
        )
        return
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stdout = root / "stdout"
        stderr = root / "stderr"
        stdout.write_text(stdout_text, encoding="utf-8")
        stderr.write_text(stderr_text, encoding="utf-8")
        await environment.upload_file(stdout, SUBJECT_STDOUT_PATH)
        await environment.upload_file(stderr, SUBJECT_STDERR_PATH)
'''

REPLACEMENTS = (
    ("Harbor imports", IMPORTS_OLD, IMPORTS_NEW, 1),
    ("root artifact directory setup", SETUP_OLD, SETUP_NEW, 1),
    (
        "subject artifact persistence calls",
        PERSIST_CALL_OLD,
        PERSIST_CALL_NEW,
        PERSIST_CALL_COUNT,
    ),
    ("subject artifact persistence implementation", PERSIST_OLD, PERSIST_NEW, 1),
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply(package_root: Path) -> Path:
    package_root = package_root.resolve(strict=True)
    target = package_root / RELAY_PATH
    if target.is_symlink() or not target.is_file():
        raise RuntimeError("pinned relay source is not a regular file")
    data = target.read_bytes()
    if digest(data) != BASE_SHA256:
        raise RuntimeError("pinned relay source hash does not match compatibility overlay")
    patched = data
    for label, old, new, expected_count in REPLACEMENTS:
        if patched.count(old) != expected_count:
            raise RuntimeError(
                f"pinned relay source does not contain the exact {label} overlay target"
            )
        patched = patched.replace(old, new, expected_count)
    if digest(patched) != PATCHED_SHA256:
        raise RuntimeError("compatibility overlay produced an unexpected relay source")

    temporary = target.with_name(f".{target.name}.{OVERLAY_ID}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(patched)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    if digest(target.read_bytes()) != PATCHED_SHA256:
        raise RuntimeError("compatibility overlay did not persist exactly")
    return target


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} PACKAGE_ROOT")
    apply(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
