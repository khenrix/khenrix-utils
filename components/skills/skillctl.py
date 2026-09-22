#!/usr/bin/env python3
"""Deliver the declared public Khenrix skills without installing a marketplace.

The controller deliberately owns only the names declared in
``capabilities.toml [skill_delivery].skills``.  It copies those directories to
the three shared skill roots and reconciles the same bounded house-style block
for Claude, Codex, agy, and Maka. Everything beside those exact paths is outside
its authority.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MANAGED_BEGIN = "<!-- khenrix-managed:begin house-style -->"
MANAGED_END = "<!-- khenrix-managed:end house-style -->"
RECEIPT_NAME = "install-receipt.json"
OPERATION_LOCK_NAME = "operation.lock"


class DeliveryError(RuntimeError):
    """A state the controller cannot change without risking unrelated data."""


@dataclass(frozen=True)
class Entry:
    key: str
    kind: str
    source: Path
    target: Path
    desired_hash: str
    current_hash: str | None
    action: str
    skill: str | None = None
    target_name: str | None = None

    def plan_record(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "skill": self.skill,
            "target_name": self.target_name,
            "source": str(self.source),
            "target": str(self.target),
            "desired_hash": self.desired_hash,
            "current_hash": self.current_hash,
            "action": self.action,
        }


@dataclass(frozen=True)
class Configuration:
    repo_root: Path
    home: Path
    state_dir: Path
    skills: tuple[str, ...]
    sources: dict[str, Path]
    targets: dict[str, Path]
    house_source: Path
    instruction_targets: dict[str, Path]
    instruction_overlays: dict[str, Path]


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_utf8_bytes(path: Path) -> tuple[bytes, str]:
    """Read UTF-8 without universal-newline conversion."""
    data = path.read_bytes()
    return data, data.decode("utf-8")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
SKILL_NAME = re.compile(r"^[a-z0-9-]{1,64}$")
YAML_BLOCK_SCALARS = {">", ">-", ">+", "|", "|-", "|+"}
YAML_EMPTY_SCALARS = {"", "~", "null"}


def skill_frontmatter_identity(text: str) -> tuple[str | None, str | None]:
    """Return the top-level name and non-empty description from skill frontmatter.

    The repository intentionally has no YAML runtime dependency. Skill headers use
    either ordinary scalar values or YAML folded/literal blocks, so parse only those
    two shapes and fail closed for missing or empty values.
    """
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return None, None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None, None
    header = lines[1:end]
    name: str | None = None
    description: str | None = None
    for index, line in enumerate(header):
        if line.startswith("name:"):
            raw_name = line.split(":", 1)[1].strip()
            if len(raw_name) >= 2 and raw_name[0] == raw_name[-1] and raw_name[0] in "\"'":
                raw_name = raw_name[1:-1].strip()
            name = raw_name or None
        elif line.startswith("description:"):
            raw_description = line.split(":", 1)[1].strip()
            if raw_description in YAML_BLOCK_SCALARS:
                content: list[str] = []
                for continuation in header[index + 1 :]:
                    if continuation and not continuation[0].isspace():
                        break
                    stripped = continuation.strip()
                    if stripped and not stripped.startswith("#"):
                        content.append(stripped)
                description = " ".join(content) or None
            else:
                if (
                    len(raw_description) >= 2
                    and raw_description[0] == raw_description[-1]
                    and raw_description[0] in "\"'"
                ):
                    raw_description = raw_description[1:-1].strip()
                description = (
                    None
                    if raw_description.lower() in YAML_EMPTY_SCALARS
                    or raw_description.startswith("#")
                    else raw_description
                )
    return name, description


def validate_install_receipt_header(receipt: Any) -> None:
    """Enforce the receipt structure shared with Agentic Setup and Maka smoke."""
    if not isinstance(receipt, dict):
        raise DeliveryError("install receipt must be a JSON object")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise DeliveryError("install receipt has an unsupported schema")
    plan_id = receipt.get("plan_id")
    if not isinstance(plan_id, str) or not SHA256_ID.fullmatch(plan_id):
        raise DeliveryError("install receipt has an invalid plan_id")
    applied_at = receipt.get("applied_at")
    if not isinstance(applied_at, str) or not applied_at:
        raise DeliveryError("install receipt has no applied_at timestamp")
    try:
        parsed = dt.datetime.fromisoformat(applied_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise DeliveryError("install receipt has an invalid applied_at timestamp") from error
    if parsed.tzinfo is None:
        raise DeliveryError("install receipt applied_at timestamp has no timezone")
    backup_id = receipt.get("backup_id")
    if backup_id is not None and (not isinstance(backup_id, str) or not backup_id):
        raise DeliveryError("install receipt has an invalid backup_id")
    skills = receipt.get("skills")
    if not isinstance(skills, dict):
        raise DeliveryError("install receipt skills must be an object")
    for name, record in skills.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise DeliveryError("install receipt has an invalid skill record")
        if not isinstance(record.get("source_hash"), str) or not SHA256_ID.fullmatch(
            record["source_hash"]
        ):
            raise DeliveryError(f"install receipt has an invalid source hash for {name}")
        targets = record.get("targets")
        if not isinstance(targets, dict):
            raise DeliveryError(f"install receipt targets must be an object for {name}")
        for target_name, target in targets.items():
            if (
                not isinstance(target_name, str)
                or not isinstance(target, dict)
                or not isinstance(target.get("path"), str)
                or not target["path"]
                or not isinstance(target.get("hash"), str)
                or not SHA256_ID.fullmatch(target["hash"])
            ):
                raise DeliveryError(
                    f"install receipt has an invalid target record for {name}:{target_name}"
                )
    instructions = receipt.get("instructions")
    if not isinstance(instructions, dict):
        raise DeliveryError("install receipt instructions must be an object")
    for target_name, record in instructions.items():
        if (
            not isinstance(target_name, str)
            or not isinstance(record, dict)
            or not isinstance(record.get("path"), str)
            or not record["path"]
            or not isinstance(record.get("managed_hash"), str)
            or not SHA256_ID.fullmatch(record["managed_hash"])
        ):
            raise DeliveryError(
                f"install receipt has an invalid instruction record for {target_name}"
            )
    maka = receipt.get("maka_instructions")
    if not isinstance(maka, dict) or not isinstance(maka.get("path"), str) or not isinstance(
        maka.get("managed_hash"), str
    ) or not SHA256_ID.fullmatch(maka["managed_hash"]):
        raise DeliveryError("install receipt has an invalid Maka instruction record")


def expand_home(raw: str, home: Path) -> Path:
    value = raw.replace("${HOME}", str(home))
    if value == "~":
        value = str(home)
    elif value.startswith("~/"):
        value = str(home / value[2:])
    return Path(value).absolute()


def load_configuration(repo_root: Path, home: Path, state_override: Path | None) -> Configuration:
    manifest = repo_root / "capabilities.toml"
    if not manifest.is_file():
        raise DeliveryError(f"capabilities.toml not found at {manifest}")
    with manifest.open("rb") as handle:
        data = tomllib.load(handle)
    raw = data.get("skill_delivery")
    if not isinstance(raw, dict):
        raise DeliveryError("capabilities.toml has no [skill_delivery] table")
    skills = raw.get("skills")
    if not isinstance(skills, list) or not skills or not all(isinstance(x, str) for x in skills):
        raise DeliveryError("[skill_delivery].skills must be a non-empty string list")
    if len(skills) != len(set(skills)):
        raise DeliveryError("[skill_delivery].skills contains duplicates")
    source_roots_raw = raw.get("source_roots", ["shared/skills"])
    if (
        not isinstance(source_roots_raw, list)
        or not source_roots_raw
        or not all(isinstance(value, str) and value for value in source_roots_raw)
        or len(source_roots_raw) != len(set(source_roots_raw))
    ):
        raise DeliveryError(
            "[skill_delivery].source_roots must be a non-empty unique string list"
        )
    source_roots: list[Path] = []
    for raw_root in source_roots_raw:
        relative = Path(raw_root)
        if relative.is_absolute() or ".." in relative.parts:
            raise DeliveryError(f"skill delivery source root escapes repository: {raw_root}")
        root = repo_root / relative
        if root.is_symlink() or not root.is_dir():
            raise DeliveryError(f"skill delivery source root must be a real directory: {root}")
        assert_no_symlink_path(root, repo_root, allow_missing=False)
        source_roots.append(root)
    sources: dict[str, Path] = {}
    for skill in skills:
        if not SKILL_NAME.fullmatch(skill):
            raise DeliveryError(f"invalid delivered skill name: {skill!r}")
        matches = [root / skill for root in source_roots if (root / skill).exists()]
        if len(matches) != 1:
            raise DeliveryError(
                f"delivered skill must resolve in exactly one source root: {skill!r}; "
                f"found {[str(path) for path in matches]}"
            )
        source = matches[0]
        if source.is_symlink() or not source.is_dir():
            raise DeliveryError(f"delivered skill source must be a real directory: {source}")
        entrypoint = source / "SKILL.md"
        if entrypoint.is_symlink() or not entrypoint.is_file():
            raise DeliveryError(f"delivered skill has no regular SKILL.md: {entrypoint}")
        for item in source.rglob("*"):
            if item.is_symlink():
                raise DeliveryError(f"delivered skill source contains a symlink: {item}")
            if not (item.is_file() or item.is_dir()):
                raise DeliveryError(
                    f"delivered skill source contains an unsupported entry: {item}"
                )
        frontmatter_name, frontmatter_description = skill_frontmatter_identity(
            entrypoint.read_text()
        )
        if frontmatter_name != skill:
            raise DeliveryError(
                f"delivered skill frontmatter name must match directory {skill!r}: "
                f"found {frontmatter_name!r}"
            )
        if not frontmatter_description:
            raise DeliveryError(
                f"delivered skill frontmatter description must be non-empty: {skill!r}"
            )
        sources[skill] = source
    target_data = raw.get("targets")
    if not isinstance(target_data, dict) or set(target_data) != {"claude", "codex_maka", "agy"}:
        raise DeliveryError(
            "[skill_delivery.targets] must declare exactly claude, codex_maka, and agy"
        )
    if not all(isinstance(value, str) for value in target_data.values()):
        raise DeliveryError("skill delivery targets must be path strings")
    targets = {name: expand_home(value, home) for name, value in target_data.items()}
    for name, path in targets.items():
        assert_inside_home(path, home, f"skill root {name}")

    source_raw = raw.get("instruction_source", "house-style.md")
    if not isinstance(source_raw, str):
        raise DeliveryError("skill_delivery.instruction_source must be a path string")
    source = repo_source_path(repo_root, source_raw, "skill delivery instruction source")
    instruction_data = raw.get("instruction_targets")
    expected_instructions = {"claude", "codex", "agy", "maka"}
    if not isinstance(instruction_data, dict) or set(instruction_data) != expected_instructions:
        raise DeliveryError(
            "[skill_delivery.instruction_targets] must declare exactly claude, codex, agy, and maka"
        )
    if not all(isinstance(value, str) for value in instruction_data.values()):
        raise DeliveryError("skill delivery instruction targets must be path strings")
    instruction_targets = {
        name: expand_home(value, home) for name, value in instruction_data.items()
    }
    for name, path in instruction_targets.items():
        assert_inside_home(path, home, f"{name} instruction target")

    # The legacy reconcile flow renders declared per-CLI overlays inside the same
    # house-style markers. Selective delivery must produce that identical block or
    # the controllers will alternate forever (most visibly for Claude).
    instruction_config = data.get("instructions") or {}
    overlay_data = instruction_config.get("overlays") or {}
    if not isinstance(overlay_data, dict):
        raise DeliveryError("[instructions.overlays] must be a table")
    unknown_overlays = set(overlay_data) - set(instruction_targets)
    if unknown_overlays:
        raise DeliveryError(
            "instruction overlays name unknown selective targets: "
            + ", ".join(sorted(unknown_overlays))
        )
    if not all(isinstance(value, str) for value in overlay_data.values()):
        raise DeliveryError("instruction overlay values must be path strings")
    instruction_overlays = {
        name: repo_source_path(repo_root, value, f"{name} instruction overlay")
        for name, value in overlay_data.items()
    }

    if state_override:
        state_dir = state_override.absolute()
    else:
        state_dir = expand_home(
            str(raw.get("state_dir", "${HOME}/.local/state/khenrix-utils/skills")), home
        )
    assert_inside_home(state_dir, home, "skill state directory")
    return Configuration(
        repo_root=repo_root,
        home=home,
        state_dir=state_dir,
        skills=tuple(skills),
        sources=sources,
        targets=targets,
        house_source=source,
        instruction_targets=instruction_targets,
        instruction_overlays=instruction_overlays,
    )


def assert_inside_home(path: Path, home: Path, label: str) -> None:
    """Require a traversal-free absolute path below HOME without resolving it.

    ``Path.relative_to`` is purely lexical: ``HOME / "safe/../../outside"`` still
    reports as relative to HOME because the ``..`` components are left intact.  Do
    not fix that with ``resolve()``.  Resolving would follow a managed symlink before
    ``assert_no_symlink_path`` gets a chance to reject it.  Refusing parent traversal
    keeps the subsequent component-by-component symlink check meaningful.
    """
    if not home.is_absolute() or not path.is_absolute():
        raise DeliveryError(f"{label} must be an absolute path inside HOME: {path}")
    if ".." in home.parts or ".." in path.parts:
        raise DeliveryError(f"{label} escapes HOME through parent traversal: {path}")
    try:
        path.relative_to(home)
    except ValueError as error:
        raise DeliveryError(f"{label} escapes HOME: {path}") from error


def repo_source_path(repo_root: Path, raw: str, label: str) -> Path:
    """Resolve one repository-owned source file without accepting path escape."""
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise DeliveryError(f"{label} escapes the repository: {raw}")
    path = repo_root / relative
    if path.is_symlink() or not path.is_file():
        raise DeliveryError(f"{label} is not a regular repository file: {path}")
    return path


def assert_no_symlink_path(path: Path, home: Path, *, allow_missing: bool = True) -> None:
    """Reject a symlink in the HOME-relative chain, including the leaf."""
    assert_inside_home(path, home, "managed path")
    relative = path.relative_to(home)
    current = home
    if current.exists() and current.is_symlink():
        raise DeliveryError(f"refusing symlinked HOME: {current}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DeliveryError(f"refusing symlink in managed path: {current}")
        if not current.exists():
            if allow_missing:
                return
            raise DeliveryError(f"required managed path does not exist: {current}")


def canonical_tree_mode(path: Path) -> int:
    """Return the portable mode recorded for a public skill tree entry."""

    if path.is_dir():
        return 0o755
    return 0o755 if path.stat().st_mode & 0o111 else 0o644


def tree_hash(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise DeliveryError(f"skill tree must be a real directory: {root}")
    digest = hashlib.sha256()
    seen = False
    for item in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        relative = item.relative_to(root).as_posix().encode()
        if item.is_symlink():
            raise DeliveryError(f"skill tree contains a symlink: {item}")
        mode = canonical_tree_mode(item)
        if item.is_dir():
            digest.update(b"D\0" + relative + b"\0" + oct(mode).encode() + b"\n")
        elif item.is_file():
            data = item.read_bytes()
            digest.update(
                b"F\0"
                + relative
                + b"\0"
                + oct(mode).encode()
                + b"\0"
                + str(len(data)).encode()
                + b"\0"
                + data
                + b"\n"
            )
            seen = True
        else:
            raise DeliveryError(f"skill tree contains an unsupported filesystem entry: {item}")
    if not seen:
        raise DeliveryError(f"skill tree contains no files: {root}")
    return "sha256:" + digest.hexdigest()


def observed_tree_hash(root: Path, *, canonical_modes: bool = False) -> str:
    """Hash skill bytes with either actual or predicted installed modes.

    ``tree_hash`` is the portable provenance hash shared with Agentic Setup: it
    deliberately canonicalizes checkout modes and excludes the root directory.
    Restore safety needs a different identity.  It must notice chmod-only edits,
    including an edit to the managed skill root itself, so this encoding includes
    every concrete mode.  ``canonical_modes=True`` predicts the exact tree that
    ``copy_skill_atomic`` will install from a source checkout.
    """

    if root.is_symlink() or not root.is_dir():
        raise DeliveryError(f"skill tree must be a real directory: {root}")
    digest = hashlib.sha256()
    root_mode = 0o755 if canonical_modes else stat.S_IMODE(root.stat().st_mode)
    digest.update(b"R\0" + oct(root_mode).encode() + b"\n")
    seen = False
    for item in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        relative = item.relative_to(root).as_posix().encode()
        if item.is_symlink():
            raise DeliveryError(f"skill tree contains a symlink: {item}")
        mode = canonical_tree_mode(item) if canonical_modes else stat.S_IMODE(item.stat().st_mode)
        if item.is_dir():
            digest.update(b"D\0" + relative + b"\0" + oct(mode).encode() + b"\n")
        elif item.is_file():
            data = item.read_bytes()
            digest.update(
                b"F\0"
                + relative
                + b"\0"
                + oct(mode).encode()
                + b"\0"
                + str(len(data)).encode()
                + b"\0"
                + data
                + b"\n"
            )
            seen = True
        else:
            raise DeliveryError(f"skill tree contains an unsupported filesystem entry: {item}")
    if not seen:
        raise DeliveryError(f"skill tree contains no files: {root}")
    return "sha256:" + digest.hexdigest()


def tree_modes_are_canonical(root: Path) -> bool:
    """Check the concrete modes written by the selective installer."""

    if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o755:
        return False
    for item in root.rglob("*"):
        if item.is_symlink() or not (item.is_dir() or item.is_file()):
            return False
        if stat.S_IMODE(item.stat().st_mode) != canonical_tree_mode(item):
            return False
    return True


def extract_managed_block(text: str, *, required: bool) -> str | None:
    begins = text.count(MANAGED_BEGIN)
    ends = text.count(MANAGED_END)
    if begins != ends or begins > 1:
        raise DeliveryError("house-style file has unpaired or duplicate managed markers")
    if begins == 0:
        if required:
            raise DeliveryError("house-style source is missing its managed markers")
        return None
    start = text.index(MANAGED_BEGIN)
    end_start = text.index(MANAGED_END)
    if end_start < start:
        raise DeliveryError("house-style managed markers are inverted")
    end = end_start + len(MANAGED_END)
    return text[start:end]


def desired_instruction_block(config: Configuration, target_name: str) -> str:
    """Render the same target-specific block as the broader reconcile flow."""
    block = extract_managed_block(config.house_source.read_text(), required=True)
    assert block is not None
    overlay = config.instruction_overlays.get(target_name)
    if overlay is None:
        return block
    end = block.index(MANAGED_END)
    body = block[:end].rstrip()
    return body + "\n\n" + overlay.read_text().strip() + "\n" + MANAGED_END


def replace_managed_block(text: str, desired: str | None) -> str:
    current = extract_managed_block(text, required=False)
    if current is None:
        if desired is None:
            return text
        separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        return text + separator + desired + "\n"
    start = text.index(MANAGED_BEGIN)
    end = text.index(MANAGED_END, start) + len(MANAGED_END)
    if desired is None:
        before = text[:start].rstrip("\n")
        after = text[end:].lstrip("\n")
        if before and after:
            return before + "\n\n" + after
        if before:
            return before + "\n"
        return after
    return text[:start] + desired + text[end:]


def plan(config: Configuration) -> tuple[str, list[Entry]]:
    entries: list[Entry] = []
    for skill in config.skills:
        source = config.sources[skill]
        desired_hash = tree_hash(source)
        for target_name, root in config.targets.items():
            assert_no_symlink_path(root, config.home)
            target = root / skill
            assert_no_symlink_path(target, config.home)
            current_hash: str | None = None
            current_modes_match = False
            if target.exists():
                current_hash = tree_hash(target)
                current_modes_match = tree_modes_are_canonical(target)
            action = (
                "MATCH"
                if current_hash == desired_hash and current_modes_match
                else ("ADD" if current_hash is None else "UPDATE")
            )
            entries.append(
                Entry(
                    key=f"skill:{skill}:{target_name}",
                    kind="skill",
                    skill=skill,
                    target_name=target_name,
                    source=source,
                    target=target,
                    desired_hash=desired_hash,
                    current_hash=current_hash,
                    action=action,
                )
            )

    for target_name, target in config.instruction_targets.items():
        desired_block = desired_instruction_block(config, target_name)
        desired_hash = sha256_bytes(desired_block.encode())
        assert_no_symlink_path(target, config.home)
        current_block: str | None = None
        if target.exists():
            if not target.is_file():
                raise DeliveryError(f"instruction target is not a regular file: {target}")
            _, current_text = read_utf8_bytes(target)
            current_block = extract_managed_block(current_text, required=False)
        current_hash = sha256_bytes(current_block.encode()) if current_block is not None else None
        entries.append(
            Entry(
                key=f"instructions:{target_name}",
                kind="instructions",
                source=config.house_source,
                target=target,
                desired_hash=desired_hash,
                current_hash=current_hash,
                action="MATCH" if current_hash == desired_hash else ("ADD" if current_hash is None else "UPDATE"),
                target_name=target_name,
            )
        )
    records = [entry.plan_record() for entry in entries]
    plan_id = sha256_bytes(canonical_json({"schema_version": SCHEMA_VERSION, "entries": records}))
    return plan_id, entries


def print_plan(plan_id: str, entries: list[Entry], *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "plan_id": plan_id,
                    "entries": [entry.plan_record() for entry in entries],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    for entry in entries:
        print(f"{entry.action:<6} {entry.key:<42} {entry.target}")
        print(f"       desired {entry.desired_hash} current {entry.current_hash or '-'}")
    print(f"plan {plan_id}")


def ensure_private_directory(path: Path, config: Configuration) -> None:
    assert_no_symlink_path(path, config.home)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


@contextlib.contextmanager
def operation_lock(config: Configuration):
    """Serialize receipt and managed-path changes with Agentic Setup."""

    ensure_private_directory(config.state_dir, config)
    path = config.state_dir / OPERATION_LOCK_NAME
    assert_no_symlink_path(path, config.home)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise DeliveryError(f"operation lock is not an owner-controlled regular file: {path}")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def ensure_target_directory(path: Path, config: Configuration) -> None:
    """Create a managed target parent without changing any existing parent mode."""
    assert_no_symlink_path(path, config.home)
    path.mkdir(parents=True, exist_ok=True)


def write_private_json(path: Path, value: Any, config: Configuration) -> None:
    ensure_private_directory(path.parent, config)
    assert_no_symlink_path(path, config.home)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(canonical_json(value))
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def next_backup_id(state_dir: Path, plan_id: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-{plan_id.removeprefix('sha256:')[:12]}"
    candidate = base
    number = 1
    while (state_dir / "backups" / candidate).exists():
        candidate = f"{base}-{number}"
        number += 1
    return candidate


def copy_skill_tree_atomic(
    source: Path,
    target: Path,
    config: Configuration,
    *,
    canonicalize_modes: bool,
) -> None:
    ensure_target_directory(target.parent, config)
    temporary = target.parent / f".{target.name}.khenrix-new-{os.getpid()}"
    displaced = target.parent / f".{target.name}.khenrix-old-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink() or displaced.exists() or displaced.is_symlink():
        raise DeliveryError(f"stale temporary delivery path exists beside {target}")
    try:
        shutil.copytree(source, temporary, copy_function=shutil.copy2)
        if canonicalize_modes:
            os.chmod(temporary, 0o755)
        for item in sorted(temporary.rglob("*"), key=lambda value: value.as_posix()):
            if item.is_symlink() or not (item.is_dir() or item.is_file()):
                raise DeliveryError(f"skill tree contains an unsupported entry: {item}")
            if canonicalize_modes:
                os.chmod(item, canonical_tree_mode(item))
        expected = observed_tree_hash(source, canonical_modes=canonicalize_modes)
        if observed_tree_hash(temporary) != expected:
            raise DeliveryError(f"copied skill tree does not preserve expected bytes and modes: {source}")
        if target.exists():
            target.rename(displaced)
        temporary.rename(target)
        if displaced.exists():
            shutil.rmtree(displaced)
    except Exception:
        if not target.exists() and displaced.exists():
            displaced.rename(target)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def copy_skill_atomic(source: Path, target: Path, config: Configuration) -> None:
    """Install a public skill with portable canonical modes."""

    copy_skill_tree_atomic(source, target, config, canonicalize_modes=True)


def restore_skill_atomic(source: Path, target: Path, config: Configuration) -> None:
    """Restore the exact bytes and modes captured in a private backup."""

    copy_skill_tree_atomic(source, target, config, canonicalize_modes=False)


def write_instructions_atomic(target: Path, desired_block: str, config: Configuration) -> None:
    ensure_target_directory(target.parent, config)
    _, current_text = read_utf8_bytes(target) if target.exists() else (b"", "")
    updated = replace_managed_block(current_text, desired_block)
    temporary = target.parent / f".{target.name}.khenrix-new-{os.getpid()}"
    try:
        temporary.write_bytes(updated.encode("utf-8"))
        os.chmod(temporary, stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def create_backup(config: Configuration, plan_id: str, changed: list[Entry]) -> tuple[str, Path, dict[str, Any]]:
    backup_id = next_backup_id(config.state_dir, plan_id)
    backup_root = config.state_dir / "backups" / backup_id
    ensure_private_directory(backup_root, config)
    records: list[dict[str, Any]] = []
    for index, entry in enumerate(changed):
        record: dict[str, Any] = {
            "key": entry.key,
            "kind": entry.kind,
            "target": str(entry.target),
            "existed": entry.target.exists(),
            "pre_apply_hash": entry.current_hash,
            "post_apply_hash": entry.desired_hash,
            "target_name": entry.target_name,
        }
        if entry.kind == "skill":
            record["pre_apply_observed_hash"] = (
                observed_tree_hash(entry.target) if entry.target.exists() else None
            )
            record["post_apply_observed_hash"] = observed_tree_hash(
                entry.source, canonical_modes=True
            )
        if entry.kind == "skill" and entry.target.exists():
            relative = Path("entries") / f"{index:02d}-{entry.skill}"
            destination = backup_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(entry.target, destination, copy_function=shutil.copy2)
            if observed_tree_hash(destination) != record["pre_apply_observed_hash"]:
                raise DeliveryError(
                    f"skill backup does not preserve pre-apply bytes and modes: {entry.target}"
                )
            record["backup"] = relative.as_posix()
        elif entry.kind == "instructions":
            original, text = read_utf8_bytes(entry.target) if entry.target.exists() else (b"", "")
            record["previous_managed_block"] = extract_managed_block(text, required=False)
            record["pre_apply_full_hash"] = sha256_bytes(original) if entry.target.exists() else None
            record["pre_apply_mode"] = (
                stat.S_IMODE(entry.target.stat().st_mode) if entry.target.exists() else None
            )
            assert entry.target_name is not None
            post_text = replace_managed_block(
                text, desired_instruction_block(config, entry.target_name)
            )
            record["post_apply_full_hash"] = sha256_bytes(post_text.encode())
            if entry.target.exists():
                relative = Path("entries") / f"{index:02d}-{entry.target_name}-instructions.txt"
                (backup_root / relative).parent.mkdir(parents=True, exist_ok=True)
                (backup_root / relative).write_bytes(original)
                record["backup"] = relative.as_posix()
        records.append(record)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "backup_id": backup_id,
        "plan_id": plan_id,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "entries": records,
    }
    write_private_json(backup_root / "manifest.json", manifest, config)
    return backup_id, backup_root, manifest


def build_receipt(config: Configuration, plan_id: str, backup_id: str | None, entries: list[Entry]) -> dict[str, Any]:
    skills: dict[str, Any] = {}
    for skill in config.skills:
        skill_entries = [entry for entry in entries if entry.kind == "skill" and entry.skill == skill]
        skills[skill] = {
            "source_hash": skill_entries[0].desired_hash,
            "targets": {
                entry.target_name: {
                    "path": str(entry.target),
                    "hash": entry.desired_hash,
                }
                for entry in skill_entries
            },
        }
    instruction_entries = [entry for entry in entries if entry.kind == "instructions"]
    instructions = {
        entry.target_name: {
            "path": str(entry.target),
            "managed_hash": entry.desired_hash,
        }
        for entry in instruction_entries
    }
    maka = instructions["maka"]
    return {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan_id,
        "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "backup_id": backup_id,
        "skills": skills,
        "instructions": instructions,
        # Stable compatibility field consumed by Agentic Setup's ownership audit.
        "maka_instructions": maka,
    }


def apply(config: Configuration, *, expect: str | None, as_json: bool) -> None:
    with operation_lock(config):
        _apply_locked(config, expect=expect, as_json=as_json)


def _apply_locked(config: Configuration, *, expect: str | None, as_json: bool) -> None:
    plan_id, entries = plan(config)
    if expect and expect != plan_id:
        raise DeliveryError(f"plan changed: expected {expect}, current plan is {plan_id}")
    changed = [entry for entry in entries if entry.action != "MATCH"]
    backup_id: str | None = None
    backup_root: Path | None = None
    manifest: dict[str, Any] | None = None
    if changed:
        ensure_private_directory(config.state_dir, config)
        backup_id, backup_root, manifest = create_backup(config, plan_id, changed)
    try:
        if changed:
            for entry in changed:
                assert_no_symlink_path(entry.target, config.home)
                if entry.kind == "skill":
                    copy_skill_atomic(entry.source, entry.target, config)
                else:
                    assert entry.target_name is not None
                    write_instructions_atomic(
                        entry.target,
                        desired_instruction_block(config, entry.target_name),
                        config,
                    )
        final_plan_id, final_entries = plan(config)
        if any(entry.action != "MATCH" for entry in final_entries):
            raise DeliveryError("post-apply verification found delivery drift")
        # The final plan differs because actions and current hashes changed; receipt retains
        # the reviewed pre-apply plan ID while the hashes prove the installed content.
        receipt = build_receipt(config, plan_id, backup_id, final_entries)
        write_private_json(config.state_dir / RECEIPT_NAME, receipt, config)
    except Exception:
        if backup_root is not None and manifest is not None:
            restore_manifest(config, backup_root, manifest, require_post_hash=False)
        raise
    result = {
        "plan_id": plan_id,
        "verified_plan_id": final_plan_id,
        "backup_id": backup_id,
        "changed": [entry.key for entry in changed],
        "receipt": str(config.state_dir / RECEIPT_NAME),
    }
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif changed:
        print(f"applied {len(changed)} change(s); backup {backup_id}")
        print(f"receipt {config.state_dir / RECEIPT_NAME}")
    else:
        print("already aligned; refreshed install receipt")


def hash_managed_target(kind: str, target: Path) -> str | None:
    if not target.exists():
        return None
    if kind == "skill":
        return observed_tree_hash(target)
    _, text = read_utf8_bytes(target)
    block = extract_managed_block(text, required=False)
    return sha256_bytes(block.encode()) if block is not None else None


def expected_restore_targets(config: Configuration) -> dict[Path, str]:
    expected = {
        root / skill: "skill"
        for root in config.targets.values()
        for skill in config.skills
    }
    expected.update({target: "instructions" for target in config.instruction_targets.values()})
    return expected


def validate_backup_entries(
    config: Configuration, backup_root: Path, entries: Any
) -> list[dict[str, Any]]:
    if not isinstance(entries, list) or not entries:
        raise DeliveryError(f"invalid backup manifest at {backup_root}: entries must be non-empty")
    expected = expected_restore_targets(config)
    seen: set[Path] = set()
    validated: list[dict[str, Any]] = []
    for record in entries:
        if not isinstance(record, dict):
            raise DeliveryError(f"invalid backup manifest at {backup_root}: entry is not an object")
        try:
            target = Path(record["target"])
            kind = record["kind"]
        except (KeyError, TypeError) as error:
            raise DeliveryError(f"invalid backup manifest at {backup_root}: missing target/kind") from error
        if target in seen:
            raise DeliveryError(f"invalid backup manifest has duplicate target: {target}")
        seen.add(target)
        if expected.get(target) != kind:
            raise DeliveryError(f"backup target is outside selective delivery ownership: {target}")
        if not isinstance(record.get("existed"), bool):
            raise DeliveryError(f"backup entry has invalid existed flag: {target}")
        if kind == "skill" and record["existed"]:
            raw_backup = record.get("backup")
            if not isinstance(raw_backup, str):
                raise DeliveryError(f"skill backup path is missing for {target}")
            relative = Path(raw_backup)
            if relative.is_absolute() or ".." in relative.parts:
                raise DeliveryError(f"skill backup path escapes its bundle: {raw_backup}")
            source = backup_root / relative
            if source.is_symlink() or not source.is_dir():
                raise DeliveryError(f"skill backup tree is missing or unsafe: {source}")
            observed = record.get("pre_apply_observed_hash")
            if not isinstance(observed, str) or not SHA256_ID.fullmatch(observed):
                raise DeliveryError(f"skill backup has no exact pre-apply hash: {source}")
            actual = observed_tree_hash(source)
            if actual != observed:
                raise DeliveryError(f"skill backup hash does not match its manifest: {source}")
        if kind == "skill":
            post_observed = record.get("post_apply_observed_hash")
            if not isinstance(post_observed, str) or not SHA256_ID.fullmatch(post_observed):
                raise DeliveryError(f"skill backup has no exact post-apply hash: {target}")
            if not record["existed"] and record.get("pre_apply_observed_hash") is not None:
                raise DeliveryError(f"absent skill backup claims a prior observed hash: {target}")
        if kind == "instructions":
            previous = record.get("previous_managed_block")
            if previous is not None:
                if not isinstance(previous, str) or extract_managed_block(previous, required=True) != previous:
                    raise DeliveryError(f"instruction backup block is invalid for {target}")
            post_full_hash = record.get("post_apply_full_hash")
            if not isinstance(post_full_hash, str) or not post_full_hash.startswith("sha256:"):
                raise DeliveryError(f"instruction backup has no post-apply full hash: {target}")
            if record["existed"]:
                raw_backup = record.get("backup")
                if not isinstance(raw_backup, str):
                    raise DeliveryError(f"instruction backup path is missing for {target}")
                relative = Path(raw_backup)
                if relative.is_absolute() or ".." in relative.parts:
                    raise DeliveryError(f"instruction backup path escapes its bundle: {raw_backup}")
                source = backup_root / relative
                if source.is_symlink() or not source.is_file():
                    raise DeliveryError(f"instruction backup file is missing or unsafe: {source}")
                if sha256_bytes(source.read_bytes()) != record.get("pre_apply_full_hash"):
                    raise DeliveryError(f"instruction backup hash does not match its manifest: {source}")
                mode = record.get("pre_apply_mode")
                if not isinstance(mode, int) or isinstance(mode, bool):
                    raise DeliveryError(f"instruction backup mode is invalid for {target}")
            elif record.get("pre_apply_full_hash") is not None or record.get("pre_apply_mode") is not None:
                raise DeliveryError(f"absent instruction backup claims prior file metadata: {target}")
        validated.append(record)
    return validated


def restore_manifest(
    config: Configuration,
    backup_root: Path,
    manifest: dict[str, Any],
    *,
    require_post_hash: bool,
) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DeliveryError(f"unsupported backup schema at {backup_root}")
    entries = validate_backup_entries(config, backup_root, manifest.get("entries"))
    if require_post_hash:
        for record in entries:
            target = Path(record["target"])
            assert_inside_home(target, config.home, "backup target")
            assert_no_symlink_path(target, config.home)
            current = hash_managed_target(record["kind"], target)
            expected = (
                record.get("post_apply_observed_hash")
                if record["kind"] == "skill"
                else record.get("post_apply_hash")
            )
            if current != expected:
                raise DeliveryError(
                    f"refusing restore because {target} changed after apply "
                    f"({current!r} != {expected!r})"
                )
    for record in reversed(entries):
        target = Path(record["target"])
        assert_inside_home(target, config.home, "backup target")
        assert_no_symlink_path(target, config.home)
        if record["kind"] == "skill":
            if target.exists():
                shutil.rmtree(target)
            if record.get("existed"):
                source = backup_root / record["backup"]
                restore_skill_atomic(source, target, config)
        else:
            current_bytes, current_text = read_utf8_bytes(target) if target.exists() else (b"", "")
            unchanged_since_apply = (
                target.exists()
                and sha256_bytes(current_bytes) == record.get("post_apply_full_hash")
            )
            if unchanged_since_apply and record["existed"]:
                data = (backup_root / record["backup"]).read_bytes()
                updated_exists = True
                mode = record["pre_apply_mode"]
            elif unchanged_since_apply:
                data = b""
                updated_exists = False
                mode = 0o600
            else:
                previous = record.get("previous_managed_block")
                updated = replace_managed_block(current_text, previous)
                data = updated.encode()
                updated_exists = bool(updated) or record["existed"]
                mode = (
                    stat.S_IMODE(target.stat().st_mode)
                    if target.exists()
                    else (record.get("pre_apply_mode") or 0o600)
                )
            if updated_exists:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.parent / f".{target.name}.khenrix-restore-{os.getpid()}"
                temporary.write_bytes(data)
                os.chmod(temporary, mode)
                os.replace(temporary, target)
            elif target.exists():
                target.unlink()


def select_backup(config: Configuration, requested: str) -> tuple[Path, dict[str, Any]]:
    root = config.state_dir / "backups"
    if requested == "latest":
        candidates = sorted(path for path in root.glob("*") if path.is_dir() and not path.is_symlink())
        if not candidates:
            raise DeliveryError("no skill delivery backups exist")
        backup = candidates[-1]
    else:
        if "/" in requested or requested in {".", ".."}:
            raise DeliveryError("backup ID must be a single directory name")
        backup = root / requested
    assert_no_symlink_path(backup, config.home, allow_missing=False)
    manifest_path = backup / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise DeliveryError(f"backup has no safe manifest: {backup}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError) as error:
        raise DeliveryError(f"cannot read backup manifest: {manifest_path}") from error
    return backup, manifest


def restore(config: Configuration, backup_id: str) -> None:
    with operation_lock(config):
        _restore_locked(config, backup_id)


def _restore_locked(config: Configuration, backup_id: str) -> None:
    backup_root, manifest = select_backup(config, backup_id)
    restore_manifest(config, backup_root, manifest, require_post_hash=True)
    restoration = {
        "schema_version": SCHEMA_VERSION,
        "restored_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "backup_id": manifest.get("backup_id", backup_root.name),
        "plan_id": manifest.get("plan_id"),
    }
    write_private_json(config.state_dir / "restore-receipt.json", restoration, config)
    receipt = config.state_dir / RECEIPT_NAME
    if receipt.exists():
        receipt.unlink()
    print(f"restored backup {backup_root.name}")


def doctor(config: Configuration, *, as_json: bool) -> None:
    problems: list[str] = []
    try:
        _, entries = plan(config)
        problems.extend(f"{entry.key}: {entry.action}" for entry in entries if entry.action != "MATCH")
    except DeliveryError as error:
        entries = []
        problems.append(str(error))
    receipt_path = config.state_dir / RECEIPT_NAME
    receipt: Any = None
    receipt_valid = False
    if not receipt_path.is_file() or receipt_path.is_symlink():
        problems.append(f"missing safe install receipt: {receipt_path}")
    else:
        try:
            receipt = json.loads(receipt_path.read_text())
        except json.JSONDecodeError as error:
            problems.append(f"invalid install receipt: {error}")
    if receipt is not None:
        try:
            validate_install_receipt_header(receipt)
        except DeliveryError as error:
            problems.append(str(error))
        else:
            receipt_valid = True
    if receipt_valid and entries:
        expected = build_receipt(config, receipt.get("plan_id", ""), receipt.get("backup_id"), entries)
        for skill in config.skills:
            actual_skill = receipt.get("skills", {}).get(skill, {})
            if actual_skill.get("source_hash") != expected["skills"][skill]["source_hash"]:
                problems.append(f"receipt source hash drifted for {skill}")
            if actual_skill.get("targets") != expected["skills"][skill]["targets"]:
                problems.append(f"receipt target records drifted for {skill}")
        for target_name, expected_record in expected["instructions"].items():
            if receipt.get("instructions", {}).get(target_name) != expected_record:
                problems.append(f"receipt instruction hash drifted for {target_name}")
        if receipt.get("maka_instructions") != expected["maka_instructions"]:
            problems.append("receipt Maka compatibility instruction hash drifted")
    result = {
        "ok": not problems,
        "receipt": str(receipt_path),
        "problems": problems,
        "managed_skills": list(config.skills),
    }
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif problems:
        for problem in problems:
            print(f"FAIL {problem}")
    else:
        print("OK selective skill delivery and all four house-style targets are aligned")
    if problems:
        raise DeliveryError(f"doctor found {len(problems)} problem(s)")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    result.add_argument("--home", type=Path, default=Path.home())
    result.add_argument("--state-dir", type=Path)
    subparsers = result.add_subparsers(dest="command", required=True)
    for name in ("plan", "status", "doctor"):
        command = subparsers.add_parser(name)
        command.add_argument("--json", action="store_true")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--expect", help="refuse unless the current content-addressed plan has this ID")
    apply_parser.add_argument("--json", action="store_true")
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--backup", default="latest", help="backup ID or latest")
    return result


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        # Keep HOME lexical so a symlink supplied through --home remains visible to
        # assert_no_symlink_path. Repository sources are outside the managed target
        # boundary and can still use resolve().
        config = load_configuration(
            options.repo_root.resolve(), options.home.absolute(), options.state_dir
        )
        if options.command in {"plan", "status"}:
            plan_id, entries = plan(config)
            print_plan(plan_id, entries, as_json=options.json)
            return 0 if options.command == "plan" or all(entry.action == "MATCH" for entry in entries) else 1
        if options.command == "apply":
            apply(config, expect=options.expect, as_json=options.json)
        elif options.command == "restore":
            restore(config, options.backup)
        elif options.command == "doctor":
            doctor(config, as_json=options.json)
        return 0
    except DeliveryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
