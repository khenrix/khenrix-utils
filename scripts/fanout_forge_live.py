"""Bind profiled fanout provider turns to a sealed Forge fixture seat.

Use ``ForgeLiveRegistry(case_id, seat_workspace, boundary=sealed_descriptor)``
with ``fanout.run_provider`` or ``fanout.run_many``. Every attempted provider
command passes through the fixture's native sandbox preflight before the
fanout process runner sees it.
"""
from __future__ import annotations

import sys
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.lib import fanout  # noqa: E402
from scripts import fanout_forge_fixtures as fixtures  # noqa: E402


_VERSION_LINES = {
    "claude": rb"([0-9]+\.[0-9]+\.[0-9]+) \(Claude Code\)",
    "codex": rb"codex-cli ([0-9]+\.[0-9]+\.[0-9]+)",
    "agy": rb"([0-9]+\.[0-9]+\.[0-9]+)",
}


class _ForgeBoundary:
    def __init__(self, case_id: str, seat_workspace: Path | str,
                 boundary: fixtures.ForgeBoundaryDescriptor | None) -> None:
        if not isinstance(boundary, fixtures.ForgeBoundaryDescriptor):
            raise fixtures.FixtureError("Forge boundary descriptor is required")
        workspace = Path(seat_workspace)
        try:
            canonical = workspace.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise fanout.ProviderRequestError("Forge seat workspace is unavailable") from error
        if not workspace.is_absolute() or workspace != canonical or not workspace.is_dir():
            raise fanout.ProviderRequestError("Forge seat workspace must be a canonical directory")
        self.case_id = case_id
        self.workspace = workspace
        self.boundary = boundary

    def wrap(self, request: fanout.ProviderRequest,
             command: fanout.ProcessCommand) -> fanout.ProcessCommand:
        if Path(request.cwd) != self.workspace or command.cwd != self.workspace:
            raise fanout.ProviderRequestError("provider cwd differs from Forge seat workspace")
        if command.environment:
            raise fixtures.FixtureError("Forge provider credential environment is not isolated")
        fixtures.require_blindness_attestation(
            boundary=self.boundary, seat_workspace=self.workspace,
        )
        binaries = dict(self.boundary.executable_paths)
        if request.executor_id not in binaries:
            raise fixtures.FixtureError("Forge executable is absent from the sealed descriptor")
        prepared = [str(binaries[request.executor_id]), *command.argv[1:]]
        argv = fixtures.executor_argv(
            self.case_id, self.workspace, prepared, boundary=self.boundary,
        )
        if (len(argv) != len(command.argv) + 3
                or argv[0:2] != ["/usr/bin/sandbox-exec", "-p"]
                or not isinstance(argv[2], str) or not argv[2]
                or argv[3:] != prepared):
            raise fanout.ProviderRequestError("Forge sandbox did not wrap the provider command")
        environment = fixtures.seat_environment(
            self.boundary, self.workspace, request.executor_id,
        )
        return replace(command, argv=argv, environment=environment)


def _sealed_version_probe(boundary: _ForgeBoundary, executor_id: str) -> str:
    fixtures.require_blindness_attestation(
        boundary=boundary.boundary, seat_workspace=boundary.workspace,
    )
    binary = dict(boundary.boundary.executable_paths).get(executor_id)
    pattern = _VERSION_LINES.get(executor_id)
    if binary is None or pattern is None:
        raise fixtures.FixtureError("Forge executable is absent from the sealed descriptor")
    prepared = [str(binary), "--version"]
    argv = fixtures.executor_argv(
        boundary.case_id, boundary.workspace, prepared, boundary=boundary.boundary,
    )
    if (len(argv) != len(prepared) + 3 or argv[:2] != ["/usr/bin/sandbox-exec", "-p"]
            or not isinstance(argv[2], str) or not argv[2] or argv[3:] != prepared):
        raise fixtures.FixtureError("Forge sandbox did not wrap the pinned version probe")
    environment = fanout.process.build_child_environment(overrides=
        fixtures.seat_environment(boundary.boundary, boundary.workspace, executor_id))
    result = subprocess.run(argv, cwd=boundary.workspace, env=environment,
                            capture_output=True, timeout=10, check=False)
    if result.returncode != 0:
        raise ValueError("Forge sandboxed CLI --version failed")
    match = re.fullmatch(pattern, result.stdout.strip())
    if match is None or re.search(
        rb"(?<![0-9])[0-9]+\.[0-9]+\.[0-9]+(?![0-9])", result.stderr,
    ):
        raise ValueError("Forge sandboxed CLI --version format changed")
    return match.group(1).decode("ascii")


class _ForgeBoundAdapter:
    def __init__(self, boundary: _ForgeBoundary) -> None:
        self._forge_boundary = boundary

    def build_command(self, request: fanout.ProviderRequest) -> fanout.ProcessCommand:
        command = super().build_command(request)
        return self._forge_boundary.wrap(request, command)


class _ForgeClaudeAdapter(_ForgeBoundAdapter, fanout.ClaudeAdapter):
    pass


class _ForgeCodexAdapter(_ForgeBoundAdapter, fanout.CodexAdapter):
    pass


class _ForgeAgyAdapter(_ForgeBoundAdapter, fanout.AgyAdapter):
    pass


class ForgeLiveRegistry(fanout.ProviderRegistry):
    """Default pinned profiles with seat-bound adapters at the launch point."""

    def __init__(self, case_id: str, seat_workspace: Path | str, *,
                 boundary: fixtures.ForgeBoundaryDescriptor | None = None,
                 version_probe: Callable[[str], str] | None = None) -> None:
        if version_probe is not None:
            raise fixtures.FixtureError("Forge custom version probe callback is not isolated")
        seat_boundary = _ForgeBoundary(case_id, seat_workspace, boundary)
        defaults = fanout.ProviderRegistry.default()
        # The sealed legacy Forge bridge characterizes its own write transport.
        # The public fanout registry remains closed until native write certification.
        profiles = (
            replace(profile, characterized=True, profile_sha256=None)
            if profile.execution_class == "repo-write" else profile
            for profile in defaults._profiles.values()
        )
        super().__init__(profiles, version_probe=lambda executor:
                         _sealed_version_probe(seat_boundary, executor))
        self._seat_boundary = seat_boundary
        self._forge_adapters = {
            "claude": _ForgeClaudeAdapter(seat_boundary),
            "codex": _ForgeCodexAdapter(seat_boundary),
            "agy": _ForgeAgyAdapter(seat_boundary),
        }

    def require(self, executor_id: str) -> fanout.ProviderAdapter:
        super().require(executor_id)
        return self._forge_adapters[executor_id]

    def assert_installed_version(self, profile: fanout.ExecutorProfile) -> None:
        if self.select(*profile.key).digest != profile.digest:
            raise fanout.ProviderRequestError("executor profile drifted before provider launch")
        try:
            observed = self._version_probe(profile.executor_id)
        except fixtures.FixtureError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise fanout.ProviderRequestError("installed CLI version is unavailable") from error
        if observed != profile.cli_version:
            raise fanout.ProviderRequestError("CLI version drifted from the pinned executor profile")
