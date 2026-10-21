"""Resumable, structured adapters for the admitted fanout executors.

The module deliberately owns only provider transport and parsing.  Scheduling,
durable run state, and peer-round policy live in later layers.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from .artifacts import ArtifactLimits, ArtifactRef, ArtifactStore, canonical_json
from .errors import (
    ArtifactError,
    ProviderProtocolError,
    ProviderRequestError,
    UnsupportedExecutorError,
)
from .process import (
    DEFAULT_EXECUTOR_SLOTS, ProcessCommand, ProcessResult, ProcessStatus,
    build_child_environment, default_claude_adc_path,
    NativeRoots, resolve_native_roots,
    run_command,
)
from .skills import SkillAdmission


_READ_ONLY = "read-only"
_REPO_WRITE = "repo-write"
_EXECUTION_CLASSES = frozenset({_READ_ONLY, _REPO_WRITE})
_ROLES = frozenset({"executor", "evaluator", "owner"})
_MCP_WRITE_TOOLS = {
    "slack": ("conversations_add_message", "slack_send_message", "slack_reply_to_thread", "slack_update_message", "slack_delete_message"),
    "jira": ("jira_create_issue", "jira_update_issue", "jira_add_comment", "jira_transition_issue", "jira_delete_issue"),
    "github": ("create_pull_request", "merge_pull_request", "create_issue", "add_issue_comment", "push_files", "create_or_update_file", "delete_file"),
    "n8n": (
        "add_data_table_column", "add_data_table_rows", "archive_workflow", "call_agent",
        "create_agent", "create_data_table", "create_folder", "create_workflow_from_code",
        "delete_agent", "delete_data_table_column", "execute_workflow", "move_workflows_to_folder",
        "mutate_agent", "publish_agent", "publish_workflow", "rename_data_table",
        "rename_data_table_column", "restore_workflow_version", "revert_agent", "test_workflow",
        "unpublish_agent", "unpublish_workflow", "update_agent_integration", "update_folder", "update_workflow",
    ),
}
_CLAUDE_READ_ONLY_FLAGS = (
    "--dangerously-skip-permissions", "--permission-prompts", "none",
    "--disallowedTools", ",".join(("Edit", "Write", "NotebookEdit", "ExitPlanMode") + tuple(
        f"mcp__{server}__{tool}" for server, tools in _MCP_WRITE_TOOLS.items() for tool in tools
    )),
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
    role: str = "executor"
    native_roots: NativeRoots | None = None

    def __post_init__(self) -> None:
        profile_binding_key(self.executor_id, self.execution_class, self.quality_tier)
        if self.role not in _ROLES:
            raise ProviderRequestError("profile role must be executor, evaluator or owner")
        if self.native_roots is not None and not isinstance(self.native_roots, NativeRoots):
            raise ProviderRequestError("profile native roots binding is invalid")
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
        descriptor = {
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
            "schema_version": "fanout-executor-profile-v3",
            "role": self.role,
        }
        if self.native_roots is not None:
            descriptor["native_roots"] = self.native_roots.to_dict()
            descriptor["schema_version"] = "fanout-executor-profile-v4"
        return descriptor

    @classmethod
    def from_dict(cls, value: Mapping[str, object], *, adapter: "ProviderAdapter") -> "ExecutorProfile":
        """Rebind a stored descriptor to one explicitly admitted adapter."""
        fields = {
            "adapter_class", "capabilities", "executor_id", "execution_class",
            "quality_tier", "requested_model", "requested_effort", "cli_version",
            "initial_argv", "resume_argv", "default_timeout", "timeout_ceiling",
            "characterized", "schema_version", "role",
        }
        if isinstance(value, Mapping) and "native_roots" in value:
            fields.add("native_roots")
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ProviderRequestError("executor profile descriptor fields are invalid")
        roots = value.get("native_roots")
        if roots is not None and (not isinstance(roots, Mapping)
                or set(roots) != {"home", "codex", "claude", "agy"}
                or any(not isinstance(path, str) or not path or "\x00" in path for path in roots.values())):
            raise ProviderRequestError("executor profile native roots are invalid")
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
                role=value["role"],
                native_roots=NativeRoots(**{name: Path(path) for name, path in roots.items()}) if roots else None,
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
    staged_skill_admission: SkillAdmission | None = field(default=None, repr=False, compare=False)
    skill_delivery_sha256: str = ""
    skill_delivery_ref: ArtifactRef | None = None
    run_id: str | None = None
    task_id: str | None = None
    target_id: str | None = None
    inputs_digest: str | None = None
    seat_id: str | None = None
    role: str = "executor"

    def __post_init__(self) -> None:
        if not isinstance(self.executor_id, str) or not self.executor_id or "\x00" in self.executor_id:
            raise ProviderRequestError("executor_id must be a non-empty string")
        if not isinstance(self.prompt, (str, bytes)):
            raise ProviderRequestError("prompt must be text or bytes")
        if self.execution_class not in _EXECUTION_CLASSES:
            raise ProviderRequestError("execution_class must be read-only or repo-write")
        if self.role not in _ROLES:
            raise ProviderRequestError("request role must be executor, evaluator or owner")
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
            or self.profile.role != self.role
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
        if self.staged_skill_admission is not None and not isinstance(
            self.staged_skill_admission, SkillAdmission
        ):
            raise ProviderRequestError("staged skill admission is invalid")
        if self.skill_delivery_ref is not None:
            if (
                not isinstance(self.skill_delivery_ref, ArtifactRef)
                or self.skill_delivery_ref.digest != self.skill_delivery_sha256
            ):
                raise ProviderRequestError("skill delivery artifact reference is invalid")
        _artifact_parts(self.artifact_prefix)

    @property
    def prompt_bytes(self) -> bytes:
        return self.prompt.encode("utf-8") if isinstance(self.prompt, str) else self.prompt


@dataclass(frozen=True, slots=True)
class UsageEvidence:
    """Immutable wire counters with separate units, scope and provenance."""

    provider: str
    session_id: str | None
    turn_id: str
    source_sha256: str
    counters: tuple[tuple[str, int | float, str, str], ...] = ()
    raw_counters: tuple[tuple[str, int | float], ...] = ()
    role: str = "executor"
    resumed: bool | None = None
    declared_cli_version: str | None = None
    observed_cli_version: str | None = None
    scope_evidence: str = "uncharacterized"
    coverage: str = "unknown"
    captured_at_ns: int = field(default_factory=time.time_ns)

    def __post_init__(self) -> None:
        if (not isinstance(self.provider, str) or not self.provider
                or self.role not in _ROLES or self.resumed is not None and type(self.resumed) is not bool
                or not isinstance(self.turn_id, str) or not self.turn_id
                or not isinstance(self.source_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None
                or self.session_id is not None and (not isinstance(self.session_id, str) or not self.session_id)
                or any(version is not None and (not isinstance(version, str) or not version)
                       for version in (self.declared_cli_version, self.observed_cli_version))
                or self.scope_evidence not in {"uncharacterized", "upstream-source-uncharacterized", "provider-docs-uncharacterized"}
                or self.coverage not in {"unknown", "main-loop-only", "native-session"}):
            raise ProviderRequestError("usage evidence identity or provenance is invalid")
        if type(self.captured_at_ns) is not int or self.captured_at_ns <= 0:
            raise ProviderRequestError("usage evidence capture time is invalid")
        counters = tuple(tuple(row) for row in self.counters)
        raw = tuple(tuple(row) for row in self.raw_counters)
        for rows, width in ((counters, 4), (raw, 2)):
            if any(len(row) != width for row in rows) or len({row[0] for row in rows}) != len(rows):
                raise ProviderRequestError("usage evidence counters are duplicated or malformed")
            for row in rows:
                if (not isinstance(row[0], str) or not row[0]
                        or type(row[1]) not in {int, float} or not math.isfinite(row[1])):
                    raise ProviderRequestError("usage evidence counter is invalid")
        if any(value < 0 or scope not in {"turn", "session-cumulative", "unknown"}
               or kind not in {"provider-reported", "estimate"} for _, value, scope, kind in counters):
            raise ProviderRequestError("usage evidence counter semantics are invalid")
        object.__setattr__(self, "counters", counters)
        object.__setattr__(self, "raw_counters", raw)

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": "fanout-usage-evidence-v1", "provider": self.provider,
            "session_id": self.session_id, "turn_id": self.turn_id, "role": self.role,
            "resumed": self.resumed, "source_sha256": self.source_sha256,
            "declared_cli_version": self.declared_cli_version,
            "observed_cli_version": self.observed_cli_version, "scope_evidence": self.scope_evidence,
            "coverage": self.coverage, "raw_counters": dict(self.raw_counters),
            "captured_at_ns": self.captured_at_ns,
            "counters": {name: {"value": value, "scope": scope, "kind": kind,
                "status": "unqualified-zero" if value == 0 else "reported"}
                for name, value, scope, kind in self.counters}}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "UsageEvidence":
        try:
            fields = dict(value)
            fields.pop("schema_version")
            fields["raw_counters"] = tuple(fields["raw_counters"].items())
            fields["counters"] = tuple((name, row["value"], row["scope"], row["kind"])
                                       for name, row in fields["counters"].items())
            evidence = cls(**fields)
        except (TypeError, ValueError, KeyError, AttributeError) as error:
            raise ProviderRequestError("usage evidence descriptor is invalid") from error
        if evidence.to_dict() != value:
            raise ProviderRequestError("usage evidence descriptor changed semantics")
        return evidence


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
    launch_metadata: Mapping[str, object] = field(default_factory=dict)
    usage_evidence: UsageEvidence | None = None

    def __post_init__(self) -> None:
        """Copy the supported scalar usage surface behind an immutable mapping."""
        if self.usage_evidence is not None and not isinstance(self.usage_evidence, UsageEvidence):
            raise ProviderRequestError("result usage evidence is invalid")
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
    successful_file_change: bool = False
    observed_tools: tuple[str, ...] = ()
    usage_evidence: UsageEvidence | None = None


class ProviderAdapter:
    """Provider-specific command grammar and structured output decoding."""

    executor_id: str
    capabilities: ProviderCapabilities

    def build_command(self, request: ProviderRequest) -> ProcessCommand:
        raise NotImplementedError

    def parse(self, stdout: bytes) -> _Parsed:
        raise NotImplementedError

    @staticmethod
    def _command(request: ProviderRequest, argv: Sequence[str], *,
                 native_roots: NativeRoots | None = None) -> ProcessCommand:
        return ProcessCommand(
            argv=tuple(argv),
            stdin=request.prompt_bytes,
            cwd=request.cwd,
            timeout=(request.timeout if request.timeout is not None
                     else request.profile.default_timeout if request.profile is not None else 120.0),
            slot_cap=request.slot_cap,
            slot_timeout=request.slot_timeout,
            slot_root=request.slot_root,
            max_output_bytes=ArtifactLimits().max_file_bytes,
            trusted_native=True,
            native_roots=native_roots or _native_roots_for_request(request),
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
            usage_evidence=_native_usage_evidence("claude", data.get("usage"), stdout,
                session_id=session_id, cost=data.get("total_cost_usd"), model_usage=model_usage),
        )


class CodexAdapter(ProviderAdapter):
    executor_id = "codex"
    capabilities = ProviderCapabilities(False, True, True)

    def build_command(self, request: ProviderRequest) -> ProcessCommand:
        roots = _native_roots_for_request(request)
        if request.session_id is not None and not request.resume:
            raise ProviderRequestError("Codex session_id is only valid when resuming")
        if request.profile is not None:
            template = request.profile.resume_argv if request.resume else request.profile.initial_argv
            argv = _render_profile_argv(template, request.session_id)
            if request.execution_class == _READ_ONLY:
                argv = (*argv, *_codex_native_mcp_denials(request.cwd, roots=roots))
            return self._command(request, argv, native_roots=roots)
        sandbox = "read-only" if request.execution_class == _READ_ONLY else "workspace-write"
        isolation = (_codex_native_mcp_denials(request.cwd, roots=roots)
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
        return self._command(request, argv, native_roots=roots)

    def parse(self, stdout: bytes) -> _Parsed:
        events = _json_lines(stdout, "Codex")
        session_id: str | None = None
        answer = ""
        usage: Mapping[str, int | float] | None = None
        final = False
        error: str | None = None
        observed_model: str | None = None
        successful_file_change = False
        observed_tools: list[str] = []
        usage_evidence = _native_usage_evidence("codex", None, stdout, session_id=None)
        for event in events:
            kind = event.get("type")
            if final and kind in {"thread.started", "turn.started", "item.started", "item.updated",
                                  "item.completed", "turn.completed", "turn.failed", "error"}:
                if error is not None:
                    continue
                raise ProviderProtocolError("Codex emitted semantic events after its terminal")
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
                elif isinstance(item, dict) and item.get("type") == "file_change":
                    if item.get("status") not in {"in_progress", "completed", "failed"}:
                        raise ProviderProtocolError("Codex file change has invalid status")
                    successful_file_change = successful_file_change or item["status"] == "completed"
                elif isinstance(item, dict) and item.get("type") == "error":
                    message = _optional_string(item.get("message"))
                    # Codex exec serializes this nonfatal metadata warning as an error item.
                    if message is None or not re.fullmatch(
                        r"Model metadata for `[^`\s]+` not found\. Defaulting to fallback metadata; "
                        r"this can degrade performance and cause issues\.", message,
                    ):
                        error = error or message or "Codex reported a structured error item"
                elif isinstance(item, dict) and item.get("type") == "mcp_tool_call":
                    server, tool = item.get("server"), item.get("tool")
                    if isinstance(server, str) and isinstance(tool, str):
                        observed_tools.append(f"{server}/{tool}")
                elif isinstance(item, dict) and item.get("type") == "command_execution":
                    observed_tools.append("shell")
            elif kind == "turn.completed":
                final = True
                observed_model = _optional_string(event.get("model"))
                usage = _usage(event.get("usage"), {
                    "input_tokens": "input", "output_tokens": "output",
                    "cached_input_tokens": "cache_read",
                    "cache_write_input_tokens": "cache_write",
                    "reasoning_output_tokens": "reasoning",
                })
                usage_evidence = _native_usage_evidence("codex", event.get("usage"), stdout,
                    session_id=session_id)
            elif kind in {"turn.failed", "error"}:
                final = True
                detail = event.get("error")
                error = _optional_string(detail.get("message")) if isinstance(detail, dict) else _optional_string(event.get("message"))
                error = error or "Codex reported a structured error"
        return _Parsed(session_id=session_id, answer=answer, usage=usage, final=final,
                       error=error, observed_model=observed_model,
                       successful_file_change=successful_file_change,
                       observed_tools=tuple(observed_tools), usage_evidence=usage_evidence)


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
        try:
            prompt = request.prompt_bytes.decode("utf-8")
            stdin = (json.dumps({"event": "user", "message": {"content": prompt}},
                                ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        except UnicodeError as error:
            raise ProviderRequestError("agy prompt must be UTF-8 text") from error
        auth_mode = {"AGY_ADC_AUTH": "true"} if os.environ.get("AGY_ADC_AUTH") == "true" else {}
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
            usage_evidence=_native_usage_evidence("agy", data.get("usage"), stdout,
                session_id=_optional_string(data.get("conversation_id"))),
        )


def _render_profile_argv(template: tuple[str, ...], session_id: str | None) -> tuple[str, ...]:
    if "{session_id}" in template and session_id is None:
        raise ProviderRequestError("resumed profile requires exact session identity")
    return tuple(session_id if arg == "{session_id}" else arg for arg in template)




class ProviderRegistry:
    """The executor-only registry; routing/evaluation targets are deliberately separate."""

    def __init__(self, profiles: Iterable[ExecutorProfile] = (), *,
                 version_probe: Callable[[str], str] | None = None,
                 contract_probe: Callable[[ExecutorProfile], None] | None = None) -> None:
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
        self._contract_probe = _installed_cli_contract if contract_probe is None else contract_probe

    @classmethod
    def default(cls, *, version_probe: Callable[[str], str] | None = None,
                contract_probe: Callable[[ExecutorProfile], None] | None = None) -> "ProviderRegistry":
        adapters = (ClaudeAdapter(), CodexAdapter(), AgyAdapter())
        versions = {"claude": "2.1.284", "codex": "0.160.0", "agy": "1.2.13"}
        models = {
            "claude": ("claude-opus-5-5", "medium", "medium", "medium"),
            "codex": ("gpt-6.1-sol", "medium", "medium", "medium"),
            "agy": ("gemini-3.8-flash-high", "high", "high", "high"),
        }
        profiles = []
        native_roots = resolve_native_roots()
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
                    characterized=execution_class == _READ_ONLY and adapter.executor_id != "codex",
                    native_roots=native_roots,
                ))
        return cls(profiles, version_probe=version_probe, contract_probe=contract_probe)

    @classmethod
    def for_installed(cls, executor_ids: Iterable[str], *,
                      version_probe: Callable[[str], str] | None = None,
                      contract_probe: Callable[[ExecutorProfile], None] | None = None) -> "ProviderRegistry":
        """Bind fresh native read-only profiles to the selected installed CLIs."""
        base = cls.default(version_probe=version_probe, contract_probe=contract_probe)
        selected = tuple(executor_ids)
        if (not selected or any(not isinstance(executor, str) for executor in selected)
                or len(set(selected)) != len(selected)):
            raise ProviderRequestError("installed executor selection must be nonempty and unique")
        for executor in selected:
            base.require(executor)
        try:
            observed = {executor: base._version_probe(executor) for executor in selected}
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ProviderRequestError("installed CLI version is unavailable") from error
        profiles = (
            replace(profile, cli_version=observed[profile.executor_id], profile_sha256=None,
                    characterized=profile.characterized and profile.cli_version == observed[profile.executor_id])
            if profile.execution_class == _READ_ONLY and profile.executor_id in observed
            else profile for profile in base._profiles.values()
        )
        return cls(profiles, version_probe=base._version_probe, contract_probe=base._contract_probe)

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

    def select(self, executor_id: str, execution_class: str, quality_tier: str, *,
               role: str = "executor") -> ExecutorProfile:
        if role not in _ROLES:
            raise ProviderRequestError("profile role must be executor, evaluator or owner")
        key = profile_binding_key(executor_id, execution_class, quality_tier)
        profile = self._profiles.get((executor_id, execution_class, quality_tier))
        if profile is None:
            raise UnsupportedExecutorError(f"unsupported executor profile: {key}")
        if role != profile.role:
            effort = "high" if executor_id == "agy" else "xhigh"
            model = "gemini-4-argon-high" if executor_id == "agy" and role != "executor" else profile.requested_model
            initial, resume = _default_argv(executor_id, execution_class, model, effort)
            profile = replace(profile, role=role, requested_model=model, requested_effort=effort,
                              initial_argv=initial, resume_argv=resume, profile_sha256=None)
        return profile

    def admit(self, executor_id: str, execution_class: str, quality_tier: str, *,
              role: str = "executor") -> ExecutorProfile:
        profile = self.select(executor_id, execution_class, quality_tier, role=role)
        capabilities = profile.capabilities
        if (not capabilities.explicit_resume or not capabilities.stdin_prompt
                or not capabilities.structured_output
                or type(profile.adapter) is ClaudeAdapter and not capabilities.assigned_session_id):
            raise ProviderRequestError("executor profile lacks required session/transport capabilities")
        built_in_readonly = (profile.execution_class == _READ_ONLY
                             and type(profile.adapter) in {ClaudeAdapter, CodexAdapter, AgyAdapter})
        if not profile.characterized and not built_in_readonly:
            raise ProviderRequestError("executor profile needs live CLI characterization")
        return profile

    def assert_installed_version(self, profile: ExecutorProfile) -> str:
        """Observe CLI drift and check supported grammar before spending a turn."""
        if self.select(*profile.key, role=profile.role).digest != profile.digest:
            raise ProviderRequestError("executor profile drifted before provider launch")
        try:
            observed = self._version_probe(profile.executor_id)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ProviderRequestError("installed CLI version is unavailable") from error
        self._contract_probe(profile)
        return observed


def _installed_cli_contract(profile: ExecutorProfile) -> None:
    """Help is a local, no-model preflight; it cannot prove served identity."""
    if type(profile.adapter) not in {ClaudeAdapter, CodexAdapter, AgyAdapter}:
        return
    argv = [profile.executor_id]
    if profile.executor_id == "codex":
        argv.append("exec")
    try:
        result = subprocess.run([*argv, "--help"], capture_output=True,
                                timeout=10, check=False,
                                env=build_child_environment(trusted_native=True))
    except (OSError, subprocess.SubprocessError) as error:
        raise ProviderRequestError("native CLI contract is unavailable") from error
    help_text = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    required = {"claude": ("--print", "--session-id", "--resume", "--output-format", "--disallowedTools", "--effort", "--dangerously-skip-permissions"),
                "codex": ("--json", "--sandbox", "--model", "--config"),
                "agy": ("--conversation", "--input-format", "--output-format", "--effort", "--mode", "--dangerously-skip-permissions")}
    if profile.executor_id == "codex" and profile.execution_class == _READ_ONLY:
        required["codex"] += ("--skip-git-repo-check",)
    if result.returncode or any(flag not in help_text for flag in required[profile.executor_id]):
        raise ProviderRequestError("native CLI lacks required structured output or invocation flags")
    if profile.executor_id != "codex" and profile.requested_effort not in help_text:
        raise ProviderRequestError("native CLI does not support requested role effort")


def _native_roots_for_request(request: ProviderRequest) -> NativeRoots:
    roots = resolve_native_roots()
    if request.profile is not None and request.profile.native_roots is not None:
        if roots != request.profile.native_roots:
            raise ProviderRequestError("native discovery roots differ from the pinned profile")
        return request.profile.native_roots
    return roots


def _codex_native_mcp_denials(cwd: Path | str, *, roots: NativeRoots | None = None) -> tuple[str, ...]:
    """Merge per-tool denials without replacing native MCP transports or reads."""
    home = (roots or resolve_native_roots()).codex
    paths = [home / "config.toml"]
    directory = Path(cwd).resolve()
    paths.extend(parent / ".codex/config.toml" for parent in reversed((directory, *directory.parents)))
    servers: dict[str, dict[str, object]] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                configured = tomllib.load(stream).get("mcp_servers", {})
        except (OSError, ValueError) as error:
            raise ProviderRequestError("native Codex MCP policy cannot be read") from error
        for name, config in configured.items():
            if isinstance(config, dict):
                servers.setdefault(name, {}).update(config)
    options = []
    for name, config in servers.items():
        # CLI override keys are raw dot-separated paths, not TOML keys.
        # A dotted server name cannot be targeted without replacing its config.
        if re.fullmatch(r"[A-Za-z0-9_:@/-]+", name) is None:
            raise ProviderRequestError("native Codex MCP server name cannot be safely addressed")
        tools = tuple(tool for group in _MCP_WRITE_TOOLS.values() for tool in group)
        existing = config.get("disabled_tools", [])
        if not isinstance(existing, list) or any(not isinstance(tool, str) for tool in existing):
            raise ProviderRequestError("native Codex MCP disabled tools are invalid")
        denied = sorted(set(existing).union(tools))
        options.extend(("-c", f"mcp_servers.{name}.disabled_tools={json.dumps(denied)}"))
    return tuple(options)


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
        sandbox = "danger-full-access"
        pinned = ("-m", model, "-c", f'model_reasoning_effort="{effort}"',
                  "-c", 'approval_policy="never"')
        isolation = ("--skip-git-repo-check",) if execution_class == _READ_ONLY else ()
        return (("codex", "exec") + isolation + ("-", "--json", "--sandbox", sandbox) + pinned,
                ("codex", "exec", "resume") + isolation
                + ("-c", f'sandbox_mode="{sandbox}"') + pinned
                + ("--json", "{session_id}", "-"))
    if executor_id == "agy":
        mode = "plan" if execution_class == _READ_ONLY else "accept-edits"
        approval = ("--dangerously-skip-permissions",)
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


def _require_native_read_request(request: ProviderRequest) -> None:
    if not isinstance(request, ProviderRequest):
        raise ProviderRequestError("request must be a ProviderRequest")
    if request.role == "owner" or request.execution_class == _REPO_WRITE:
        raise ProviderRequestError("shared-repository writes belong to the invoking owner CLI")
    if request.retries:
        raise ProviderRequestError("native automatic retries must be zero")
    if request.resume and (request.profile is None or request.profile.native_roots is None):
        raise ProviderRequestError(
            "native resume lacks historical discovery roots; use fresh admission or historical inspection")


def run_provider(request: ProviderRequest, *, registry: ProviderRegistry | None = None) -> ProviderResult:
    """Run a trusted native read turn; owner actions stay with the invoking CLI."""
    _require_native_read_request(request)
    return _run_provider_with_runner(request, registry=registry, runner=run_command,
                                     capture_native=True)


def _run_provider_with_runner(request: ProviderRequest, *, registry: ProviderRegistry | None = None,
                              runner: Callable[[ProcessCommand], ProcessResult],
                              capture_native: bool = False) -> ProviderResult:
    """Hermetic transport seam for tests; production callers use run_provider."""
    if not isinstance(request, ProviderRequest):
        raise ProviderRequestError("request must be a ProviderRequest")
    if request.profile is None:
        raise ProviderRequestError("provider request requires a pinned profile")
    selected_registry = registry or ProviderRegistry.default()
    profile = selected_registry.admit(
        request.executor_id, request.execution_class,
        request.profile.quality_tier, role=request.role,
    )
    if request.profile.digest != profile.digest:
        raise ProviderRequestError("request executor profile changed before provider launch")
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
        observed_version = selected_registry.assert_installed_version(profile)
        if ((request.run_id is not None or request.inputs_digest is not None)
                and observed_version != profile.cli_version):
            raise ProviderRequestError(
                "recorded CLI version changed before launch; use a reviewed transition or fresh admission"
            )
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
    result = _make_result(request, adapter, last_process, parsed, attempts, duration,
                          observed_cli_version=observed_version)
    from .diagnostic import describe_native_launch
    result = replace(result, launch_metadata=describe_native_launch(
        profile, command, observed_cli_version=observed_version,
        observed_tools=parsed.observed_tools if parsed else (),
    ))
    if capture_native:
        from .native_trace import capture_native_trace
        trace = capture_native_trace(request, result)
        result = replace(result, launch_metadata={**result.launch_metadata, "native_tool_trace": trace})
        if request.run_id is not None and result.valid and trace["coverage"] != "complete":
            result = replace(result, valid=False, reason="read-trace-unqualified",
                             hint="native tool evidence lacks exact complete turn coverage")
    return result


def run_many(requests: Sequence[ProviderRequest], *, max_workers: int = DEFAULT_EXECUTOR_SLOTS,
             registry: ProviderRegistry | None = None) -> tuple[ProviderResult, ...]:
    """Run a bounded batch while retaining caller order and each terminal result."""
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
        raise ProviderRequestError("max_workers must be a positive integer")
    for request in requests:
        _require_native_read_request(request)
    return _run_many_with_runner(requests, max_workers=max_workers, registry=registry, runner=run_command,
                                 capture_native=True)


def _run_many_with_runner(requests: Sequence[ProviderRequest], *, max_workers: int,
                          registry: ProviderRegistry | None = None,
                          runner: Callable[[ProcessCommand], ProcessResult],
                          capture_native: bool = False) -> tuple[ProviderResult, ...]:
    """Hermetic bounded-batch seam for fake process tests."""
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
        raise ProviderRequestError("max_workers must be a positive integer")
    if not requests:
        return ()
    workers = min(max_workers, DEFAULT_EXECUTOR_SLOTS, len(requests))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_provider_with_runner, request, registry=registry, runner=runner,
                               capture_native=capture_native)
                   for request in requests]
        return tuple(future.result() for future in futures)


def _make_result(request: ProviderRequest, adapter: ProviderAdapter, process: ProcessResult,
                 parsed: _Parsed | None, attempts: int, duration: float, *,
                 observed_cli_version: str | None = None) -> ProviderResult:
    answer = parsed.answer if parsed else ""
    session_id = parsed.session_id if parsed else None
    usage = parsed.usage if parsed else None
    usage_evidence = (parsed.usage_evidence if parsed else None) or _native_usage_evidence(
        request.executor_id, None, process.stdout, session_id=session_id)
    usage_evidence = replace(usage_evidence, role=request.role, resumed=request.resume,
        session_id=session_id or request.session_id,
        declared_cli_version=request.profile.cli_version if request.profile else None,
        observed_cli_version=observed_cli_version,
        turn_id=_digest(canonical_json({"prefix": request.artifact_prefix,
            "prompt": _digest(request.prompt_bytes), "source": usage_evidence.source_sha256})))
    if parsed and parsed.error:
        valid, reason, hint = False, "provider-error", parsed.error
    elif process.status == ProcessStatus.TIMEOUT:
        valid, reason, hint = False, "timeout", "provider exceeded its configured deadline"
    elif process.status == ProcessStatus.SPAWN_ERROR:
        valid, reason, hint = False, "spawn-error", process.error or "provider did not start"
    elif (process.status is not ProcessStatus.EXIT or type(process.returncode) is not int
          or process.returncode != 0 or not process.capture_complete):
        valid, reason, hint = False, "process-error", process.error or "provider process evidence is incomplete or unsuccessful"
    elif parsed and parsed.successful_file_change and request.execution_class == _READ_ONLY:
        valid, reason, hint = False, "protocol-error", "read-only provider reported a successful file change"
    elif parsed is None or not parsed.final or not session_id or not answer.strip():
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
        usage_evidence=usage_evidence,
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
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
        result["cost_usd"] = cost
    return result or None


_USAGE_NAMES = {
    "input_tokens": "input", "output_tokens": "output", "cached_input_tokens": "cache_read",
    "cache_read_input_tokens": "cache_read", "cache_creation_input_tokens": "cache_write",
    "cache_write_input_tokens": "cache_write", "reasoning_output_tokens": "reasoning",
    "thinking_tokens": "thinking", "cache_read_tokens": "cache_read", "total_tokens": "total",
}
USAGE_COUNTERS = ("input", "output", "cache_read", "cache_write", "reasoning", "thinking",
                  "total", "consumed_input", "provider_requests", "cost_usd")


def _native_usage_evidence(provider, raw, stdout, *, session_id, cost=None, model_usage=None):
    raw = raw if isinstance(raw, dict) else {}
    normalized = dict(_usage(raw, _USAGE_NAMES, cost=cost) or {})
    if provider == "claude":
        parts = [normalized.get(name) for name in ("input", "cache_read", "cache_write")]
        if all(type(part) is int for part in parts):
            normalized["consumed_input"] = sum(parts)
    elif provider == "codex" and "input" in normalized:
        # Codex input already includes cached input; these are not disjoint buckets.
        normalized["consumed_input"] = normalized["input"]

    def numeric_rows(value, prefix):
        for name, child in value.items():
            if type(child) in {int, float} and math.isfinite(child):
                yield prefix + str(name), child
            elif isinstance(child, dict):
                yield from numeric_rows(child, prefix + str(name) + ".")

    raw_rows = list(numeric_rows(raw, "usage."))
    if type(cost) in {int, float} and math.isfinite(cost):
        raw_rows.append(("total_cost_usd", cost))
    if isinstance(model_usage, dict):
        raw_rows.extend(numeric_rows(model_usage, "modelUsage."))
    return UsageEvidence(provider, session_id, _digest(stdout), _digest(stdout),
        counters=tuple((name, value,
            "session-cumulative" if provider == "codex" or name == "cost_usd" else
            "turn" if provider == "claude" else "unknown",
            "estimate" if name == "cost_usd" else "provider-reported")
            for name, value in normalized.items()), raw_counters=tuple(raw_rows),
        scope_evidence={"codex": "upstream-source-uncharacterized",
            "claude": "provider-docs-uncharacterized"}.get(provider, "uncharacterized"),
        coverage="main-loop-only" if provider == "claude" else "native-session" if provider == "codex" else "unknown")


def aggregate_usage_evidence(records: Iterable[UsageEvidence | None], *,
                             required_roles: Iterable[str] = ()) -> dict[str, object]:
    """Report every supplied turn; cumulative deltas require its initial session."""
    entries = []
    seen = {}
    for evidence in records:
        if evidence is None:
            entries.append(None)
            continue
        if not isinstance(evidence, UsageEvidence):
            raise ProviderRequestError("usage aggregation requires versioned evidence")
        identity = evidence.provider, evidence.session_id, evidence.turn_id
        if identity in seen:
            if seen[identity] != evidence:
                raise ProviderRequestError("usage turn identity has conflicting evidence")
            continue
        seen[identity] = evidence
        entries.append(evidence)
    entries.sort(key=lambda item: item.captured_at_ns if item is not None else math.inf)
    session_sizes = {}
    for evidence in entries:
        if evidence is not None:
            key = evidence.provider, evidence.session_id
            session_sizes[key] = session_sizes.get(key, 0) + 1
    previous = {}
    contributions = []
    for evidence in entries:
        values = dict.fromkeys(USAGE_COUNTERS)
        if evidence is None:
            contributions.append(("unknown", values))
            continue
        key = evidence.provider, evidence.session_id
        prior = previous.get(key)
        counters = {name: (value, scope, kind) for name, value, scope, kind in evidence.counters}
        for name, (value, scope, _) in counters.items():
            if value == 0 or evidence.session_id is None:
                continue
            if scope == "turn" or scope == "unknown" and session_sizes[key] == 1 and evidence.resumed is False:
                values[name] = value
            elif scope == "session-cumulative":
                if prior is None and evidence.resumed is False:
                    values[name] = value
                elif prior is not None and prior.get(name) is not None and value >= prior[name]:
                    values[name] = value - prior[name]
        # A reset, missing counter or absent initial observation taints later deltas.
        previous[key] = {name: counters[name][0] if values.get(name) is not None else None
                         for name in USAGE_COUNTERS if name in counters}
        contributions.append((evidence.role, values))

    def totals(rows):
        return {name: sum(row[name] for row in rows)
                if rows and all(row.get(name) is not None for row in rows) else None
                for name in USAGE_COUNTERS}

    roles = {role for role, _ in contributions}
    missing = sorted(set(required_roles) - roles)
    return {"schema_version": "fanout-usage-summary-v1", "entries": len(entries),
        "totals": dict.fromkeys(USAGE_COUNTERS) if missing else totals([row for _, row in contributions]),
        "by_role": {role: totals([row for current, row in contributions if current == role]) for role in sorted(roles)},
        "missing_roles": missing, "counter_kinds": {name: "estimate" if name == "cost_usd" else "provider-reported"
                                                     for name in USAGE_COUNTERS},
        "measured_savings_eligible": False}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "AgyAdapter", "ClaudeAdapter", "CodexAdapter", "ExecutorProfile",
    "ProviderAdapter", "ProviderCapabilities", "ProviderRegistry", "ProviderRequest",
    "ProviderResult", "RoutingTargetRegistry", "run_many", "run_provider",
    "UsageEvidence", "aggregate_usage_evidence",
]
