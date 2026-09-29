"""Deterministic, fail-closed skill discovery and seat-bundle staging."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from .artifacts import ArtifactStore, canonical_json
from .errors import SkillAdmissionError
from .plan import FanoutPlanV1, FanoutPlanV2, effective_skills
from .runstate import RunInputs
from .verification import TargetEvidenceEnvelope, read_target_evidence


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_REFERENCE_DEFINITION = re.compile(
    r"^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*(?:\r?\n[ \t]*)?(<[^>\n]+>|\S+)",
    re.MULTILINE,
)
_REFERENCE_USE = re.compile(r"\[([^\]\n]+)\](?:\[([^\]\n]*)\])?")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_ENCODED_AMBIGUITY = re.compile(r"%(?:2[fF]|5[cC]|25)")


@dataclass(frozen=True, slots=True)
class SkillLimits:
    """Finite limits for one untrusted skill directory."""

    max_file_bytes: int = 1_000_000
    max_tree_bytes: int = 5_000_000
    max_files: int = 256
    max_entries: int = 512
    max_directories: int = 128
    max_depth: int = 8

    def __post_init__(self) -> None:
        for name in ("max_file_bytes", "max_tree_bytes", "max_files", "max_entries", "max_directories", "max_depth"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise SkillAdmissionError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class SkillRoot:
    """One explicit provider/target source root, ordered by ``precedence``."""

    identity: str
    path: Path | str
    precedence: int
    provider: str = ""
    target: str = ""

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.identity):
            raise SkillAdmissionError("skill root identity must be a safe component")
        if not isinstance(self.precedence, int) or isinstance(self.precedence, bool) or self.precedence < 0:
            raise SkillAdmissionError("skill root precedence must be a non-negative integer")
        for field in ("provider", "target"):
            value = getattr(self, field)
            if value and not _NAME.fullmatch(value):
                raise SkillAdmissionError(f"skill root {field} must be a safe component")
        object.__setattr__(self, "path", Path(self.path))


@dataclass(frozen=True, slots=True)
class ResolvedSkill:
    """One fully inspected source tree, including all identical shadow origins."""

    name: str
    source: SkillRoot
    origins: tuple[SkillRoot, ...]
    tree_hash: str
    skill_md: str
    file_hashes: Mapping[str, str]
    manifest: tuple[tuple[str, str, int, int], ...]
    references: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class SkillAdmission:
    """Exact skills admitted to one provider session, optionally with a staged bundle."""

    task_id: str
    seat_id: str
    provider: str
    session_id: str
    skills: tuple[ResolvedSkill, ...]
    staged_root: Path | None = None
    engine_delivered: bool = False
    engine_evidence: tuple["SkillLoadEvidence", ...] = ()

    def manifest_dict(self) -> dict[str, object]:
        return {
            "schema_version": "v1",
            "task_id": self.task_id,
            "seat_id": self.seat_id,
            "provider": self.provider,
            "session_id": self.session_id,
            "skills": [
                {
                    "name": skill.name,
                    "source": skill.source.identity,
                    "origins": [origin.identity for origin in skill.origins],
                    "tree_hash": skill.tree_hash,
                    "skill_md": skill.skill_md,
                    "references": [{"path": path, "sha256": digest} for path, digest in skill.references],
                    "files": [
                        {"path": path, "sha256": digest, "size": size, "mode": mode}
                        for path, digest, size, mode in skill.manifest
                    ],
                }
                for skill in self.skills
            ],
        }


@dataclass(frozen=True, slots=True)
class SkillLoadEvidence:
    """One untrusted delivery observation; only ``engine`` is delivery authority."""

    kind: str
    skill: str = ""
    tree_hash: str = ""
    source: str = ""
    provider: str = ""
    session_id: str = ""
    seat_id: str = ""
    truncated: bool = False
    text: str = ""

    @classmethod
    def engine(cls, skill: ResolvedSkill, admission: SkillAdmission, **overrides: object) -> "SkillLoadEvidence":
        values: dict[str, object] = {
            "kind": "engine", "skill": skill.name, "tree_hash": skill.tree_hash,
            "source": skill.source.identity, "provider": admission.provider,
            "session_id": admission.session_id, "seat_id": admission.seat_id,
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    @classmethod
    def native(cls, skill: ResolvedSkill, admission: SkillAdmission, **overrides: object) -> "SkillLoadEvidence":
        values: dict[str, object] = {
            "kind": "native", "skill": skill.name, "tree_hash": skill.tree_hash,
            "source": skill.source.identity, "provider": admission.provider,
            "session_id": admission.session_id, "seat_id": admission.seat_id,
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    @classmethod
    def model_acknowledgement(cls, text: str) -> "SkillLoadEvidence":
        return cls(kind="model", text=text)


def load_target_skill_evidence(
    envelope: TargetEvidenceEnvelope, *, plan: FanoutPlanV2,
    inputs: RunInputs, store: ArtifactStore,
) -> tuple[SkillLoadEvidence, ...]:
    """Restore target-bound observations without treating model text as delivery proof."""
    data = read_target_evidence(
        envelope, plan=plan, inputs=inputs, store=store,
        evidence_kind="skill-load", max_bytes=1024 * 1024,
    )
    try:
        value = json.loads(data.decode("utf-8"))
        canonical = canonical_json(value)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as error:
        raise SkillAdmissionError("target skill evidence schema is invalid") from error
    target = {
        "run_id": envelope.run_id, "task_id": envelope.task_id,
        "target_id": envelope.target_id, "repository": envelope.repository,
        "branch_ref": envelope.branch_ref, "base_oid": envelope.base_oid,
        "baseline_sha256": envelope.baseline_sha256,
    }
    if (
        not isinstance(value, dict) or set(value) != {"schema_version", "target", "events"}
        or value["schema_version"] != "fanout-target-skill-load-v1"
        or not isinstance(value["events"], list) or len(value["events"]) > 128
        or canonical != data
    ):
        raise SkillAdmissionError("target skill evidence schema is invalid")
    if value["target"] != target:
        raise SkillAdmissionError("target skill evidence target differs")
    fields = {
        "kind", "skill", "tree_hash", "source", "provider", "session_id",
        "seat_id", "truncated", "text",
    }
    events = []
    for item in value["events"]:
        if (
            not isinstance(item, dict) or set(item) != fields
            or item["kind"] not in {"engine", "native", "model"}
            or type(item["truncated"]) is not bool
            or any(not isinstance(item[name], str) or len(item[name].encode("utf-8")) > 65536
                   for name in fields - {"truncated"})
        ):
            raise SkillAdmissionError("target skill event is invalid")
        events.append(SkillLoadEvidence(**item))
    return tuple(events)


class SkillResolver:
    """Resolve only configured roots, then stage verified immutable seat bundles."""

    def __init__(self, roots: Sequence[SkillRoot], *, limits: SkillLimits | None = None) -> None:
        if not roots or any(not isinstance(root, SkillRoot) for root in roots):
            raise SkillAdmissionError("skill resolver requires explicit SkillRoot values")
        ordered = tuple(sorted(roots, key=lambda root: (root.precedence, root.identity, str(root.path))))
        if len({root.identity for root in ordered}) != len(ordered):
            raise SkillAdmissionError("skill root identities must be unique")
        if len({root.precedence for root in ordered}) != len(ordered):
            raise SkillAdmissionError("skill root precedence must be unique")
        self.roots = ordered
        self.limits = limits or SkillLimits()

    def resolve(self, names: Sequence[str]) -> tuple[ResolvedSkill, ...]:
        names = tuple(names)
        if len(names) != len(set(names)):
            raise SkillAdmissionError("requested skill names must be unique")
        return tuple(self._resolve(name) for name in names)

    def admit(self, names: Sequence[str], *, task_id: str, seat_id: str, provider: str, session_id: str) -> SkillAdmission:
        _identity(task_id, "task id")
        _identity(seat_id, "seat id")
        _identity(provider, "provider")
        _identity(session_id, "session id")
        return SkillAdmission(task_id, seat_id, provider, session_id, self.resolve(names))

    def admit_plan(self, plan: FanoutPlanV1, task_id: str, *, seat_id: str, provider: str,
                   session_id: str) -> SkillAdmission:
        return self.admit(effective_skills(plan, task_id), task_id=task_id, seat_id=seat_id,
                          provider=provider, session_id=session_id)

    def stage(self, admission: SkillAdmission, destination: Path | str) -> SkillAdmission:
        if not isinstance(admission, SkillAdmission):
            raise SkillAdmissionError("stage requires a SkillAdmission")
        destination = Path(destination)
        if destination.name in {"", ".", ".."}:
            raise SkillAdmissionError("staged destination name is unsafe")
        parent = destination.parent
        _safe_existing_directory(parent, "staging parent")
        try:
            os.mkdir(destination, 0o700)
        except FileExistsError as error:
            raise SkillAdmissionError(f"staged destination already exists: {destination}") from error
        except OSError as error:
            raise SkillAdmissionError(f"could not reserve staged destination: {destination}") from error
        owned = [(destination, _inode(destination))]
        temporary: Path | None = None
        try:
            temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=destination))
            os.chmod(temporary, 0o700)
            owned.append((temporary, _inode(temporary)))
            for skill in admission.skills:
                current = self._resolve(skill.name)
                if (current.source != skill.source or current.origins != skill.origins
                        or current.tree_hash != skill.tree_hash or current.manifest != skill.manifest):
                    raise SkillAdmissionError(f"skill drift detected before staging: {skill.name}")
                self._copy_skill(current, temporary / skill.name, owned)
                staged_hash = _tree_hash(temporary / skill.name, self.limits)[0]
                if staged_hash != skill.tree_hash:
                    raise SkillAdmissionError(f"staged skill hash drift: {skill.name}")
                after = self._resolve(skill.name)
                if (after.source != skill.source or after.origins != skill.origins
                        or after.tree_hash != skill.tree_hash or after.manifest != skill.manifest):
                    raise SkillAdmissionError(f"skill drift detected while staging: {skill.name}")
            manifest = canonical_json(admission.manifest_dict())
            _write_private_file(temporary / "manifest.json", manifest, executable=False, owned=owned)
            _fsync_directory(temporary)
            ready = destination / "ready"
            os.rename(temporary, ready)
            owned = _relocate_owned(owned, temporary, ready)
            temporary = None
            _fsync_directory(destination)
            _fsync_directory(parent)
        except OSError as error:
            _cleanup_owned(owned)
            raise SkillAdmissionError(f"could not atomically stage skill bundle: {destination}") from error
        except Exception:
            _cleanup_owned(owned)
            raise
        return replace(admission, staged_root=ready)

    def verify_engine_delivery(self, admission: SkillAdmission,
                               evidence: Iterable[SkillLoadEvidence]) -> SkillAdmission:
        events = tuple(evidence)
        self._verify_staged(admission)
        self._verify_events(admission, events, "engine", required=True)
        return replace(admission, engine_delivered=True, engine_evidence=events)

    def verify_native_events(self, admission: SkillAdmission,
                             evidence: Iterable[SkillLoadEvidence]) -> tuple[SkillLoadEvidence, ...]:
        events = tuple(evidence)
        if events:
            self._verify_events(admission, events, "native", required=True)
        return events

    def verify_model_acknowledgements(self, admission: SkillAdmission,
                                      evidence: Iterable[SkillLoadEvidence]) -> tuple[SkillLoadEvidence, ...]:
        events = tuple(evidence)
        if any(event.kind != "model" for event in events):
            raise SkillAdmissionError("model acknowledgement telemetry must be model text only")
        return events

    def _verify_events(self, admission: SkillAdmission, events: tuple[SkillLoadEvidence, ...], kind: str,
                       *, required: bool) -> None:
        expected = {skill.name: skill for skill in admission.skills}
        if not events and required and expected:
            raise SkillAdmissionError(f"missing {kind} delivery evidence")
        seen: set[str] = set()
        for event in events:
            if not isinstance(event, SkillLoadEvidence) or event.kind != kind:
                raise SkillAdmissionError(f"{kind} evidence has an invalid kind")
            if event.truncated:
                raise SkillAdmissionError(f"{kind} evidence was truncated")
            skill = expected.get(event.skill)
            if skill is None:
                raise SkillAdmissionError(f"{kind} evidence names an unadmitted skill")
            if event.skill in seen:
                raise SkillAdmissionError(f"duplicate {kind} evidence for skill {event.skill}")
            seen.add(event.skill)
            if event.tree_hash != skill.tree_hash:
                raise SkillAdmissionError(f"{kind} evidence hash mismatch for skill {event.skill}")
            if event.source != skill.source.identity:
                raise SkillAdmissionError(f"{kind} evidence source mismatch for skill {event.skill}")
            if event.provider != admission.provider or event.session_id != admission.session_id or event.seat_id != admission.seat_id:
                raise SkillAdmissionError(f"{kind} evidence provider/session/seat identity mismatch")
        if set(seen) != set(expected):
            raise SkillAdmissionError(f"missing {kind} evidence for admitted skill")

    def _resolve(self, name: str) -> ResolvedSkill:
        _skill_name(name)
        candidates: list[ResolvedSkill] = []
        for root in self.roots:
            root_path = _safe_existing_directory(root.path, f"skill root {root.identity}")
            skill_path = root_path / name
            if skill_path.exists() or skill_path.is_symlink():
                candidates.append(self._resolve_from_root(root, name))
        if not candidates:
            raise SkillAdmissionError(f"required skill is missing from configured roots: {name}")
        selected = candidates[0]
        divergent = [candidate for candidate in candidates[1:] if candidate.tree_hash != selected.tree_hash]
        if divergent:
            raise SkillAdmissionError(f"divergent skill shadows for {name}")
        return replace(selected, origins=tuple(candidate.source for candidate in candidates))

    def _resolve_from_root(self, root: SkillRoot, name: str) -> ResolvedSkill:
        _skill_name(name)
        root_path = _safe_existing_directory(root.path, f"skill root {root.identity}")
        path = root_path / name
        tree_hash, records, contents = _tree_hash(path, self.limits)
        skill_md = contents.get("SKILL.md")
        if skill_md is None:
            raise SkillAdmissionError(f"skill has no regular SKILL.md: {path}")
        try:
            decoded = skill_md.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SkillAdmissionError(f"SKILL.md must be valid UTF-8: {path}") from error
        file_hashes = {relative: digest for relative, digest, _size, _mode in records}
        references = _references(decoded, path, contents, file_hashes)
        return ResolvedSkill(name, root, (root,), tree_hash, decoded, file_hashes, records, references)

    def _copy_skill(self, skill: ResolvedSkill, destination: Path,
                    owned: list[tuple[Path, tuple[int, int]]]) -> None:
        _mkdir_owned(destination, owned)
        for relative, digest, _size, mode in skill.manifest:
            target = destination / relative
            cursor = destination
            for part in PurePosixPath(relative).parts[:-1]:
                cursor /= part
                if not cursor.exists():
                    _mkdir_owned(cursor, owned)
            source = Path(skill.source.path) / skill.name / relative
            data = _read_regular(source)
            if hashlib.sha256(data).hexdigest() != digest:
                raise SkillAdmissionError(f"skill drift while staging: {skill.name}/{relative}")
            _write_private_file(target, data, executable=bool(mode & 0o111), owned=owned)
        _fsync_directory(destination)

    def _verify_staged(self, admission: SkillAdmission) -> None:
        verify_staged_admission(admission, limits=self.limits)


def verify_staged_admission(
    admission: SkillAdmission, *, limits: SkillLimits | None = None,
) -> None:
    """Reverify the exact immutable staged tree retained by a Task 6 admission."""
    if not isinstance(admission, SkillAdmission):
        raise SkillAdmissionError("staged verification requires a skill admission")
    if admission.engine_delivered and not admission.engine_evidence and admission.skills:
        raise SkillAdmissionError("engine delivery evidence is missing")
    root = admission.staged_root
    if root is None:
        raise SkillAdmissionError("engine delivery requires a staged skill bundle")
    root = _safe_existing_directory(root, "staged bundle")
    if stat.S_IMODE(os.lstat(root).st_mode) != 0o700:
        raise SkillAdmissionError("staged bundle is not private")
    expected_entries = {"manifest.json", *(skill.name for skill in admission.skills)}
    observed_entries = {entry.name for entry in os.scandir(root)}
    if observed_entries != expected_entries:
        raise SkillAdmissionError("staged bundle contains unrelated or missing skills")
    manifest = root / "manifest.json"
    if (
        stat.S_IMODE(os.lstat(manifest).st_mode) != 0o400
        or _read_regular(manifest) != canonical_json(admission.manifest_dict())
    ):
        raise SkillAdmissionError("staged bundle manifest drifted")
    bounded = limits or SkillLimits()
    for skill in admission.skills:
        staged = root / skill.name
        if _tree_hash(staged, bounded)[0] != skill.tree_hash:
            raise SkillAdmissionError(f"staged skill hash drift: {skill.name}")
        expected = {path: mode for path, _digest, _size, mode in skill.manifest}
        for relative, path, info in _walk(staged, bounded):
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                if mode != 0o700:
                    raise SkillAdmissionError(f"staged skill is not private: {path}")
            elif mode != (0o500 if expected[relative] & 0o111 else 0o400):
                raise SkillAdmissionError(f"staged skill is not read-only: {path}")


def _tree_hash(root: Path, limits: SkillLimits) -> tuple[str, tuple[tuple[str, str, int, int], ...], dict[str, bytes]]:
    root = _safe_skill_directory(root)
    entries = _walk(root, limits)
    digest = hashlib.sha256()
    records: list[tuple[str, str, int, int]] = []
    contents: dict[str, bytes] = {}
    seen_file = False
    for relative, path, entry in entries:
        encoded = relative.encode()
        mode = _canonical_mode(entry.st_mode)
        if stat.S_ISDIR(entry.st_mode):
            digest.update(b"D\0" + encoded + b"\0" + oct(mode).encode() + b"\n")
            continue
        data = _read_regular(path, expected=entry)
        digest.update(b"F\0" + encoded + b"\0" + oct(mode).encode() + b"\0" + str(len(data)).encode() + b"\0" + data + b"\n")
        records.append((relative, hashlib.sha256(data).hexdigest(), len(data), mode))
        contents[relative] = data
        seen_file = True
    if not seen_file:
        raise SkillAdmissionError(f"skill tree contains no files: {root}")
    return "sha256:" + digest.hexdigest(), tuple(records), contents


def _walk(root: Path, limits: SkillLimits) -> list[tuple[str, Path, os.stat_result]]:
    total = 0
    files = 0
    entries = 0
    directories = 0
    result: list[tuple[str, Path, os.stat_result]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in os.scandir(directory):
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if not _safe_relative(relative):
                raise SkillAdmissionError(f"unsafe skill path: {path}")
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise SkillAdmissionError(f"skill tree contains a symlink: {path}")
            depth = len(PurePosixPath(relative).parts)
            if depth > limits.max_depth:
                raise SkillAdmissionError(f"skill tree exceeds depth limit: {path}")
            entries += 1
            if entries > limits.max_entries:
                raise SkillAdmissionError(f"skill entry count exceeds limit: {root}")
            if stat.S_ISDIR(info.st_mode):
                directories += 1
                if directories > limits.max_directories:
                    raise SkillAdmissionError(f"skill directory count exceeds limit: {root}")
                _safe_mode(info, path)
                result.append((relative, path, info))
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                _safe_mode(info, path)
                if info.st_nlink != 1:
                    raise SkillAdmissionError(f"skill tree contains a hard-linked file: {path}")
                if info.st_size > limits.max_file_bytes:
                    raise SkillAdmissionError(f"skill file size exceeds limit: {path}")
                files += 1
                total += info.st_size
                if files > limits.max_files:
                    raise SkillAdmissionError(f"skill file count exceeds limit: {root}")
                if total > limits.max_tree_bytes:
                    raise SkillAdmissionError(f"skill tree size exceeds limit: {root}")
                result.append((relative, path, info))
            else:
                raise SkillAdmissionError(f"skill tree contains an unsupported filesystem entry: {path}")
    return sorted(result, key=lambda item: item[0])


def _references(skill_md: str, root: Path, contents: Mapping[str, bytes],
                file_hashes: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    paths = set(_local_markdown_targets(skill_md))
    result: list[tuple[str, str]] = []
    for relative in sorted(paths):
        if relative not in contents:
            raise SkillAdmissionError(f"missing referenced resource: {root / relative}")
        try:
            linked = contents[relative].decode("utf-8")
        except UnicodeDecodeError:
            linked = ""
        if tuple(_local_markdown_targets(linked)):
            raise SkillAdmissionError(f"reference index supports one-level resources only: {root / relative}")
        result.append((relative, file_hashes[relative]))
    return tuple(result)


def _local_markdown_targets(text: str) -> tuple[str, ...]:
    """Return safe direct local link paths; ignore ordinary external URI schemes."""
    targets: list[str] = []
    for raw in (*_LINK.findall(text), *_reference_style_targets(text)):
        stripped = raw.strip()
        target = stripped[1:stripped.index(">")] if stripped.startswith("<") and ">" in stripped else stripped.split(maxsplit=1)[0]
        if not target:
            continue
        decoded_target = unquote(target)
        if _WINDOWS_DRIVE.match(target) or _WINDOWS_DRIVE.match(decoded_target):
            raise SkillAdmissionError(f"referenced resource escapes skill root: {raw}")
        parsed = urlsplit(target)
        if parsed.scheme:
            if parsed.scheme.lower() == "file":
                raise SkillAdmissionError(f"referenced resource uses unsafe file URI: {raw}")
            continue
        if parsed.netloc or parsed.query or parsed.path.startswith(("/", "\\")):
            raise SkillAdmissionError(f"referenced resource escapes skill root: {raw}")
        if _ENCODED_AMBIGUITY.search(parsed.path):
            raise SkillAdmissionError(f"referenced resource has an encoded path ambiguity: {raw}")
        path = unquote(parsed.path)
        if not path:
            continue
        if path.startswith("./"):
            path = path[2:]
        if ("%" in path or "\x00" in path or "\\" in path or ":" in path
                or _WINDOWS_ABSOLUTE.match(path) or not _safe_relative(path)):
            raise SkillAdmissionError(f"referenced resource escapes skill root: {raw}")
        targets.append(path)
    return tuple(targets)


def _reference_style_targets(text: str) -> tuple[str, ...]:
    definitions: dict[str, str] = {}
    for match in _REFERENCE_DEFINITION.finditer(text):
        label = " ".join(match.group(1).split()).casefold()
        target = match.group(2)
        if label in definitions and definitions[label] != target:
            raise SkillAdmissionError(f"conflicting Markdown reference definition: {match.group(1)}")
        definitions[label] = target
    body = _REFERENCE_DEFINITION.sub("", text)
    targets: list[str] = []
    for match in _REFERENCE_USE.finditer(body):
        if match.group(2) is None and body[match.end():].startswith("("):
            continue
        label = " ".join((match.group(2) or match.group(1)).split()).casefold()
        if label in definitions:
            targets.append(definitions[label])
    return tuple(targets)


def _safe_skill_directory(path: Path) -> Path:
    path = _safe_existing_directory(path, "skill tree")
    return path


def _safe_existing_directory(path: Path | str, what: str) -> Path:
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as error:
            raise SkillAdmissionError(f"{what} does not exist: {path}") from error
        if stat.S_ISLNK(info.st_mode):
            raise SkillAdmissionError(f"{what} contains a symlink: {current}")
    try:
        info = os.lstat(path)
    except OSError as error:
        raise SkillAdmissionError(f"{what} does not exist: {path}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise SkillAdmissionError(f"{what} must be a real directory: {path}")
    _safe_mode(info, path)
    return path


def _read_regular(path: Path, *, expected: os.stat_result | None = None) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SkillAdmissionError(f"could not safely read skill file: {path}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SkillAdmissionError(f"skill file changed type while reading: {path}")
        if expected is not None and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns
        ):
            raise SkillAdmissionError(f"skill file drift while reading: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as file:
            data = file.read()
        final = os.fstat(descriptor)
        if expected is not None and (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != (
            expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns
        ):
            raise SkillAdmissionError(f"skill file drift while reading: {path}")
        return data
    finally:
        os.close(descriptor)


def _write_private_file(path: Path, data: bytes, *, executable: bool,
                        owned: list[tuple[Path, tuple[int, int]]] | None = None) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o500 if executable else 0o400)
    try:
        if owned is not None:
            info = os.fstat(descriptor)
            owned.append((path, (info.st_dev, info.st_ino)))
        with os.fdopen(descriptor, "wb", closefd=False) as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    finally:
        os.close(descriptor)


def _inode(path: Path) -> tuple[int, int]:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise SkillAdmissionError(f"refusing symlinked staging path: {path}")
    return info.st_dev, info.st_ino


def _mkdir_owned(path: Path, owned: list[tuple[Path, tuple[int, int]]]) -> None:
    os.mkdir(path, 0o700)
    owned.append((path, _inode(path)))


def _relocate_owned(owned: list[tuple[Path, tuple[int, int]]], old: Path,
                    new: Path) -> list[tuple[Path, tuple[int, int]]]:
    relocated: list[tuple[Path, tuple[int, int]]] = []
    for path, identity in owned:
        try:
            relocated.append((new / path.relative_to(old), identity))
        except ValueError:
            relocated.append((path, identity))
    return relocated


def _cleanup_owned(owned: Sequence[tuple[Path, tuple[int, int]]]) -> None:
    """Remove only exact files and empty directories created by this call."""
    for path, identity in reversed(owned):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if (info.st_dev, info.st_ino) != identity:
            continue
        try:
            if stat.S_ISDIR(info.st_mode):
                os.rmdir(path)
            elif stat.S_ISREG(info.st_mode):
                os.unlink(path)
        except OSError:
            continue


def _canonical_mode(mode: int) -> int:
    return 0o755 if mode & 0o111 else 0o644


def _safe_mode(info: os.stat_result, path: Path) -> None:
    if stat.S_IMODE(info.st_mode) & 0o7022:
        raise SkillAdmissionError(f"skill tree contains an unsafe mode: {path}")


def _safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _skill_name(value: str) -> None:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise SkillAdmissionError("skill names must be safe single path components")


def _identity(value: str, where: str) -> None:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise SkillAdmissionError(f"{where} must be a safe non-empty identity")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
