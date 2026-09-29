"""Resumable, structured adapters for the admitted fanout executors.

The module deliberately owns only provider transport and parsing.  Scheduling,
durable run state, and peer-round policy live in later layers.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from .artifacts import ArtifactRef, ArtifactStore, canonical_json
from .agy_guard import AgyReadOnlyGuard, validate_agy_readonly_guard
from .errors import (
    ArtifactError,
    ProviderProtocolError,
    ProviderRequestError,
    UnsupportedExecutorError,
)
from .process import (
    DEFAULT_EXECUTOR_SLOTS, ProcessCommand, ProcessResult, ProcessStatus,
    build_child_environment, default_claude_adc_path, default_claude_vertex_route,
    run_command,
)


_READ_ONLY = "read-only"
_REPO_WRITE = "repo-write"
_EXECUTION_CLASSES = frozenset({_READ_ONLY, _REPO_WRITE})
_CLAUDE_READ_ONLY_FLAGS = (
    "--restricted", "--strict-mcp-config", "--tools", "Read,Glob,Grep",
    "--permission-mode", "plan", "--permission-prompts", "none",
    "--disallowedTools", "ExitPlanMode",
)
_CODEX_READ_ONLY_FLAGS = (
    "--ignore-user-config", "--disable", "apps", "--disable", "plugins",
    "--disable", "remote_plugin", "--disable", "hooks",
    "-c", 'cli_auth_credentials_store="keyring"',
)
_CODEX_PROJECT_TRUST = "{codex_project_trust}"
_CODEX_HOST_PLATFORM = sys.platform
_CODEX_SYSTEM_CONFIG_PATHS = tuple(
    Path("/etc/codex") / name for name in (
        "config.toml", "managed_config.toml", "requirements.toml",
    )
)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Capabilities that must be present for a provider to be an executor."""

    assigned_session_id: bool
    explicit_resume: bool
    structured_output: bool
    stdin_prompt: bool = True


@dataclass(frozen=True, slots=True)
class ExecutorProfile:
    """One exact executor/class/tier invocation contract."""

    executor_id: str
    adapter: "ProviderAdapter"
    capabilities: ProviderCapabilities
    execution_class: str
    quality_tier: str
    requested_model: str
    requested_effort: str
    cli_version: str
    initial_argv: tuple[str, ...]
    resume_argv: tuple[str, ...]
    default_timeout: int | float
    timeout_ceiling: int | float
    characterized: bool = True
    profile_sha256: str | None = None

    def __post_init__(self) -> None:
        profile_binding_key(self.executor_id, self.execution_class, self.quality_tier)
        derived = self.digest
        if self.profile_sha256 is not None and self.profile_sha256 != derived:
            raise ProviderRequestError("executor profile digest differs from its descriptor")
        object.__setattr__(self, "profile_sha256", derived)

    @property
    def key(self) -> tuple[str, str, str]:
        return self.executor_id, self.execution_class, self.quality_tier

    @property
    def binding_key(self) -> str:
        return profile_binding_key(*self.key)

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()

    def to_dict(self) -> dict[str, object]:
        """Return the exact descriptor committed by this profile's digest."""
        return {
            "adapter_class": f"{type(self.adapter).__module__}.{type(self.adapter).__qualname__}",
            "capabilities": {
                "assigned_session_id": self.capabilities.assigned_session_id,
                "explicit_resume": self.capabilities.explicit_resume,
                "stdin_prompt": self.capabilities.stdin_prompt,
                "structured_output": self.capabilities.structured_output,
            },
            "executor_id": self.executor_id,
            "execution_class": self.execution_class,
            "quality_tier": self.quality_tier,
            "requested_model": self.requested_model,
            "requested_effort": self.requested_effort,
            "cli_version": self.cli_version,
            "initial_argv": list(self.initial_argv),
            "resume_argv": list(self.resume_argv),
            "default_timeout": self.default_timeout,
            "timeout_ceiling": self.timeout_ceiling,
            "characterized": self.characterized,
            "schema_version": "fanout-executor-profile-v2",
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object], *, adapter: "ProviderAdapter") -> "ExecutorProfile":
        """Rebind a stored descriptor to one explicitly admitted adapter."""
        fields = {
            "adapter_class", "capabilities", "executor_id", "execution_class",
            "quality_tier", "requested_model", "requested_effort", "cli_version",
            "initial_argv", "resume_argv", "default_timeout", "timeout_ceiling",
            "characterized", "schema_version",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ProviderRequestError("executor profile descriptor fields are invalid")
        capabilities = value["capabilities"]
        if not isinstance(capabilities, Mapping) or set(capabilities) != {
            "assigned_session_id", "explicit_resume", "stdin_prompt", "structured_output",
        } or any(type(item) is not bool for item in capabilities.values()):
            raise ProviderRequestError("executor profile capabilities are invalid")
        initial, resume = value["initial_argv"], value["resume_argv"]
        if (not isinstance(initial, list) or not isinstance(resume, list)
                or any(not isinstance(item, str) for item in initial + resume)):
            raise ProviderRequestError("executor profile argv is invalid")
        try:
            profile = cls(
                executor_id=value["executor_id"], adapter=adapter,
                capabilities=ProviderCapabilities(**capabilities),
                execution_class=value["execution_class"],
                quality_tier=value["quality_tier"],
                requested_model=value["requested_model"],
                requested_effort=value["requested_effort"],
                cli_version=value["cli_version"],
                initial_argv=tuple(initial), resume_argv=tuple(resume),
                default_timeout=value["default_timeout"],
                timeout_ceiling=value["timeout_ceiling"],
                characterized=value["characterized"],
            )
        except (TypeError, ValueError, ProviderRequestError) as error:
            raise ProviderRequestError("executor profile descriptor is invalid") from error
        if profile.to_dict() != value:
            raise ProviderRequestError("executor profile descriptor changed identity")
        return profile


def profile_binding_key(executor_id: str, execution_class: str, quality_tier: str) -> str:
    """Reversible string key for the triple in JSON run-input maps."""
    if (
        not isinstance(executor_id, str) or not executor_id or "\x00" in executor_id
        or execution_class not in _EXECUTION_CLASSES
        or quality_tier not in {"standard", "deep"}
        or (execution_class, quality_tier) == (_REPO_WRITE, "deep")
    ):
        raise ProviderRequestError("profile key is invalid")
    if executor_id == "maka":
        raise UnsupportedExecutorError("maka is a routing/evaluation target, not an executor")
    return f"{executor_id}/{execution_class}/{quality_tier}"


def parse_profile_binding_key(value: str) -> tuple[str, str, str]:
    if not isinstance(value, str):
        raise ProviderRequestError("profile key is invalid")
    parts = value.rsplit("/", 2)
    if len(parts) != 3 or profile_binding_key(*parts) != value:
        raise ProviderRequestError("profile key is invalid")
    return parts[0], parts[1], parts[2]


def profile_binding_changed(old: Mapping[str, str], new: Mapping[str, str],
                            executor_id: str, execution_class: str, quality_tier: str,
                            *, old_shape: str, new_shape: str) -> bool:
    """Compare one selected triple across explicit old/new run-input shapes."""
    if old_shape not in {"flat", "class-tier"} or new_shape not in {"flat", "class-tier"}:
        raise ProviderRequestError("profile binding shape is unsupported")
    key = profile_binding_key(executor_id, execution_class, quality_tier)
    return old.get(key if old_shape == "class-tier" else executor_id) != new.get(
        key if new_shape == "class-tier" else executor_id
    )


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """One isolated provider turn; text is retained only as stdin bytes."""

    executor_id: str
    prompt: str | bytes = field(repr=False)
    cwd: Path | str = "."
    execution_class: str = _READ_ONLY
    session_id: str | None = None
    resume: bool = False
    timeout: float | None = None
    timeout_override_reason: str | None = None
    timeout_override_review_sha256: str | None = None
    retries: int = 0
    profile: ExecutorProfile | None = field(default=None, repr=False, compare=False)
    artifact_store: ArtifactStore | None = field(default=None, repr=False, compare=False)
    artifact_prefix: str = "providers"
    slot_cap: int = DEFAULT_EXECUTOR_SLOTS
    slot_timeout: float = 30.0
    slot_root: Path | str | None = None
    context_sha256: str = ""
    skill_bundle_sha256: str = ""
    staged_skill_root: Path | str | None = None
    skill_delivery_sha256: str = ""
    skill_delivery_ref: ArtifactRef | None = None
    agy_guard: AgyReadOnlyGuard | None = field(default=None, repr=False, compare=False)
    run_id: str | None = None
    task_id: str | None = None
    target_id: str | None = None
    inputs_digest: str | None = None
    seat_id: str | None = None
    native_boundary: object | None = field(default=None, repr=False, compare=False)
    native_controller: object | None = field(default=None, repr=False, compare=False)
    native_verification: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.executor_id, str) or not self.executor_id or "\x00" in self.executor_id:
            raise ProviderRequestError("executor_id must be a non-empty string")
        if not isinstance(self.prompt, (str, bytes)):
            raise ProviderRequestError("prompt must be text or bytes")
        if self.execution_class not in _EXECUTION_CLASSES:
            raise ProviderRequestError("execution_class must be read-only or repo-write")
        if self.session_id is not None and (
            not isinstance(self.session_id, str) or not self.session_id or "\x00" in self.session_id
        ):
            raise ProviderRequestError("session_id must be a non-empty string when supplied")
        if not isinstance(self.resume, bool):
            raise ProviderRequestError("resume must be a boolean")
        if self.resume and self.session_id is None:
            raise ProviderRequestError("resume requires an exact session_id")
        if isinstance(self.retries, bool) or not isinstance(self.retries, int) or self.retries < 0:
            raise ProviderRequestError("retries must be a non-negative integer")
        if self.timeout is not None and (
            not isinstance(self.timeout, (int, float)) or isinstance(self.timeout, bool)
            or not math.isfinite(self.timeout) or not 0 < self.timeout <= 3600
        ):
            raise ProviderRequestError("timeout must be finite, positive, and at most 3600 seconds")
        if self.profile is not None and (
            not isinstance(self.profile, ExecutorProfile)
            or self.profile.executor_id != self.executor_id
            or self.profile.execution_class != self.execution_class
        ):
            raise ProviderRequestError("request profile differs from executor or execution class")
        if (self.timeout_override_reason is None) != (self.timeout_override_review_sha256 is None):
            raise ProviderRequestError("timeout override requires reason and review evidence")
        if self.timeout_override_reason is not None and (
            not isinstance(self.timeout_override_reason, str)
            or not self.timeout_override_reason.strip()
            or not isinstance(self.timeout_override_review_sha256, str)
            or len(self.timeout_override_review_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.timeout_override_review_sha256)
        ):
            raise ProviderRequestError("timeout override review evidence is invalid")
        if (self.profile is not None and self.timeout is not None
                and self.timeout > self.profile.timeout_ceiling
                and self.timeout_override_reason is None):
            raise ProviderRequestError("over-ceiling timeout needs a reviewed override")
        if not Path(self.cwd).is_dir():
            raise ProviderRequestError("cwd must name an existing directory")
        for name in ("context_sha256", "skill_bundle_sha256", "skill_delivery_sha256"):
            value = getattr(self, name)
            if value and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
                raise ProviderRequestError(f"{name} must be an empty value or lower-case SHA-256 digest")
        if self.staged_skill_root is not None:
            staged_root = Path(self.staged_skill_root)
            if not staged_root.is_absolute() or not staged_root.is_dir():
                raise ProviderRequestError("staged_skill_root must name an existing absolute directory")
            object.__setattr__(self, "staged_skill_root", staged_root)
        if self.skill_delivery_ref is not None:
            if (
                not isinstance(self.skill_delivery_ref, ArtifactRef)
                or self.skill_delivery_ref.digest != self.skill_delivery_sha256
            ):
                raise ProviderRequestError("skill delivery artifact reference is invalid")
        if self.agy_guard is not None and not isinstance(self.agy_guard, AgyReadOnlyGuard):
            raise ProviderRequestError("agy guard must be a controller-issued read-only guard")
        if any(value is not None for value in (
            self.native_boundary, self.native_controller, self.native_verification,
        )) and not all(value is not None for value in (
            self.native_boundary, self.native_controller, self.native_verification,
        )):
            raise ProviderRequestError("native seat boundary requires controller verification context")
        _artifact_parts(self.artifact_prefix)

    @property
    def prompt_bytes(self) -> bytes:
        return self.prompt.encode("utf-8") if isinstance(self.prompt, str) else self.prompt


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Terminal evidence for exactly one provider request, including raw captures."""

    executor_id: str
    valid: bool
    reason: str
    hint: str | None
    attempt_count: int
    duration: float
    usage: Mapping[str, int | float] | None
    session_id: str | None
    answer: str = field(repr=False)
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    answer_ref: ArtifactRef | None = None
    stdout_ref: ArtifactRef | None = None
    stderr_ref: ArtifactRef | None = None
    answer_digest: str = ""
    stdout_digest: str = ""
    stderr_digest: str = ""
    requested_model: str | None = None
    observed_model: str | None = None

    def __post_init__(self) -> None:
        """Copy the supported scalar usage surface behind an immutable mapping."""
        if self.usage is not None:
            if not isinstance(self.usage, Mapping):
                raise ProviderRequestError("result usage must be a mapping or None")
            frozen_usage: dict[str, int | float] = {}
            for key, value in self.usage.items():
                if not isinstance(key, str) or not key or "\x00" in key:
                    raise ProviderRequestError("result usage keys must be non-empty strings")
                if (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not math.isfinite(value) or value < 0):
                    raise ProviderRequestError("result usage values must be finite non-negative numbers")
                frozen_usage[key] = value
            object.__setattr__(self, "usage", MappingProxyType(frozen_usage))


@dataclass(frozen=True, slots=True)
class _Parsed:
    session_id: str | None
    answer: str
    usage: Mapping[str, int | float] | None
    final: bool
    error: str | None = None
    observed_model: str | None = None


class ProviderAdapter:
    """Provider-specific command grammar and structured output decoding."""

    executor_id: str
    capabilities: ProviderCapabilities

    def build_command(self, request: ProviderRequest) -> ProcessCommand:
        raise NotImplementedError

    def parse(self, stdout: bytes) -> _Parsed:
        raise NotImplementedError

    @staticmethod
    def _command(request: ProviderRequest, argv: Sequence[str]) -> ProcessCommand:
        return ProcessCommand(
            argv=tuple(argv),
            stdin=request.prompt_bytes,
            cwd=request.cwd,
            timeout=(request.timeout if request.timeout is not None
                     else request.profile.default_timeout if request.profile is not None else 120.0),
            slot_cap=request.slot_cap,
            slot_timeout=request.slot_timeout,
            slot_root=request.slot_root,
        )


class ClaudeAdapter(ProviderAdapter):
    executor_id = "claude"
    capabilities = ProviderCapabilities(True, True, True)

    def build_command(self, request: ProviderRequest) -> ProcessCommand:
        session_id = request.session_id or str(uuid.uuid4())
        try:
            parsed_id = uuid.UUID(session_id)
        except ValueError as error:
            raise ProviderRequestError("Claude session_id must be a UUID") from error
        if str(parsed_id) != session_id.lower():
            raise ProviderRequestError("Claude session_id must be a canonical UUID")
        if request.profile is not None:
            template = request.profile.resume_argv if request.resume else request.profile.initial_argv
            argv = _render_profile_argv(template, session_id)
        else:
            if request.resume:
                argv = ["claude", "--print", "--resume", session_id, "--output-format", "json"]
            else:
                argv = ["claude", "--print", "--session-id", session_id, "--output-format", "json"]
            if request.execution_class == _READ_ONLY:
                argv.extend(_CLAUDE_READ_ONLY_FLAGS)
            else:
                argv.append("--dangerously-skip-permissions")
        command = self._command(request, argv)
        if request.execution_class == _READ_ONLY:
            if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
                raise ProviderRequestError("Claude read-only credential route has a parent override")
            if os.environ.get("AGY_ADC_AUTH") != "true":
                return command
            try:
                adc = default_claude_adc_path()
                route = default_claude_vertex_route()
            except Exception as error:
                raise ProviderRequestError("Claude read-only Vertex route is unsafe") from error
            if adc is None or route is None:
                raise ProviderRequestError("Claude read-only Vertex route is unavailable")
            return replace(command, environment={
                "GOOGLE_APPLICATION_CREDENTIALS": str(adc), **route,
            })
        if (os.environ.get("AGY_ADC_AUTH") != "true"
                or "GOOGLE_APPLICATION_CREDENTIALS" in os.environ):
            return command
        adc = default_claude_adc_path()
        if adc is None:
            return command
        return replace(command, environment={"GOOGLE_APPLICATION_CREDENTIALS": str(adc)})

    def parse(self, stdout: bytes) -> _Parsed:
        data = _json_object(stdout, "Claude")
        session_id = _optional_string(data.get("session_id"))
        error = _optional_string(data.get("result")) if data.get("is_error") is True else None
        model_usage = data.get("modelUsage")
        observed_model: str | None = None
        if model_usage is not None:
            if not isinstance(model_usage, dict) or any(
                not isinstance(model, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}", model) is None
                for model in model_usage
            ) or len(model_usage) > 1:
                raise ProviderProtocolError("Claude result has ambiguous model usage")
            observed_model = next(iter(model_usage), None)
        return _Parsed(
            session_id=session_id,
            answer=_optional_string(data.get("result")) or "",
            usage=_usage(data.get("usage"), {
                "input_tokens": "input", "output_tokens": "output",
                "cache_read_input_tokens": "cache_read",
                "cache_creation_input_tokens": "cache_write",
            }, cost=data.get("total_cost_usd")),
            final=(data.get("type") == "result"
                   and data.get("subtype") == "success"
                   and data.get("is_error") is False),
            error=error,
            observed_model=observed_model,
        )


class CodexAdapter(ProviderAdapter):
    executor_id = "codex"
    capabilities = ProviderCapabilities(False, True, True)

    def build_command(self, request: ProviderRequest) -> ProcessCommand:
        if request.session_id is not None and not request.resume:
            raise ProviderRequestError("Codex session_id is only valid when resuming")
        if request.execution_class == _READ_ONLY:
            _codex_read_only_host_config_preflight()
        if request.profile is not None:
            template = request.profile.resume_argv if request.resume else request.profile.initial_argv
            argv = _render_profile_argv(template, request.session_id)
            if request.execution_class == _READ_ONLY:
                if argv.count(_CODEX_PROJECT_TRUST) != 1:
                    raise ProviderRequestError("Codex read-only profile lacks exact project trust")
                trust = _codex_project_trust_override(request.cwd)
                argv = tuple(trust if arg == _CODEX_PROJECT_TRUST else arg for arg in argv)
            return self._command(request, argv)
        sandbox = "read-only" if request.execution_class == _READ_ONLY else "workspace-write"
        isolation = (_CODEX_READ_ONLY_FLAGS + ("-c", _codex_project_trust_override(request.cwd))
                     if request.execution_class == _READ_ONLY else ())
        if request.resume:
            # `exec resume` does not expose `--sandbox`; Codex's documented config
            # override is the only exact posture control on that grammar.
            argv = [
                "codex", "exec", "resume", *isolation,
                "-c", f'sandbox_mode="{sandbox}"',
                "--json", request.session_id, "-",
            ]
        else:
            argv = ["codex", "exec", *isolation, "-", "--json", "--sandbox", sandbox]
        return self._command(request, argv)

    def parse(self, stdout: bytes) -> _Parsed:
        events = _json_lines(stdout, "Codex")
        session_id: str | None = None
        answer = ""
        usage: Mapping[str, int | float] | None = None
        final = False
        error: str | None = None
        observed_model: str | None = None
        for event in events:
            kind = event.get("type")
            if kind == "thread.started":
                candidate = _optional_string(event.get("thread_id"))
                if candidate is None:
                    raise ProviderProtocolError("Codex thread.started lacks thread_id")
                if session_id is not None and session_id != candidate:
                    raise ProviderProtocolError("Codex emitted conflicting thread identities")
                session_id = candidate
            elif kind == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    answer = item["text"] if isinstance(item.get("text"), str) else ""
            elif kind == "turn.completed":
                final = True
                observed_model = _optional_string(event.get("model"))
                usage = _usage(event.get("usage"), {
                    "input_tokens": "input", "output_tokens": "output",
                    "cached_input_tokens": "cache_read",
                    "cache_write_input_tokens": "cache_write",
                    "reasoning_output_tokens": "reasoning",
                })
            elif kind in {"turn.failed", "error"}:
                final = True
                detail = event.get("error")
                error = _optional_string(detail.get("message")) if isinstance(detail, dict) else _optional_string(event.get("message"))
                error = error or "Codex reported a structured error"
        return _Parsed(session_id, answer, usage, final, error, observed_model)


class AgyAdapter(ProviderAdapter):
    executor_id = "agy"
    capabilities = ProviderCapabilities(False, True, True)

    def build_command(self, request: ProviderRequest) -> ProcessCommand:
        if request.session_id is not None and not request.resume:
            raise ProviderRequestError("agy session_id is only valid when resuming")
        if request.profile is not None:
            template = request.profile.resume_argv if request.resume else request.profile.initial_argv
            argv = list(_render_profile_argv(template, request.session_id))
        else:
            mode = "plan" if request.execution_class == _READ_ONLY else "accept-edits"
            argv = ["agy"]
            if request.resume:
                argv.extend(["--conversation", request.session_id])
            argv.extend(["--mode", mode, "--input-format", "stream-json",
                         "--output-format", "stream-json"])
        guard = None
        if request.execution_class == _READ_ONLY:
            guard = request.agy_guard
            if guard is None:
                raise ProviderRequestError("agy read-only request lacks a native guard")
            validate_agy_readonly_guard(guard, request.cwd, request.profile)
            argv[0] = str(guard.binary)
            if not request.resume:
                argv.insert(1, "--new-project")
        try:
            prompt = request.prompt_bytes.decode("utf-8")
            stdin = (json.dumps({"event": "user", "message": {"content": prompt}},
                                ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        except UnicodeError as error:
            raise ProviderRequestError("agy prompt must be UTF-8 text") from error
        auth_mode = guard.environment if guard is not None else (
            {"AGY_ADC_AUTH": "true"} if os.environ.get("AGY_ADC_AUTH") == "true" else {}
        )
        return replace(self._command(request, argv), stdin=stdin, environment=auth_mode)

    def parse(self, stdout: bytes) -> _Parsed:
        events = _json_lines(stdout, "agy")
        session_id: str | None = None
        data: dict[str, Any] | None = None
        for index, event in enumerate(events):
            kind = event.get("event")
            if kind == "init":
                if index != 0 or not isinstance(event.get("init"), dict):
                    raise ProviderProtocolError("agy stream has invalid init")
                session_id = _optional_string(event.get("conversation_id"))
                if session_id is None:
                    raise ProviderProtocolError("agy init lacks conversation identity")
            elif kind == "step_update":
                step = event.get("step_update")
                if (session_id is None or not isinstance(step, dict)
                        or step.get("conversation_id") != session_id):
                    raise ProviderProtocolError("agy step has conflicting conversation identity")
            elif kind == "result":
                if index != len(events) - 1 or not isinstance(event.get("result"), dict):
                    raise ProviderProtocolError("agy stream has invalid terminal result")
                data = event["result"]
                result_id = _optional_string(data.get("conversation_id"))
                if session_id is None:
                    if index != 0 or data.get("status") != "ERROR":
                        raise ProviderProtocolError("agy result lacks init identity")
                elif result_id != session_id:
                    raise ProviderProtocolError("agy result has conflicting conversation identity")
            else:
                raise ProviderProtocolError("agy stream has an unknown event")
        if data is None:
            raise ProviderProtocolError("agy stream lacks a terminal result")
        status = _optional_string(data.get("status"))
        if status not in {"SUCCESS", "ERROR"}:
            raise ProviderProtocolError("agy response lacks a terminal status")
        if status == "SUCCESS" and "error" in data:
            raise ProviderProtocolError("agy success result contains an error")
        return _Parsed(
            session_id=_optional_string(data.get("conversation_id")),
            answer=_optional_string(data.get("response")) or "",
            usage=_usage(data.get("usage"), {
                "input_tokens": "input", "output_tokens": "output",
                "thinking_tokens": "thinking", "cache_read_tokens": "cache_read",
                "total_tokens": "total",
            }),
            final=True,
            error=(_optional_string(data.get("error")) or "agy reported a structured error")
                  if status == "ERROR" else None,
            observed_model=_optional_string(data.get("model")),
        )


def _render_profile_argv(template: tuple[str, ...], session_id: str | None) -> tuple[str, ...]:
    if "{session_id}" in template and session_id is None:
        raise ProviderRequestError("resumed profile requires exact session identity")
    return tuple(session_id if arg == "{session_id}" else arg for arg in template)


def _codex_project_trust_override(cwd: Path | str) -> str:
    path = Path(cwd)
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProviderRequestError("Codex read-only workspace must be canonical") from error
    if not path.is_absolute() or path != canonical or not path.is_dir():
        raise ProviderRequestError("Codex read-only workspace must be an absolute canonical directory")
    # A whole inline table preserves dots and quotes in the exact project key;
    # dotted CLI overrides split those characters as TOML key separators.
    return f'projects={{{json.dumps(str(path), ensure_ascii=False)}={{trust_level="untrusted"}}}}'


def _codex_read_only_host_config_preflight() -> None:
    if os.name != "posix":
        raise ProviderRequestError("Codex read-only host config preflight needs Unix paths")
    system_dir = _CODEX_SYSTEM_CONFIG_PATHS[0].parent
    try:
        directory = system_dir.lstat()
    except FileNotFoundError:
        # With no /etc/codex, a writable parent can create one while a seat
        # waits for an executor slot. stat follows macOS's /etc -> /private/etc.
        protected_dir = system_dir.parent
        try:
            directory = protected_dir.stat()
        except OSError as error:
            raise ProviderRequestError("Codex system config parent cannot be checked") from error
    except OSError as error:
        raise ProviderRequestError("Codex system config directory cannot be checked") from error
    else:
        protected_dir = system_dir
    if not stat.S_ISDIR(directory.st_mode):
        raise ProviderRequestError("Codex system config directory is not a regular directory")
    for path in _CODEX_SYSTEM_CONFIG_PATHS:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ProviderRequestError("Codex system host config cannot be checked") from error
        raise ProviderRequestError("Codex system host config is present")
    try:
        mutable = (directory.st_uid == os.geteuid()
                   or os.access(protected_dir, os.W_OK, effective_ids=True))
    except (OSError, NotImplementedError) as error:
        raise ProviderRequestError("Codex system config directory cannot be checked") from error
    if mutable:
        raise ProviderRequestError("Codex system config directory is writable by this user")
    if _CODEX_HOST_PLATFORM == "darwin":
        try:
            forced = _codex_macos_forced_preferences_present()
        except Exception as error:
            raise ProviderRequestError("Codex managed preferences cannot be checked") from error
        if forced:
            raise ProviderRequestError("Codex managed preferences are forced")


def _codex_macos_forced_preferences_present() -> bool:
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    create = core.CFStringCreateWithCString
    create.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32)
    create.restype = ctypes.c_void_p
    synchronize = core.CFPreferencesAppSynchronize
    synchronize.argtypes = (ctypes.c_void_p,)
    synchronize.restype = ctypes.c_ubyte
    is_forced = core.CFPreferencesAppValueIsForced
    is_forced.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    is_forced.restype = ctypes.c_ubyte
    release = core.CFRelease
    release.argtypes = (ctypes.c_void_p,)
    release.restype = None
    refs: list[int] = []

    def cf_string(value: str) -> int:
        ref = create(None, value.encode("utf-8"), 0x08000100)
        if not ref:
            raise OSError("CoreFoundation could not create a preference key")
        refs.append(ref)
        return ref

    try:
        application = cf_string("com.openai.codex")
        if not synchronize(application):
            raise OSError("CoreFoundation could not synchronize managed preferences")
        forced = []
        for name in ("config_toml_base64", "requirements_toml_base64"):
            forced.append(bool(is_forced(cf_string(name), application)))
        return any(forced)
    finally:
        for ref in reversed(refs):
            release(ref)


class ProviderRegistry:
    """The executor-only registry; routing/evaluation targets are deliberately separate."""

    def __init__(self, profiles: Iterable[ExecutorProfile] = (), *,
                 version_probe: Callable[[str], str] | None = None) -> None:
        registered: dict[tuple[str, str, str], ExecutorProfile] = {}
        adapters: dict[str, ProviderAdapter] = {}
        digests: dict[str, str] = {}
        for profile in profiles:
            if (not isinstance(profile, ExecutorProfile)
                    or not isinstance(profile.executor_id, str)
                    or not profile.executor_id
                    or "\x00" in profile.executor_id):
                raise ProviderRequestError("registry entries must be named ExecutorProfile values")
            if not isinstance(profile.adapter, ProviderAdapter):
                raise ProviderRequestError("registry adapters must be ProviderAdapter values")
            if profile.adapter.executor_id != profile.executor_id:
                raise ProviderRequestError("registry profile ID must match adapter ID")
            adapter_capabilities = getattr(profile.adapter, "capabilities", None)
            if (not isinstance(profile.capabilities, ProviderCapabilities)
                    or not isinstance(adapter_capabilities, ProviderCapabilities)
                    or profile.capabilities != adapter_capabilities):
                raise ProviderRequestError("registry profile capabilities must match adapter capabilities")
            if profile.executor_id == "maka":
                raise UnsupportedExecutorError("maka is a routing/evaluation target, not an executor")
            if profile.key in registered:
                raise ProviderRequestError(f"duplicate executor profile: {profile.binding_key}")
            if profile.executor_id in adapters and adapters[profile.executor_id] is not profile.adapter:
                raise ProviderRequestError("executor profiles must share one exact adapter")
            if (
                not profile.requested_model or not profile.requested_effort or not profile.cli_version
                or not profile.initial_argv or not profile.resume_argv
                or profile.initial_argv[0] != profile.executor_id
                or profile.resume_argv[0] != profile.executor_id
                or "{session_id}" not in profile.resume_argv
                or not 0 < profile.default_timeout <= profile.timeout_ceiling <= 3600
            ):
                raise ProviderRequestError("executor profile invocation is incomplete")
            if type(profile.adapter) in {ClaudeAdapter, CodexAdapter, AgyAdapter}:
                expected_argv = _default_argv(
                    profile.executor_id, profile.execution_class,
                    profile.requested_model, profile.requested_effort,
                )
                if (profile.initial_argv, profile.resume_argv) != expected_argv:
                    raise ProviderRequestError("built-in executor profile argv differs from its pins")
            registered[profile.key] = profile
            adapters[profile.executor_id] = profile.adapter
            derived = profile.digest
            if profile.profile_sha256 is not None and profile.profile_sha256 != derived:
                raise ProviderRequestError(
                    "registry profile digest must match its adapter identity"
                )
            digests[profile.binding_key] = derived
        self._profiles = registered
        self._adapters = adapters
        self._profile_digests = MappingProxyType(digests)
        self._version_probe = _installed_cli_version if version_probe is None else version_probe

    @classmethod
    def default(cls, *, version_probe: Callable[[str], str] | None = None) -> "ProviderRegistry":
        adapters = (ClaudeAdapter(), CodexAdapter(), AgyAdapter())
        versions = {"claude": "2.1.281", "codex": "0.157.1", "agy": "1.2.12"}
        models = {
            "claude": ("claude-opus-5-5", "max", "xhigh", "ultracode"),
            "codex": ("gpt-6-sol", "xhigh", "xhigh", "ultra"),
            "agy": ("gemini-3.8-flash-high", "high", "high", "high"),
        }
        profiles = []
        for adapter in adapters:
            model, read_effort, write_effort, deep_effort = models[adapter.executor_id]
            for execution_class, quality_tier, effort, timeout in (
                (_READ_ONLY, "standard", read_effort, 900),
                (_REPO_WRITE, "standard", write_effort, 3600),
                (_READ_ONLY, "deep", deep_effort, 1800),
            ):
                initial, resume = _default_argv(adapter.executor_id, execution_class, model, effort)
                profiles.append(ExecutorProfile(
                    adapter.executor_id, adapter, adapter.capabilities,
                    execution_class, quality_tier, model, effort, versions[adapter.executor_id],
                    initial, resume, timeout, timeout,
                    characterized=(
                        quality_tier == "standard"
                        or (execution_class == _READ_ONLY and adapter.executor_id in {"agy", "claude", "codex"})
                    ),
                ))
        return cls(profiles, version_probe=version_probe)

    @property
    def executor_ids(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    @property
    def profile_digests(self) -> Mapping[str, str]:
        return self._profile_digests

    def require(self, executor_id: str) -> ProviderAdapter:
        adapter = self._adapters.get(executor_id)
        if adapter is None:
            if executor_id == "maka":
                raise UnsupportedExecutorError("maka is not an executor")
            raise UnsupportedExecutorError(f"unsupported executor: {executor_id}")
        return adapter

    def select(self, executor_id: str, execution_class: str, quality_tier: str) -> ExecutorProfile:
        key = profile_binding_key(executor_id, execution_class, quality_tier)
        profile = self._profiles.get((executor_id, execution_class, quality_tier))
        if profile is None:
            raise UnsupportedExecutorError(f"unsupported executor profile: {key}")
        return profile

    def admit(self, executor_id: str, execution_class: str, quality_tier: str) -> ExecutorProfile:
        profile = self.select(executor_id, execution_class, quality_tier)
        if not profile.characterized:
            raise ProviderRequestError("executor profile needs live CLI characterization")
        return profile

    def assert_installed_version(self, profile: ExecutorProfile) -> None:
        """Reject a changed licensed CLI before a pinned provider turn starts."""
        if self.select(*profile.key).digest != profile.digest:
            raise ProviderRequestError("executor profile drifted before provider launch")
        try:
            observed = self._version_probe(profile.executor_id)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ProviderRequestError("installed CLI version is unavailable") from error
        if observed != profile.cli_version:
            raise ProviderRequestError("CLI version drifted from the pinned executor profile")


def _installed_cli_version(executor_id: str) -> str:
    version_lines = {
        "claude": rb"([0-9]+\.[0-9]+\.[0-9]+) \(Claude Code\)",
        "codex": rb"codex-cli ([0-9]+\.[0-9]+\.[0-9]+)",
        "agy": rb"([0-9]+\.[0-9]+\.[0-9]+)",
    }
    pattern = version_lines.get(executor_id)
    if pattern is None:
        raise ValueError("CLI version format is unknown; provide an explicit version probe")
    result = subprocess.run(
        [executor_id, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False, env=build_child_environment(),
    )
    if result.returncode != 0:
        raise ValueError("CLI --version failed")
    match = re.fullmatch(pattern, result.stdout.strip())
    if match is None or re.search(rb"(?<![0-9])[0-9]+\.[0-9]+\.[0-9]+(?![0-9])", result.stderr):
        raise ValueError("CLI --version did not match the exact provider version line")
    return match.group(1).decode("ascii")


def _default_argv(executor_id: str, execution_class: str, model: str,
                  effort: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if executor_id == "claude":
        mode = (_CLAUDE_READ_ONLY_FLAGS
                if execution_class == _READ_ONLY else ("--dangerously-skip-permissions",))
        base = ("claude", "--print")
        profile = ("--output-format", "json", "--model", model, "--effort", effort) + mode
        return (base + ("--session-id", "{session_id}") + profile,
                base + ("--resume", "{session_id}") + profile)
    if executor_id == "codex":
        sandbox = "read-only" if execution_class == _READ_ONLY else "workspace-write"
        pinned = ("-m", model, "-c", f'model_reasoning_effort="{effort}"')
        isolation = (_CODEX_READ_ONLY_FLAGS + ("-c", _CODEX_PROJECT_TRUST)
                     if execution_class == _READ_ONLY else ())
        return (("codex", "exec") + isolation + ("-", "--json", "--sandbox", sandbox) + pinned,
                ("codex", "exec", "resume") + isolation
                + ("-c", f'sandbox_mode="{sandbox}"') + pinned
                + ("--json", "{session_id}", "-"))
    if executor_id == "agy":
        mode = "plan" if execution_class == _READ_ONLY else "accept-edits"
        approval = () if execution_class == _READ_ONLY else ("--dangerously-skip-permissions",)
        profile = approval + ("--mode", mode,
                   "--input-format", "stream-json", "--output-format", "stream-json",
                   "--model", model, "--effort", effort)
        return (("agy",) + profile,
                ("agy", "--conversation", "{session_id}") + profile)
    raise ProviderRequestError(f"no default profile for executor: {executor_id}")


class RoutingTargetRegistry:
    """Non-executor destinations, currently Maka's routing/evaluation role."""

    def __init__(self, targets: Iterable[str] = ()) -> None:
        values = tuple(targets)
        if any(not isinstance(target, str) or not target for target in values):
            raise ProviderRequestError("routing target IDs must be non-empty strings")
        if len(set(values)) != len(values):
            raise ProviderRequestError("routing target IDs must be unique")
        self._targets = values

    @classmethod
    def default(cls) -> "RoutingTargetRegistry":
        return cls(("maka",))

    @property
    def target_ids(self) -> tuple[str, ...]:
        return self._targets


def _require_native_seat_boundary(request: ProviderRequest) -> None:
    if not isinstance(request, ProviderRequest):
        raise ProviderRequestError("request must be a ProviderRequest")
    if (request.execution_class == _REPO_WRITE or request.target_id is not None
            or request.inputs_digest is not None or request.native_boundary is not None):
        if request.native_boundary is None:
            raise ProviderRequestError(
                "v2 or repo-write provider launch requires a validated native seat boundary"
            )
        from .native_boundary import validate_native_boundary
        if request.profile is None:
            raise ProviderRequestError("native seat boundary requires a pinned profile")
        # Authenticate the descriptor before refusal, without constructing an
        # adapter command. Task 12 must validate the exact built command.
        validate_native_boundary(
            request.native_boundary, request, None,
            request.native_controller, request.native_verification,
        )
        raise ProviderRequestError(
            "v2 or repo-write provider launch requires a live-certified credential-blind native seat boundary"
        )


def run_provider(request: ProviderRequest, *, registry: ProviderRegistry | None = None) -> ProviderResult:
    """Execute v1 read-only turns; v2 and repo-write await live certification."""
    _require_native_seat_boundary(request)
    return _run_provider_with_runner(request, registry=registry, runner=run_command)


def _run_provider_with_runner(request: ProviderRequest, *, registry: ProviderRegistry | None = None,
                              runner: Callable[[ProcessCommand], ProcessResult]) -> ProviderResult:
    """Hermetic transport seam for tests; production callers use run_provider."""
    if not isinstance(request, ProviderRequest):
        raise ProviderRequestError("request must be a ProviderRequest")
    if request.profile is None:
        raise ProviderRequestError("provider request requires a pinned profile")
    selected_registry = registry or ProviderRegistry.default()
    profile = selected_registry.admit(
        request.executor_id, request.execution_class,
        request.profile.quality_tier,
    )
    if request.profile.digest != profile.digest:
        raise ProviderRequestError("request executor profile changed before provider launch")
    selected_registry.assert_installed_version(profile)
    request = replace(request, profile=profile)
    adapter = selected_registry.require(request.executor_id)
    # Claude makes the initial ID caller-owned. Establish it once before the
    # attempt loop so a known-unstarted spawn retry retains one identity.
    if isinstance(adapter, ClaudeAdapter) and request.session_id is None:
        request = replace(request, session_id=str(uuid.uuid4()))
    started = time.monotonic()
    last_process: ProcessResult | None = None
    parsed: _Parsed | None = None
    attempts = 0
    for attempts in range(1, request.retries + 2):
        command = adapter.build_command(request)
        last_process = runner(command)
        if last_process.status == ProcessStatus.SPAWN_ERROR and attempts <= request.retries:
            continue
        if last_process.status != ProcessStatus.SPAWN_ERROR and last_process.stdout:
            try:
                parsed = adapter.parse(last_process.stdout)
            except ProviderProtocolError:
                parsed = None
        break
    assert last_process is not None
    duration = time.monotonic() - started
    return _make_result(request, adapter, last_process, parsed, attempts, duration)


def run_many(requests: Sequence[ProviderRequest], *, max_workers: int = DEFAULT_EXECUTOR_SLOTS,
             registry: ProviderRegistry | None = None) -> tuple[ProviderResult, ...]:
    """Run a bounded batch while retaining caller order and each terminal result."""
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
        raise ProviderRequestError("max_workers must be a positive integer")
    for request in requests:
        _require_native_seat_boundary(request)
    return _run_many_with_runner(requests, max_workers=max_workers, registry=registry, runner=run_command)


def _run_many_with_runner(requests: Sequence[ProviderRequest], *, max_workers: int,
                          registry: ProviderRegistry | None = None,
                          runner: Callable[[ProcessCommand], ProcessResult]) -> tuple[ProviderResult, ...]:
    """Hermetic bounded-batch seam for fake process tests."""
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
        raise ProviderRequestError("max_workers must be a positive integer")
    if not requests:
        return ()
    workers = min(max_workers, DEFAULT_EXECUTOR_SLOTS, len(requests))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_provider_with_runner, request, registry=registry, runner=runner)
                   for request in requests]
        return tuple(future.result() for future in futures)


def _make_result(request: ProviderRequest, adapter: ProviderAdapter, process: ProcessResult,
                 parsed: _Parsed | None, attempts: int, duration: float) -> ProviderResult:
    answer = parsed.answer if parsed else ""
    session_id = parsed.session_id if parsed else None
    usage = parsed.usage if parsed else None
    if parsed and parsed.error:
        valid, reason, hint = False, "provider-error", parsed.error
    elif process.status == ProcessStatus.TIMEOUT:
        valid, reason, hint = False, "timeout", "provider exceeded its configured deadline"
    elif process.status == ProcessStatus.SPAWN_ERROR:
        valid, reason, hint = False, "spawn-error", process.error or "provider did not start"
    elif parsed is None or not parsed.final or not session_id or not answer:
        valid, reason, hint = False, "protocol-error", "provider output lacks a final answer and exact session identity"
    elif request.session_id is not None and session_id != request.session_id:
        valid, reason, hint = False, "protocol-error", "provider returned a different session identity"
    elif (request.profile is not None and parsed is not None
          and parsed.observed_model is not None
          and parsed.observed_model != request.profile.requested_model):
        valid, reason, hint = False, "model-drift", "provider reported a model different from the pinned request"
    elif process.returncode not in {0, None}:
        valid, reason, hint = False, "process-error", f"provider exited with status {process.returncode}"
    else:
        valid, reason, hint = True, "ok", None
    answer_ref, stdout_ref, stderr_ref, storage_error = _write_artifacts(
        request, answer, process.stdout, process.stderr,
    )
    if storage_error is not None:
        valid = False
        reason = "artifact-storage-error"
        hint = f"artifact storage failed: {type(storage_error).__name__}"
    return ProviderResult(
        executor_id=adapter.executor_id,
        valid=valid,
        reason=reason,
        hint=hint,
        attempt_count=attempts,
        duration=duration,
        usage=usage,
        session_id=session_id,
        answer=answer,
        stdout=process.stdout,
        stderr=process.stderr,
        answer_ref=answer_ref,
        stdout_ref=stdout_ref,
        stderr_ref=stderr_ref,
        answer_digest=_digest(answer.encode("utf-8")),
        stdout_digest=_digest(process.stdout),
        stderr_digest=_digest(process.stderr),
        requested_model=request.profile.requested_model if request.profile else None,
        observed_model=parsed.observed_model if parsed else None,
    )


def _write_artifacts(request: ProviderRequest, answer: str, stdout: bytes,
                     stderr: bytes) -> tuple[
                         ArtifactRef | None,
                         ArtifactRef | None,
                         ArtifactRef | None,
                         ArtifactError | ValueError | OSError | None,
                     ]:
    if request.artifact_store is None:
        return None, None, None, None
    prefix = "/".join(_artifact_parts(request.artifact_prefix))
    refs: list[ArtifactRef | None] = [None, None, None]
    payloads = (
        ("answer.txt", answer.encode("utf-8")),
        ("stdout.bin", stdout),
        ("stderr.bin", stderr),
    )
    for index, (name, payload) in enumerate(payloads):
        try:
            refs[index] = request.artifact_store.write_bytes(f"{prefix}/{name}", payload)
        except (ArtifactError, ValueError, OSError) as error:
            return refs[0], refs[1], refs[2], error
    return refs[0], refs[1], refs[2], None


def _artifact_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise ProviderRequestError("artifact_prefix must be a non-empty relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProviderRequestError("artifact_prefix must be contained")
    return path.parts


def _json_object(stdout: bytes, provider: str, *, strict: bool = True) -> dict[str, Any]:
    try:
        data = json.loads(stdout.decode("utf-8"), strict=strict)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderProtocolError(f"{provider} emitted malformed JSON") from error
    if not isinstance(data, dict):
        raise ProviderProtocolError(f"{provider} JSON result must be an object")
    return data


def _json_lines(stdout: bytes, provider: str) -> list[dict[str, Any]]:
    try:
        decoded = stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProviderProtocolError(f"{provider} emitted non-UTF-8 JSONL") from error
    if not decoded:
        raise ProviderProtocolError(f"{provider} emitted no JSONL events")
    lines = decoded.removesuffix("\n").split("\n")
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ProviderProtocolError(f"{provider} emitted malformed JSONL") from error
        if not isinstance(event, dict):
            raise ProviderProtocolError(f"{provider} JSONL event must be an object")
        events.append(event)
    return events


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _usage(raw: object, names: Mapping[str, str], *, cost: object = None) -> Mapping[str, int | float] | None:
    if not isinstance(raw, dict):
        raw = {}
    result: dict[str, int | float] = {}
    for source, target in names.items():
        value = raw.get(source)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[target] = value
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        result["cost_usd"] = cost
    return result or None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "AgyAdapter", "ClaudeAdapter", "CodexAdapter", "ExecutorProfile",
    "ProviderAdapter", "ProviderCapabilities", "ProviderRegistry", "ProviderRequest",
    "ProviderResult", "RoutingTargetRegistry", "run_many", "run_provider",
]
