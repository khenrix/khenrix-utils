"""Fail-closed compilation of approved Superpowers plans into fanout packets."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .artifacts import canonical_json
from .errors import CompilerError, PlanValidationError, SkillAdmissionError
from .plan import (
    FanoutPlanV1, FanoutPlanV2, PlanTaskV1, PlanTaskV2, ProviderPolicyV1,
    SourceInfoV1, SourceStepV1, SourceStepV2,
    effective_skills,
)
from .skills import ResolvedSkill, SkillResolver
from .targets import TargetSpec


PARSER_VERSION = "superpowers-writing-plans-v10-fanout-strict"
PARSER_VERSION_V2 = "superpowers-writing-plans-v11-target-aware"
LEGACY_INSPECTION_PARSER_VERSION = "superpowers-writing-plans-v6-legacy-inspection"
COMPILER_VERSION = "fanout-compiler-v12"
DRAFT_SCHEMA_VERSION = "fanout-draft-v1"
DRAFT_SCHEMA_VERSION_V2 = "fanout-draft-v2"
COMPILER_VERSION_V2 = "fanout-compiler-v13-target-aware"

_PLAN_TITLE = re.compile(r"^# [^\n]+ Implementation Plan$")
_TASK = re.compile(r"^### Task ([1-9][0-9]*): (.+)$")
_STEP = re.compile(r"^- \[ \] \*\*Step ([1-9][0-9]*): (.+)\*\*[ \t]*$")
_DEPENDS = re.compile(r"^  \*\*Depends on:\*\* (.+)$")
_DEPENDENCY_MODES = re.compile(r"^  \*\*Dependency modes:\*\* (.+)$")
_TARGET = re.compile(r"^\*\*Target:\*\* ([A-Za-z0-9][A-Za-z0-9_-]*)$")
_SOURCE_STEP_ID = re.compile(r"Task [1-9][0-9]*/Step [1-9][0-9]*\Z")
_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_CHECKBOX = re.compile(r"^\s*[-*+] \[[ xX]\]")
_ACTION_LIST = re.compile(r"^\s*(?:[-*+] |[1-9][0-9]*[.)] )")
_FILE_ITEM = re.compile(r"^- (?:Create|Modify|Test): .+")
_SOURCE_FILE = re.compile(r"^- (Create|Modify|Test): `([^`]+)`(?: \([^`]*\))?$")
_MODIFY_LINES = re.compile(r":[0-9]+(?:-[0-9]+)?$")
_TIER_HINT = re.compile(r"\bquality[\s_-]+tier\b", re.IGNORECASE)
_TIER_VALUE = re.compile(r"^(?:\*\*)?quality[ _]tier\s*[:=]\s*(?:\*\*)?\s*`?(normal|deep)`?\.?$", re.IGNORECASE)
_INTERFACE_ITEM = re.compile(r"^- (?:Consumes|Produces): .+")
_METADATA = ("Goal", "Architecture", "Tech Stack", "Spec")
_VAGUE = re.compile(r"\b(?:tbd|todo|implement later|appropriate validation|fill in details)\b", re.IGNORECASE)


class PlanParseMode(str, Enum):
    """Caller-owned source parsing modes with distinct provenance semantics."""

    FANOUT_STRICT = "fanout-strict"
    LEGACY_INSPECTION = "legacy-inspection"


@dataclass(frozen=True, slots=True)
class SourceStepRecord:
    """One normalized atomic slice and its declared immediate source dependencies."""

    id: str
    task_id: str
    title: str
    content: str
    sha256: str
    depends_on: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class SourceStepRecordV2(SourceStepRecord):
    target_id: str
    dependency_modes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _Fence:
    character: str
    length: int


@dataclass(frozen=True, slots=True)
class ParsedSuperpowersPlan:
    """Strict source evidence with explicit consecutive source ordering."""

    source_path: str
    source_sha256: str
    source_binding_sha256: str
    parser_version: str
    mode: PlanParseMode
    global_constraints: tuple[str, ...]
    global_constraints_sha256: str
    steps: tuple[SourceStepRecord, ...]
    source_write_paths: tuple[tuple[str, tuple[str, ...]], ...]
    source_test_paths: tuple[tuple[str, tuple[str, ...]], ...]
    required_order: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ParsedSuperpowersPlanV2(ParsedSuperpowersPlan):
    task_targets: Mapping[str, str]
    step_targets: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class CompiledFanoutPlanV1:
    """A plan plus byte-bound draft and resolved-skill provenance."""

    plan: FanoutPlanV1
    parser_version: str
    compiler_version: str
    global_constraints: tuple[str, ...]
    global_constraints_sha256: str
    draft_sha256: str
    draft_mapping_sha256: str
    resolved_skill_manifests: Mapping[str, tuple[Mapping[str, object], ...]]
    resolved_skills_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "compiled-fanout-plan-v1",
            "plan": self.plan.to_dict(),
            "parser_version": self.parser_version,
            "compiler_version": self.compiler_version,
            "global_constraints": list(self.global_constraints),
            "global_constraints_sha256": self.global_constraints_sha256,
            "draft_sha256": self.draft_sha256,
            "draft_mapping_sha256": self.draft_mapping_sha256,
            "resolved_skill_manifests": {
                task_id: [dict(manifest) for manifest in manifests]
                for task_id, manifests in self.resolved_skill_manifests.items()
            },
            "resolved_skills_sha256": self.resolved_skills_sha256,
        }

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class CompiledFanoutPlanV2(CompiledFanoutPlanV1):
    plan: FanoutPlanV2
    target_registry_sha256: str
    source_step_targets_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            **CompiledFanoutPlanV1.to_dict(self),
            "schema_version": "compiled-fanout-plan-v2",
            "target_registry_sha256": self.target_registry_sha256,
            "source_step_targets_sha256": self.source_step_targets_sha256,
        }


def parse_superpowers_plan(source_bytes: bytes, *, source_path: str,
                           mode: PlanParseMode = PlanParseMode.FANOUT_STRICT) -> ParsedSuperpowersPlan:
    return _parse_superpowers_plan(source_bytes, source_path=source_path, mode=mode, target_aware=False)


def parse_superpowers_plan_v2(source_bytes: bytes, *, source_path: str) -> ParsedSuperpowersPlanV2:
    return _parse_superpowers_plan(source_bytes, source_path=source_path,
                                   mode=PlanParseMode.FANOUT_STRICT, target_aware=True)


def _parse_superpowers_plan(source_bytes: bytes, *, source_path: str,
                            mode: PlanParseMode, target_aware: bool) -> ParsedSuperpowersPlan | ParsedSuperpowersPlanV2:
    """Parse the evidenced subset of Markdown emitted by ``writing-plans``.

    Whole-source digests always use the bytes received.  Semantic per-step and
    global-constraint hashes normalize CRLF to LF, trim outer blank lines, and
    add exactly one trailing LF, so formatting transport cannot alter an
    atomic source identity while it remains visible in the whole-source hash.

    Fanout-compatible Steps declare dependencies immediately after their
    canonical heading as ``  **Depends on:** none`` or a comma-separated list
    of earlier ``Task N/Step M`` IDs. Legacy plans may be parsed with missing
    metadata for inspection only when a caller explicitly selects
    ``PlanParseMode.LEGACY_INSPECTION``. That mode is never compilable.
    """
    if not isinstance(mode, PlanParseMode):
        raise CompilerError("parse mode must be a PlanParseMode")
    if not isinstance(source_bytes, bytes):
        raise CompilerError("source must be UTF-8 bytes")
    try:
        raw = source_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CompilerError("source is not valid UTF-8") from error
    if raw.startswith("\ufeff"):
        raise CompilerError("source UTF-8 BOM is unsupported")
    if "\r" in raw.replace("\r\n", ""):
        raise CompilerError("source uses unsupported bare CR newlines")
    lines = raw.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or not _PLAN_TITLE.fullmatch(lines[0]):
        raise CompilerError("source must begin with '# … Implementation Plan'")
    legacy_fence_compatibility = mode is PlanParseMode.LEGACY_INSPECTION
    parser_version = PARSER_VERSION_V2 if target_aware else _parser_version(mode)

    task_positions: list[tuple[int, re.Match[str]]] = []
    for index, line in _unfenced_lines(lines, 0, len(lines),
                                       legacy_fence_compatibility=legacy_fence_compatibility):
        match = _TASK.fullmatch(line)
        if match:
            task_positions.append((index, match))
        elif line.startswith("### Task "):
            raise CompilerError(f"malformed task heading at source line {index + 1}")
    if not task_positions:
        raise CompilerError("source contains no Task sections")
    _validate_preamble(lines, task_positions[0][0], strict=not legacy_fence_compatibility)

    steps: list[SourceStepRecord] = []
    source_write_lines: list[tuple[str, tuple[str, ...]]] = []
    source_test_lines: list[tuple[str, tuple[str, ...]]] = []
    task_targets: dict[str, str] = {}
    expected_task = 1
    for task_index, (start, task_match) in enumerate(task_positions):
        number, title = int(task_match.group(1)), task_match.group(2)
        if number != expected_task or title != title.strip():
            raise CompilerError(f"malformed or skipped Task {number}")
        expected_task += 1
        next_task = task_positions[task_index + 1][0] if task_index + 1 < len(task_positions) else len(lines)
        end = _task_body_end(lines, start + 1, next_task,
                             legacy_fence_compatibility=legacy_fence_compatibility)
        if not legacy_fence_compatibility:
            _reject_unowned_document_work(lines, end, next_task)
        body = lines[start + 1:end]
        target_id = _task_target(body, number) if target_aware else None
        if target_id is not None:
            task_targets[f"Task {number}"] = target_id
        task_steps, write_lines, test_lines = _parse_task(
            body, number, target_id=target_id,
            legacy_fence_compatibility=legacy_fence_compatibility)
        steps.extend(task_steps)
        source_write_lines.append((f"Task {number}", write_lines))
        source_test_lines.append((f"Task {number}", test_lines))
    if not steps:
        raise CompilerError("source contains no atomic Steps")
    required_order = _validate_declared_dependencies(steps)
    if target_aware:
        _validate_dependency_modes(steps)
    source_writes = tuple((task_id, tuple(_source_file_path(line, number) for line in write_lines))
                          for number, (task_id, write_lines) in enumerate(source_write_lines, start=1))
    source_tests = tuple((task_id, tuple(_source_file_path(line, number) for line in test_lines))
                         for number, (task_id, test_lines) in enumerate(source_test_lines, start=1))

    constraints = _global_constraints(lines, task_positions[0][0])
    fields = dict(
        source_path=source_path,
        source_sha256=_sha256(source_bytes),
        source_binding_sha256=_source_binding_sha256(source_bytes, mode, parser_version),
        parser_version=parser_version,
        mode=mode,
        global_constraints=constraints,
        global_constraints_sha256=_sha256(_canonical_text(constraints).encode("utf-8")),
        steps=tuple(steps),
        source_write_paths=source_writes,
        source_test_paths=source_tests,
        required_order=required_order,
    )
    if target_aware:
        return ParsedSuperpowersPlanV2(
            **fields,
            task_targets=MappingProxyType(dict(sorted(task_targets.items()))),
            step_targets=MappingProxyType({step.id: step.target_id for step in steps}),
        )
    return ParsedSuperpowersPlan(**fields)


def compile_superpowers_plan(source_bytes: bytes, *, source_path: str, draft_bytes: bytes,
                             resolver: SkillResolver) -> CompiledFanoutPlanV1:
    """Compile exactly one strict CLI draft without creating work or dependencies."""
    if isinstance(source_bytes, ParsedSuperpowersPlan):
        _require_strict_parse(source_bytes)
        raise CompilerError("compiler requires approved strict source bytes, not a parsed source object")
    parsed = parse_superpowers_plan(source_bytes, source_path=source_path,
                                    mode=PlanParseMode.FANOUT_STRICT)
    _require_strict_parse(parsed)
    source_tier = _source_quality_tier(parsed.global_constraints)
    draft = _load_draft(draft_bytes)
    if draft["source_sha256"] != parsed.source_sha256:
        raise CompilerError("draft.source_sha256 does not match the approved source")
    try:
        defaults = ProviderPolicyV1.from_dict(draft["defaults"], where="draft.defaults")
        tasks = tuple(PlanTaskV1.from_dict(item, where=f"draft.tasks[{index}]")
                      for index, item in enumerate(draft["tasks"]))
        plan = FanoutPlanV1(
            source=SourceInfoV1(source_path, parsed.source_sha256, parsed.parser_version),
            defaults=defaults,
            source_steps=tuple(SourceStepV1(step.id, step.sha256) for step in parsed.steps),
            tasks=tasks,
        )
    except PlanValidationError as error:
        raise CompilerError(f"draft validation failed: {error}") from error
    _require_quality_tiers(plan, source_tier)
    _require_executable_evidence(plan)
    _require_source_write_intent(plan, parsed.source_write_paths, parsed.source_test_paths)
    _require_exact_source_dependencies(plan, parsed.steps, parsed.required_order)
    manifests = _resolve_effective_skills(plan, resolver)
    canonical_draft = canonical_json(draft)
    frozen_manifests = MappingProxyType({task_id: tuple(manifests[task_id]) for task_id in sorted(manifests)})
    return CompiledFanoutPlanV1(
        plan=plan,
        parser_version=parsed.parser_version,
        compiler_version=COMPILER_VERSION,
        global_constraints=parsed.global_constraints,
        global_constraints_sha256=parsed.global_constraints_sha256,
        draft_sha256=_sha256(draft_bytes),
        draft_mapping_sha256=_sha256(canonical_draft),
        resolved_skill_manifests=frozen_manifests,
        resolved_skills_sha256=_sha256(canonical_json({
            task_id: [dict(item) for item in manifests[task_id]] for task_id in sorted(manifests)
        })),
    )


def compile_superpowers_plan_v2(source_bytes: bytes, *, source_path: str, draft_bytes: bytes,
                                resolver: SkillResolver) -> CompiledFanoutPlanV2:
    """Compile source-declared targets without accepting draft-only retargeting."""
    if isinstance(source_bytes, ParsedSuperpowersPlan):
        raise CompilerError("compiler requires approved strict source bytes, not a parsed source object")
    parsed = parse_superpowers_plan_v2(source_bytes, source_path=source_path)
    draft = _load_draft(draft_bytes, schema_version=DRAFT_SCHEMA_VERSION_V2)
    if draft["source_sha256"] != parsed.source_sha256:
        raise CompilerError("draft.source_sha256 does not match the approved source")
    try:
        plan = FanoutPlanV2(
            source=SourceInfoV1(source_path, parsed.source_sha256, parsed.parser_version),
            defaults=ProviderPolicyV1.from_dict(draft["defaults"], where="draft.defaults"),
            targets=tuple(TargetSpec.from_dict(item) for item in draft["targets"]),
            source_steps=tuple(SourceStepV2(step.id, step.sha256, step.target_id)
                               for step in parsed.steps),
            tasks=tuple(PlanTaskV2.from_dict(item, where=f"draft.tasks[{index}]")
                        for index, item in enumerate(draft["tasks"])),
        )
    except PlanValidationError as error:
        raise CompilerError(f"draft validation failed: {error}") from error
    _require_quality_tiers(plan, _source_quality_tier(parsed.global_constraints))
    _require_executable_evidence(plan)
    _require_source_write_intent(plan, parsed.source_write_paths, parsed.source_test_paths)
    _require_exact_source_dependencies(plan, parsed.steps, parsed.required_order)
    _require_exact_dependency_modes_v2(plan, parsed.steps, parsed.required_order)
    manifests = _resolve_effective_skills(plan, resolver)
    return CompiledFanoutPlanV2(
        plan=plan,
        parser_version=parsed.parser_version,
        compiler_version=COMPILER_VERSION_V2,
        global_constraints=parsed.global_constraints,
        global_constraints_sha256=parsed.global_constraints_sha256,
        draft_sha256=_sha256(draft_bytes),
        draft_mapping_sha256=_sha256(canonical_json(draft)),
        resolved_skill_manifests=MappingProxyType({task_id: tuple(manifests[task_id])
                                                   for task_id in sorted(manifests)}),
        resolved_skills_sha256=_sha256(canonical_json({
            task_id: [dict(item) for item in manifests[task_id]] for task_id in sorted(manifests)
        })),
        target_registry_sha256=_sha256(canonical_json([target.to_dict() for target in plan.targets])),
        source_step_targets_sha256=_sha256(canonical_json({
            step.id: step.target_id for step in plan.source_steps
        })),
    )


def _parser_version(mode: PlanParseMode) -> str:
    if mode is PlanParseMode.FANOUT_STRICT:
        return PARSER_VERSION
    if mode is PlanParseMode.LEGACY_INSPECTION:
        return LEGACY_INSPECTION_PARSER_VERSION
    raise CompilerError("parse mode is unsupported")


def _source_binding_sha256(source_bytes: bytes, mode: PlanParseMode, parser_version: str) -> str:
    return _sha256(canonical_json({
        "source_sha256": _sha256(source_bytes),
        "mode": mode.value,
        "parser_version": parser_version,
    }))


def _require_strict_parse(parsed: ParsedSuperpowersPlan) -> None:
    if (parsed.mode is not PlanParseMode.FANOUT_STRICT
            or parsed.parser_version != PARSER_VERSION):
        raise CompilerError("legacy inspection parse results cannot be compiled")


def _validate_preamble(lines: Sequence[str], end: int, *, strict: bool) -> None:
    metadata: dict[str, int] = {}
    global_heading: int | None = None
    for index, line in _unfenced_lines(lines, 1, end,
                                       legacy_fence_compatibility=not strict):
        if "**Target:" in line or "**Dependency modes:" in line:
            raise CompilerError("source Target or Dependency modes metadata must be inside a v2 Task")
        for name in _METADATA:
            prefix = f"**{name}:** "
            if line.startswith(prefix):
                if name in metadata or not line[len(prefix):].strip():
                    raise CompilerError(f"source {name} metadata is malformed or duplicated")
                metadata[name] = index
        if line == "## Global Constraints":
            if global_heading is not None:
                raise CompilerError("source Global Constraints section is duplicated")
            global_heading = index
    missing = [name for name in _METADATA if name not in metadata]
    if missing:
        raise CompilerError(f"source is missing required metadata: {', '.join(missing)}")
    if global_heading is None:
        raise CompilerError("source is missing ## Global Constraints")
    _global_constraints(lines, end)
    if strict:
        _reject_unowned_document_work(lines, 1, end, allow_global_constraints=True)


def _global_constraints(lines: Sequence[str], end: int) -> tuple[str, ...]:
    start = next(index for index, line in _unfenced_lines(lines, 1, end)
                 if line == "## Global Constraints") + 1
    stop = end
    for index in range(start, end):
        if _fence_opener(lines[index]) is not None:
            raise CompilerError("source Global Constraints does not support fenced content")
        if lines[index].startswith("## "):
            stop = index
            break
    section = list(lines[start:stop])
    while section and not section[0].strip():
        section.pop(0)
    while section and not section[-1].strip():
        section.pop()
    if not section:
        raise CompilerError("source Global Constraints section is empty")
    constraints: list[list[str]] = []
    for line in section:
        if _CHECKBOX.match(line) or (line.startswith("- ") and "**Step " in line):
            raise CompilerError("source Global Constraints contains a Step or checkbox")
        if line.startswith("- ") and line[2:].strip():
            constraints.append([line[2:].strip()])
        elif not line.strip():
            continue
        elif line.startswith(("  ", "\t")) and line.strip():
            if not constraints:
                raise CompilerError("source Global Constraints continuation has no bullet")
            constraints[-1].append(line.strip())
        else:
            raise CompilerError("source content outside an owned section is ambiguous")
    if not constraints:
        raise CompilerError("source Global Constraints requires bullet statements")
    return tuple("\n".join(constraint) for constraint in constraints)


def _unfenced_lines(lines: Sequence[str], start: int, limit: int, *,
                    legacy_fence_compatibility: bool = False):
    """Yield lines outside backtick/tilde fences using the parser's one fence grammar."""
    in_fence: _Fence | None = None
    for index in range(start, limit):
        was_fenced = in_fence is not None
        in_fence = _next_fence_state(in_fence, lines[index],
                                     legacy_fence_compatibility=legacy_fence_compatibility)
        if was_fenced or in_fence is not None:
            continue
        yield index, lines[index]


def _fence_opener(line: str) -> _Fence | None:
    """Return a supported CommonMark-style opener, including its exact delimiter."""
    match = _FENCE.fullmatch(line)
    if match is None:
        return None
    marker, suffix = match.group(2), match.group(3)
    if marker[0] == "`" and "`" in suffix:
        return None
    return _Fence(marker[0], len(marker))


def _next_fence_state(open_fence: _Fence | None, line: str, *,
                      legacy_fence_compatibility: bool = False) -> _Fence | None:
    """Advance one shared fence scanner without treating fence-looking content as syntax."""
    if open_fence is None:
        return _fence_opener(line)
    match = _FENCE.fullmatch(line)
    if match is None:
        return open_fence
    marker, suffix = match.group(2), match.group(3)
    if legacy_fence_compatibility and marker[0] == open_fence.character:
        return None
    if marker[0] != open_fence.character or len(marker) < open_fence.length or suffix.strip():
        return open_fence
    return None


def _task_body_end(lines: Sequence[str], start: int, limit: int, *,
                   legacy_fence_compatibility: bool = False) -> int:
    """Find a task's next unfenced document-level section boundary."""
    for index, line in _unfenced_lines(lines, start, limit,
                                       legacy_fence_compatibility=legacy_fence_compatibility):
        if line.startswith("## "):
            return index
    return limit


def _reject_unowned_document_work(lines: Sequence[str], start: int, limit: int, *,
                                  allow_global_constraints: bool = False) -> None:
    """Document sections may contain prose or preamble notes, never source work."""
    in_constraints = False
    in_document_list = False
    for _, line in _unfenced_lines(lines, start, limit):
        if "**Target:" in line or "**Dependency modes:" in line:
            raise CompilerError("source Target or Dependency modes metadata must be inside a v2 Task")
        if line.startswith("## "):
            in_document_list = allow_global_constraints and line in {
                "## Review Focus", "## File Structure",
            }
            if line == "## Global Constraints":
                if not allow_global_constraints:
                    raise CompilerError("source Global Constraints appears outside the preamble")
                in_constraints = True
            else:
                in_constraints = False
            continue
        if in_constraints:
            continue
        if (in_document_list and line.startswith("- ") and not _CHECKBOX.match(line)
                and "**Step " not in line and "**Depends on:" not in line):
            continue
        if (_ACTION_LIST.match(line) or "**Depends on:" in line
                or line.startswith(("**Files:**", "**Interfaces:**"))):
            raise CompilerError("source has actionable content outside a Task section")


def _task_target(body: Sequence[str], task_number: int) -> str:
    first_step = next((index for index, line in _unfenced_lines(body, 0, len(body))
                       if _STEP.fullmatch(line)), len(body))
    declarations = [(index, line) for index, line in _unfenced_lines(body, 0, len(body))
                    if "**Target:" in line]
    if len(declarations) != 1 or declarations[0][0] >= first_step:
        raise CompilerError(f"Task {task_number} requires exactly one preamble Target declaration")
    match = _TARGET.fullmatch(declarations[0][1])
    if match is None:
        raise CompilerError(f"Task {task_number} Target declaration is malformed")
    return match.group(1)


def _parse_task(body: Sequence[str], task_number: int, *, target_id: str | None = None,
                legacy_fence_compatibility: bool = False) -> tuple[list[SourceStepRecord], tuple[str, ...], tuple[str, ...]]:
    positions: list[tuple[int, re.Match[str]]] = []
    write_paths: list[str] = []
    test_paths: list[str] = []
    in_fence: _Fence | None = None
    first_step: int | None = None
    latest_step: int | None = None
    structural_section: str | None = None
    for index, line in enumerate(body):
        was_fenced = in_fence is not None
        in_fence = _next_fence_state(in_fence, line,
                                     legacy_fence_compatibility=legacy_fence_compatibility)
        if was_fenced or in_fence is not None:
            continue
        match = _STEP.fullmatch(line)
        if match:
            positions.append((index, match))
            first_step = index if first_step is None else first_step
            latest_step = index
            structural_section = None
            continue
        if "**Depends on:" in line:
            canonical = latest_step is not None and index == latest_step + 1 and _DEPENDS.fullmatch(line)
            if not canonical:
                raise CompilerError(f"Task {task_number} has a noncanonical Depends on declaration")
            continue
        if "**Target:" in line and target_id is None:
            raise CompilerError(f"Task {task_number} Target metadata requires v2 parsing")
        if "**Dependency modes:" in line:
            canonical = (target_id is not None and latest_step is not None
                         and index == latest_step + 2 and _DEPENDENCY_MODES.fullmatch(line))
            if not canonical:
                raise CompilerError(f"Task {task_number} has a noncanonical Dependency modes declaration")
            continue
        if line.startswith("**Files:**"):
            structural_section = "files" if first_step is None else None
            continue
        if line.startswith("**Interfaces:**"):
            structural_section = "interfaces" if first_step is None else None
            continue
        if _CHECKBOX.match(line):
            raise CompilerError(f"Task {task_number} has a noncanonical checkbox outside a Step declaration")
        if _ACTION_LIST.match(line):
            allowed_file = structural_section == "files" and _FILE_ITEM.fullmatch(line)
            allowed_interface = structural_section == "interfaces" and _INTERFACE_ITEM.fullmatch(line)
            if not (allowed_file or allowed_interface):
                raise CompilerError(f"Task {task_number} has an unowned actionable list outside a Step declaration")
            if (allowed_file and not legacy_fence_compatibility
                    and line.startswith(("- Create:", "- Modify:"))):
                write_paths.append(line)
            elif allowed_file and not legacy_fence_compatibility and line.startswith("- Test:"):
                test_paths.append(line)
        elif "**Step " in line and line.lstrip().startswith("- ["):
            raise CompilerError(f"Task {task_number} has an unsupported Step construct")
        elif line.startswith("###"):
            raise CompilerError(f"Task {task_number} contains an unsupported nested heading")
    if in_fence is not None:
        raise CompilerError(f"Task {task_number} has an unterminated fenced example")
    if not positions:
        raise CompilerError(f"Task {task_number} contains no Steps")
    first = positions[0][0]
    preamble = body[:first]
    if not any(line.strip() for line in preamble):
        raise CompilerError(f"Task {task_number} has no owned task section before Step 1")
    if not any(line.startswith("**Files:**") for line in preamble):
        raise CompilerError(f"Task {task_number} is missing its required **Files:** section")
    records: list[SourceStepRecord] = []
    expected = 1
    for position_index, (start, match) in enumerate(positions):
        number, title = int(match.group(1)), match.group(2)
        if number != expected or title != title.strip():
            raise CompilerError(f"Task {task_number} has malformed or skipped Step {number}")
        expected += 1
        stop = positions[position_index + 1][0] if position_index + 1 < len(positions) else len(body)
        slice_lines = body[start:stop]
        depends_on = _step_dependencies(slice_lines, task_number, number)
        content = _canonical_text(slice_lines)
        mode_line = slice_lines[2] if len(slice_lines) > 2 and _DEPENDENCY_MODES.fullmatch(slice_lines[2]) else None
        modes = _source_dependency_modes(mode_line, task_number, number) if target_id is not None else ()
        detail = _canonical_text(slice_lines[(3 if mode_line is not None else 2)
                                             if depends_on is not None else 1:])
        if not detail.strip():
            raise CompilerError(f"Task {task_number}/Step {number} is empty")
        identifier = f"Task {task_number}/Step {number}"
        record_args = (identifier, f"Task {task_number}", title, content,
                       _sha256(content.encode("utf-8")), depends_on)
        records.append(SourceStepRecordV2(*record_args, target_id, modes) if target_id is not None
                       else SourceStepRecord(*record_args))
    return records, tuple(write_paths), tuple(test_paths)


def _source_dependency_modes(line: str | None, task_number: int, step_number: int) -> tuple[tuple[str, str], ...]:
    if line is None:
        return ()
    value = _DEPENDENCY_MODES.fullmatch(line).group(1)
    entries: list[tuple[str, str]] = []
    for item in value.split(", "):
        predecessor, separator, mode = item.partition("=")
        if not separator or not _SOURCE_STEP_ID.fullmatch(predecessor) or mode not in {"artifact", "handover"}:
            raise CompilerError(f"Task {task_number}/Step {step_number} has invalid Dependency modes")
        entries.append((predecessor, mode))
    if not entries or len({predecessor for predecessor, _ in entries}) != len(entries):
        raise CompilerError(f"Task {task_number}/Step {step_number} has repeated Dependency modes")
    return tuple(entries)


def _validate_dependency_modes(steps: Sequence[SourceStepRecord]) -> None:
    for step in steps:
        cross_task = {dependency for dependency in step.depends_on or ()
                      if dependency.split("/", 1)[0] != step.task_id}
        declared = {dependency for dependency, _ in step.dependency_modes}
        if declared != cross_task:
            raise CompilerError(f"{step.id} Dependency modes must name every cross-task predecessor exactly once")


def _source_file_path(line: str, task_number: int) -> str:
    match = _SOURCE_FILE.fullmatch(line)
    if match is None:
        raise CompilerError(f"Task {task_number} source file intent needs one backticked POSIX path")
    kind, path = match.groups()
    if kind == "Modify":
        path = _MODIFY_LINES.sub("", path)
    pure = PurePosixPath(path)
    if (pure.is_absolute() or not pure.parts or str(pure) != path
            or any(part in {".", ".."} for part in pure.parts) or "\\" in path):
        raise CompilerError(f"Task {task_number} source file intent has an unsafe path")
    return path


def _step_dependencies(lines: Sequence[str], task_number: int, step_number: int) -> tuple[str, ...] | None:
    if len(lines) < 2:
        return None
    line = lines[1]
    match = _DEPENDS.fullmatch(line)
    if match is None:
        if line.strip().startswith("**Depends on:"):
            raise CompilerError(f"Task {task_number}/Step {step_number} has malformed Depends on metadata")
        return None
    value = match.group(1)
    if value == "none":
        return ()
    dependencies = tuple(value.split(", "))
    if ", ".join(dependencies) != value or not dependencies or any(
            not _SOURCE_STEP_ID.fullmatch(dependency) for dependency in dependencies):
        raise CompilerError(f"Task {task_number}/Step {step_number} has malformed Depends on metadata")
    if len(dependencies) != len(set(dependencies)):
        raise CompilerError(f"Task {task_number}/Step {step_number} repeats a Depends on identifier")
    return dependencies


def _validate_declared_dependencies(steps: Sequence[SourceStepRecord]) -> tuple[tuple[str, str], ...]:
    positions = {step.id: index for index, step in enumerate(steps)}
    edges: list[tuple[str, str]] = []
    for index, step in enumerate(steps):
        if step.depends_on is None:
            continue
        for dependency in step.depends_on:
            dependency_index = positions.get(dependency)
            if dependency_index is None:
                raise CompilerError(f"{step.id} Depends on an unknown source Step")
            if dependency_index >= index:
                raise CompilerError(f"{step.id} Depends on a forward or self source Step")
            edges.append((dependency, step.id))
    return tuple(edges)


def _load_draft(draft_bytes: bytes, *, schema_version: str = DRAFT_SCHEMA_VERSION) -> dict[str, object]:
    if not isinstance(draft_bytes, bytes):
        raise CompilerError("draft must be UTF-8 JSON bytes")
    try:
        draft = json.loads(draft_bytes.decode("utf-8"), object_pairs_hook=_no_duplicate_object,
                           parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CompilerError(f"draft is not strict JSON: {error}") from error
    if not isinstance(draft, dict):
        raise CompilerError("draft must be a JSON object")
    allowed = {"schema_version", "source_sha256", "defaults", "tasks"}
    if schema_version == DRAFT_SCHEMA_VERSION_V2:
        allowed.add("targets")
    if set(draft) != allowed:
        fields = ("schema_version, source_sha256, defaults, tasks" if schema_version == DRAFT_SCHEMA_VERSION
                  else "schema_version, source_sha256, defaults, tasks, targets")
        raise CompilerError("draft fields must be exactly " + fields)
    if draft["schema_version"] != schema_version:
        raise CompilerError("draft.schema_version is unsupported")
    if not isinstance(draft["source_sha256"], str):
        raise CompilerError("draft.source_sha256 must be a string")
    if not isinstance(draft["defaults"], dict):
        raise CompilerError("draft.defaults must be an object")
    if not isinstance(draft["tasks"], list) or not draft["tasks"]:
        raise CompilerError("draft.tasks must be a non-empty array")
    if any(not isinstance(item, dict) for item in draft["tasks"]):
        raise CompilerError("draft.tasks must contain objects")
    if schema_version == DRAFT_SCHEMA_VERSION_V2 and (
        not isinstance(draft["targets"], list) or not draft["targets"]
    ):
        raise CompilerError("draft.targets must be a non-empty array")
    return draft


def _require_executable_evidence(plan: FanoutPlanV1) -> None:
    for task in plan.tasks:
        if task.kind != "work" or task.execution_class == "orchestrator-action":
            continue
        if task.execution_class == "repo-write":
            if not task.owned_paths:
                raise CompilerError(f"draft task {task.id} repo-write requires owned_paths")
            if not task.checks:
                raise CompilerError(f"draft task {task.id} requires at least one argv check")
        texts = (task.title, task.objective, *task.acceptance)
        if any(_VAGUE.search(text) for text in texts):
            raise CompilerError(f"draft task {task.id} contains a vague placeholder")


def _source_quality_tier(constraints: Sequence[str]) -> str | None:
    tier = None
    for constraint in constraints:
        if _TIER_HINT.search(constraint) is None:
            continue
        match = _TIER_VALUE.fullmatch(constraint)
        if match is None or tier is not None:
            raise CompilerError("source quality_tier declaration is malformed or duplicated")
        tier = "standard" if match.group(1).lower() == "normal" else "deep"
    return tier


def _require_quality_tiers(plan: FanoutPlanV1, source_tier: str | None) -> None:
    if source_tier is not None and plan.defaults.quality_tier != source_tier:
        raise CompilerError(
            f"draft.defaults quality_tier={plan.defaults.quality_tier} differs from "
            f"approved source quality_tier={source_tier}")
    deep_plan = plan.defaults.quality_tier == "deep"
    for task in plan.tasks:
        if task.kind != "work":
            continue
        if deep_plan and task.execution_class != "read-only":
            raise CompilerError("quality_tier=deep requires read-only work throughout the plan")
        if task.execution_class == "orchestrator-action":
            continue
        tier = (task.provider_policy or plan.defaults).quality_tier
        if deep_plan and tier != "deep":
            raise CompilerError(
                f"draft task {task.id} quality_tier={tier} differs from deep plan defaults")


def _require_source_write_intent(
        plan: FanoutPlanV1, source_writes: Sequence[tuple[str, tuple[str, ...]]],
        source_tests: Sequence[tuple[str, tuple[str, ...]]]) -> None:
    declared = {task_id: set(paths) for task_id, paths in source_writes}
    optional = {task_id: set(paths) for task_id, paths in source_tests}
    covered: dict[str, set[str]] = {task_id: set() for task_id in declared}
    write_owners: dict[str, set[str]] = {task_id: set() for task_id in declared}
    for task in plan.tasks:
        if task.kind != "work" or task.execution_class != "repo-write":
            continue
        source_tasks = {step_id.split("/", 1)[0] for step_id in task.source_step_ids}
        allowed = set().union(*(declared[source_id] | optional[source_id]
                                for source_id in source_tasks))
        if not set(task.owned_paths) <= allowed:
            raise CompilerError(f"draft task {task.id} owned_paths exceed source write intent")
        for source_id in source_tasks:
            owned_source_paths = set(task.owned_paths) & declared[source_id]
            if owned_source_paths:
                write_owners[source_id].add(task.id)
                covered[source_id].update(owned_source_paths)
    for source_id, paths in declared.items():
        if paths and len(write_owners[source_id]) > 1:
            raise CompilerError(f"{source_id} repo-write ownership is split across work tasks")
        if covered[source_id] != paths:
            raise CompilerError(f"{source_id} source write intent needs repo-write ownership of {sorted(paths - covered[source_id])}")


def _require_exact_source_dependencies(plan: FanoutPlanV1, steps: Sequence[SourceStepRecord],
                                       edges: Sequence[tuple[str, str]]) -> None:
    missing = [step.id for step in steps if step.depends_on is None]
    if missing:
        raise CompilerError(f"source {missing[0]} is missing required Depends on metadata")
    owners: dict[str, str] = {}
    work = {task.id: task for task in plan.tasks if task.kind == "work"}
    for task in work.values():
        for step_id in task.source_step_ids:
            owners[step_id] = task.id
    expected: dict[str, set[str]] = {task_id: set() for task_id in work}
    for earlier, later in edges:
        before, after = owners[earlier], owners[later]
        if before != after:
            expected[after].add(before)
    for task_id, task in work.items():
        actual = set(task.depends_on)
        if actual != expected[task_id]:
            raise CompilerError(f"draft task {task_id} dependency edges do not exactly preserve source dependency metadata")


def _require_exact_dependency_modes_v2(plan: FanoutPlanV2, steps: Sequence[SourceStepRecordV2],
                                       edges: Sequence[tuple[str, str]]) -> None:
    owners = {step_id: task.id for task in plan.tasks if task.kind == "work"
              for step_id in task.source_step_ids}
    step_index = {step.id: step for step in steps}
    expected: dict[tuple[str, str], str] = {}
    for earlier, later in edges:
        before, after = owners[earlier], owners[later]
        if before == after:
            continue
        source_step = step_index[later]
        mode = dict(source_step.dependency_modes).get(earlier, "artifact")
        pair = (before, after)
        if pair in expected and expected[pair] != mode:
            raise CompilerError(f"source dependencies between {before} and {after} disagree on mode")
        expected[pair] = mode
    for task in plan.tasks:
        if task.kind != "work":
            continue
        for predecessor, mode in task.dependency_modes.items():
            if mode != expected[(predecessor, task.id)]:
                raise CompilerError(f"draft task {task.id} dependency mode differs from source metadata")


def _resolve_effective_skills(plan: FanoutPlanV1, resolver: SkillResolver) -> dict[str, tuple[Mapping[str, object], ...]]:
    if not isinstance(resolver, SkillResolver):
        raise CompilerError("compiler requires a Task 6 SkillResolver")
    manifests: dict[str, tuple[Mapping[str, object], ...]] = {}
    snapshots: dict[str, tuple[ResolvedSkill, ...]] = {}
    for task in plan.tasks:
        if task.kind != "work" or task.execution_class == "orchestrator-action":
            continue
        try:
            resolved = resolver.resolve(effective_skills(plan, task.id))
        except (PlanValidationError, SkillAdmissionError) as error:
            raise CompilerError(f"draft task {task.id} skill resolution failed: {error}") from error
        snapshots[task.id] = resolved
        manifests[task.id] = tuple(_skill_manifest(skill) for skill in resolved)
    for task_id, before in snapshots.items():
        try:
            after = resolver.resolve(tuple(skill.name for skill in before))
        except SkillAdmissionError as error:
            raise CompilerError(f"draft task {task_id} skill drift detected: {error}") from error
        if tuple(_skill_manifest(skill) for skill in after) != manifests[task_id]:
            raise CompilerError(f"draft task {task_id} skill drift detected")
    return manifests


def _skill_manifest(skill: ResolvedSkill) -> Mapping[str, object]:
    return MappingProxyType({
        "name": skill.name,
        "source": skill.source.identity,
        "origins": tuple(origin.identity for origin in skill.origins),
        "tree_hash": skill.tree_hash,
        "references": tuple({"path": path, "sha256": digest} for path, digest in skill.references),
        "files": tuple({"path": path, "sha256": digest, "size": size, "mode": mode}
                       for path, digest, size, mode in skill.manifest),
    })


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _canonical_text(lines: Sequence[str]) -> str:
    normalized = list(lines)
    while normalized and not normalized[0].strip():
        normalized.pop(0)
    while normalized and not normalized[-1].strip():
        normalized.pop()
    return "\n".join(normalized) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
