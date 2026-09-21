#!/usr/bin/env python3
"""Positive and negative tests for the pinned RelayAgent compatibility overlay."""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import apply_maka_compat as compat


class FakeEnvironment:
    def __init__(
        self,
        owner: str,
        preparation_code: int = 0,
        artifact_preparation_code: int = 0,
        compose_codes: list[int] | None = None,
    ):
        self.owner = owner
        self.preparation_code = preparation_code
        self.artifact_preparation_code = artifact_preparation_code
        self.compose_codes = list(compose_codes or [])
        self.calls: list[tuple[str, str | None]] = []
        self.compose_calls: list[tuple[list[str], bool, bytes | None]] = []
        self.uploads: list[tuple[str, bytes]] = []
        self._platform = FakeUnixOps()

    async def exec(self, command: str, user: str | None = None):
        self.calls.append((command, user))
        if command.startswith('printf "%s:%s"'):
            return SimpleNamespace(return_code=0, stdout=self.owner)
        if "mkdir -p /logs/agent /logs/artifacts" in command:
            return SimpleNamespace(return_code=self.preparation_code, stdout="")
        if command.startswith("mkdir -p /logs/artifacts"):
            return SimpleNamespace(return_code=self.artifact_preparation_code, stdout="")
        return SimpleNamespace(return_code=0, stdout="")

    async def upload_file(self, source_path: Path | str, target_path: str):
        self.uploads.append((target_path, Path(source_path).read_bytes()))

    async def _run_docker_compose_command(
        self,
        command: list[str],
        check: bool = True,
        stdin_data: bytes | None = None,
    ):
        self.compose_calls.append((command, check, stdin_data))
        code = self.compose_codes.pop(0) if self.compose_codes else 0
        return SimpleNamespace(return_code=code, stdout="", stderr="")


class FakeUnixOps:
    pass


class ForeignPlatform:
    pass


def stage_and_apply(source_package: Path, destination: Path) -> Path:
    source = source_package / compat.RELAY_PATH
    target = destination / compat.RELAY_PATH
    target.parent.mkdir(parents=True)
    shutil.copyfile(source, target)
    shutil.copyfile(source.parent / "eval_framework.py", target.parent / "eval_framework.py")
    compat.apply(destination)
    assert compat.digest(target.read_bytes()) == compat.PATCHED_SHA256
    return target


async def exercise_relay(relay_module) -> None:
    relay_module.DockerEnvironment = FakeEnvironment
    relay_module.UnixOps = FakeUnixOps

    root_agent = object.__new__(relay_module.RelayAgent)
    root_agent._subject_owner = None
    root = FakeEnvironment("0:0")
    await root_agent.setup(root)
    root_prepare = root.calls[1]
    assert root_prepare[1] == "root"
    assert "chown" not in root_prepare[0]
    assert "chmod 700 /logs/agent /logs/artifacts" in root_prepare[0]
    assert "test -w /logs/agent && test -w /logs/artifacts" in root_prepare[0]
    assert root_agent._subject_owner == "0:0"

    result = SimpleNamespace(
        stdout="root stdout: 'quotes', $shell, ☃\n\x00tail",
        stderr="root stderr: `literal`\n",
    )
    await relay_module._persist_subject_outputs(root, result, root_agent._subject_owner)
    assert root.uploads == []
    assert len(root.compose_calls) == 2
    expected = [
        (relay_module.SUBJECT_STDOUT_PATH, result.stdout.encode("utf-8")),
        (relay_module.SUBJECT_STDERR_PATH, result.stderr.encode("utf-8")),
    ]
    for (command, check, stdin_data), (target, payload) in zip(
        root.compose_calls, expected, strict=True
    ):
        assert command == [
            "exec",
            "-T",
            "-u",
            "root",
            "main",
            "bash",
            "-c",
            relay_module.ROOT_ARTIFACT_WRITE_SCRIPT,
            "maka-artifact-writer",
            target,
        ]
        assert check is False
        assert stdin_data == payload
        assert result.stdout not in " ".join(command)
        assert result.stderr not in " ".join(command)
    assert any(
        "chmod 700 /logs/artifacts" in command
        and "test -w /logs/artifacts" in command
        for command, _user in root.calls
    )

    user_agent = object.__new__(relay_module.RelayAgent)
    user_agent._subject_owner = None
    user = FakeEnvironment("1000:1001")
    await user_agent.setup(user)
    assert "chown 1000:1001 /logs/agent /logs/artifacts" in user.calls[1][0]
    assert user_agent._subject_owner == "1000:1001"
    await relay_module._persist_subject_outputs(user, result, user_agent._subject_owner)
    assert user.compose_calls == []
    assert user.uploads == expected

    foreign = FakeEnvironment("0:0")
    foreign._platform = ForeignPlatform()
    await relay_module._persist_subject_outputs(foreign, result, "0:0")
    assert foreign.compose_calls == []
    assert foreign.uploads == expected

    failed_agent = object.__new__(relay_module.RelayAgent)
    failed_agent._subject_owner = None
    failed = FakeEnvironment("0:0", preparation_code=1)
    try:
        await failed_agent.setup(failed)
    except RuntimeError as error:
        assert str(error) == "Maka Eval could not prepare task-owned artifact directories"
    else:
        raise AssertionError("RelayAgent accepted an unwritable root artifact directory")

    failed_artifact_prepare = FakeEnvironment("0:0", artifact_preparation_code=1)
    try:
        await relay_module._persist_subject_outputs(failed_artifact_prepare, result, "0:0")
    except RuntimeError as error:
        assert str(error) == "Maka Eval could not prepare subject artifact output"
    else:
        raise AssertionError("RelayAgent accepted an unwritable subject artifact directory")

    failed_artifact_write = FakeEnvironment("0:0", compose_codes=[1])
    try:
        await relay_module._persist_subject_outputs(failed_artifact_write, result, "0:0")
    except RuntimeError as error:
        assert str(error) == "Maka Eval could not persist root subject artifact output"
    else:
        raise AssertionError("RelayAgent accepted a failed root artifact stream")

    try:
        await relay_module._persist_root_harbor_artifact(
            root, b"payload", "/logs/artifacts/unexpected"
        )
    except RuntimeError as error:
        assert str(error) == "Maka Eval refused an unexpected subject artifact path"
    else:
        raise AssertionError("RelayAgent accepted an unexpected root artifact path")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} PACKAGE_ROOT")
    package_root = Path(sys.argv[1]).resolve(strict=True)
    assert importlib.metadata.version("harbor") == "0.20.0"
    from harbor.environments.docker.docker import DockerEnvironment

    assert tuple(
        inspect.signature(DockerEnvironment._run_docker_compose_command).parameters
    ) == (
        "self",
        "command",
        "check",
        "timeout_sec",
        "stdin_data",
        "on_output",
    )

    with tempfile.TemporaryDirectory() as temporary:
        test_root = Path(temporary)
        positive = test_root / "positive"
        patched = stage_and_apply(package_root, positive)

        negative = test_root / "negative"
        negative_target = negative / compat.RELAY_PATH
        negative_target.parent.mkdir(parents=True)
        base = (package_root / compat.RELAY_PATH).read_bytes()
        negative_data = base + b"\n# unexpected mutation\n"
        negative_target.write_bytes(negative_data)
        try:
            compat.apply(negative)
        except RuntimeError as error:
            assert "hash does not match" in str(error)
        else:
            raise AssertionError("overlay accepted an unpinned relay source")
        assert negative_target.read_bytes() == negative_data

        relay_root = patched.parent
        sys.path.insert(0, str(relay_root))
        try:
            eval_framework = importlib.import_module("eval_framework")
            eval_framework.install("harbor")
            relay_module = importlib.import_module("relay_agent")
            asyncio.run(exercise_relay(relay_module))
        finally:
            sys.path.remove(str(relay_root))


if __name__ == "__main__":
    main()
