"""Reusable local preparation over the existing authenticated journal and artifact store.

Only source-controlled local checks are admitted. The historical seat event named
``provider-terminal`` records a local process terminal here, never model evidence.
The owner supplies an authenticated journal; this module does not escrow capabilities.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .artifacts import ArtifactRef, ArtifactStore, canonical_json
from .container_source import ReleaseSourceBinding, release_source_binding
from .errors import ArtifactError, ArtifactExistsError, FanoutError
from .process import ProcessCommand, ProcessStatus, run_command
from .runstate import OwnerCapability, RunInputs, RunJournal

MAX_RECIPE_BYTES = 64 * 1024
_STAGES = frozenset({"source-binding", "host-runtime"})
_TASK = "preparation"


class PreparationError(FanoutError):
    """Preparation is invalid, drifted, damaged, or cannot safely be resumed."""


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("non-finite JSON constant")


def _json(raw: bytes) -> object:
    if not isinstance(raw, bytes) or len(raw) > MAX_RECIPE_BYTES:
        raise PreparationError("preparation input exceeds its byte bound")
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise PreparationError("preparation input is malformed") from None


def _ref(value: object) -> ArtifactRef:
    if not isinstance(value, dict) or set(value) != {"path", "digest", "size"}:
        raise PreparationError("preparation artifact reference is invalid")
    ref = ArtifactRef(**value)
    try:
        parts = ArtifactStore._parts(ref.path)
    except ArtifactError:
        raise PreparationError("preparation artifact reference is invalid") from None
    if (len(ref.path.encode("utf-8")) > 4096 or "/".join(parts) != ref.path
            or parts[0] == ArtifactStore._TEMP_DIR
            or not isinstance(ref.digest, str) or len(ref.digest) != 64
            or any(char not in "0123456789abcdef" for char in ref.digest)
            or type(ref.size) is not int or not 0 <= ref.size <= 64 * 1024 * 1024):
        raise PreparationError("preparation artifact reference is invalid")
    return ref


@dataclass(frozen=True, slots=True)
class PreparationStage:
    id: str
    timeout: float = 30.0
    max_output_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if (self.id not in _STAGES or type(self.timeout) not in {int, float}
                or not math.isfinite(self.timeout) or not 0.05 <= self.timeout <= 30
                or type(self.max_output_bytes) is not int
                or not 1 <= self.max_output_bytes <= 1024 * 1024):
            raise PreparationError("preparation stage is unsupported or exceeds its bounds")


@dataclass(frozen=True, slots=True)
class PreparationRecipe:
    plan_sha256: str
    release_binding: ReleaseSourceBinding
    stages: tuple[PreparationStage, ...]
    evidence: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.plan_sha256, str) or len(self.plan_sha256) != 64
                or any(char not in "0123456789abcdef" for char in self.plan_sha256)
                or not isinstance(self.release_binding, ReleaseSourceBinding)
                or not isinstance(self.stages, tuple) or not 1 <= len(self.stages) <= 8
                or any(not isinstance(item, PreparationStage) for item in self.stages)
                or len({item.id for item in self.stages}) != len(self.stages)
                or not isinstance(self.evidence, tuple) or len(self.evidence) > 16):
            raise PreparationError("preparation recipe bindings are invalid")
        for item in self.evidence:
            if not isinstance(item, ArtifactRef):
                raise PreparationError("preparation evidence must be artifact references")
            _ref(dataclasses.asdict(item))

    def to_bytes(self) -> bytes:
        return canonical_json({
            "schema_version": "fanout-preparation-recipe-v1",
            "plan_sha256": self.plan_sha256, "release_binding": self.release_binding.to_dict(),
            "stages": [dataclasses.asdict(item) for item in self.stages],
            "evidence": [dataclasses.asdict(item) for item in self.evidence],
        })

    @property
    def digest(self) -> str:
        return _digest(self.to_bytes())

    @classmethod
    def from_bytes(cls, raw: bytes) -> PreparationRecipe:
        value = _json(raw)
        if (not isinstance(value, dict) or set(value) != {
            "schema_version", "plan_sha256", "release_binding", "stages", "evidence",
        } or value.get("schema_version") != "fanout-preparation-recipe-v1"
                or not isinstance(value.get("stages"), list) or not 1 <= len(value["stages"]) <= 8
                or not isinstance(value.get("evidence"), list) or len(value["evidence"]) > 16):
            raise PreparationError("preparation recipe schema is invalid")
        try:
            stages = []
            for stage in value["stages"]:
                if not isinstance(stage, dict) or set(stage) != {"id", "timeout", "max_output_bytes"}:
                    raise PreparationError("preparation stage schema is invalid")
                stages.append(PreparationStage(**stage))
            return cls(value["plan_sha256"], ReleaseSourceBinding.from_dict(value["release_binding"]),
                       tuple(stages), tuple(_ref(item) for item in value["evidence"]))
        except (TypeError, ValueError):
            raise PreparationError("preparation recipe bindings are invalid") from None


def _safe_root(root: Path | str, *, private: bool = False) -> Path:
    path = Path(root)
    if (not path.is_absolute() or ".." in path.parts or len(os.fsencode(path)) > 4096
            or str(path) != str(root)):
        raise PreparationError("preparation root must be a bounded canonical absolute path")
    try:
        for ancestor in (*reversed(path.parents), path):
            if not stat.S_ISDIR(ancestor.lstat().st_mode):
                raise PreparationError("preparation root is unsafe or contains a symlink")
        info = path.lstat()
        if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise PreparationError("preparation store root must be owner-private")
    except OSError:
        raise PreparationError("preparation root is unavailable") from None
    return path


def _store_binding(artifacts: ArtifactStore) -> dict[str, object]:
    if not isinstance(artifacts, ArtifactStore):
        raise PreparationError("preparation needs an existing artifact store")
    root = _safe_root(artifacts.root, private=True)
    try:
        info, pinned = root.lstat(), os.fstat(artifacts._root_fd)
    except (OSError, TypeError):
        raise PreparationError("preparation artifact store is closed or unavailable") from None
    if (info.st_dev, info.st_ino) != (pinned.st_dev, pinned.st_ino):
        raise PreparationError("preparation artifact store root was replaced")
    return {"root": str(root), "device": info.st_dev, "inode": info.st_ino,
            "max_file_bytes": artifacts.limits.max_file_bytes,
            "max_total_bytes": artifacts.limits.max_total_bytes}


def preparation_inputs(run_id: str, recipe: PreparationRecipe, artifacts: ArtifactStore) -> RunInputs:
    """Bind the local recipe and exact store into a journal before any process starts."""
    recipe = PreparationRecipe.from_bytes(recipe.to_bytes())
    runtime = recipe.release_binding.runtime_sha256
    return RunInputs(
        run_id=run_id, compiled_plan_sha256=recipe.plan_sha256,
        source_sha256=recipe.release_binding.digest, draft_sha256=recipe.digest,
        compiler_sha256=runtime, parser_sha256=runtime,
        provider_profiles={"local-preparation": _digest(canonical_json([dataclasses.asdict(stage) for stage in recipe.stages]))},
        skill_manifests={"preparation-store": _digest(canonical_json(_store_binding(artifacts)))},
    )


def _publish(artifacts: ArtifactStore, raw: bytes, *, json_artifact: bool = False,
             existing: bool = False) -> ArtifactRef:
    digest = _digest(raw)
    path = f"preparation/blobs/{digest}.{'json' if json_artifact else 'bin'}"
    ref = ArtifactRef(path, digest, len(raw))
    if existing:
        artifacts.read_bytes(ref)
        return ref
    try:
        return artifacts.write_bytes(path, raw)
    except ArtifactExistsError:
        if artifacts.read_bytes(ref) != raw:
            raise PreparationError("preparation artifact differs")
        return ref


def _stage_command(stage: PreparationStage, binding: ReleaseSourceBinding,
                   source_root: Path, slots: Path) -> ProcessCommand:
    argv = (sys.executable, "-B", str(source_root / "scripts/fanout_prepare.py"),
            "--internal-stage", stage.id, "--source-root", str(source_root))
    if any(len(os.fsencode(argument)) > 4096 for argument in argv):
        raise PreparationError("preparation command argument exceeds its byte bound")
    return ProcessCommand(
        argv=argv,
        stdin=canonical_json(binding.to_dict()), cwd=source_root,
        timeout=stage.timeout, max_output_bytes=stage.max_output_bytes, slot_root=slots,
    )


def _current(recipe: PreparationRecipe, root: Path) -> None:
    try:
        _safe_root(root)
        if release_source_binding(root) != recipe.release_binding:
            raise PreparationError("preparation source binding changed")
    except ValueError:
        raise PreparationError("preparation source closure is unsafe or unavailable") from None


def _summary(artifacts: ArtifactStore, digest: str, intent: ArtifactRef,
             recipe: PreparationRecipe, inputs: RunInputs, stage: PreparationStage) -> dict[str, object]:
    raw = artifacts.read_by_digest(f"preparation/blobs/{digest}.json", digest)
    value = _json(raw)
    if (not isinstance(value, dict) or value.get("schema_version") != "fanout-local-stage-result-v1"
            or value.get("inputs_digest") != inputs.digest or value.get("recipe_sha256") != recipe.digest
            or value.get("stage") != stage.id or value.get("intent_ref") != dataclasses.asdict(intent)
            or raw != canonical_json(value)):
        raise PreparationError("preparation summary association differs")
    for name in ("stdout_ref", "stderr_ref", "command_ref", "started_ref"):
        artifacts.read_bytes(_ref(value.get(name)))
    return value


def prepare_local(recipe: PreparationRecipe, *, source_root: Path | str,
                  artifacts: ArtifactStore, journal: RunJournal,
                  owner: OwnerCapability) -> dict[str, object]:
    """Run fixed local stages once, or authenticate their complete settled evidence."""
    root = _safe_root(source_root)
    recipe = PreparationRecipe.from_bytes(recipe.to_bytes())
    journal.authorize_owner(owner)
    expected = preparation_inputs(journal.inputs.run_id, recipe, artifacts)
    if journal.inputs != expected:
        raise PreparationError("preparation journal inputs or store association differ")
    if artifacts.root == root or root in artifacts.root.parents:
        raise PreparationError("preparation artifacts cannot be inside the source root")
    _current(recipe, root)
    try:
        for ref in recipe.evidence:
            artifacts.read_bytes(ref)
        summaries = []
        for stage in recipe.stages:
            _current(recipe, root)
            phase = journal.state.seat_phase(_TASK, stage.id, 1, 1)
            if phase is not None and phase != "checkpoint-verified":
                if phase != "uncertain-attempt":
                    journal.append("uncertain-attempt", task_id=_TASK, seat_id=stage.id,
                                   attempt=1, round=1, owner=owner)
                raise PreparationError("interrupted preparation stage is uncertain; automatic rerun is refused")
            command = _stage_command(stage, recipe.release_binding, root, journal.root / "preparation-slots")
            settled = phase == "checkpoint-verified"
            stdin_ref = _publish(artifacts, command.stdin, existing=settled)
            command_ref = _publish(artifacts, canonical_json({
                "argv": list(command.argv), "cwd": str(command.cwd),
                "stdin_ref": dataclasses.asdict(stdin_ref), "timeout": command.timeout,
                "max_output_bytes": command.max_output_bytes,
            }), json_artifact=True, existing=settled)
            intent = _publish(artifacts, canonical_json({
                "schema_version": "fanout-local-stage-intent-v1", "inputs_digest": expected.digest,
                "recipe_sha256": recipe.digest, "stage": stage.id,
                "release_binding": recipe.release_binding.to_dict(),
                "command_ref": dataclasses.asdict(command_ref),
            }), json_artifact=True, existing=settled)
            if phase == "checkpoint-verified":
                if journal.state.evidence_digest("dispatch-intent", _TASK, stage.id, 1, 1) != intent.digest:
                    raise PreparationError("preparation command or intent association changed")
                digest = journal.state.evidence_digest("provider-terminal", _TASK, stage.id, 1, 1)
                if (not digest or journal.state.evidence_digest("checkpoint-verified", _TASK, stage.id, 1, 1) != digest
                        or journal.state.evidence_digest("checkpoint-published", _TASK, stage.id, 1, 1) != digest):
                    raise PreparationError("preparation terminal evidence is incomplete")
                summary = _summary(artifacts, digest, intent, recipe, expected, stage)
            else:
                journal.append("dispatch-intent", task_id=_TASK, seat_id=stage.id, attempt=1,
                               round=1, evidence_sha256=intent.digest)
                started_ref = None

                def started(pid: int) -> None:
                    nonlocal started_ref
                    started_ref = _publish(artifacts, canonical_json({
                        "pid": pid, "intent_ref": dataclasses.asdict(intent),
                    }), json_artifact=True)
                    journal.append("process-started", task_id=_TASK, seat_id=stage.id, attempt=1, round=1)

                try:
                    _current(recipe, root)
                    result = run_command(command, on_started=started,
                                         base_environment={"LANG": "C", "LLM_FANOUT_DEPTH": "0"})
                except BaseException:
                    if journal.state.seat_phase(_TASK, stage.id, 1, 1) != "uncertain-attempt":
                        journal.append("uncertain-attempt", task_id=_TASK, seat_id=stage.id,
                                       attempt=1, round=1, owner=owner)
                    raise
                stdout = _publish(artifacts, result.stdout)
                stderr = _publish(artifacts, result.stderr)
                _store_binding(artifacts)
                summary = {
                    "schema_version": "fanout-local-stage-result-v1", "stage": stage.id,
                    "inputs_digest": expected.digest, "recipe_sha256": recipe.digest,
                    "intent_ref": dataclasses.asdict(intent), "command_ref": dataclasses.asdict(command_ref),
                    "started_ref": dataclasses.asdict(started_ref) if started_ref else None,
                    "stdout_ref": dataclasses.asdict(stdout), "stderr_ref": dataclasses.asdict(stderr),
                    "pid": result.pid, "process_status": result.status.value,
                    "returncode": result.returncode, "capture_complete": result.capture_complete,
                    "timeout": dataclasses.asdict(result.timeout) if result.timeout else None,
                    "error": result.error,
                    "success": result.status is ProcessStatus.EXIT and result.returncode == 0 and result.capture_complete,
                }
                summary_ref = _publish(artifacts, canonical_json(summary), json_artifact=True)
                if started_ref is None:
                    journal.append("uncertain-attempt", task_id=_TASK, seat_id=stage.id,
                                   attempt=1, round=1, owner=owner)
                    raise PreparationError("local stage did not start; retained capture cannot authorize an automatic rerun")
                journal.append("provider-terminal", task_id=_TASK, seat_id=stage.id, attempt=1,
                               round=1, evidence_sha256=summary_ref.digest)
                _current(recipe, root)
                _summary(artifacts, summary_ref.digest, intent, recipe, expected, stage)
                for event in ("artifacts-durable", "publication-intent", "checkpoint-published", "checkpoint-verified"):
                    journal.append(event, task_id=_TASK, seat_id=stage.id, attempt=1, round=1,
                                   evidence_sha256=summary_ref.digest if event.startswith("checkpoint-") else None)
            summaries.append(summary)
            if not summary["success"]:
                break
        _current(recipe, root)
        return {"status": "local-preparation-complete" if all(item["success"] for item in summaries) else "local-preparation-failed",
                "certification": False, "release_binding": recipe.release_binding.to_dict(),
                "recipe_sha256": recipe.digest, "stages": summaries,
                "evidence": [dataclasses.asdict(ref) for ref in recipe.evidence]}
    except (ArtifactError, OSError):
        raise PreparationError("preparation artifact evidence is unavailable or damaged") from None
