"""Native evaluator bridge using role-bound profiles and shared supervision."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ProcessValidationError, ProviderProtocolError
from .process import ProcessCommand, run_command
from .providers import (AgyAdapter, ClaudeAdapter, CodexAdapter, ExecutorProfile,
                        ProviderRegistry, ProviderRequest, UsageEvidence, aggregate_usage_evidence)

MODES = ("normal", "deep")
ADAPTERS = {"claude": ClaudeAdapter(), "codex": CodexAdapter(), "agy": AgyAdapter()}
_NATIVE_CONFIG_ENV = {
    "claude": {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID",
               "CLOUD_ML_REGION", "ANTHROPIC_BASE_URL", "GOOGLE_APPLICATION_CREDENTIALS", "AGY_ADC_AUTH"},
    "codex": {"CODEX_HOME", "OPENAI_BASE_URL"},
    "agy": {"GOOGLE_APPLICATION_CREDENTIALS", "AGY_ADC_AUTH", "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_REGION"},
}



@dataclass
class PromptSpec:
    name: str
    argv: list[str]
    stdin: bytes = field(repr=False)
    model: str
    thinking: str | None
    cwd: str = "."
    role: str | None = None
    profile: ExecutorProfile | None = field(default=None, repr=False)
    registry: ProviderRegistry | None = field(default=None, repr=False)
    native_request: ProviderRequest | None = field(default=None, repr=False)
    _original_native_request: ProviderRequest | None = field(default=None, init=False, repr=False)
    _native_config: tuple[tuple[str, str | None], ...] = field(default=(), init=False, repr=False)

    def __post_init__(self) -> None:
        self._original_native_request = self.native_request
        if self.native_request is not None:
            names = {"HOME", "PATH", "XDG_CONFIG_HOME", "XDG_DATA_HOME"} | _NATIVE_CONFIG_ENV[self.name]
            self._native_config = tuple((name, os.environ.get(name)) for name in sorted(names))


def build_native_spec(name: str, prompt: str, cfg: dict | None = None, *,
                      cwd: Path | str = ".", registry: ProviderRegistry | None = None,
                      session_id: str | None = None, resume: bool = False,
                      role: str = "evaluator") -> PromptSpec:
    """Use a shared native read role, including installed reads and auth."""
    from dataclasses import replace
    from .providers import _default_argv

    selected = registry or ProviderRegistry.default()
    if role not in {"executor", "evaluator"}:
        raise ProcessValidationError("native evaluator bridge requires an executor or evaluator read role")
    profile = selected.admit(name, "read-only", "standard", role=role)
    if cfg is not None:
        model = cfg[name]["model"]
        effort = cfg[name].get("thinking") or profile.requested_effort
        if effort != profile.requested_effort:
            raise RuntimeError("native evaluator effort differs from its role")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@ ()-]{0,127}", model):
            raise RuntimeError("eval model must be an explicit model identifier")
        initial_argv, resume_argv = _default_argv(name, "read-only", model, effort)
        profile = replace(profile, requested_model=model, initial_argv=initial_argv,
                          resume_argv=resume_argv, profile_sha256=None)
    if name == "claude" and not resume and session_id is None:
        session_id = str(uuid.uuid4())
    request = ProviderRequest(name, prompt, cwd=cwd, profile=profile, role=role,
                              session_id=session_id, resume=resume)
    command = profile.adapter.build_command(request)
    return PromptSpec(name, list(command.argv), command.stdin, profile.requested_model,
                      profile.requested_effort, str(cwd), role, profile, selected, request)


def resolve_mode_config(args) -> dict:
    registry = ProviderRegistry.default()
    tier = "deep" if args.mode == "deep" else "standard"
    return {name: {"model": getattr(args, f"model_{name}", None) or
                         registry.select(name, "read-only", tier).requested_model,
                   "model_explicit": getattr(args, f"model_{name}", None) is not None,
                   "thinking": registry.select(name, "read-only", tier).requested_effort}
            for name in ADAPTERS}


def effective_timeout(args) -> int | float:
    if args.timeout is not None:
        return args.timeout
    tier = "deep" if args.mode == "deep" else "standard"
    return ProviderRegistry.default().select("claude", "read-only", tier).default_timeout


def run(spec: PromptSpec, workdir: Path, *, timeout: int, retries: int,
        environment: dict, real_home: Path) -> dict:
    if type(retries) is not int or retries != 0:
        raise RuntimeError("native evaluator automatic retries must be zero")
    request = spec.native_request
    if (not isinstance(request, ProviderRequest) or spec.profile is None
            or request is not spec._original_native_request
            or spec.registry is None or spec.role not in {"executor", "evaluator"}
            or request.role != spec.role or request.profile is None
            or request.execution_class != "read-only"
            or spec.profile.execution_class != "read-only"
            or spec.profile.role != request.role
            or spec.profile.digest != request.profile.digest
            or spec.name != request.executor_id
            or spec.model != request.profile.requested_model
            or spec.thinking != request.profile.requested_effort
            or Path(spec.cwd) != Path(request.cwd)):
        raise ProcessValidationError("native evaluator lacks a role-bound profile")
    if any(environment.get(name) != value or os.environ.get(name) != value
           for name, value in spec._native_config):
        raise ProcessValidationError("native evaluator configuration environment differs from its build")
    if request.resume and request.profile.native_roots is None:
        raise ProcessValidationError(
            "native evaluator resume lacks historical discovery roots; use fresh admission or historical inspection")
    expected = request.profile.adapter.build_command(request)
    if list(spec.argv) != list(expected.argv) or spec.stdin != expected.stdin:
        raise ProcessValidationError("native evaluator command differs from its profile")
    observed_version = spec.registry._version_probe(spec.name)
    spec.registry._contract_probe(spec.profile)
    command = ProcessCommand(spec.argv, spec.stdin, cwd=spec.cwd, timeout=timeout,
                             max_output_bytes=8 * 1024 * 1024, trusted_native=True,
                             environment=expected.environment, native_roots=expected.native_roots)
    started = time.monotonic()
    result = run_command(command, base_environment=environment)
    stdout = workdir / f"{spec.name}.stdout.attempt-1.txt"
    stderr = workdir / f"{spec.name}.stderr.attempt-1.txt"
    stdout.write_bytes(result.stdout)
    stderr.write_bytes(result.stderr)
    parsed = None
    if result.stdout:
        try:
            parsed = ADAPTERS[spec.name].parse(result.stdout)
        except ProviderProtocolError:
            pass
    from .providers import _make_result
    native_result = _make_result(request, request.profile.adapter, result, parsed,
                                 1, time.monotonic() - started,
                                 observed_cli_version=observed_version)
    answer_file = workdir / f"{spec.name}.result.txt"
    answer_file.write_text(native_result.answer)
    record = {"name": spec.name, "model": spec.model, "thinking": spec.thinking,
              "valid": native_result.valid, "reason": native_result.reason,
              "hint": native_result.hint,
              "attempts": 1, "duration_sec": round(time.monotonic() - started, 4),
              "result_file": str(answer_file), "stdout_file": str(stdout), "stderr_file": str(stderr),
              "usage": dict(native_result.usage) if native_result.usage is not None else None,
              "usage_evidence": native_result.usage_evidence.to_dict(),
              "observed_model": native_result.observed_model,
              "session_id": native_result.session_id}
    from .diagnostic import describe_native_launch
    record["launch_metadata"] = describe_native_launch(
        spec.profile, command, observed_cli_version=observed_version,
        observed_tools=parsed.observed_tools if parsed else (),
    )
    (workdir / "manifest.json").write_text(json.dumps({"providers": [record]}, indent=2) + "\n")
    return record
