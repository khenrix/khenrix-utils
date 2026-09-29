"""Immutable v1 execution-plan schema and deterministic scheduling helpers.

This module deliberately stops at the plan boundary: it neither parses a
Superpowers document nor resolves a skill or starts an executor.  Its job is
to make that later work consume one strict, canonical, self-contained plan.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .artifacts import canonical_json
from .errors import PlanValidationError
from .targets import TargetSpec


SCHEMA_VERSION = "v1"
DEFAULT_EXECUTOR_IDS = ("claude", "codex", "agy")
_EXECUTION_CLASSES = frozenset({"read-only", "repo-write", "orchestrator-action"})
_EXECUTOR_CLASSES = frozenset({"read-only", "repo-write"})
_ORCHESTRATOR_CONTROL_SKILLS = frozenset({
    "brainstorming", "dispatching-parallel-agents", "executing-plans",
    "finishing-a-development-branch", "llm-council", "llm-fanout-execute",
    "llm-fanout-plan", "llm-forge", "requesting-code-review",
    "subagent-driven-development", "using-git-worktrees", "using-superpowers",
    "verification-before-completion", "writing-plans",
})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_STEP_ID = re.compile(r"Task [1-9][0-9]*/Step [1-9][0-9]*\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_POSIX_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh", "csh", "tcsh"})
_WINDOWS_COMMAND_SHELLS = frozenset({"cmd", "cmd.exe"})
_POWERSHELLS = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})


@dataclass(frozen=True, slots=True)
class SourceInfoV1:
    """The approved input and parser identity from which the plan was compiled."""

    path: str
    sha256: str
    parser_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _contained_path(self.path, "source path"))
        _sha256(self.sha256, "source sha256")
        object.__setattr__(self, "parser_version", _text(self.parser_version, "parser_version"))

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256, "parser_version": self.parser_version}


@dataclass(frozen=True, slots=True)
class SourceStepV1:
    """One atomic source Task/Step whose content may not be subdivided."""

    id: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _SOURCE_STEP_ID.fullmatch(self.id):
            raise PlanValidationError("source step id must be a stable 'Task N/Step N' identifier")
        _sha256(self.sha256, f"source step {self.id} sha256")

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class SourceStepV2(SourceStepV1):
    target_id: str

    def __post_init__(self) -> None:
        SourceStepV1.__post_init__(self)
        object.__setattr__(self, "target_id", _text(self.target_id, "source step target_id"))

    def to_dict(self) -> dict[str, str]:
        return {**SourceStepV1.to_dict(self), "target_id": self.target_id}


@dataclass(frozen=True, slots=True)
class ProviderPolicyV1:
    """Bounded provider settings for an executor work item."""

    executor_ids: tuple[str, ...] = DEFAULT_EXECUTOR_IDS
    rounds: int = 2
    timeout: int | float | None = None
    retries: int = 0
    minimum_success: int = 2
    quality_tier: str = "standard"
    timeout_override_reason: str | None = None
    timeout_override_review_sha256: str | None = None

    def __post_init__(self) -> None:
        ids = _string_tuple(self.executor_ids, "executor_ids", nonempty=True)
        if "maka" in ids:
            raise PlanValidationError("maka is a routing/evaluation target, not an executor")
        if len(ids) < 2 or len(ids) != len(set(ids)):
            raise PlanValidationError("executor_ids must contain at least two unique executors")
        _int_in_range(self.rounds, "rounds", 1, 3)
        if self.timeout is not None:
            _positive_number(self.timeout, "timeout", maximum=3600)
        _int_in_range(self.retries, "retries", 0, 3)
        _int_in_range(self.minimum_success, "minimum_success", 2, len(ids))
        if self.quality_tier not in {"standard", "deep"}:
            raise PlanValidationError("quality_tier must be standard or deep")
        if self.timeout_override_reason is not None and (
            not isinstance(self.timeout_override_reason, str)
            or not self.timeout_override_reason.strip()
        ):
            raise PlanValidationError("timeout override reason must be non-empty")
        if self.timeout_override_review_sha256 is not None:
            _sha256(self.timeout_override_review_sha256, "timeout override review sha256")
        if (self.timeout_override_reason is None) != (self.timeout_override_review_sha256 is None):
            raise PlanValidationError("timeout override requires both reason and review evidence")
        object.__setattr__(self, "executor_ids", ids)

    def effective_timeout(self, profile: object) -> int | float:
        """Apply a selected profile's default and ceiling to this task policy."""
        if getattr(profile, "quality_tier", None) != self.quality_tier:
            raise PlanValidationError("provider profile quality tier differs from the task")
        default = getattr(profile, "default_timeout", None)
        ceiling = getattr(profile, "timeout_ceiling", None)
        if (
            isinstance(default, bool) or not isinstance(default, (int, float))
            or isinstance(ceiling, bool) or not isinstance(ceiling, (int, float))
            or not 0 < default <= ceiling <= 3600
        ):
            raise PlanValidationError("selected provider profile timeout is invalid")
        effective = default if self.timeout is None else self.timeout
        if effective > ceiling and (
            self.timeout_override_reason is None
            or self.timeout_override_review_sha256 is None
        ):
            raise PlanValidationError("over-ceiling timeout needs a reviewed override reason")
        return effective

    @classmethod
    def from_dict(cls, data: object, *, where: str) -> "ProviderPolicyV1":
        values = _object(data, where, {
            "executor_ids", "rounds", "timeout", "retries", "minimum_success",
            "quality_tier", "timeout_override_reason", "timeout_override_review_sha256",
        }, required={"executor_ids", "rounds", "retries", "minimum_success"})
        return cls(
            executor_ids=tuple(_list(values["executor_ids"], f"{where}.executor_ids")),
            rounds=values["rounds"],
            timeout=values.get("timeout"),
            retries=values["retries"],
            minimum_success=values["minimum_success"],
            quality_tier=values.get("quality_tier", "standard"),
            timeout_override_reason=values.get("timeout_override_reason"),
            timeout_override_review_sha256=values.get("timeout_override_review_sha256"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "executor_ids": list(self.executor_ids),
            "rounds": self.rounds,
            "timeout": self.timeout,
            "retries": self.retries,
            "minimum_success": self.minimum_success,
            "quality_tier": self.quality_tier,
            "timeout_override_reason": self.timeout_override_reason,
            "timeout_override_review_sha256": self.timeout_override_review_sha256,
        }


@dataclass(frozen=True, slots=True)
class PlanCheckV1:
    """A bounded argv-only verifier declaration; ``cwd=''`` means repository root."""

    argv: tuple[str, ...]
    cwd: str = ""
    env_allowlist: tuple[str, ...] = ()
    timeout: int | float = 120
    accepted_exit_codes: tuple[int, ...] = (0,)
    expected_artifacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        argv = _string_tuple(self.argv, "check argv", nonempty=True)
        _reject_shell_command_string(argv)
        cwd = _contained_path(self.cwd, "check cwd", allow_root=True)
        env = _string_tuple(self.env_allowlist, "check env_allowlist")
        if len(env) != len(set(env)) or any(not _ENV_NAME.fullmatch(name) for name in env):
            raise PlanValidationError("check env_allowlist must contain unique environment variable names")
        _positive_number(self.timeout, "check timeout", maximum=3600)
        codes = _exit_codes(self.accepted_exit_codes)
        artifacts = tuple(_contained_path(path, "expected artifact") for path in self.expected_artifacts)
        if len(artifacts) != len(set(artifacts)):
            raise PlanValidationError("expected_artifacts must be unique")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "env_allowlist", env)
        object.__setattr__(self, "accepted_exit_codes", codes)
        object.__setattr__(self, "expected_artifacts", artifacts)

    @classmethod
    def from_dict(cls, data: object, *, where: str) -> "PlanCheckV1":
        values = _object(data, where, {
            "argv", "cwd", "env_allowlist", "timeout", "accepted_exit_codes", "expected_artifacts",
        }, required={"argv", "cwd", "env_allowlist", "timeout", "accepted_exit_codes", "expected_artifacts"})
        return cls(
            argv=tuple(_list(values["argv"], f"{where}.argv")),
            cwd=values["cwd"],
            env_allowlist=tuple(_list(values["env_allowlist"], f"{where}.env_allowlist")),
            timeout=values["timeout"],
            accepted_exit_codes=tuple(_list(values["accepted_exit_codes"], f"{where}.accepted_exit_codes")),
            expected_artifacts=tuple(_list(values["expected_artifacts"], f"{where}.expected_artifacts")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv), "cwd": self.cwd,
            "env_allowlist": list(self.env_allowlist), "timeout": self.timeout,
            "accepted_exit_codes": list(self.accepted_exit_codes),
            "expected_artifacts": list(self.expected_artifacts),
        }


@dataclass(frozen=True, slots=True)
class PlanTaskV1:
    """A structural group or one schedulable work item in a v1 plan."""

    id: str
    kind: str
    title: str
    objective: str
    parent_id: str | None = None
    source_step_ids: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    execution_class: str | None = None
    required_skills: tuple[str, ...] = ()
    none_reason: str | None = None
    owned_paths: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    checks: tuple[PlanCheckV1, ...] = ()
    provider_policy: ProviderPolicyV1 | None = None
    reconciliation_policy: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "task id"))
        if self.kind not in {"group", "work"}:
            raise PlanValidationError("task kind must be group or work")
        object.__setattr__(self, "title", _text(self.title, "task title"))
        object.__setattr__(self, "objective", _text(self.objective, "task objective"))
        if self.parent_id is not None:
            object.__setattr__(self, "parent_id", _identifier(self.parent_id, "parent_id"))
        source_ids = _string_tuple(self.source_step_ids, "source_step_ids")
        deps = _string_tuple(self.depends_on, "depends_on")
        skills = _string_tuple(self.required_skills, "required_skills")
        paths = tuple(_contained_path(path, "owned path") for path in self.owned_paths)
        acceptance = _string_tuple(self.acceptance, "acceptance")
        checks = tuple(self.checks)
        if any(not isinstance(check, PlanCheckV1) for check in checks):
            raise PlanValidationError("checks must contain PlanCheckV1 values")
        if len(source_ids) != len(set(source_ids)) or len(deps) != len(set(deps)):
            raise PlanValidationError("source_step_ids and depends_on must be unique")
        if len(skills) != len(set(skills)):
            raise PlanValidationError("required_skills must be unique")
        if len(paths) != len(set(paths)):
            raise PlanValidationError("owned_paths must be unique")
        if len(acceptance) != len(set(acceptance)):
            raise PlanValidationError("acceptance statements must be unique")
        reason = _optional_text(self.none_reason, "none_reason")
        if self.kind == "group":
            if any((source_ids, deps, paths, acceptance, checks)) or self.execution_class is not None:
                raise PlanValidationError("group tasks may only express hierarchy and required_skills")
            if reason is not None or self.provider_policy is not None or self.reconciliation_policy is not None:
                raise PlanValidationError("group tasks may not carry executor policy or none_reason")
        else:
            if not source_ids:
                raise PlanValidationError("work tasks must reference at least one source step")
            if self.execution_class not in _EXECUTION_CLASSES:
                raise PlanValidationError("work execution_class is invalid")
            if not acceptance:
                raise PlanValidationError("work tasks require acceptance statements")
            if self.execution_class == "orchestrator-action":
                if skills or self.provider_policy is not None or checks or paths or self.reconciliation_policy is not None:
                    raise PlanValidationError("orchestrator-action tasks cannot carry executor work")
                if reason is None:
                    raise PlanValidationError("orchestrator-action tasks require a none_reason")
            else:
                if self.provider_policy is not None and not isinstance(self.provider_policy, ProviderPolicyV1):
                    raise PlanValidationError("provider_policy must be a ProviderPolicyV1 or null")
                policy = (
                    self.reconciliation_policy if self.reconciliation_policy is not None
                    else "synthesis-required" if self.execution_class == "repo-write"
                    else "select-or-synthesize"
                )
                if not isinstance(policy, str) or policy not in {"select-or-synthesize", "synthesis-required"}:
                    raise PlanValidationError("reconciliation_policy is invalid")
                if self.execution_class == "repo-write" and policy != "synthesis-required":
                    raise PlanValidationError("repo-write reconciliation requires synthesis")
                object.__setattr__(self, "reconciliation_policy", policy)
        object.__setattr__(self, "source_step_ids", source_ids)
        object.__setattr__(self, "depends_on", deps)
        object.__setattr__(self, "required_skills", skills)
        object.__setattr__(self, "none_reason", reason)
        object.__setattr__(self, "owned_paths", paths)
        object.__setattr__(self, "acceptance", acceptance)
        object.__setattr__(self, "checks", checks)

    @classmethod
    def from_dict(cls, data: object, *, where: str) -> "PlanTaskV1":
        values = _object(data, where, {
            "id", "kind", "parent_id", "title", "objective", "source_step_ids", "depends_on",
            "execution_class", "required_skills", "none_reason", "owned_paths", "acceptance",
            "checks", "provider_policy", "reconciliation_policy",
        }, required={"id", "kind", "parent_id", "title", "objective", "required_skills"})
        kind = values["kind"]
        if kind == "work":
            missing = {"source_step_ids", "depends_on", "execution_class", "none_reason", "owned_paths",
                       "acceptance", "checks", "provider_policy"} - set(values)
            if missing:
                raise PlanValidationError(f"{where} is missing fields: {', '.join(sorted(missing))}")
        return cls(
            id=values["id"], kind=kind, parent_id=values["parent_id"], title=values["title"],
            objective=values["objective"],
            source_step_ids=tuple(_list(values.get("source_step_ids", []), f"{where}.source_step_ids")),
            depends_on=tuple(_list(values.get("depends_on", []), f"{where}.depends_on")),
            execution_class=values.get("execution_class"),
            required_skills=tuple(_list(values["required_skills"], f"{where}.required_skills")),
            none_reason=values.get("none_reason"),
            owned_paths=tuple(_list(values.get("owned_paths", []), f"{where}.owned_paths")),
            acceptance=tuple(_list(values.get("acceptance", []), f"{where}.acceptance")),
            checks=tuple(PlanCheckV1.from_dict(item, where=f"{where}.checks[{index}]")
                         for index, item in enumerate(_list(values.get("checks", []), f"{where}.checks"))),
            provider_policy=(None if values.get("provider_policy") is None else
                             ProviderPolicyV1.from_dict(values["provider_policy"], where=f"{where}.provider_policy")),
            reconciliation_policy=values.get("reconciliation_policy"),
        )

    def to_dict(self) -> dict[str, object]:
        value = {
            "id": self.id, "kind": self.kind, "parent_id": self.parent_id,
            "title": self.title, "objective": self.objective,
            "source_step_ids": list(self.source_step_ids), "depends_on": list(self.depends_on),
            "execution_class": self.execution_class, "required_skills": list(self.required_skills),
            "none_reason": self.none_reason, "owned_paths": list(self.owned_paths),
            "acceptance": list(self.acceptance), "checks": [check.to_dict() for check in self.checks],
            "provider_policy": None if self.provider_policy is None else self.provider_policy.to_dict(),
        }
        if self.execution_class == "read-only" and self.reconciliation_policy == "synthesis-required":
            value["reconciliation_policy"] = self.reconciliation_policy
        return value


@dataclass(frozen=True, slots=True)
class PlanTaskV2(PlanTaskV1):
    target_id: str | None = None
    dependency_modes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        PlanTaskV1.__post_init__(self)
        if self.kind == "work":
            object.__setattr__(self, "target_id", _text(self.target_id, "task target_id"))
        elif self.target_id is not None:
            raise PlanValidationError("group tasks cannot declare a target_id")
        if not isinstance(self.dependency_modes, Mapping):
            raise PlanValidationError("dependency_modes must be an object")
        modes = dict(self.dependency_modes)
        if set(modes) != set(self.depends_on):
            raise PlanValidationError("dependency_modes must name exactly the task dependencies")
        if any(not isinstance(mode, str) or mode not in {"artifact", "handover"}
               for mode in modes.values()):
            raise PlanValidationError("dependency_modes values must be artifact or handover")
        object.__setattr__(self, "dependency_modes", MappingProxyType(modes))

    @classmethod
    def from_dict(cls, data: object, *, where: str) -> "PlanTaskV2":
        if not isinstance(data, dict):
            raise PlanValidationError(f"{where} must be an object")
        extra = {"target_id", "dependency_modes"}
        if data.get("kind") == "work" and not extra <= set(data):
            raise PlanValidationError(f"{where} requires target_id and dependency_modes")
        base = PlanTaskV1.from_dict({key: value for key, value in data.items() if key not in extra}, where=where)
        return cls(
            **{item.name: getattr(base, item.name) for item in fields(PlanTaskV1)},
            target_id=data.get("target_id"), dependency_modes=data.get("dependency_modes", {}),
        )

    def to_dict(self) -> dict[str, object]:
        value = PlanTaskV1.to_dict(self)
        if self.kind == "work":
            value.update({"target_id": self.target_id, "dependency_modes": dict(self.dependency_modes)})
        return value


@dataclass(frozen=True, slots=True)
class FanoutPlanV1:
    """The complete immutable v1 scheduling packet."""

    source: SourceInfoV1
    defaults: ProviderPolicyV1 = field(default_factory=ProviderPolicyV1)
    source_steps: tuple[SourceStepV1, ...] = ()
    tasks: tuple[PlanTaskV1, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise PlanValidationError("schema_version must be exactly v1")
        if not isinstance(self.source, SourceInfoV1):
            raise PlanValidationError("source must be SourceInfoV1")
        if not isinstance(self.defaults, ProviderPolicyV1):
            raise PlanValidationError("defaults must be ProviderPolicyV1")
        steps = tuple(self.source_steps)
        tasks = tuple(self.tasks)
        if not steps or not tasks:
            raise PlanValidationError("plans require non-empty source_steps and tasks")
        if any(type(step) is not SourceStepV1 for step in steps):
            raise PlanValidationError("source_steps must contain SourceStepV1 values")
        if any(type(task) is not PlanTaskV1 for task in tasks):
            raise PlanValidationError("tasks must contain PlanTaskV1 values")
        if len({step.id for step in steps}) != len(steps):
            raise PlanValidationError("source step IDs must be unique")
        if len({task.id for task in tasks}) != len(tasks):
            raise PlanValidationError("task IDs must be unique")
        object.__setattr__(self, "source_steps", steps)
        object.__setattr__(self, "tasks", tasks)
        _validate_structure(self)

    @classmethod
    def from_dict(cls, data: object) -> "FanoutPlanV1":
        values = _object(data, "plan", {"schema_version", "source", "defaults", "source_steps", "tasks"},
                         required={"schema_version", "source", "defaults", "source_steps", "tasks"})
        source_data = _object(values["source"], "source", {"path", "sha256", "parser_version"},
                              required={"path", "sha256", "parser_version"})
        return cls(
            schema_version=values["schema_version"],
            source=SourceInfoV1(**source_data),
            defaults=ProviderPolicyV1.from_dict(values["defaults"], where="defaults"),
            source_steps=tuple(_source_step(item, index) for index, item in enumerate(_list(values["source_steps"], "source_steps"))),
            tasks=tuple(PlanTaskV1.from_dict(item, where=f"tasks[{index}]")
                        for index, item in enumerate(_list(values["tasks"], "tasks"))),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "source": self.source.to_dict(),
            "defaults": self.defaults.to_dict(),
            "source_steps": [step.to_dict() for step in self.source_steps],
            "tasks": [task.to_dict() for task in self.tasks],
        }


@dataclass(frozen=True, slots=True)
class FanoutPlanV2(FanoutPlanV1):
    targets: tuple[TargetSpec, ...] = ()
    schema_version: str = "v2"

    def __post_init__(self) -> None:
        if self.schema_version != "v2":
            raise PlanValidationError("schema_version must be exactly v2")
        if not isinstance(self.source, SourceInfoV1) or not isinstance(self.defaults, ProviderPolicyV1):
            raise PlanValidationError("v2 source or defaults is invalid")
        targets, steps, tasks = tuple(self.targets), tuple(self.source_steps), tuple(self.tasks)
        if not targets or not steps or not tasks:
            raise PlanValidationError("v2 plans require targets, source_steps, and tasks")
        if any(not isinstance(target, TargetSpec) for target in targets):
            raise PlanValidationError("targets must contain TargetSpec values")
        if len({target.id for target in targets}) != len(targets):
            raise PlanValidationError("target ids must be unique")
        if any(not isinstance(step, SourceStepV2) for step in steps):
            raise PlanValidationError("source_steps must contain SourceStepV2 values")
        if any(not isinstance(task, PlanTaskV2) for task in tasks):
            raise PlanValidationError("tasks must contain PlanTaskV2 values")
        if len({step.id for step in steps}) != len(steps) or len({task.id for task in tasks}) != len(tasks):
            raise PlanValidationError("source step and task ids must be unique")
        registry = {target.id for target in targets}
        if any(step.target_id not in registry for step in steps):
            raise PlanValidationError("source step target_id is absent from targets")
        source_task_targets: dict[str, str] = {}
        for step in steps:
            source_task_id = step.id.split("/", 1)[0]
            previous = source_task_targets.setdefault(source_task_id, step.target_id)
            if previous != step.target_id:
                raise PlanValidationError(f"source Task {source_task_id} declares more than one target")
        step_targets = {step.id: step.target_id for step in steps}
        writers: set[str] = set()
        for task in tasks:
            if task.kind != "work":
                continue
            if task.target_id not in registry:
                raise PlanValidationError(f"task {task.id} target_id is absent from targets")
            if any(step_targets.get(source_id) != task.target_id for source_id in task.source_step_ids):
                raise PlanValidationError(f"task {task.id} source step target differs from task target")
            if task.execution_class == "repo-write":
                if task.target_id in writers:
                    raise PlanValidationError(f"target {task.target_id} permits only one repository writer")
                writers.add(task.target_id)
        task_index = {task.id: task for task in tasks}
        for task in tasks:
            if task.kind != "work":
                continue
            for predecessor_id, mode in task.dependency_modes.items():
                predecessor = task_index.get(predecessor_id)
                if mode == "handover" and (predecessor is None or predecessor.execution_class != "repo-write"):
                    raise PlanValidationError(f"task {task.id} handover requires a repository writer predecessor")
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "source_steps", steps)
        object.__setattr__(self, "tasks", tasks)
        _validate_structure(self)

    @classmethod
    def from_dict(cls, data: object) -> "FanoutPlanV2":
        values = _object(data, "plan", {"schema_version", "source", "defaults", "source_steps", "tasks", "targets"},
                         required={"schema_version", "source", "defaults", "source_steps", "tasks", "targets"})
        source_data = _object(values["source"], "source", {"path", "sha256", "parser_version"},
                              required={"path", "sha256", "parser_version"})
        return cls(
            schema_version=values["schema_version"], source=SourceInfoV1(**source_data),
            defaults=ProviderPolicyV1.from_dict(values["defaults"], where="defaults"),
            targets=tuple(TargetSpec.from_dict(item) for item in _list(values["targets"], "targets")),
            source_steps=tuple(SourceStepV2(**_object(item, f"source_steps[{index}]",
                {"id", "sha256", "target_id"}, required={"id", "sha256", "target_id"}))
                for index, item in enumerate(_list(values["source_steps"], "source_steps"))),
            tasks=tuple(PlanTaskV2.from_dict(item, where=f"tasks[{index}]")
                for index, item in enumerate(_list(values["tasks"], "tasks"))),
        )

    def to_dict(self) -> dict[str, object]:
        return {**FanoutPlanV1.to_dict(self), "targets": [target.to_dict() for target in self.targets]}


def load_fanout_plan(data: Mapping[str, object]) -> FanoutPlanV1 | FanoutPlanV2:
    """Load an exact plan schema without interpreting one version as another."""
    if not isinstance(data, Mapping):
        raise PlanValidationError("plan must be a mapping")
    if data.get("schema_version") == "v1":
        return FanoutPlanV1.from_dict(dict(data))
    if data.get("schema_version") == "v2":
        return FanoutPlanV2.from_dict(dict(data))
    raise PlanValidationError("schema_version must be exactly v1 or v2")


def validate_plan(plan: FanoutPlanV1 | FanoutPlanV2 | Mapping[str, object]) -> FanoutPlanV1 | FanoutPlanV2:
    """Validate a typed plan or parse and validate a JSON-shaped mapping."""
    if isinstance(plan, FanoutPlanV2):
        FanoutPlanV2.__post_init__(plan)
        return plan
    if isinstance(plan, FanoutPlanV1):
        FanoutPlanV1.__post_init__(plan)
        return plan
    if isinstance(plan, Mapping):
        return load_fanout_plan(plan)
    raise PlanValidationError("plan must be a FanoutPlanV1, FanoutPlanV2, or mapping")


def effective_skills(plan: FanoutPlanV1, task_id: str) -> tuple[str, ...]:
    """Return a work task's inherited executor skills in deterministic root-to-leaf order."""
    plan = validate_plan(plan)
    task = _task_index(plan).get(task_id)
    if task is None or task.kind != "work":
        raise PlanValidationError("effective skills require a known work task")
    return _effective_skills(plan, task)


def topological_order(plan: FanoutPlanV1) -> tuple[str, ...]:
    """Return dependency-safe work IDs, preserving declared order among ready peers."""
    plan = validate_plan(plan)
    return _topological_order(plan)


def _topological_order(plan: FanoutPlanV1) -> tuple[str, ...]:
    work = tuple(task for task in plan.tasks if task.kind == "work")
    order = {task.id: index for index, task in enumerate(work)}
    remaining = {task.id: set(task.depends_on) for task in work}
    result: list[str] = []
    while remaining:
        ready = sorted((task_id for task_id, deps in remaining.items() if not deps), key=order.__getitem__)
        if not ready:
            raise PlanValidationError("work dependencies contain a cycle")
        result.extend(ready)
        for task_id in ready:
            del remaining[task_id]
        ready_set = set(ready)
        for deps in remaining.values():
            deps.difference_update(ready_set)
    return tuple(result)


def write_plan(path: Path | str, plan: FanoutPlanV1, *, replace: bool = False) -> None:
    """Atomically publish canonical bytes, exclusively unless replacement is explicit."""
    if not isinstance(replace, bool):
        raise PlanValidationError("replace must be a boolean")
    plan = validate_plan(plan)
    destination = Path(path)
    if not destination.name or destination.name in {".", ".."} or not destination.parent.is_dir():
        raise PlanValidationError("plan destination must have an existing parent directory")
    payload = canonical_json(plan.to_dict())
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        if replace:
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                raise PlanValidationError(f"plan revision already exists: {destination}") from error
            os.unlink(temporary)
        _fsync_directory(destination.parent)
    except OSError as error:
        raise PlanValidationError(f"could not publish plan: {destination}") from error
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_plan(path: Path | str) -> FanoutPlanV1:
    """Read exactly one canonical UTF-8 plan revision without duplicate JSON keys."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise PlanValidationError(f"could not read plan: {path}") from error
    try:
        decoded = raw.decode("utf-8")
        data = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, PlanValidationError) as error:
        raise PlanValidationError("plan is not strict UTF-8 JSON") from error
    plan = load_fanout_plan(data)
    if raw != canonical_json(plan.to_dict()):
        raise PlanValidationError("plan is not canonical JSON")
    return plan


def _validate_structure(plan: FanoutPlanV1) -> None:
    tasks = _task_index(plan)
    steps = {step.id for step in plan.source_steps}
    work = tuple(task for task in plan.tasks if task.kind == "work")
    for task in plan.tasks:
        if task.parent_id is not None:
            parent = tasks.get(task.parent_id)
            if parent is None or parent.kind != "group":
                raise PlanValidationError(f"task {task.id} has no group parent {task.parent_id}")
            if parent.id == task.id:
                raise PlanValidationError(f"task {task.id} cannot parent itself")
    _validate_parent_cycles(tasks)
    for task in plan.tasks:
        if task.kind == "work":
            if task.execution_class == "repo-write" and task.reconciliation_policy != "synthesis-required":
                raise PlanValidationError("repo-write reconciliation requires synthesis")
            if task.execution_class == "read-only" and task.reconciliation_policy not in {
                "select-or-synthesize", "synthesis-required"
            }:
                raise PlanValidationError("read-only reconciliation_policy is invalid")
            if task.execution_class == "orchestrator-action" and task.reconciliation_policy is not None:
                raise PlanValidationError("orchestrator-action cannot carry reconciliation_policy")
            if task.id in task.depends_on:
                raise PlanValidationError(f"work task {task.id} cannot depend on itself")
            for dependency in task.depends_on:
                target = tasks.get(dependency)
                if target is None or target.kind != "work":
                    raise PlanValidationError(f"work task {task.id} depends on non-work task {dependency}")
            unknown = set(task.source_step_ids) - steps
            if unknown:
                raise PlanValidationError(f"work task {task.id} references unknown source step")
            if task.execution_class in _EXECUTOR_CLASSES:
                policy = task.provider_policy or plan.defaults
                if task.execution_class == "repo-write" and policy.quality_tier == "deep":
                    raise PlanValidationError("deep quality tier is read-only until a write profile is characterized")
                skills = _effective_skills(plan, task)
                if skills and task.none_reason is not None:
                    raise PlanValidationError(f"work task {task.id} has both effective skills and none_reason")
                if not skills and task.none_reason is None:
                    raise PlanValidationError(f"work task {task.id} needs executor skills or a reviewed none_reason")
    _validate_source_partition(work, steps)
    _topological_order(plan)
    _validate_owned_path_overlap(plan, work)


def _validate_parent_cycles(tasks: Mapping[str, PlanTaskV1]) -> None:
    for task in tasks.values():
        seen: set[str] = set()
        cursor = task
        while cursor.parent_id is not None:
            if cursor.parent_id in seen:
                raise PlanValidationError("task hierarchy contains a cycle")
            seen.add(cursor.parent_id)
            cursor = tasks[cursor.parent_id]


def _validate_source_partition(work: Sequence[PlanTaskV1], steps: set[str]) -> None:
    assigned = [source_id for task in work for source_id in task.source_step_ids]
    if len(assigned) != len(set(assigned)):
        raise PlanValidationError("atomic source steps may only be assigned once")
    if set(assigned) != steps:
        raise PlanValidationError("work tasks must partition every registered atomic source step")


def _effective_skills(plan: FanoutPlanV1, task: PlanTaskV1) -> tuple[str, ...]:
    tasks = _task_index(plan)
    lineage: list[PlanTaskV1] = [task]
    cursor = task
    visited = {task.id}
    while cursor.parent_id is not None:
        parent_id = cursor.parent_id
        if parent_id in visited:
            raise PlanValidationError("task hierarchy contains a cycle")
        parent = tasks.get(parent_id)
        if parent is None:
            raise PlanValidationError(f"task {task.id} has no parent {parent_id}")
        visited.add(parent_id)
        cursor = parent
        lineage.append(cursor)
    inherited = tuple(skill for item in reversed(lineage) for skill in item.required_skills)
    if len(inherited) != len(set(inherited)):
        raise PlanValidationError(f"task {task.id} has duplicate or conflicting inherited skills")
    return tuple(skill for skill in inherited if skill not in _ORCHESTRATOR_CONTROL_SKILLS)


def _validate_owned_path_overlap(plan: FanoutPlanV1, work: Sequence[PlanTaskV1]) -> None:
    reachable = _reachability(work)
    for index, left in enumerate(work):
        for right in work[index + 1:]:
            if isinstance(plan, FanoutPlanV2) and left.target_id != right.target_id:
                continue
            if (left.id, right.id) in reachable or (right.id, left.id) in reachable:
                continue
            if any(_paths_overlap(a, b) for a in left.owned_paths for b in right.owned_paths):
                raise PlanValidationError(
                    f"unordered work tasks {left.id} and {right.id} have overlapping owned_paths"
                )


def _reachability(work: Sequence[PlanTaskV1]) -> set[tuple[str, str]]:
    dependencies = {task.id: set(task.depends_on) for task in work}
    result: set[tuple[str, str]] = set()
    for task_id, direct in dependencies.items():
        pending = list(direct)
        seen: set[str] = set()
        while pending:
            predecessor = pending.pop()
            if predecessor in seen:
                continue
            seen.add(predecessor)
            result.add((predecessor, task_id))
            pending.extend(dependencies[predecessor])
    return result


def _paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _task_index(plan: FanoutPlanV1) -> dict[str, PlanTaskV1]:
    return {task.id: task for task in plan.tasks}


def _source_step(data: object, index: int) -> SourceStepV1:
    values = _object(data, f"source_steps[{index}]", {"id", "sha256"}, required={"id", "sha256"})
    return SourceStepV1(**values)


def _object(data: object, where: str, allowed: set[str], *, required: set[str]) -> dict[str, object]:
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
        raise PlanValidationError(f"{where} must be an object")
    unknown = set(data) - allowed
    missing = required - set(data)
    if unknown:
        raise PlanValidationError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise PlanValidationError(f"{where} is missing fields: {', '.join(sorted(missing))}")
    return dict(data)


def _list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise PlanValidationError(f"{where} must be an array")
    return list(value)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or "\x00" in value:
        raise PlanValidationError(f"{where} must be a non-empty normalized string")
    return value


def _optional_text(value: object, where: str) -> str | None:
    if value is None:
        return None
    return _text(value, where)


def _identifier(value: object, where: str) -> str:
    return _text(value, where)


def _string_tuple(value: object, where: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise PlanValidationError(f"{where} must be an array of strings")
    result = tuple(_text(item, where) for item in value)
    if nonempty and not result:
        raise PlanValidationError(f"{where} must not be empty")
    return result


def _sha256(value: object, where: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PlanValidationError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _contained_path(value: object, where: str, *, allow_root: bool = False) -> str:
    if allow_root and value == "":
        return ""
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PlanValidationError(f"{where} must be a non-empty relative POSIX path")
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts)
            or str(path) != value):
        raise PlanValidationError(f"{where} must be a normalized contained path")
    return value


def _reject_shell_command_string(argv: tuple[str, ...]) -> None:
    while argv:
        executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if executable == "env":
            index = 1
            while index < len(argv):
                argument = argv[index]
                if argument == "--":
                    index += 1
                    break
                if argument in {"-i", "--ignore-environment"}:
                    index += 1
                    continue
                if argument.startswith("-"):
                    raise PlanValidationError("check argv uses an unsupported env option")
                name, separator, _ = argument.partition("=")
                if separator and _ENV_NAME.fullmatch(name):
                    index += 1
                    continue
                break
            argv = argv[index:]
            continue
        if executable in {"timeout", "gtimeout"}:
            if len(argv) < 3 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[smhd]?", argv[1]):
                raise PlanValidationError("check argv uses an unsupported timeout form")
            argv = argv[2:]
            continue
        if executable == "mise" and len(argv) > 1 and argv[1] == "exec":
            try:
                separator = argv.index("--", 2)
            except ValueError as error:
                raise PlanValidationError("check argv uses an unsupported mise exec form") from error
            if (separator == len(argv) - 1 or any(
                    not re.fullmatch(r"[A-Za-z0-9_.:+/-]+@[A-Za-z0-9_.:+/-]+", selector)
                    for selector in argv[2:separator])):
                raise PlanValidationError("check argv uses an unsupported mise exec form")
            argv = argv[separator + 1:]
            continue
        break
    if not argv:
        raise PlanValidationError("check argv must name a program")
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    flags = tuple(argument.casefold() for argument in argv[1:])
    if executable in _POSIX_SHELLS and any(
        flag == "--command" or (flag.startswith("-") and not flag.startswith("--") and "c" in flag[1:])
        for flag in flags
    ):
        raise PlanValidationError("check argv may not invoke a POSIX shell command string")
    if executable in _WINDOWS_COMMAND_SHELLS and any(flag in {"/c", "/k"} for flag in flags):
        raise PlanValidationError("check argv may not invoke a cmd command string")
    if executable in _POWERSHELLS and any(flag in {
        "-command", "/command", "-c", "/c", "-encodedcommand", "/encodedcommand", "-enc", "/enc",
    } for flag in flags):
        raise PlanValidationError("check argv may not invoke a PowerShell command string")


def _int_in_range(value: object, where: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise PlanValidationError(f"{where} must be an integer from {lower} to {upper}")
    return value


def _positive_number(value: object, where: str, *, maximum: int) -> int | float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= maximum):
        raise PlanValidationError(f"{where} must be a finite positive number at most {maximum}")
    return value


def _exit_codes(value: object) -> tuple[int, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise PlanValidationError("accepted_exit_codes must be an array of integers")
    codes = tuple(value)
    if not codes or any(isinstance(code, bool) or not isinstance(code, int) or code < 0 or code > 255
                        for code in codes):
        raise PlanValidationError("accepted_exit_codes must contain exit codes from 0 to 255")
    if len(codes) != len(set(codes)):
        raise PlanValidationError("accepted_exit_codes must be unique")
    return codes


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PlanValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise PlanValidationError(f"non-finite JSON number: {value}")


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DEFAULT_EXECUTOR_IDS", "FanoutPlanV1", "PlanCheckV1", "PlanTaskV1", "ProviderPolicyV1",
    "SCHEMA_VERSION", "SourceInfoV1", "SourceStepV1", "effective_skills", "load_plan",
    "topological_order", "validate_plan", "write_plan",
]
