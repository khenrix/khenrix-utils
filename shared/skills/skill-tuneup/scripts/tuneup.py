#!/usr/bin/env python3
"""Deterministic substrate for the skill-tuneup skill (stdlib only).

Subcommands keep the judgment-free parts of a tune-up reproducible. Run `--help` for the
current list rather than trusting an enumeration here — this header listed four of nine
for three runs, because a comment cannot fail a test when it goes stale.

  tuneup.py --self-test                logic tests; run from checkout, temp fixtures, no network

Judgment-shaped work (research, audit, proportionality) lives in SKILL.md and
references/ — this script only reports facts. Run memory lives in
docs/tuneups/log/<target>.jsonl (committed; outside every eval-receipt closure).
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
import fcntl
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Generation-agnostic model-ID shapes (never encodes a "latest", so it can't rot).
MODEL_RX = re.compile(
    r"(claude-[a-z]+-[0-9][0-9a-z.-]*"      # claude-opus-4-8, claude-fable-5, claude-haiku-4-5-20251001
    r"|gpt-[0-9][0-9a-z.+-]*"               # gpt-5.5, gpt-4o
    r"|\bo[0-9]-[a-z][a-z0-9-]*"            # o4-mini, o3-pro
    r"|gemini-[0-9][0-9a-z.-]*)"            # gemini-3.5-flash, gemini-2.5-pro
)
# Commit subjects that do NOT count as a substantive baseline.
CHORE_RX = re.compile(r"^(chore|docs|style|typo)[:(\s]", re.IGNORECASE)
SKILL_NAME_RX = re.compile(r"^[a-z0-9-]{1,64}$")
SCAN_SUFFIXES = (".md", ".py", ".toml", ".json", ".sh", ".tmpl", ".txt")
# Generated / fixture / workspace paths never count as staleness evidence,
# and this script's own self-test fixtures would flag themselves.
EXCLUDE_RX = re.compile(r"(^|/)(marketplaces/|__pycache__/|workspace/|evals/_fixtures/)"
                        r"|skills/skill-tuneup/scripts/tuneup\.py/?$")
FINAL_PANEL = ("codex", "agy")


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number {value!r} is not allowed")
    return parsed


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON object key {key!r}")
        out[key] = value
    return out


def _validate_finite_json(value: object, *, path: str = "$") -> None:
    """Reject values Python's permissive JSON encoder would silently emit."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path}: non-finite JSON number is not allowed")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path}: JSON object keys must be strings, got {key!r}")
            _validate_finite_json(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_finite_json(child, path=f"{path}[{index}]")


def _strict_json_loads(raw: str | bytes, *, context: str = "JSON"):
    """Decode standards-compliant JSON with duplicate-key and finite-number checks."""
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
        _validate_finite_json(value)
        return value
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context}: {exc}") from exc


def _strict_json_dumps(value: object, **kwargs) -> str:
    _validate_finite_json(value)
    try:
        return json.dumps(value, allow_nan=False, **kwargs)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cannot serialize strict JSON: {exc}") from exc


def _atomic_jsonl_append(path: Path, serialized_line: str) -> None:
    """Append one pre-serialized line without exposing a torn tracked ledger."""
    payload = serialized_line.encode("utf-8")
    lock_root = Path.home() / ".cache" / "khenrix-utils" / "skill-tuneup-log-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest() + ".lock"
    lock_fd = os.open(lock_root / lock_name, os.O_CREAT | os.O_RDWR, 0o600)
    temp_path = None
    try:
        with os.fdopen(lock_fd, "rb", closefd=True) as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            existing = path.read_bytes() if path.exists() else b""
            if existing and not existing.endswith(b"\n"):
                raise ValueError(f"{path}: existing JSONL does not end with a newline")
            # Revalidate after taking the stable external lock. A concurrent or manual
            # edit between the caller's read and publication must fail closed.
            for line_number, line in enumerate(existing.decode("utf-8").splitlines(), 1):
                if line.strip():
                    _strict_json_loads(line, context=f"{path} line {line_number}")
            mode = path.stat().st_mode & 0o7777 if path.exists() else 0o644
            with tempfile.NamedTemporaryFile(
                    mode="wb", dir=path.parent, prefix=f".{path.name}.",
                    suffix=".tmp", delete=False) as handle:
                temp_path = Path(handle.name)
                handle.write(existing)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.chmod(mode)
            os.replace(temp_path, path)
            temp_path = None
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


_GIT_AUTHORITY = None


def _git_authority():
    """Load the Git authority owned by this source checkout or rendered plugin."""
    global _GIT_AUTHORITY
    if _GIT_AUTHORITY is not None:
        return _GIT_AUTHORITY
    import importlib.util
    here = Path(__file__).resolve()
    candidates = [
        here.parents[4] / "scripts" / "lib" / "git_authority.py",
        here.parents[3] / "lib" / "git_authority.py",
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location("_khenrix_git_authority", candidate)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GIT_AUTHORITY = module
        return module
    raise RuntimeError(
        f"Git authority not found; looked in {[str(path) for path in candidates]}")


def _git(repo: Path, *args: str) -> str:
    return _git_authority().run(
        args, repo=repo, capture_output=True, text=True, check=True).stdout


def _require_exact_git_toplevel(repo: Path) -> None:
    """Require the CLI's --repo to name the checkout root, never a nested directory."""
    try:
        result = _git_authority().run(
            ["rev-parse", "--show-toplevel"], repo=repo,
            capture_output=True, text=True, check=False)
    except OSError as e:
        raise ValueError(
            f"--repo must be the exact git toplevel; cannot inspect {repo}: {e}") from e
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(
            f"--repo must be the exact git toplevel; {repo} is not a git working tree")
    top = Path(result.stdout.strip()).resolve()
    if top != repo:
        raise ValueError(
            f"--repo must be the exact git toplevel {top}; got nested path {repo}")


# Where a skill's source can live. khenrix-utils layouts first (a khenrix skill must never
# be matched by a generic layout), then the conventions foreign repos use. This is the ONLY
# place layout is encoded — baseline/stale-models resolve through it, so teaching it a new
# layout teaches the whole engine.
KHENRIX_LAYOUTS = ("shared/skills/{s}", "shared/skill-templates/{s}")
FOREIGN_LAYOUTS = (".agents/skills/{s}", ".claude/skills/{s}", "skills/{s}")


def _source_checkout_root() -> Path | None:
    """The exact khenrix-utils checkout owning this source engine, if this is that copy."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        source = candidate / "shared" / "skills" / "skill-tuneup" / "scripts" / "tuneup.py"
        try:
            if source.samefile(here):
                return candidate.resolve()
        except OSError:
            continue
    return None


_ENGINE_KHENRIX_ROOT = _source_checkout_root()
# Production identity is immutable and path-based. The bare-script self-test uses explicit
# temporary checkout fixtures; it activates this set only for the duration of that process.
_SELF_TEST_KHENRIX_ROOTS: set[Path] | None = None
_SELF_TEST_REGISTRY_ROOT: Path | None = None


def _skill_layouts(repo: Path) -> tuple[str, ...]:
    return KHENRIX_LAYOUTS if is_khenrix_repo(repo) else FOREIGN_LAYOUTS


def _validate_skill_name(skill: str) -> None:
    if not SKILL_NAME_RX.fullmatch(skill):
        raise ValueError(
            f"invalid skill name {skill!r}; expected lowercase letters, digits, and hyphens "
            "only (1-64 characters)")


def _symlink_component(repo: Path, path: Path) -> Path | None:
    """First symlink from REPO to PATH; lexical ownership must match commit ownership."""
    current = repo
    for part in path.relative_to(repo).parts:
        current /= part
        if current.is_symlink():
            return current
    return None


def _skill_tree_symlink(repo: Path, path: Path, manifest_path: Path) -> Path | None:
    """First linked component anywhere in a skill tree, without following directory links."""
    link = _symlink_component(repo, manifest_path)
    if link is not None or not path.is_dir():
        return link
    # Path.rglob yields a directory symlink but does not recurse through it. Refusing every
    # linked descendant keeps scans, edits, review material, history, and the eventual
    # commit inside the repository that target-info reports.
    return next((child for child in path.rglob("*") if child.is_symlink()), None)


def _skill_tree_git_boundary(repo: Path, path: Path) -> tuple[Path, str] | None:
    """First nested repository boundary, including an ancestor or staged gitlink."""
    try:
        rel = path.relative_to(repo)
    except ValueError:
        return None
    # A pathspec BELOW a gitlink does not match the gitlink itself. Query from the layout
    # anchor and retain only entries on or below this target, or between the repo and it.
    # This also catches a deinitialized gitlink whose worktree no longer contains `.git`.
    result = _git_authority().run(
        ["ls-files", "--stage", "-z", "--", rel.parts[0]], repo=repo,
        capture_output=True, check=False)
    if result.returncode == 0:
        for record in result.stdout.split(b"\0"):
            if not record or b"\t" not in record:
                continue
            metadata, raw_name = record.split(b"\t", 1)
            indexed = Path(os.fsdecode(raw_name))
            if (metadata.split(b" ", 1)[0] == b"160000"
                    and (indexed == rel or indexed in rel.parents or rel in indexed.parents)):
                return repo / indexed, "gitlink"
    # Walking PATH cannot see a repository marker on one of its ancestors. Check every
    # lexical component below the outer repo before scanning descendants.
    current = repo
    for part in rel.parts:
        current /= part
        marker = current / ".git"
        if os.path.lexists(marker):
            return marker, ".git boundary"
    if not path.is_dir():
        return None
    # Do not follow directory symlinks. They are diagnosed separately, while a literal
    # `.git` entry is a repository boundary even when it is a file or a dangling link.
    for root, dirs, files in os.walk(path, followlinks=False):
        if ".git" in dirs or ".git" in files:
            return Path(root) / ".git", ".git boundary"
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
    return None


def _manifest_ownership_problem(repo: Path, manifest_path: Path) -> str | None:
    """Why the canonical manifest is not a regular blob in outer HEAD and index."""
    rel = manifest_path.relative_to(repo)
    git_rel = rel.as_posix()
    raw_rel = os.fsencode(git_rel)
    index = _git_authority().run(
        ["ls-files", "--stage", "-z", "--", git_rel], repo=repo,
        capture_output=True, check=False)
    index_owned = False
    if index.returncode == 0:
        for record in index.stdout.split(b"\0"):
            if not record or b"\t" not in record:
                continue
            metadata, raw_name = record.split(b"\t", 1)
            fields = metadata.split()
            if (raw_name == raw_rel and len(fields) == 3 and fields[2] == b"0"
                    and fields[0] in (b"100644", b"100755")):
                index_owned = True
                break
    head = _git_authority().run(
        ["ls-tree", "-z", "HEAD", "--", git_rel], repo=repo,
        capture_output=True, check=False)
    head_owned = False
    if head.returncode == 0:
        for record in head.stdout.split(b"\0"):
            if not record or b"\t" not in record:
                continue
            metadata, raw_name = record.split(b"\t", 1)
            fields = metadata.split()
            if (raw_name == raw_rel and len(fields) == 3 and fields[1] == b"blob"
                    and fields[0] in (b"100644", b"100755")):
                head_owned = True
                break
    if index_owned and head_owned:
        return None
    missing = " and ".join(name for name, owned in (
        ("outer HEAD", head_owned), ("outer index", index_owned)) if not owned)
    return (f"{rel} is not a regular tracked blob in {missing}; skill-tuneup only "
            "maintains an existing skill whose history and eventual commit own its manifest")


def _ignored_consumable_problem(repo: Path, path: Path) -> str | None:
    """First non-disposable ignored entry below target source ``path``.

    ``__pycache__`` and ``*.pyc`` are the intentionally narrow exception: Python creates
    them locally and neither can be a reviewed, committed source input. Every other ignored
    regular file or symlink is candidate content the run could read, review, or stage
    inconsistently, irrespective of its extension.
    """
    rel = path.relative_to(repo)
    result = _git_authority().run(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--",
         rel.as_posix()], repo=repo, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        return (f"cannot prove ignored-source ownership for {rel}: git ls-files failed"
                + (f" ({detail})" if detail else ""))
    for raw_name in filter(None, result.stdout.split(b"\0")):
        ignored = repo / os.fsdecode(raw_name)
        ignored_rel = str(ignored.relative_to(repo))
        # Do not infer consumption from a suffix: extensionless executables, YAML, and links
        # are all source/review inputs too. The two bytecode-cache forms are the only
        # disposable exception; nonignored new files remain valid candidate changes.
        disposable = "__pycache__" in ignored.relative_to(repo).parts or ignored.suffix == ".pyc"
        if not disposable:
            return (f"{rel} contains ignored source {ignored_rel}; the outer clean-tree "
                    "check, review, staging, and commit cannot see the same content")
    return None


def _skill_candidates(repo: Path, skill: str) -> list[dict]:
    """Classify every layout once, including dangling links that stat cannot resolve."""
    _validate_skill_name(skill)
    states = []
    for pat in _skill_layouts(repo):
        path = repo / pat.format(s=skill)
        manifest = "SKILL.md.tmpl" if pat.startswith("shared/skill-templates/") else "SKILL.md"
        manifest_path = path / manifest
        # Refuse a linked layout root before walking it. For a repository-owned directory,
        # find nested Git ownership boundaries before scanning descendants for links so a
        # nested repo's own metadata is reported as the stronger ownership failure.
        link = _symlink_component(repo, manifest_path)
        boundary = None if link is not None else _skill_tree_git_boundary(repo, path)
        if link is None and boundary is None:
            link = _skill_tree_symlink(repo, path, manifest_path)
        ownership = None
        if link is None and boundary is None and manifest_path.is_file():
            ownership = _manifest_ownership_problem(repo, manifest_path)
            if ownership is None:
                ownership = _ignored_consumable_problem(repo, path)
        usable = (path.is_dir() and manifest_path.is_file()
                  and link is None and boundary is None and ownership is None)
        if usable:
            kind = "usable"
        elif link is not None:
            kind = "linked"
        elif boundary is not None:
            kind = "git-boundary"
        elif ownership is not None:
            kind = "unowned"
        elif path.is_dir():
            kind = "missing-manifest"
        else:
            kind = "absent"
        states.append({"path": path, "manifest": manifest, "link": link,
                       "boundary": boundary, "ownership": ownership, "kind": kind})
    return states


def _candidate_diagnostic(repo: Path, state: dict) -> str:
    rel = state["path"].relative_to(repo)
    if state["kind"] == "linked":
        link = state["link"].relative_to(repo)
        return (f"{rel} is symlink-backed at {link}; replace the linked path with "
                "repository-owned content")
    if state["kind"] == "git-boundary":
        boundary, kind = state["boundary"]
        where = boundary.relative_to(repo)
        return (f"{rel} contains a nested Git {kind} at {where}; the outer repository "
                "commit does not own those contents")
    if state["kind"] == "unowned":
        return state["ownership"]
    return f"{rel} exists but has no {state['manifest']}"


def _capabilities_source_refusal(repo: Path) -> str | None:
    """Why capabilities.toml is not repository-owned, including a dangling link."""
    caps = repo / "capabilities.toml"
    if caps.is_symlink():
        return ("capabilities.toml is symlink-backed; replace the link with a regular "
                "tracked file so facts, history, review, and commit share one source")
    if not caps.is_file():
        return ("capabilities.toml is missing or not a regular file; restore the "
                "repository-owned registry before resolving a khenrix target")
    return None


def _require_owned_capabilities(repo: Path) -> None:
    problem = _capabilities_source_refusal(repo)
    if problem:
        raise ValueError(problem)


def skill_paths(repo: Path, skill: str) -> list[Path]:
    """Source-of-truth dirs for a skill.

    Layouts are selected by REPO KIND, not tried in order: a khenrix checkout uses only
    khenrix layouts, any other repo only the foreign ones rooted directly under the exact
    `repo` argument. Mixing them would let a stray generic copy inside khenrix-utils be
    resolved and edited alongside the real source. A skill matching multiple layouts in
    the same tier is ambiguous — return all so the caller can refuse rather than silently
    pick one. Bare directories are not skills: normal layouts require SKILL.md and the
    template layout requires SKILL.md.tmpl.
    """
    return [state["path"] for state in _skill_candidates(repo, skill)
            if state["kind"] == "usable"]


def is_khenrix_repo(repo: Path) -> bool:
    """Whether REPO is the exact checkout that owns this engine's khenrix gate.

    Files named capabilities.toml and shared/skills are not an identity proof: ordinary
    projects can have both. The engine checkout stays identifiable even while either source
    is missing or dangling, which lets target resolution refuse the defect instead of
    silently downgrading to foreign layouts.
    """
    resolved = repo.resolve()
    if _ENGINE_KHENRIX_ROOT is not None and resolved == _ENGINE_KHENRIX_ROOT:
        return True
    return (_SELF_TEST_KHENRIX_ROOTS is not None
            and resolved in _SELF_TEST_KHENRIX_ROOTS)


def target_info(repo: Path, skill: str) -> dict:
    """Resolve a target and say which gate tier applies — the two-tier contract in one place.

    The tier follows the LAYOUT, not a probe for gate files: outside khenrix-utils the
    khenrix receipt gate is inapplicable by construction, since a receipt is only meaningful
    against this repo's eval harness. That is a claim about the khenrix gate and NOT about
    the target repo, which may well have tests or a precommit hook of its own — run those,
    they simply cannot produce a receipt. Report the tier so the run states plainly that it
    shipped without one.
    """
    states = _skill_candidates(repo, skill)
    paths = [state["path"] for state in states if state["kind"] == "usable"]
    linked_misses = [_candidate_diagnostic(repo, state)
                     for state in states if state["kind"] == "linked"]
    boundary_misses = [_candidate_diagnostic(repo, state)
                       for state in states if state["kind"] == "git-boundary"]
    ownership_misses = [_candidate_diagnostic(repo, state)
                        for state in states if state["kind"] == "unowned"]
    missing_misses = [_candidate_diagnostic(repo, state)
                      for state in states if state["kind"] == "missing-manifest"]
    khenrix = is_khenrix_repo(repo)
    cap_refusal = _capabilities_source_refusal(repo) if khenrix else None
    source_refusals = [cap_refusal] if cap_refusal else []
    near_misses = (linked_misses + boundary_misses + ownership_misses
                   + missing_misses + source_refusals)
    full_gate = bool(khenrix and any(
        str(p.relative_to(repo)).startswith(("shared/skills", "shared/skill-templates"))
        for p in paths))
    # Multiple same-tier matches (e.g. .agents/skills/x AND skills/x) are ambiguous.
    # `_require_skill_paths` below is the single enforcement point for every command that
    # consumes a target; this descriptive result stays useful to target-info JSON callers.
    # A linked same-name candidate is not merely an ignorable near miss when another
    # layout is valid: silently dropping it turns an ambiguous target into a successful
    # resolution and can make the run edit/commit the wrong source of truth.
    ambiguous = len(paths) > 1 or bool(
        paths and (linked_misses or boundary_misses or ownership_misses or source_refusals))
    return {
        "ambiguous": ambiguous,
        "near_misses": near_misses,
        "linked_near_misses": linked_misses,
        "git_boundary_near_misses": boundary_misses,
        "ownership_near_misses": ownership_misses,
        "missing_manifest_near_misses": missing_misses,
        "source_refusals": source_refusals,
        "repo": str(repo),
        "repo_name": repo.name,
        "skill": skill,
        "paths": [str(p.relative_to(repo)) for p in paths],
        "found": bool(paths),
        "khenrix_repo": khenrix,
        "tier": "full-gate" if full_gate else "council-only",
        "gate": ("evals + receipt + make precommit"
                 if full_gate else
                 "research + both council reviews + audit + convergence — NO khenrix "
                 "receipt (run any gate the target repo has of its own, and report it "
                 "separately); say so plainly in the run's output"),
        "log_target": log_target_key(repo, skill),
    }


def _target_resolution_problem(repo: Path, skill: str, info: dict) -> str | None:
    """One refusal message shared by target-info, baseline, and stale-models."""
    if info["source_refusals"]:
        return (f"{skill!r} has an unsafe khenrix source in {repo}: "
                f"{'; '.join(info['source_refusals'])} — refusing")
    if info["git_boundary_near_misses"]:
        candidates = info["paths"] + info["git_boundary_near_misses"]
        if info["paths"]:
            return (f"{skill!r} matches Git-boundary and repository-owned candidates in "
                    f"{repo}: {', '.join(candidates)} — refusing; remove the nested "
                    "repository so there is one outer-commit-owned source of truth")
        return (f"{skill!r} has no outer-commit-owned source in {repo}: "
                f"{'; '.join(info['git_boundary_near_misses'])} — refusing")
    if info["linked_near_misses"]:
        candidates = info["paths"] + info["linked_near_misses"]
        if info["paths"]:
            return (f"{skill!r} matches linked and repository-owned candidates in {repo}: "
                    f"{', '.join(candidates)} — refusing; remove or replace the linked "
                    "duplicate so there is one source of truth")
        return (f"{skill!r} has no repository-owned source in {repo}: "
                f"{'; '.join(info['linked_near_misses'])} — refusing")
    if info["ownership_near_misses"]:
        candidates = info["paths"] + info["ownership_near_misses"]
        if info["paths"]:
            return (f"{skill!r} matches owned and unowned candidates in {repo}: "
                    f"{', '.join(candidates)} — refusing; keep one outer-commit-owned "
                    "source of truth")
        return (f"{skill!r} has no outer-commit-owned source in {repo}: "
                f"{'; '.join(info['ownership_near_misses'])} — refusing")
    if len(info["paths"]) > 1:
        return (f"{skill!r} matches MORE THAN ONE layout in {repo}: "
                f"{', '.join(info['paths'])} — refusing; remove or rename the duplicate "
                "so there is one source of truth")
    if not info["found"]:
        looked = ", ".join(p.format(s=skill) for p in _skill_layouts(repo))
        detail = (f" — {'; '.join(info['missing_manifest_near_misses'])}"
                  if info["missing_manifest_near_misses"] else "")
        return f"no skill {skill!r} in {repo}{detail} (looked in {looked})"
    return None


def _require_skill_paths(repo: Path, skill: str) -> tuple[dict, list[Path]]:
    """Resolve exactly one owned target or fail before any command reads a candidate."""
    info = target_info(repo, skill)
    problem = _target_resolution_problem(repo, skill, info)
    if problem:
        if (not info["found"] and not info["linked_near_misses"]
                and not info["git_boundary_near_misses"]
                and not info["ownership_near_misses"]):
            raise FileNotFoundError(problem)
        raise ValueError(problem)
    return info, [repo / rel for rel in info["paths"]]


def pick_baseline(commits: list[dict]) -> dict | None:
    """Newest commit whose subject isn't a chore/docs/style tweak (commits newest-first).
    Falls back to the newest commit at all if every subject looks like a chore."""
    for c in commits:
        if not CHORE_RX.match(c["subject"]):
            return c
    return commits[0] if commits else None


def _git_file_at(repo: Path, revision: str, relpath: str) -> str | None:
    result = _git_authority().run(
        ["show", f"{revision}:{relpath}"], repo=repo,
        capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def _facts_snapshot_at(repo: Path, revision: str, skill: str) -> str | None:
    """Canonical semantic facts for one skill at a revision; comments do not render."""
    import tomllib
    text = _git_file_at(repo, revision, "capabilities.toml")
    if text is None:
        return None
    try:
        facts = tomllib.loads(text).get("skill_facts", {}).get(skill)
    except tomllib.TOMLDecodeError:
        # Historical invalid TOML still counts as a change rather than disappearing from
        # the research baseline; current invalid TOML is rejected by the render gate.
        return f"invalid-toml:{hashlib.sha256(text.encode()).hexdigest()}"
    return (json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if facts is not None else None)


def _facts_change_log(repo: Path, skill: str, fmt: str) -> list[str]:
    """capabilities commits whose exact rendered facts changed, including value-only edits."""
    lines = _git(repo, "log", "--no-merges", fmt, "--", "capabilities.toml").splitlines()
    changed = []
    for line in lines:
        sha = line.split("\0", 1)[0]
        if _facts_snapshot_at(repo, sha, skill) != _facts_snapshot_at(repo, f"{sha}^", skill):
            changed.append(line)
    return changed


def baseline(repo: Path, skill: str) -> dict | None:
    _, paths = _require_skill_paths(repo, skill)
    fmt = "--format=%H%x00%aI%x00%s"
    lines = []
    lines += _git(repo, "log", "--no-merges", fmt, "--",
                  *[str(p.relative_to(repo)) for p in paths]).splitlines()
    if repo / "shared" / "skill-templates" / skill in paths:
        # Header pickaxe misses the normal change shape: values edited below an unchanged
        # [skill_facts.<s>.*] header. Compare the exact semantic slice at each revision.
        lines += _facts_change_log(repo, skill, fmt)
    commits, seen = [], set()
    for ln in lines:
        sha, date, subject = ln.split("\0", 2)
        if sha not in seen:
            seen.add(sha)
            commits.append({"sha": sha, "date": date, "subject": subject})
    commits.sort(key=lambda c: c["date"], reverse=True)
    picked = pick_baseline(commits)
    if picked:
        picked = {**picked, "skipped_as_chore": sum(1 for c in commits
                                                    if c["date"] > picked["date"])}
    return picked


def _slug(label: str) -> str:
    """Display label -> id shape: 'Gemini 3.5 Flash (High)' -> 'gemini-3.5-flash'."""
    return re.sub(r"\s+", "-", re.sub(r"\s*\(.*?\)", "", label).strip().lower())


def registry_repo(repo: Path) -> Path:
    """The khenrix-utils checkout: model registry + run-log home, whichever repo is TARGET.

    Derived from where THIS FILE lives rather than $HOME/git/khenrix-utils — the engine is
    always run out of the checkout, so this is correct on any machine and can't silently
    bind to a second, stale clone at the conventional path.

    Raises rather than falling back to the target repo: a foreign repo has no
    capabilities.toml, so `approved_models` would come back empty and `tag_model` would
    degrade every hit to "found" — silently disabling the staleness check in exactly the
    situation nobody is watching it.
    """
    if is_khenrix_repo(repo):
        return repo
    if _SELF_TEST_REGISTRY_ROOT is not None:
        # The hermetic self-test injects one dedicated registry fixture. Prefer it even
        # when the engine itself lives in a source checkout, or foreign-target log probes
        # would write into that real checkout.
        return _SELF_TEST_REGISTRY_ROOT
    if _ENGINE_KHENRIX_ROOT is not None:
        return _ENGINE_KHENRIX_ROOT
    here = Path(__file__).resolve()
    raise FileNotFoundError(
        "cannot locate the khenrix-utils checkout from "
        f"{here} — it holds the approved-model registry and the run log. "
        "Run tuneup.py from the checkout (not a copied file).")


def approved_models(repo: Path, extra_csv: str = "") -> set[str]:
    """Approved set = every string in capabilities.toml [models] lists + --approved extras.
    Entries are also slugged, since agy's entry is a display label, not an id."""
    import tomllib
    ids: set[str] = set()
    registry = registry_repo(repo)
    _require_owned_capabilities(registry)
    caps_path = registry / "capabilities.toml"
    if caps_path.is_file():
        with open(caps_path, "rb") as f:
            caps = tomllib.load(f)
        for v in caps.get("models", {}).values():
            if isinstance(v, list):
                for x in v:
                    ids.update((x.lower(), _slug(x)))
    ids.update(x.strip().lower() for x in extra_csv.split(",") if x.strip())
    return ids


def tag_model(mid: str, approved: set[str]) -> str:
    """current if the id equals an approved id or is a dated variant of one
    (claude-haiku-4-5-20251001 startswith claude-haiku-4-5 + '-')."""
    if not approved:
        return "found"
    low = mid.lower()
    if low in approved or any(low.startswith(a + "-") for a in approved):
        return "current"
    return "stale-candidate"


def _facts_lines(caps_text: str, skill: str) -> list[tuple[int, str]]:
    """(lineno, line) pairs inside [skill_facts.<skill>...] sections of capabilities.toml."""
    out, active = [], False
    for i, line in enumerate(caps_text.splitlines(), 1):
        m = re.match(r"\s*\[+([^\]]+)\]+", line)
        if m:
            root = f"skill_facts.{skill}"
            active = m.group(1) == root or m.group(1).startswith(root + ".")
        elif active:
            out.append((i, line))
    return out


def scan_stale_models(repo: Path, skill: str | None, approved: set[str]) -> list[dict]:
    hits = []
    if skill:
        _, roots = _require_skill_paths(repo, skill)
    else:
        if is_khenrix_repo(repo):
            _require_owned_capabilities(repo)
        roots = [repo / "shared", repo / "capabilities.toml", repo / "docs"]
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for p in files:
            rel = str(p.relative_to(repo))
            if not p.is_file() or p.suffix not in SCAN_SUFFIXES or EXCLUDE_RX.search(rel + "/"):
                continue
            for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                for m in MODEL_RX.finditer(line):
                    hits.append({"file": rel, "line": i, "id": m.group(0),
                                 "status": tag_model(m.group(0), approved)})
    if skill and repo / "shared" / "skill-templates" / skill in roots:
        caps = repo / "capabilities.toml"
        if caps.is_file():
            for i, line in _facts_lines(caps.read_text(errors="ignore"), skill):
                for m in MODEL_RX.finditer(line):
                    hits.append({"file": "capabilities.toml", "line": i, "id": m.group(0),
                                 "status": tag_model(m.group(0), approved)})
    return hits


# --------------------------------------------------------------------------- #
# Triage — rank all skills by staleness. Read-only by construction.
# --------------------------------------------------------------------------- #
def receipt_state(repo: Path, skill: str) -> str:
    """fresh | stale-source | stale-evalset | missing | no-evals | unknown."""
    if not (repo / "evals" / skill / "evals.json").exists():
        return "no-evals"
    rp = repo / "evals" / skill / "receipt.json"
    if not rp.exists():
        return "missing"
    try:
        sys.path.insert(0, str(repo / "scripts" / "lib"))
        import checks
        rec = json.loads(rp.read_text())
        if rec.get("source_hash") != checks.source_hash(repo, skill):
            return "stale-source"
        if rec.get("eval_set_hash") != checks.eval_set_hash(repo, skill):
            return "stale-evalset"
        return "fresh"
    except Exception:  # noqa: BLE001 — plugin copy has no scripts/lib; degrade
        return "unknown"


RECEIPT_SCORE = {"no-evals": 40, "missing": 30, "stale-source": 20,
                 "stale-evalset": 20, "unknown": 5, "fresh": 0}


def triage_score(receipt: str, age_days: float | None, stale_hits: int, md_lines: int) -> int:
    score = RECEIPT_SCORE.get(receipt, 5)
    score += min(stale_hits * 10, 30)
    if age_days is not None:
        score += min(int(age_days / 30) * 2, 24)   # ~2 pts per month unmaintained, cap 24
    # An UNKNOWN age deliberately scores NOTHING. It is missing evidence, not staleness:
    # awarding points made a git failure outrank 70 days of real neglect and, because every
    # row got the same bonus, turned the board into an alphabetical tiebreak that
    # triage_recommendation then reported as a winner. The all-unknown case is a DIAGNOSIS,
    # not a ranking — triage_recommendation says so instead.
    if md_lines > 450:
        score += 10                                # near the 500-line hard cap
    return score


def triage_recommendation(rows: list[dict]) -> str:
    """The one-line verdict under the triage table.

    A recommendation needs a SIGNAL, not just a first row. Rows sort by (-score, skill),
    so once every score is 0 the top is whichever skill sorts first ALPHABETICALLY — and a
    tool whose entire product is "which skill needs work" would recommend a multi-hour run
    on the exact evidence that nothing needs work. Reachable as soon as the last scoring
    skill drops off the board.
    """
    if not rows:
        return "no skills found."
    # Missing AGE is not missing EVIDENCE. Most of the score (receipt state, stale model
    # ids, the line budget) never touches git, so a board whose ages all failed to resolve
    # can still hold a decisive signal — suppressing the recommendation there withheld an
    # answer the tool had good grounds for. The age gap becomes a NOTE on the answer, and
    # only an all-unknown board with nothing else to say degrades to the bare diagnosis.
    # `"age_days" in r`, not `.get(...) is None`: an ABSENT key is a caller that did not
    # report an age, not a checkout whose age is unknown. triage() always sets the key.
    unknown = [r for r in rows if "age_days" in r and r["age_days"] is None]
    if rows[0]["score"] > 0:
        note = ""
        if len(unknown) == len(rows):
            note = ("  (note: baseline age is UNKNOWN for every skill — git failed, so the "
                    "age component of the ranking is missing; the rest of the score stands)")
        elif unknown:
            note = f"  (note: baseline age is unknown for {len(unknown)} of {len(rows)} skills)"
        return f"recommend: deep tune-up of '{rows[0]['skill']}' first{note}"
    if len(unknown) == len(rows):
        return ("baseline age is UNKNOWN for every skill — git failed or this is not a "
                "checkout with history. No other signal fired either, but the ranking is "
                "incomplete: fix that before concluding there is nothing to do.")
    if unknown:
        # No signal fired, but some evidence never arrived — an all-clear would overclaim.
        return (f"no staleness signal fired, but baseline age is unknown for "
                f"{len(unknown)} of {len(rows)} skills — the all-clear is INCOMPLETE.")
    return "no skill shows a staleness signal — nothing to tune up."


def triage(repo: Path) -> list[dict]:
    # Step 3 documents triage for "a target repo", but the ranking only knows khenrix
    # layouts — on a foreign repo it produced an EMPTY table, which reads as "nothing to
    # tune" rather than "I cannot rank this". Refuse instead of answering wrongly.
    if not is_khenrix_repo(repo):
        raise ValueError(
            f"triage ranks khenrix-utils skills only; {repo} is not that checkout. "
            "For a skill in another repo use `target-info --skill <name>` to resolve its "
            "tier, then run the deep pass directly.")
    # A set, not concatenation: a name present in BOTH source dirs is one skill with two
    # layouts (skill_paths already treats that as ambiguous), not two rows on the board.
    names = {p.name for root in (repo / "shared" / "skills",
                                 repo / "shared" / "skill-templates")
             if root.is_dir()
             for p in root.iterdir()
             if (p.is_dir() or p.is_symlink()) and SKILL_NAME_RX.fullmatch(p.name)}
    infos = {name: target_info(repo, name) for name in sorted(names)}
    refused = {
        name: info for name, info in infos.items()
        if (info["ambiguous"] or info["linked_near_misses"]
            or info["git_boundary_near_misses"] or info["ownership_near_misses"]
            or info["source_refusals"])
    }
    if refused:
        details = []
        for name, info in refused.items():
            candidates = (info["paths"] + info["linked_near_misses"]
                          + info["git_boundary_near_misses"]
                          + info["ownership_near_misses"] + info["source_refusals"])
            details.append(f"{name}: {', '.join(candidates)}")
        raise ValueError(
            "triage cannot rank while target-info refuses these target(s): "
            + "; ".join(details)
            + ". Resolve each source-of-truth conflict, then rerun triage.")
    skills = sorted(name for name, info in infos.items() if info["found"])
    approved = approved_models(repo)
    now = datetime.now(timezone.utc)
    rows = []
    for s in skills:
        try:
            b = baseline(repo, s)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            b = None
        age = (now - datetime.fromisoformat(b["date"])).days if b else None
        stale = sum(1 for h in scan_stale_models(repo, s, approved)
                    if h["status"] == "stale-candidate")
        md = next((p / f for p in skill_paths(repo, s)
                   for f in ("SKILL.md", "SKILL.md.tmpl") if (p / f).is_file()), None)
        lines = len(md.read_text(errors="ignore").splitlines()) if md else 0
        receipt = receipt_state(repo, s)
        rows.append({"skill": s, "score": triage_score(receipt, age, stale, lines),
                     "receipt": receipt, "age_days": age, "stale_model_hits": stale,
                     "skill_md_lines": lines,
                     "baseline": (b or {}).get("sha", "")[:9] or None})
    rows.sort(key=lambda r: (-r["score"], r["skill"]))
    return rows


# --------------------------------------------------------------------------- #
# Run memory — docs/tuneups/log/<target>.jsonl (committed, append-only).
# --------------------------------------------------------------------------- #
REQUIRED_LOG_KEYS = {"target", "finding_id", "decision"}
DECISIONS = {"applied", "rejected", "deferred"}


# Machine-global and OUT OF TREE. Not TMPDIR (per-process on many setups, so two runs would
# each make their own "mutex" and never see each other), and not beside the engine either:
# `_skill_source_files` rglobs the skill dir and pathlib's rglob matches dotfiles, so an
# in-tree lock puts a random per-run token into skill-tuneup's own source_hash — `make
# precommit` would then fail the receipt check on every run, at the ship step, while the
# lock is held. It is also untracked, so `git add -A` would commit it and render.py would
# copy it into all three marketplaces.
LOCK_DIR = Path.home() / ".cache" / "khenrix-utils" / "skill-tuneup.lock.d"
# Must strictly exceed the longest step a run can take BETWEEN REFRESHES, or a LIVE run's
# lock becomes stealable. It does NOT cover Step 7's CHECKPOINT: that is a human wait, so
# it is unbounded and no window can. Step 7 therefore refreshes immediately before
# presenting the checkpoint AND immediately on resume, which is what turns an unbounded
# wait back into a bounded gap. `lock status` samples the age without acquiring.
# RAISED 90 -> 135 ON 2026-08-15, because the window had silently stopped satisfying the
# invariant in the first line of this comment. MODE_TIMEOUT["deep"] is 1800s (it was 1200
# when 90 was chosen; the council engine recalibrated it 2026-08-13 after six real deep
# councils measured 753-1238s), and --retries defaults to 2, so ONE fan-out is now
# 3 x 1800 = 90 min plus backoff — i.e. exactly the old window, with no margin at all. A
# legitimate deep run could therefore have its own lock stolen mid-fan-out. 135 restores
# the original ~1.5x margin (90 was 1.5x the then-longest 60 min step), derived rather
# than rounded so the next MODE_TIMEOUT change can redo the arithmetic. Step 6's own
# "deep + retries 1" guidance for tuning this machinery is 60 min.
# The eval run is LONGER than this window — eval_harness iterates cases
# serially across providers and both conditions — and is deliberately not covered by it:
# Step 6 mandates backgrounding the run and Step 1 mandates a `lock refresh` between polls,
# so an eval is an attended step whose lock never goes 135 min untouched. Kept a constant rather than derived: lock_acquire runs in a DIFFERENT
# process with no knowledge of the holder's --timeout/--retries, so deriving it would mean
# writing a deadline into the lock — a new refresh contract, and a crashed deep run
# blocking the next for an hour. The two stay linked by reading this comment.
LOCK_STALE_MIN = 135


def lock_acquire(stale_min: int = LOCK_STALE_MIN) -> tuple[bool, str]:
    """mkdir-based mutex carrying an ownership token.

    The token is what makes a steal *detectable*: `touch -c` on a lock another run already
    removed silently succeeds, so the previous heartbeat could not distinguish "still mine"
    from "gone and re-taken". refresh() compares the token before bumping the mtime.
    """
    tok = LOCK_DIR / "owner"
    if LOCK_DIR.is_dir():
        age_min = (time.time() - LOCK_DIR.stat().st_mtime) / 60
        if age_min <= stale_min:
            held = tok.read_text().strip() if tok.is_file() else "unknown"
            return False, f"held by {held} ({age_min:.0f} min old)"
        shutil.rmtree(LOCK_DIR, ignore_errors=True)  # stale: a crashed run
    try:
        LOCK_DIR.parent.mkdir(parents=True, exist_ok=True)
        LOCK_DIR.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        return False, "raced with another run"
    owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    tok.write_text(owner + "\n")
    return True, owner


def _norm_owner(owner: str) -> str:
    """Accept the token in the shape `lock acquire` PRINTS it, not just the internal one.

    acquire emits `OWNER=<token>` — a KEY=VALUE line an operator naturally copies whole,
    especially since shell state does not survive between the orchestrator's Bash calls.
    Comparing that literal against the stored bare token made refresh report
    "lock was STOLEN — STOP" to a run that still held its own lock, and made release
    refuse, leaking the lock until it aged out. A false steal alarm is worse than a
    missed one: it aborts correct work.
    """
    return owner.strip().removeprefix("OWNER=").strip()


def lock_refresh(owner: str) -> tuple[bool, str]:
    owner = _norm_owner(owner)
    tok = LOCK_DIR / "owner"
    if not tok.is_file():
        return False, "lock is GONE — another run removed it"
    cur = tok.read_text().strip()
    if cur != owner:
        return False, f"lock was STOLEN — now held by {cur}"
    os.utime(LOCK_DIR, None)
    return True, owner


def lock_status() -> dict:
    """Read-only view of the lock. NEVER acquires, never steals, never writes.

    It exists because sampling the age used to require `lock acquire` — the one command
    that REMOVES a lock older than the stale window. So the documented way to diagnose
    "is the holder alive?" was also the way to destroy it, and past 135 minutes the
    diagnostic *was* the theft. A question must not be answerable only by an action.
    """
    if not LOCK_DIR.is_dir():
        return {"held": False}
    tok = LOCK_DIR / "owner"
    return {
        "held": True,
        "owner": tok.read_text().strip() if tok.is_file() else None,
        "age_min": round((time.time() - LOCK_DIR.stat().st_mtime) / 60, 1),
        "stale_after_min": LOCK_STALE_MIN,
    }


def lock_release(owner: str) -> tuple[bool, str]:
    owner = _norm_owner(owner)
    tok = LOCK_DIR / "owner"
    if tok.is_file() and tok.read_text().strip() != owner:
        return False, "not the owner — refusing to release someone else's lock"
    shutil.rmtree(LOCK_DIR, ignore_errors=True)
    return True, "released"


def log_path(repo: Path, target: str) -> Path:
    """Run memory always lands in the khenrix-utils checkout, never the target repo.

    `repo` is the TARGET's repo; resolving through registry_repo() is what makes that true.
    Writing under the target instead would create docs/tuneups/log/ inside a foreign repo,
    where `git add -A` would sweep it into that project's commit — and the next run, reading
    from khenrix-utils, would see no history and re-propose everything already decided.
    """
    return registry_repo(repo) / "docs" / "tuneups" / "log" / f"{target}.jsonl"


def _require_owned_log_path(repo: Path, target: str) -> Path:
    """Return the lexical in-repository log path without following any link."""
    registry = registry_repo(repo)
    log_dir = registry / "docs" / "tuneups" / "log"
    path = log_path(repo, target)
    if path.parent != log_dir:
        raise ValueError(
            f"invalid log target {target!r}; it must name one file directly under {log_dir}")
    link = _symlink_component(registry, path)
    if link is not None:
        raise ValueError(
            f"run log {path.relative_to(registry)} is symlink-backed at "
            f"{link.relative_to(registry)}; refusing to read or write outside the "
            "repository-owned log")
    return path


def _require_active_log_append_only(repo: Path, path: Path) -> None:
    """Fail unless a tracked active log is the exact HEAD blob plus appended bytes.

    ``review-material`` excludes this file because its compact ledger represents the
    current run. That substitution is sound only for append-only edits: otherwise a
    deletion or same-length rewrite of committed history disappears from both the diff and
    the ledger. A log absent from HEAD is a legitimate new run log and has no prefix yet.
    """
    relpath = str(path.relative_to(repo))
    tree = _git_authority().run(
        ["ls-tree", "-z", "HEAD", "--", relpath], repo=repo,
        capture_output=True, check=False)
    if tree.returncode != 0:
        raise RuntimeError(
            f"cannot inspect committed active run log HEAD:{relpath}: "
            f"{tree.stderr.decode('utf-8', 'replace').strip()}")
    records = [record for record in tree.stdout.split(b"\0") if record]
    if not records:
        return
    if len(records) != 1 or b"\t" not in records[0]:
        raise RuntimeError(
            f"committed active run log HEAD:{relpath} is ambiguous")
    metadata, recorded_path = records[0].split(b"\t", 1)
    fields = metadata.split()
    if (len(fields) != 3 or fields[0] not in (b"100644", b"100755")
            or fields[1] != b"blob" or recorded_path != os.fsencode(relpath)):
        raise RuntimeError(
            f"committed active run log HEAD:{relpath} is not a regular blob")
    blob = _git_authority().run(
        ["cat-file", "blob", fields[2]], repo=repo,
        capture_output=True, check=False)
    if blob.returncode != 0:
        raise RuntimeError(
            f"cannot read committed active run log HEAD:{relpath}: "
            f"{blob.stderr.decode('utf-8', 'replace').strip()}")
    try:
        current = path.read_bytes()
    except OSError as e:
        raise RuntimeError(
            f"committed active run log {relpath} is missing or unreadable: {e}") from e
    if not current.startswith(blob.stdout):
        raise RuntimeError(
            f"active run log {relpath} is not append-only from HEAD; committed history "
            "was deleted or rewritten, so excluding its diff would hide candidate bytes")


def log_target_key(repo: Path, skill: str) -> str:
    """Foreign targets are keyed <repo-name>@<hash>:<skill>.

    The basename alone collides (~/git/foo and ~/work/foo are different repos with the same
    name, and a run log that merges them would silently apply one project's decisions to
    another). The short hash of the canonical repo root disambiguates; the readable name is
    kept so the file is still greppable by a human.
    """
    if is_khenrix_repo(repo):
        return skill
    root = str(repo.resolve())
    h = hashlib.sha256(root.encode()).hexdigest()[:8]
    return f"{repo.resolve().name}@{h}:{skill}"


# Severity decides when a run STOPS, so the bar has to be objective enough that a tiring
# operator can't quietly relabel a defect to end the loop. Tests, not adjectives:
SEVERITIES = ("blocking", "serious", "minor")
SEVERITY_TESTS = {
    "blocking": "produces a wrong result, makes a gate pass/fail incorrectly, loses data, "
                "exposes a secret, or documents behaviour the code does not have",
    "serious":  "a real edge case that CAN fire in normal use, or an eval gap that would "
                "hide a genuine regression — bounded, but a correctness defect",
    "minor":    "polish, naming, hardening for a condition never observed, preference",
}
STALL_LIMIT = 2  # consecutive non-decreasing cycles => the loop is not converging


CYCLE_END = "cycle-end"      # one per CYCLE, written after that cycle's council review
RUN_START = "run-start"      # one per RUN, written at Step 1
RUN_END = "run-convergence"  # one per RUN — the run's outcome, NOT a cycle boundary
RUN_GAP_RESOLUTION = "run-gap-resolution"  # exact append-only adjudication of old history
RUN_GAP_RESOLUTION_VALUE = "pre-start-gap-accounted"
RUN_GAP_RESOLUTION_LEGACY_VALUE = "prior-history-not-current-run"
RUN_GAP_RESOLUTION_SCHEMA = 3
RUN_GAP_REPLAY_PREFIX = "run-gap-lifecycle-replay"
RUN_GAP_RESOLUTION_TITLE = "resolved pre-start gap without changing current run scope"
LIFECYCLE_MARKERS = (RUN_END, RUN_START, CYCLE_END)


def _run_start_problem(entry: dict) -> str | None:
    """Why this run boundary is not structural, or None for a usable marker."""
    if entry.get("decision") != "applied":
        return f"{RUN_START} decision must be 'applied' to open a run"
    return None


def _run_end_problem(entry: dict, *, allow_legacy: bool) -> str | None:
    """Why this run terminal is not structural, or None for one exact schema.

    Historical logs contain one early stalled form with only ``decision=deferred``.
    Readers retain that narrow form as a terminal so append-only history does not change
    meaning. Writers never create it. Every modern terminal binds the decision to an
    explicit boolean outcome and an exact, non-negative integer cycle count; in particular,
    bool is not accepted as int and contradictory decision/outcome pairs never delimit.
    """
    decision = entry.get("decision")
    has_converged = "converged" in entry
    has_cycles = "cycles" in entry
    if (allow_legacy and decision == "deferred"
            and not has_converged and not has_cycles):
        return None
    cycles = entry.get("cycles")
    if type(cycles) is not int or cycles < 0:
        return (
            f"{RUN_END} `cycles` must be an exact non-negative integer "
            "(booleans are not integers here)")
    converged = entry.get("converged")
    if decision == "applied" and converged is True:
        return None
    if decision == "deferred" and converged is False:
        return None
    return (
        f"{RUN_END} must pair decision='applied' with converged=true or "
        "decision='deferred' with converged=false")


def _is_run_end(entry: dict) -> bool:
    """Reader-compatible trusted terminal predicate used at every run boundary."""
    return (entry.get("finding_id") == RUN_END
            and _run_end_problem(entry, allow_legacy=True) is None)


def _cycle_end_problem(entry: dict, previous_cycle: int | None) -> str | None:
    """Why this cycle delimiter is not an exact, monotonic marker, or None."""
    if entry.get("decision") != "applied":
        return f"{CYCLE_END} decision must be 'applied' to delimit a reviewed cycle"
    n = entry.get("cycle")
    if type(n) is not int:
        return (
            f"{CYCLE_END} is missing an integer `cycle` number. It is required: "
            "without it a duplicate marker looks identical to a clean cycle.")
    if previous_cycle is not None and n <= previous_cycle:
        return (
            f"{CYCLE_END} cycle={n} is not greater than the previous "
            f"({previous_cycle}) — duplicate or out-of-order marker")
    return None


def _cycle_end_markers(entries: list, start: int, stop: int) -> tuple[set[int], set[int]]:
    """Trusted-boundary and malformed cycle-end indices in one run segment."""
    trusted, invalid = set(), set()
    previous_cycle = None
    terminal = False
    sequence_valid = True
    for i in range(start + 1, stop):
        entry = entries[i]
        if entry.get("finding_id") == RUN_END:
            if _is_run_end(entry):
                terminal = True
            continue
        if entry.get("finding_id") != CYCLE_END:
            continue
        if terminal or not sequence_valid:
            invalid.add(i)
            sequence_valid = False
            continue
        problem = _cycle_end_problem(entry, previous_cycle)
        if problem:
            invalid.add(i)
            sequence_valid = False
        else:
            trusted.add(i)
            previous_cycle = entry["cycle"]
    return trusted, invalid


def _unaccounted_run_indices(entries: list, start: int, stop: int,
                             trusted_cycles: set[int], invalid_markers: set[int],
                             structural_markers: set[int]) -> list[int]:
    """Applied/invalid records not closed by a valid cycle in one interrupted run."""
    accounted_through = max(trusted_cycles, default=start)
    return [
        i for i in range(accounted_through + 1, stop)
        if i not in structural_markers
        and (i in invalid_markers
             or (entries[i].get("decision") == "applied"
                 and entries[i].get("finding_id") not in LIFECYCLE_MARKERS))
    ]


def _current_run_start(entries: list) -> int:
    start = max((i for i, e in enumerate(entries)
                 if e.get("finding_id") == RUN_START
                 and _run_start_problem(e) is None), default=None)
    if start is None:
        # FAIL CLOSED. Falling back to the whole log would scope a fresh run to every prior
        # run's history — exactly what this scoping exists to prevent — and it was the one
        # path in this engine that failed open.
        raise ValueError(
            f"no {RUN_START!r} marker in the log — write it at Step 1, before any finding. "
            "Without it the run's cycles cannot be isolated from earlier runs.")
    return start


def _pre_start_gap(entries: list, start: int | None = None) -> dict | None:
    """Exact ambiguous prefix before the current run, or None when the boundary is sound.

    The fingerprint covers the last trusted boundary, every intervening record, and the
    current run-start. A trusted boundary is a run-convergence or an exact structural gap
    resolution. It deliberately does NOT cover later cycles: adjudicating old history must
    not expire merely because the current run legitimately continues.
    """
    start = _current_run_start(entries) if start is None else start
    run_starts = [i for i, entry in enumerate(entries[:start + 1])
                  if entry.get("finding_id") == RUN_START
                  and _run_start_problem(entry) is None]
    previous_start = -1
    previous_end = -1
    resolution_boundary = -1
    structural_markers = set()
    invalid_cycle_markers = set()
    invalid_run_markers = {
        i for i, entry in enumerate(entries)
        if ((entry.get("finding_id") == RUN_START
             and _run_start_problem(entry) is not None)
            or (entry.get("finding_id") == RUN_END and not _is_run_end(entry)))
    }
    scan_from = 0
    gap = None

    def build_gap(after_index: int, between_end: int, current_start: int,
                  *, included_indices: set[int] | None = None,
                  pending_indices: list[int] | tuple[int, ...] = (),
                  resolution_after_index: int | None = None) -> dict:
        """Build one exact gap; included_indices permits a non-contiguous debt batch."""
        between = entries[after_index + 1:between_end]
        ambiguous = []
        for relative_i, entry in enumerate(between):
            absolute_i = after_index + 1 + relative_i
            if included_indices is not None and absolute_i not in included_indices:
                continue
            if absolute_i in structural_markers:
                continue
            invalid_lifecycle = (absolute_i in invalid_cycle_markers
                                 or absolute_i in invalid_run_markers)
            if not invalid_lifecycle and (
                    entry.get("decision") != "applied"
                    or entry.get("finding_id") in LIFECYCLE_MARKERS):
                continue
            ambiguous.append({
                "gap_index": relative_i,
                "target": entry.get("target"),
                "finding_id": entry.get("finding_id"),
                "effective_severity": _effective_severity(entry),
            })
        payload = {
            "after": entries[after_index] if after_index >= 0 else None,
            "between": between,
            "start": entries[current_start],
        }
        raw = _strict_json_dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")
        return {
            "after_index": after_index,
            "between_end_index": between_end,
            "start_index": current_start,
            "target": entries[current_start].get("target"),
            "run_start_ts": entries[current_start].get("ts"),
            "gap_sha256": hashlib.sha256(raw).hexdigest(),
            "gap_records": len(between),
            "applied_findings": len(ambiguous),
            "finding_ids": [record["finding_id"] for record in ambiguous],
            "applied_records": ambiguous,
            "_included_indices": (None if included_indices is None
                                  else tuple(sorted(included_indices))),
            "_pending_indices": tuple(sorted(set(pending_indices))),
            "_structural_markers": tuple(sorted(structural_markers)),
            "_resolution_after_index": resolution_after_index,
        }

    for current_start in run_starts:
        trusted_cycles, invalid_cycles = _cycle_end_markers(
            entries, previous_start, current_start)
        invalid_cycle_markers.update(invalid_cycles)
        segment_ends = [
            i for i in range(scan_from, current_start)
            if _is_run_end(entries[i])
        ]
        if segment_ends:
            # The first terminal closes the run. A later one is malformed lifecycle debt,
            # not a newer trusted boundary that may erase the records between them.
            previous_end = segment_ends[0]
            invalid_run_markers.update(segment_ends[1:])
        scan_from = current_start + 1

        context = _gap_resolution_context(entries, gap) if gap is not None else None
        if context and context["resolved"]:
            structural = context["structural_index"]
            structural_markers.add(structural)
            last_cycle = max(trusted_cycles, default=-1)
            # The exact marker is trusted, but it is not a synthetic terminal. A later
            # valid cycle-end closes all preceding work in this interrupted run; without
            # one, retain the tail after the resolved marker as exact next-gap debt.
            boundary = (last_cycle if last_cycle > structural
                        else max(last_cycle, gap["start_index"]))
            resolution_boundary = max(resolution_boundary, boundary)

        boundary_is_terminal = previous_end >= resolution_boundary
        trusted_boundary = previous_end if boundary_is_terminal else resolution_boundary
        last_trusted_cycle = max(trusted_cycles, default=previous_start)
        unclosed_invalid_cycles = [
            i for i in invalid_cycles if i > last_trusted_cycle]
        if (boundary_is_terminal and unclosed_invalid_cycles
                and min(unclosed_invalid_cycles) <= previous_end):
            first_invalid = min(unclosed_invalid_cycles)
            trusted_boundary = max(
                (i for i in trusted_cycles if i < first_invalid),
                default=previous_start)
        resolved_tail = []
        if context and context["resolved"]:
            preview = build_gap(trusted_boundary, current_start, current_start)
            preview_records = preview["applied_records"]
            has_structural_problem = any(
                record["finding_id"] in (*LIFECYCLE_MARKERS, RUN_GAP_RESOLUTION)
                for record in preview_records)
            terminal_bookkeeping = (
                boundary_is_terminal
                and trusted_boundary > previous_start
                and not has_structural_problem)
            if not terminal_bookkeeping:
                resolved_tail = [
                    trusted_boundary + 1 + record["gap_index"]
                    for record in preview_records
                ]

        pending = list(gap.get("_pending_indices", ())) if gap is not None else []
        if gap is not None:
            pending.extend(_unaccounted_run_indices(
                entries, previous_start, current_start, trusted_cycles,
                invalid_cycles | invalid_run_markers, structural_markers))
            pending = sorted(set(pending))

        # Resolving the immutable original batch cannot erase debt accumulated while it
        # was carried. Promote that debt to the next exact batch, still anchored to the
        # newest run-start; cycle-counted records were never added to `pending`.
        promoted = sorted(set(pending + resolved_tail))
        if context and context["resolved"] and promoted:
            gap = build_gap(
                min(promoted) - 1, current_start, current_start,
                included_indices=set(promoted),
                resolution_after_index=context["structural_index"])
            previous_start = current_start
            continue

        # A terminal closes the current-run scanner, not the accounting interval. Work
        # left after the last reviewed cycle still needs exact adjudication on rollover;
        # ordinary records written after the terminal remain inter-run bookkeeping.
        terminal = next(
            (i for i in range(previous_start + 1, current_start)
             if _is_run_end(entries[i])), None)
        if gap is None and terminal is not None:
            pre_terminal = _unaccounted_run_indices(
                entries, previous_start, terminal, trusted_cycles,
                invalid_cycles | invalid_run_markers, structural_markers)
            if pre_terminal:
                post_terminal_structural = [
                    i for i in range(terminal + 1, current_start)
                    if i in invalid_cycles or i in invalid_run_markers
                    or (entries[i].get("decision") == "applied"
                        and entries[i].get("finding_id") == RUN_GAP_RESOLUTION)
                ]
                selected = set(pre_terminal + post_terminal_structural)
                gap = build_gap(
                    max((i for i in trusted_cycles if i < terminal),
                        default=previous_start),
                    current_start, current_start, included_indices=selected)
                previous_start = current_start
                continue

        inherited = gap if gap is not None and not context["resolved"] else None
        after_index = trusted_boundary if inherited is None else inherited["after_index"]
        between_end = current_start if inherited is None else inherited["between_end_index"]
        included = (None if inherited is None
                    else inherited.get("_included_indices"))
        gap = build_gap(
            after_index, between_end, current_start,
            included_indices=None if included is None else set(included),
            pending_indices=pending if inherited is not None else (),
            resolution_after_index=(None if inherited is None
                                    else inherited.get("_resolution_after_index")))
        ambiguous = gap["applied_records"]
        # Ordinary bookkeeping after a completed run stays outside the next one. A
        # reserved resolution is different: after a terminal it was already stray work.
        has_structural_problem = any(
            record["finding_id"] in (*LIFECYCLE_MARKERS, RUN_GAP_RESOLUTION)
            for record in ambiguous)
        if not ambiguous or (
                inherited is None
                and boundary_is_terminal
                and after_index > previous_start
                and not has_structural_problem):
            gap = None
        previous_start = current_start

    # A marker appended under the active start is not encountered by the rollover loop.
    # Expose any debt queued behind the batch it resolves immediately, so a clean cycle
    # cannot converge during the one-run window before the next start.
    if gap is not None:
        context = _gap_resolution_context(entries, gap)
        if context["resolved"]:
            structural_markers.add(context["structural_index"])
            pending = list(gap.get("_pending_indices", ()))
            if pending:
                gap = build_gap(
                    min(pending) - 1, gap["start_index"], gap["start_index"],
                    included_indices=set(pending),
                    resolution_after_index=context["structural_index"])
            else:
                gap["_structural_markers"] = tuple(sorted(structural_markers))
    return gap


def _effective_severity(entry: dict) -> str:
    """The two convergence classes; missing/invalid severity always fails closed."""
    return "minor" if entry.get("severity") == "minor" else "serious"


def _gap_record_key(entry: dict) -> tuple:
    return (entry.get("target"), entry.get("finding_id"), _effective_severity(entry))


def _gap_record_needs_surrogate(record: dict) -> bool:
    """Lifecycle records cannot be replayed under their reserved finding_id safely."""
    return record.get("finding_id") in (*LIFECYCLE_MARKERS, RUN_GAP_RESOLUTION)


def _gap_record_is_lifecycle_surrogate(record: dict) -> bool:
    """True for the reserved, provenance-bearing ordinary debt emitted by schema v3."""
    finding_id = record.get("finding_id")
    return (
        isinstance(finding_id, str)
        and re.fullmatch(
            rf"{re.escape(RUN_GAP_REPLAY_PREFIX)}-[0-9a-f]{{64}}-[0-9]+",
            finding_id) is not None
    )


def _gap_replay_finding_id(gap: dict, record: dict) -> str:
    """An ordinary finding id bound to one exact fingerprinted lifecycle occurrence."""
    return (f"{RUN_GAP_REPLAY_PREFIX}-{gap['gap_sha256']}-"
            f"{record['gap_index']}")


def _gap_replay_entry(gap: dict, record: dict, *, current_target: str | None = None) -> dict:
    """CLI recipe for current-owned lifecycle debt; ordinary so existing accounting sees it."""
    return {
        # A malformed lifecycle record may itself carry the wrong target. The surrogate
        # must be appendable to the active log while still binding that original value.
        "target": gap["target"] if current_target is None else current_target,
        "finding_id": _gap_replay_finding_id(gap, record),
        "decision": "applied",
        "severity": "serious",
        "gap_sha256": gap["gap_sha256"],
        "gap_index": record["gap_index"],
        "replays_target": record.get("target"),
        "replays_finding_id": record.get("finding_id"),
        "title": f"re-recorded current-owned lifecycle gap index {record['gap_index']}",
    }


def _gap_replay_matches(candidate: dict, gap: dict, record: dict) -> bool:
    expected = _gap_replay_entry(gap, record)
    exact = ("target", "finding_id", "decision", "severity", "gap_sha256",
             "gap_index", "replays_target", "replays_finding_id")
    return (
        all(candidate.get(key) == expected[key] for key in exact)
        and isinstance(candidate.get("title"), str) and bool(candidate["title"].strip())
        and isinstance(candidate.get("reason"), str) and bool(candidate["reason"].strip())
    )


def _gap_index_list_problem(entry: dict, key: str) -> str | None:
    if key not in entry:
        return f"{key} is required by the current resolution schema"
    value = entry[key]
    if not isinstance(value, list):
        return f"{key} must be a list"
    if any(type(index) is not int for index in value):
        return f"{key} must contain only integers (not booleans)"
    if value != sorted(set(value)):
        return f"{key} must be unique and strictly increasing"
    return None


def _ordinary_gap_replay_problem(entries: list, marker_index: int, gap: dict,
                                 records: list[dict]) -> str | None:
    required = Counter(
        (record["target"], record["finding_id"], record["effective_severity"])
        for record in records
    )
    replay_after = max(gap["start_index"], gap.get("_resolution_after_index") or -1)
    available = Counter(
        _gap_record_key(candidate)
        for candidate in entries[replay_after + 1:marker_index]
        if candidate.get("decision") == "applied"
        and candidate.get("finding_id") not in (*LIFECYCLE_MARKERS, RUN_GAP_RESOLUTION)
    )
    missing = required - available
    if missing:
        return f"selected gap occurrences were not re-recorded under this run: {dict(missing)}"
    return None


def _gap_resolution_problem(entries: list, marker_index: int, gap: dict) -> str | None:
    """Why one candidate is not a valid structural marker, or None when it is exact."""
    entry = entries[marker_index]
    if entry.get("finding_id") != RUN_GAP_RESOLUTION:
        return f"finding_id must be {RUN_GAP_RESOLUTION!r}"
    if entry.get("target") != gap["target"]:
        return f"target must match the active run ({gap['target']!r})"
    if entry.get("decision") != "applied":
        return "decision must be 'applied'"
    if "run_start_ts" not in entry:
        return "run_start_ts must be present and match the emitted value"
    if (not isinstance(entry.get("gap_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["gap_sha256"]) is None):
        return "gap_sha256 must be lowercase 64-hex"
    for key in ("gap_records", "applied_findings"):
        if type(entry.get(key)) is not int or entry[key] < 0:
            return f"{key} must be a non-negative integer"
    expected = {
        "run_start_ts": gap["run_start_ts"],
        "gap_sha256": gap["gap_sha256"],
        "gap_records": gap["gap_records"],
        "applied_findings": gap["applied_findings"],
    }
    if any(entry.get(key) != value for key, value in expected.items()):
        return f"marker does not match the current ambiguity ({json.dumps(expected)})"
    if "severity" in entry:
        return "structural markers must not have severity"
    if not isinstance(entry.get("title"), str) or not entry["title"].strip():
        return "title must be non-empty audit text"
    if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
        return "reason must be non-empty audit text"

    resolution = entry.get("resolution")
    has_selected = "re_recorded_gap_indices" in entry
    selected = entry.get("re_recorded_gap_indices", [])
    if resolution == RUN_GAP_RESOLUTION_LEGACY_VALUE:
        if "schema_version" in entry:
            return "legacy all-prior markers cannot declare a schema version"
        if any(_gap_record_is_lifecycle_surrogate(record)
               for record in gap["applied_records"]):
            return "legacy markers cannot account for carried schema-v3 lifecycle debt"
        if selected != []:
            return "legacy all-prior markers cannot claim re-recorded gap indices"
        return None
    if resolution != RUN_GAP_RESOLUTION_VALUE:
        return (f"resolution must be {RUN_GAP_RESOLUTION_VALUE!r} "
                f"(or legacy {RUN_GAP_RESOLUTION_LEGACY_VALUE!r})")

    schema = entry.get("schema_version")
    records = {record["gap_index"]: record for record in gap["applied_records"]}
    if "schema_version" in entry:
        if type(schema) is not int or schema != RUN_GAP_RESOLUTION_SCHEMA:
            return f"schema_version must be the integer {RUN_GAP_RESOLUTION_SCHEMA}"
        if has_selected:
            return "schema v3 uses exhaustive prior_gap_indices/current_gap_indices, not re_recorded_gap_indices"
        for key in ("prior_gap_indices", "current_gap_indices"):
            problem = _gap_index_list_problem(entry, key)
            if problem:
                return problem
        prior = entry["prior_gap_indices"]
        current = entry["current_gap_indices"]
        if set(prior) & set(current):
            return "prior_gap_indices and current_gap_indices must be disjoint"
        emitted = sorted(records)
        classified = sorted(prior + current)
        if classified != emitted:
            missing = sorted(set(emitted) - set(classified))
            extra = sorted(set(classified) - set(emitted))
            return ("ownership lists must classify every applied gap index exactly once "
                    f"(missing={missing}, extra={extra})")
        reclassified = [index for index in prior
                        if _gap_record_is_lifecycle_surrogate(records[index])]
        if reclassified:
            return ("provenance-bound current lifecycle debt cannot be reclassified prior "
                    f"before a valid cycle accounts for it: {reclassified}")

        ordinary = [records[index] for index in current
                    if not _gap_record_needs_surrogate(records[index])]
        problem = _ordinary_gap_replay_problem(entries, marker_index, gap, ordinary)
        if problem:
            return problem

        replay_after = max(gap["start_index"], gap.get("_resolution_after_index") or -1)
        candidates = entries[replay_after + 1:marker_index]
        missing_surrogates = [
            record["gap_index"] for record in (records[index] for index in current)
            if _gap_record_needs_surrogate(record)
            and not any(_gap_replay_matches(candidate, gap, record)
                        for candidate in candidates)
        ]
        if missing_surrogates:
            return ("current lifecycle gap occurrences lack their bound ordinary serious "
                    f"surrogate: {missing_surrogates}")
        return None

    # Reader compatibility for v2 markers. New writes require schema v3, but historical
    # fingerprinted occurrence lists must remain structural in append-only logs.
    if not has_selected:
        return "re_recorded_gap_indices is required by the current resolution schema"
    if not isinstance(selected, list):
        return "re_recorded_gap_indices must be a list"
    if any(type(index) is not int for index in selected):
        return "re_recorded_gap_indices must contain only integers (not booleans)"
    if selected != sorted(set(selected)):
        return "re_recorded_gap_indices must be unique and strictly increasing"

    if any(index not in records for index in selected):
        return "each re_recorded_gap_indices value must name an applied gap finding"
    if any(_gap_record_is_lifecycle_surrogate(record) for record in records.values()):
        return "legacy markers cannot account for carried schema-v3 lifecycle debt"
    return _ordinary_gap_replay_problem(
        entries, marker_index, gap, [records[index] for index in selected])


def _gap_resolution_matches(entries: list, marker_index: int, gap: dict) -> bool:
    """True only when this candidate satisfies the exact schema and replay accounting."""
    return _gap_resolution_problem(entries, marker_index, gap) is None


def _gap_resolution_context(entries: list, gap: dict) -> dict:
    """Classify candidates in the one legal window; exactly one valid marker is structural."""
    resolution_after = max(gap["start_index"], gap.get("_resolution_after_index") or -1)
    boundary = next((i for i in range(resolution_after + 1, len(entries))
                     if (_is_run_end(entries[i])
                         or (entries[i].get("finding_id") == RUN_START
                             and _run_start_problem(entries[i]) is None))), None)
    stop = len(entries) if boundary is None else boundary
    candidates = [i for i in range(resolution_after + 1, stop)
                  if entries[i].get("finding_id") == RUN_GAP_RESOLUTION]
    valid = [i for i in candidates if _gap_resolution_matches(entries, i, gap)]
    structural = valid[0] if len(valid) == 1 else None
    return {
        "resolution_open": boundary is None,
        "valid_indices": valid,
        "structural_index": structural,
        "structural_resolution": (entries[structural].get("resolution")
                                  if structural is not None else None),
        "resolved": structural is not None,
    }


def _public_gap(gap: dict | None, context: dict | None = None) -> dict | None:
    if gap is None:
        return None
    return {key: gap[key] for key in (
        "run_start_ts", "gap_sha256", "gap_records", "applied_findings"
    )} | {
        "applied_records": gap["applied_records"],
        "resolved": bool(context and context["resolved"]),
        "resolution_open": bool(context and context["resolution_open"]),
        "resolution": ((context or {}).get("structural_resolution")
                       or RUN_GAP_RESOLUTION_VALUE),
    }


def cycle_severity_counts(entries: list) -> tuple[list[int], bool, list[str]]:
    """(per-cycle blocking+serious counts for the CURRENT run, tail_open, warnings).

    Scoped to the newest `run-start`: a previous run's history must not let a fresh run
    inherit its convergence or stall state. Cycles are delimited by `cycle-end` — NOT by
    `run-convergence`, which is written once per run (every historical entry carries
    `cycles: 3`), so counting on it silently measured runs and called them cycles.

    `tail_open` is True when applied findings sit after the last `cycle-end`: the cycle is
    still in flight and MUST NOT be read as a completed zero-serious cycle. An unsevered
    applied finding counts as serious so an omitted tag can never end the run.
    """
    start = _current_run_start(entries)
    # Findings before the newest run-start are ADVISORY, not fatal. A backfilled marker
    # looks identical to two legitimate states — closing out a deferred finding between
    # runs (this log's established practice) and a run that died before writing its
    # run-convergence — so raising permanently locked the target for every later run, and
    # misdiagnosed it as "you wrote run-start late" when the operator had not. The hard
    # guarantees that DO hold are elsewhere: a missing run-start refuses, an unnumbered or
    # duplicate cycle-end refuses, and an open tail can never converge.
    warnings = []
    gap = _pre_start_gap(entries, start)
    gap_context = _gap_resolution_context(entries, gap) if gap else None
    if gap and not gap_context["resolved"]:
        orphans = gap["finding_ids"]
        message = (
            f"{len(orphans)} applied finding(s) remain in an unresolved prefix before this "
            f"{RUN_START!r} ({orphans[:3]}…). An earlier run-start was missing or late, a "
            "carried gap was never resolved, or a reserved resolution followed a terminal. "
            "They are not counted in this run. Partition every emitted gap index "
            "and re-record the current-run occurrences before resolving the exact gap.")
        if not gap_context["resolution_open"]:
            message += (f" The resolution window is closed by this run's {RUN_END!r}; "
                        "do not append a resolution recipe after it.")
        warnings.append(message)
    structural_resolutions = set(gap.get("_structural_markers", ())) if gap else set()
    if gap_context and gap_context["structural_index"] is not None:
        structural_resolutions.add(gap_context["structural_index"])
    cycles, cur, seen_cycles = [], [], []
    cycle_sequence_valid = True
    for absolute_i in range(start + 1, len(entries)):
        e = entries[absolute_i]
        fid = e.get("finding_id")
        if fid == RUN_END:
            problem = _run_end_problem(e, allow_legacy=True)
            if problem:
                # Persisted malformed/contradictory terminals are lifecycle debt, not
                # boundaries. Continuing the scan is what prevents one from erasing the
                # serious work that follows it in the same reviewed cycle.
                cur.append({**e, "severity": "serious"})
                continue
            # Terminal: this run is complete. Anything after belongs to no run, so stop
            # rather than letting a later run that forgot its own run-start silently
            # inherit this run's cycle history and stall state. But do NOT drop it
            # silently: real work appended after a completed run means the log describes
            # two things at once, and reporting the finished run's verdict would report a
            # stale "converged" over unaccounted findings.
            # Any later lifecycle marker is malformed here: a valid applied run-start would
            # have become `start`, while a second run-end cannot close the same run twice.
            rest = [x.get("finding_id") for x in entries[absolute_i + 1:]
                    if (x.get("finding_id") in LIFECYCLE_MARKERS
                        or x.get("decision") == "applied")]
            if rest:
                warnings.append(
                    f"{len(rest)} record(s) follow this run's {RUN_END!r} with no newer "
                    f"valid applied {RUN_START!r} ({rest[:3]}…) — they belong to no run "
                    "and are NOT counted. Start the next run with run-start.")
            break
        if fid == CYCLE_END:
            # `cycle` is REQUIRED, not optional. Without it a duplicate marker is
            # indistinguishable from a legitimate zero-finding cycle — and a zero-finding
            # cycle IS the convergence condition, so any heuristic strict enough to catch
            # the duplicate also makes converging impossible. A monotonic number separates
            # them exactly.
            n = e.get("cycle")
            problem = _cycle_end_problem(e, seen_cycles[-1] if seen_cycles else None)
            if problem or not cycle_sequence_valid:
                # Persisted logs can be edited by hand or predate write-time validation.
                # A bad delimiter taints the run segment: trusting a later well-shaped
                # marker would retroactively hide the corruption and every record after it.
                cur.append({**e, "severity": "serious"})
                cycle_sequence_valid = False
                continue
            seen_cycles.append(n)
            cycles.append(cur)
            cur = []
        elif fid == RUN_START:
            if _run_start_problem(e):
                cur.append({**e, "severity": "serious"})
        elif fid == RUN_GAP_RESOLUTION:
            if absolute_i not in structural_resolutions and e.get("decision") == "applied":
                cur.append({**e, "severity": "serious"})
        elif fid not in LIFECYCLE_MARKERS and e.get("decision") == "applied":
            cur.append(e)
    # Everything that is not an explicit, valid "minor" counts as serious. Whitelisting the
    # only value that can END a run means a null, a typo or an absent key all fail CLOSED.
    counts = [sum(1 for e in c if e.get("severity") != "minor") for c in cycles]
    return counts, bool(cur), warnings


def convergence_status(entries: list) -> dict:
    """Severity-gated stop rule, replacing the old fixed cycle cap.

    - converged: the newest COMPLETED cycle applied nothing blocking or serious. Positive
      evidence there is nothing left worth finding, which a counter never gave.
    - stalled: the best (minimum) count has not improved for STALL_LIMIT cycles. Testing
      "did not decrease" was insufficient — an oscillation like [2,1,2,1,...] never
      converges and never stalls, i.e. the termination guarantee had an infinite loop in
      exactly the shape it existed to prevent. Improvement-of-best terminates because the
      minimum is a non-negative integer that must strictly fall to keep the loop alive.
    """
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("run log must be a list of JSON objects")
    gap = _pre_start_gap(entries)
    gap_context = _gap_resolution_context(entries, gap) if gap else None
    counts, tail_open, warnings = cycle_severity_counts(entries)
    run_gap = _public_gap(gap, gap_context)
    if not counts:
        return {"cycles": 0, "counts": [], "tail_open": tail_open, "warnings": warnings,
                "verdict": "cycle in flight" if tail_open else "no-cycles-yet",
                "converged": False, "run_gap": run_gap}
    base = {"cycles": len(counts), "counts": counts, "tail_open": tail_open,
            "warnings": warnings, "run_gap": run_gap}
    if tail_open:  # never converge on an unclosed cycle
        return {**base, "converged": False, "verdict": "cycle in flight — close it first"}
    if counts[-1] == 0:
        if warnings:
            # Advisory diagnostics must not lock the target — but converging ON an
            # ambiguous log would report a clean run over records the parse dropped.
            return {**base, "converged": False,
                    "verdict": "clean cycle, but the log is ambiguous — resolve the "
                               "warning(s) before declaring convergence"}
        return {**base, "converged": True, "verdict": "converged"}
    best_at = min(range(len(counts)), key=lambda i: (counts[i], i))  # first index of the min
    stalled = (len(counts) - 1 - best_at) >= STALL_LIMIT
    return {**base, "converged": False,
            "verdict": "stalled — hand over" if stalled else "keep-iterating"}


def _log_target_skill(repo: Path, target: str) -> str:
    """Return the skill bound to this exact repository-qualified log key."""
    if is_khenrix_repo(repo):
        _validate_skill_name(target)
        return target
    _, separator, skill = target.rpartition(":")
    if not separator:
        _validate_skill_name(target)
        expected = log_target_key(repo, target)
        raise ValueError(
            f"target {target!r} is unqualified but {repo} is not the khenrix-utils "
            f"checkout — use the log_target from `target-info` (expected {expected!r})")
    _validate_skill_name(skill)
    expected = log_target_key(repo, skill)
    if target != expected:
        raise ValueError(
            f"log target {target!r} is not bound to repository {repo}; expected {expected!r} "
            "from `target-info`")
    return skill


def _check_log_key(repo: Path, target: str) -> None:
    """Refuse a run-log key that is not bound to this exact repo and skill."""
    _log_target_skill(repo, target)


def _validate_run_gap_resolution(repo: Path, target: str, entry: dict) -> None:
    """Refuse a broad, stale, malformed, or duplicate waiver at write time.

    The parser repeats these checks because a tracked JSONL file can also be edited by hand.
    Write-time validation exists to keep a typo from permanently entering append-only history.
    """
    entries = log_entries(repo, target)
    if entry.get("schema_version") != RUN_GAP_RESOLUTION_SCHEMA:
        raise ValueError(
            f"v1/v2 {RUN_GAP_RESOLUTION!r} values are reader-only; new writes require "
            f"schema_version={RUN_GAP_RESOLUTION_SCHEMA} with exhaustive "
            "prior_gap_indices/current_gap_indices")
    gap = _pre_start_gap(entries)
    if gap is None:
        raise ValueError(f"no pre-start ambiguity exists for {RUN_GAP_RESOLUTION!r} to resolve")
    context = _gap_resolution_context(entries, gap)
    if not context["resolution_open"]:
        raise ValueError(
            f"the current run is already terminal; {RUN_GAP_RESOLUTION!r} must be appended "
            f"after its {RUN_START!r} and before its {RUN_END!r}")
    if context["valid_indices"]:
        raise ValueError("this exact pre-start ambiguity is already resolved")
    candidate_entries = entries + [entry]
    problem = _gap_resolution_problem(candidate_entries, len(entries), gap)
    if problem:
        raise ValueError(f"invalid {RUN_GAP_RESOLUTION!r}: {problem}")
    candidate_context = _gap_resolution_context(candidate_entries, gap)
    if candidate_context["structural_index"] != len(entries):
        raise ValueError(
            f"{RUN_GAP_RESOLUTION!r} would not be the unique valid marker for this gap")


def log_append(repo: Path, target: str, entry: dict) -> dict:
    _check_log_key(repo, target)
    if not isinstance(entry, dict):
        raise ValueError("log append entry must be a JSON object")
    missing = REQUIRED_LOG_KEYS - entry.keys()
    if missing:
        raise ValueError(f"log entry missing keys: {sorted(missing)}")
    if not isinstance(entry["finding_id"], str) or not entry["finding_id"].strip():
        raise ValueError("finding_id must be a non-empty string")
    if entry["decision"] not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    if "ts" in entry and (
            not isinstance(entry["ts"], str) or not entry["ts"].strip()):
        raise ValueError(
            "ts must be a non-empty string; omit it to stamp the current time")
    # An explicit null is NOT the same as an absent key: `.get(k, default)` returns None
    # for an explicit null, so a null-severity finding counted as 0 and converged a cycle
    # that had applied work. Absent is allowed (defaults to serious); null is not.
    if "severity" in entry and entry["severity"] not in SEVERITIES:
        # Show the TESTS, not just the labels: this is the moment an operator is choosing
        # a severity, and severity is what decides when the run stops. SEVERITY_TESTS was
        # dead code duplicating SKILL.md's table until it was wired in here.
        raise ValueError(
            f"severity must be one of {sorted(SEVERITIES)} (got {entry['severity']!r}); "
            "omit the key entirely to default to 'serious'.\n"
            + "\n".join(f"  {k}: {v}" for k, v in SEVERITY_TESTS.items()))
    if entry["target"] != target:
        raise ValueError(f"entry target {entry['target']!r} != --target {target!r}")
    if entry["finding_id"] == RUN_START:
        problem = _run_start_problem(entry)
        if problem:
            raise ValueError(f"invalid {RUN_START!r}: {problem}")
    if entry["finding_id"] == RUN_END:
        problem = _run_end_problem(entry, allow_legacy=False)
        if problem:
            raise ValueError(f"invalid {RUN_END!r}: {problem}")
        entries = log_entries(repo, target)
        start = _current_run_start(entries)
        if any(_is_run_end(existing) for existing in entries[start + 1:]):
            raise ValueError(
                f"cannot append {RUN_END!r}: the current run is already terminal; "
                f"append {RUN_START!r} first")
    # Validate the complete delimiter contract before it enters append-only history.
    # Numbering is scoped to the newest run-start; gaps are allowed, reversals are not.
    if entry["finding_id"] == CYCLE_END:
        problem = _cycle_end_problem(entry, None)
        if problem:
            raise ValueError(f"invalid {CYCLE_END!r}: {problem}")
        entries = log_entries(repo, target)
        start = _current_run_start(entries)
        previous_cycle = None
        for existing in entries[start + 1:]:
            if _is_run_end(existing):
                raise ValueError(
                    f"cannot append {CYCLE_END!r}: the current run is already terminal; "
                    f"append {RUN_START!r} first")
            if existing.get("finding_id") != CYCLE_END:
                continue
            problem = _cycle_end_problem(existing, previous_cycle)
            if problem:
                raise ValueError(
                    f"cannot append {CYCLE_END!r}: current run already contains an "
                    f"invalid delimiter ({problem})")
            previous_cycle = existing["cycle"]
        problem = _cycle_end_problem(entry, previous_cycle)
        if problem:
            raise ValueError(f"invalid {CYCLE_END!r}: {problem}")
    if entry["finding_id"] == RUN_GAP_RESOLUTION:
        _validate_run_gap_resolution(repo, target, entry)
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    serialized = _strict_json_dumps(entry, sort_keys=True) + "\n"
    p = _require_owned_log_path(repo, target)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_jsonl_append(p, serialized)
    return entry


def log_entries(repo: Path, target: str) -> list[dict]:
    """EVERY entry in write order — the raw history.

    Distinct from log_list(), which collapses to the latest decision per finding_id: cycle
    accounting needs each cycle's own applied findings, and a deduped view would drop a
    finding that was applied in one cycle and superseded in a later one.
    """
    _check_log_key(repo, target)
    p = _require_owned_log_path(repo, target)
    if not p.is_file():
        return []
    entries = []
    for line_number, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        entry = _strict_json_loads(line, context=f"run log line {line_number}")
        if not isinstance(entry, dict):
            raise ValueError(f"run log line {line_number} must be a JSON object")
        missing = REQUIRED_LOG_KEYS - entry.keys()
        if missing:
            raise ValueError(
                f"run log line {line_number} is missing keys: {sorted(missing)}")
        if entry.get("target") != target:
            raise ValueError(
                f"run log line {line_number} targets {entry.get('target')!r}, "
                f"expected {target!r}")
        finding_id = entry.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ValueError(
                f"run log line {line_number} has no non-empty string finding_id")
        if entry.get("decision") not in DECISIONS:
            raise ValueError(
                f"run log line {line_number} has invalid decision "
                f"{entry.get('decision')!r}; expected one of {sorted(DECISIONS)}")
        entries.append(entry)
    return entries


def log_list(repo: Path, target: str) -> list[dict]:
    """Latest decision per finding_id (later lines win)."""
    latest: dict[str, dict] = {}
    for entry in log_entries(repo, target):
        latest[entry["finding_id"]] = entry
    return sorted(
        latest.values(),
        key=lambda e: e.get("ts") if isinstance(e.get("ts"), str) else "")


# --------------------------------------------------------------------------- #
_MISSING = object()


def _raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


def _self_test() -> int:
    import tempfile
    ok = []
    globals()["_SELF_TEST_KHENRIX_ROOTS"] = set()

    def _mark_khenrix(root: Path) -> Path:
        """Explicitly inject a temporary khenrix checkout; production never shape-guesses."""
        _SELF_TEST_KHENRIX_ROOTS.add(root.resolve())
        return root

    def _init_repo(root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True,
                       capture_output=True)

    def _commit_fixture(root: Path, *paths: str, message: str = "fixture") -> None:
        """Put selected fixture source under real outer HEAD+index ownership."""
        subprocess.run(
            ["git", "-C", str(root), "add", "--", *(paths or (".",))],
            check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", message], check=True, capture_output=True)

    def _make_skill(root: Path, rel: str, *, template: bool = False) -> Path:
        path = root / rel
        path.mkdir(parents=True)
        manifest = "SKILL.md.tmpl" if template else "SKILL.md"
        (path / manifest).write_text("---\nname: test-skill\n---\n")
        return path

    # Rendered plugin copies deliberately have no operational checkout identity. Keep one
    # explicit registry alive for the whole hermetic test so their foreign-repo cases can
    # still exercise model/log behavior without shape-guessing a production checkout.
    self_test_registry_td = tempfile.TemporaryDirectory()
    self_test_registry = _mark_khenrix(Path(self_test_registry_td.name) / "registry")
    (self_test_registry / "shared" / "skills").mkdir(parents=True)
    (self_test_registry / "capabilities.toml").write_text("[models]\n")
    self_test_engine = self_test_registry / REVIEWER_ENGINE_RELPATH
    self_test_engine.parent.mkdir(parents=True)
    self_test_engine.write_text(
        "SENTINEL_PREFIX = 'SENTINEL-'\n"
        "MODES = {'normal': {}, 'deep': {}}\n"
        "def apply_member_note(prompt): return prompt\n"
        "def apply_readonly_posture(prompt): return prompt\n"
        "def apply_sentinel(prompt, token): return prompt\n")
    _init_repo(self_test_registry)
    _commit_fixture(
        self_test_registry, "capabilities.toml", REVIEWER_ENGINE_RELPATH,
        message="registry base")
    globals()["_SELF_TEST_REGISTRY_ROOT"] = self_test_registry

    # model regex: must-match and must-NOT-match shapes
    for s in ("claude-opus-4-8", "claude-fable-5", "claude-haiku-4-5-20251001",
              "gpt-5.5", "gpt-4o", "o4-mini", "o3-pro", "gemini-3.5-flash"):
        ok.append((f"regex matches {s}", bool(MODEL_RX.fullmatch(s))))
    for s in ("gpt_helper.py", "solo4-mini", "clock-opus-4", "audio2-track", "claude-code"):
        ok.append((f"regex ignores {s}", not MODEL_RX.search(s)))
    # approved-set tagging incl. dated-variant prefix rule
    approved = {"claude-opus-4-8", "claude-haiku-4-5"}
    ok.append(("exact id is current", tag_model("claude-opus-4-8", approved) == "current"))
    ok.append(("dated variant is current", tag_model("claude-haiku-4-5-20251001", approved) == "current"))
    ok.append(("unknown id is stale-candidate", tag_model("claude-opus-4-6", approved) == "stale-candidate"))
    ok.append(("no approved set -> found", tag_model("gpt-5.5", set()) == "found"))
    ok.append(("display label slugs to id", _slug("Gemini 3.5 Flash (High)") == "gemini-3.5-flash"))
    ok.append(("plain id survives slugging", _slug("claude-opus-4-8") == "claude-opus-4-8"))
    ok.append(("own self-test fixtures excluded from scans",
               bool(EXCLUDE_RX.search("shared/skills/skill-tuneup/scripts/tuneup.py"))
               and not EXCLUDE_RX.search("shared/skills/skill-tuneup/scripts/other.py")))
    # baseline subject filtering (newest-first)
    commits = [{"sha": "c1", "date": "2026-07-01", "subject": "chore: bump receipts"},
               {"sha": "c2", "date": "2026-06-20", "subject": "docs: fix typo"},
               {"sha": "c3", "date": "2026-06-01", "subject": "fix(llm-council): retry judge"}]
    picked = pick_baseline(commits)
    ok.append(("baseline skips chore/docs", picked["sha"] == "c3"))
    ok.append(("skips are countable", sum(1 for c in commits if c["date"] > picked["date"]) == 2))
    ok.append(("all-chore history falls back to newest",
               pick_baseline(commits[:2])["sha"] == "c1"))
    ok.append(("empty history -> None", pick_baseline([]) is None))
    # triage scoring: monotonic in each signal
    ok.append(("no-evals outranks fresh",
               triage_score("no-evals", 10, 0, 100) > triage_score("fresh", 10, 0, 100)))
    ok.append(("stale hits raise score",
               triage_score("fresh", 10, 3, 100) > triage_score("fresh", 10, 0, 100)))
    ok.append(("age raises score, capped",
               triage_score("fresh", 400, 0, 100) > triage_score("fresh", 30, 0, 100)
               and triage_score("fresh", 4000, 0, 100) == triage_score("fresh", 400, 0, 100)))
    ok.append(("near line-cap raises score",
               triage_score("fresh", 10, 0, 480) > triage_score("fresh", 10, 0, 100)))
    # skill_facts section slicing
    caps = ("[models]\nx = 1\n"
            "[skill_facts.khenrix-setup]\nroot = 'gpt-5.4'\n"
            "[skill_facts.khenrix-setup.claude]\nm = 'claude-opus-4-8'\n"
            "[skill_facts.khenrix-setup.codex.nested]\nn = 'gemini-3.5-flash'\n"
            "[skill_facts.khenrix-setup-extra.claude]\nm = 'claude-opus-4-9'\n"
            "[skill_facts.other.claude]\nm = 'gpt-5.5'\n")
    lines = _facts_lines(caps, "khenrix-setup")
    ok.append(("facts slice includes the exact bare section",
               any("gpt-5.4" in ln for _, ln in lines)))
    ok.append(("facts slice finds own section", any("claude-opus-4-8" in ln for _, ln in lines)))
    ok.append(("facts slice includes deep descendants",
               any("gemini-3.5-flash" in ln for _, ln in lines)))
    ok.append(("facts slice excludes prefix and unrelated sections",
               not any("claude-opus-4-9" in ln or "gpt-5.5" in ln for _, ln in lines)))
    # Two-tier targeting + the receipt gate. These branches shipped untested once and one of
    # them was DEAD (`provenance == "seed"` vs a producer writing "seeded: …"), so assert the
    # actual strings rather than trusting the shape.
    with tempfile.TemporaryDirectory() as td:
        r = _mark_khenrix(Path(td))
        policy_toml = (
            "[eval]\nrequired_providers=['codex','agy']\n"
            "judge='codex'\nmode='normal'\n")
        git_env = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
        git_env.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        })

        def _fixture_git(*args: str, date: str | None = None) -> str:
            env = git_env if date is None else {
                **git_env,
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
            }
            return subprocess.run(
                ["git", "-C", str(r), *args], capture_output=True, text=True,
                check=True, env=env).stdout

        _make_skill(r, "shared/skills/alpha")
        (r / "capabilities.toml").write_text("[models]\nclaude = []\n" + policy_toml)
        _fixture_git("init", "-q")
        _fixture_git("add", "-A")
        _fixture_git("commit", "-qm", "add alpha", date="2026-01-01T00:00:00+00:00")
        alpha_sha = _fixture_git("rev-parse", "HEAD").strip()
        nested_repo_arg = r / "shared"
        repo_commands = (
            ["baseline", "--repo", str(nested_repo_arg), "--skill", "alpha"],
            ["stale-models", "--repo", str(nested_repo_arg), "--skill", "alpha"],
            ["triage", "--repo", str(nested_repo_arg)],
            ["convergence-status", "--repo", str(nested_repo_arg), "--target", "alpha"],
            ["target-info", "--repo", str(nested_repo_arg), "--skill", "alpha"],
            ["verify-final-receipt", "--repo", str(nested_repo_arg),
             "--skill", "alpha"],
            ["review-material", "--repo", str(nested_repo_arg), "--skill", "alpha",
             "--target", "alpha"],
            ["review-diff", "--repo", str(nested_repo_arg), "--skill", "alpha",
             "--target", "alpha"],
            ["log", "list", "--repo", str(nested_repo_arg), "--target", "alpha"],
        )
        for command in repo_commands:
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                _rc = main(command)
            ok.append((f"CLI {command[0]} refuses a nested --repo path cleanly",
                       _rc == 2 and "exact git toplevel" in _buf.getvalue()
                       and str(r) in _buf.getvalue()
                       and "Traceback" not in _buf.getvalue()))
        # A template-shaped directory with the wrong manifest must not activate template
        # facts in baseline/model scans merely because the directory exists.
        _make_skill(r, "shared/skill-templates/alpha")
        (r / "capabilities.toml").write_text(
            "[models]\nclaude = []\n"
            + policy_toml +
            "[skill_facts.alpha.claude]\nmodel = 'claude-opus-4-6'\n")
        _fixture_git("add", "-A")
        _fixture_git("commit", "-qm", "add invalid template",
                     date="2026-02-01T00:00:00+00:00")
        _make_skill(r, "shared/skill-templates/templated", template=True)
        _fixture_git("add", "-A")
        _fixture_git("commit", "-qm", "add valid template",
                     date="2026-03-01T00:00:00+00:00")
        # Facts-only history is a separate commit: otherwise the template path itself
        # selects the same SHA and the baseline assertion cannot prove the -G branch ran.
        (r / "capabilities.toml").write_text(
            "[models]\nclaude = []\n"
            + policy_toml +
            "[skill_facts.alpha.claude]\nmodel = 'claude-opus-4-6'\n"
            "[skill_facts.templated.claude]\nmodel = 'gpt-5.5'\nnote = 'one'\n")
        _fixture_git("add", "capabilities.toml")
        _fixture_git("commit", "-qm", "add template facts",
                     date="2026-04-01T00:00:00+00:00")
        templated_sha = _fixture_git("rev-parse", "HEAD").strip()
        # A prefix sibling in a newer facts-only commit must not become this skill's
        # baseline or leak into its stale-model scan.
        with (r / "capabilities.toml").open("a") as handle:
            handle.write("[skill_facts.templated-extra.claude]\n"
                         "model = 'claude-opus-4-9'\n")
        _fixture_git("add", "capabilities.toml")
        _fixture_git("commit", "-qm", "add prefix sibling facts",
                     date="2026-05-01T00:00:00+00:00")
        ok.append(("template baseline ignores a newer prefix sibling",
                   baseline(r, "templated")["sha"] == templated_sha))
        # The table header is unchanged here. A regex pickaxe for the header misses this
        # normal edit shape, so the baseline must compare the exact semantic facts value.
        cap_path = r / "capabilities.toml"
        cap_path.write_text(cap_path.read_text().replace("note = 'one'", "note = 'two'"))
        _fixture_git("add", "capabilities.toml")
        _fixture_git("commit", "-qm", "change template fact value",
                     date="2026-06-01T00:00:00+00:00")
        templated_value_sha = _fixture_git("rev-parse", "HEAD").strip()
        for layout in (".agents/skills/beta", ".claude/skills/beta", "skills/beta"):
            _make_skill(r, layout)
        ti_a = target_info(r, "alpha")
        ok.append(("khenrix layout resolves to full-gate", ti_a["tier"] == "full-gate"))
        ok.append(("full-gate log target is unqualified", ti_a["log_target"] == "alpha"))
        ok.append(("invalid template shape cannot influence baseline",
                   baseline(r, "alpha")["sha"] == alpha_sha))
        ok.append(("invalid template shape cannot add skill-fact model hits",
                   not any(hit["file"] == "capabilities.toml"
                           for hit in scan_stale_models(r, "alpha", set()))))
        ok.append(("templated manifest resolves to full-gate",
                   target_info(r, "templated")["tier"] == "full-gate"))
        ok.append(("valid template includes its capabilities history",
                   baseline(r, "templated")["sha"] == templated_value_sha))
        ok.append(("valid template includes its skill-fact model hits",
                   any(hit["file"] == "capabilities.toml" and hit["id"] == "gpt-5.5"
                       for hit in scan_stale_models(r, "templated", set()))))
        ok.append(("template facts exclude a prefix sibling",
                   not any(hit["id"] == "claude-opus-4-9"
                           for hit in scan_stale_models(r, "templated", set()))))
        # capabilities.toml is source for every full-gate target and an additional facts
        # source for templates. Git histories the lexical symlink blob, while current scans
        # and render follow its referent, so working, internal, and dangling links must all
        # be refused before either source is read.
        caps_external = Path(td) / "external-capabilities.toml"
        caps_external.write_text("[models]\nsentinel = ['gpt-9.9']\n")
        for label in ("internal", "external", "dangling"):
            linked_repo = _mark_khenrix(Path(td) / f"caps-{label}")
            _make_skill(linked_repo, "shared/skills/alpha")
            _make_skill(linked_repo, "shared/skill-templates/templated", template=True)
            caps_link = linked_repo / "capabilities.toml"
            if label == "internal":
                owned = linked_repo / "config" / "capabilities.toml"
                owned.parent.mkdir()
                owned.write_text("[models]\ninternal = ['gpt-9.8']\n")
                caps_link.symlink_to(owned)
            elif label == "external":
                caps_link.symlink_to(caps_external)
            else:
                caps_link.symlink_to(linked_repo / "missing-capabilities.toml")
            _init_repo(linked_repo)
            _commit_fixture(
                linked_repo,
                "shared/skills/alpha/SKILL.md",
                "shared/skill-templates/templated/SKILL.md.tmpl")
            linked_info = target_info(linked_repo, "templated")
            ok.append((f"{label} capabilities link stays in khenrix tier and is refused",
                       linked_info["found"] and linked_info["ambiguous"]
                       and linked_info["tier"] == "full-gate"
                       and linked_info["source_refusals"]
                       and "capabilities.toml is symlink-backed"
                       in linked_info["source_refusals"][0]))
            for command in ("target-info", "baseline", "stale-models"):
                _buf = io.StringIO()
                with contextlib.redirect_stdout(_buf):
                    _rc = main([command, "--repo", str(linked_repo),
                                "--skill", "templated"])
                ok.append((f"{command} rejects {label} capabilities link cleanly",
                           _rc != 0
                           and "capabilities.toml is symlink-backed" in _buf.getvalue()
                           and "gpt-9.9" not in _buf.getvalue()))
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf):
                _rc = main(["triage", "--repo", str(linked_repo)])
            ok.append((f"triage rejects {label} capabilities link cleanly",
                       _rc != 0
                       and "capabilities.toml is symlink-backed" in _buf.getvalue()
                       and "gpt-9.9" not in _buf.getvalue()))
        missing_caps_repo = _mark_khenrix(Path(td) / "caps-missing")
        _make_skill(missing_caps_repo, "shared/skills/alpha")
        _init_repo(missing_caps_repo)
        _commit_fixture(missing_caps_repo, "shared/skills/alpha/SKILL.md")
        missing_caps_info = target_info(missing_caps_repo, "alpha")
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            missing_caps_rc = main(
                ["target-info", "--repo", str(missing_caps_repo), "--skill", "alpha"])
        ok.append(("an exact khenrix checkout with missing capabilities cannot downgrade",
                   missing_caps_info["khenrix_repo"]
                   and missing_caps_info["tier"] == "full-gate"
                   and missing_caps_info["source_refusals"]
                   and missing_caps_rc == 2 and "capabilities.toml is missing"
                   in _buf.getvalue()))
        # Repo/layout roots can themselves be dangling links. A leaf below a dangling
        # ancestor does not lexist, so classification must retain the ancestor link instead
        # of silently selecting a usable shadow from another layout.
        linked_root_repo = _mark_khenrix(Path(td) / "linked-khenrix-root")
        linked_root_repo.mkdir()
        (linked_root_repo / "capabilities.toml").write_text("[models]\n")
        (linked_root_repo / "shared").mkdir()
        (linked_root_repo / "shared" / "skills").symlink_to(
            linked_root_repo / "missing-shared-skills", target_is_directory=True)
        _make_skill(linked_root_repo, "skills/shadow")
        _init_repo(linked_root_repo)
        linked_root_info = target_info(linked_root_repo, "shadow")
        ok.append(("a dangling khenrix skill root cannot downgrade the repo tier",
                   linked_root_info["khenrix_repo"]
                   and linked_root_info["linked_near_misses"]
                   and "skills/shadow" not in linked_root_info["paths"]))
        foreign_root_repo = Path(td) / "linked-foreign-root"
        (foreign_root_repo / ".agents").mkdir(parents=True)
        (foreign_root_repo / ".agents" / "skills").symlink_to(
            foreign_root_repo / "missing-agents-skills", target_is_directory=True)
        _make_skill(foreign_root_repo, "skills/shadow")
        _init_repo(foreign_root_repo)
        for label, linked_repo in (("khenrix", linked_root_repo),
                                   ("foreign", foreign_root_repo)):
            for command in ("target-info", "baseline", "stale-models",
                            "verify-final-receipt"):
                _buf = io.StringIO()
                with contextlib.redirect_stdout(_buf):
                    _rc = main([command, "--repo", str(linked_repo),
                                "--skill", "shadow"])
                ok.append((f"{command} rejects a dangling {label} layout root",
                           _rc == 2 and "symlink-backed" in _buf.getvalue()
                           and "Traceback" not in _buf.getvalue()))
        # Stray foreign copies INSIDE khenrix-utils must not resolve — layouts are chosen
        # by repo kind, so the real source can never be shadowed by a generic one.
        ok.append(("all foreign layouts are ignored inside a khenrix repo",
                   target_info(r, "beta")["found"] is False))
        ok.append(("source checkout identity is exact or absent in a rendered bundle",
                   _ENGINE_KHENRIX_ROOT is None
                   or is_khenrix_repo(_ENGINE_KHENRIX_ROOT)))
        # Shape is not identity. An ordinary project may legitimately have both paths;
        # its .agents skill must remain visible as a council-only target.
        ordinary = Path(td) / "ordinary-shaped-project"
        (ordinary / "capabilities.toml").parent.mkdir(parents=True)
        (ordinary / "capabilities.toml").write_text("[project]\nname = 'ordinary'\n")
        _make_skill(ordinary, "shared/skills/unrelated")
        _make_skill(ordinary, ".agents/skills/wanted")
        _init_repo(ordinary)
        _commit_fixture(ordinary, ".agents/skills/wanted/SKILL.md")
        ordinary_info = target_info(ordinary, "wanted")
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            ordinary_rc = main(
                ["target-info", "--repo", str(ordinary), "--skill", "wanted"])
        ok.append(("an ordinary shared/skills + capabilities shape stays foreign",
                   not ordinary_info["khenrix_repo"]
                   and ordinary_info["tier"] == "council-only"
                   and ordinary_info["paths"] == [".agents/skills/wanted"]
                   and ordinary_rc == 0 and "council-only" in _buf.getvalue()))
        # same basename, different roots — the collision an unhashed key would merge
        c1, c2 = Path(td) / "a" / "dup", Path(td) / "b" / "dup"
        _make_skill(c1, "skills/x")
        _make_skill(c2, "skills/x")
        ok.append(("same-basename repos get distinct log keys",
                   log_target_key(c1, "x") != log_target_key(c2, "x")))
        ok.append(("unqualified key for a foreign repo is refused",
                   _raises(lambda: _check_log_key(c1, "x"), ValueError)))
        ok.append(("qualified key for a foreign repo is accepted",
                   _check_log_key(c1, log_target_key(c1, "x")) is None))
        for wrong_key in (
            "other@deadbeef:x",
            f"{c1.name}@deadbeef:x",
            log_target_key(c2, "x"),
            f"junk:{log_target_key(c1, 'x')}",
        ):
            ok.append((f"foreign log key {wrong_key!r} is repository-bound",
                       _raises(lambda key=wrong_key: _check_log_key(c1, key), ValueError)))
        ok.append(("run log resolves into the khenrix checkout, not the target repo",
                   is_khenrix_repo(log_path(c1, log_target_key(c1, "x")).parents[3])))
        ok.append(("missing skill reports not-found", target_info(r, "nope")["found"] is False))
        f = Path(td) / "foreign"
        _make_skill(f, "skills/gamma")
        _make_skill(f, ".claude/skills/delta")
        _make_skill(f, ".agents/skills/epsilon")
        (f / ".agents" / "skills" / "empty").mkdir(parents=True)
        _make_skill(f, ".agents/skills/template-only", template=True)
        external = Path(td) / "external-skill"
        _make_skill(external, "source")
        canonical = _make_skill(f, "canonical-source")
        (f / ".agents" / "skills" / "linked-out").symlink_to(
            external / "source", target_is_directory=True)
        (f / ".agents" / "skills" / "linked-in").symlink_to(
            canonical, target_is_directory=True)
        linked_manifest = f / "skills" / "linked-manifest"
        linked_manifest.mkdir(parents=True)
        (linked_manifest / "SKILL.md").symlink_to(canonical / "SKILL.md")
        nested_link = _make_skill(f, ".agents/skills/nested-linked")
        (nested_link / "references").mkdir()
        outside_note = external / "outside.md"
        outside_note.write_text("model = 'gpt-9.9'\n")
        (nested_link / "references" / "outside.md").symlink_to(outside_note)
        _make_skill(f, "skills/shadowed")
        (f / ".agents" / "skills" / "shadowed").symlink_to(
            canonical, target_is_directory=True)
        # Dangling links are still links even though Path.is_dir()/is_file() return false.
        # Each form must remain visible both alone and beside an otherwise valid layout.
        (f / ".agents" / "skills" / "dangling-dir").symlink_to(
            f / "does-not-exist", target_is_directory=True)
        dangling_manifest = f / ".claude" / "skills" / "dangling-manifest"
        dangling_manifest.mkdir(parents=True)
        (dangling_manifest / "SKILL.md").symlink_to(f / "missing-SKILL.md")
        _make_skill(f, "skills/dangling-shadow-dir")
        (f / ".agents" / "skills" / "dangling-shadow-dir").symlink_to(
            f / "missing-shadow", target_is_directory=True)
        _make_skill(f, "skills/dangling-shadow-manifest")
        dangling_shadow_manifest = f / ".claude" / "skills" / "dangling-shadow-manifest"
        dangling_shadow_manifest.mkdir(parents=True)
        (dangling_shadow_manifest / "SKILL.md").symlink_to(f / "missing-shadow-SKILL.md")
        _make_skill(f, ".agents/skills/three-layouts")
        _make_skill(f, ".claude/skills/three-layouts")
        (f / "skills" / "three-layouts").mkdir(parents=True)
        _init_repo(f)
        _commit_fixture(f)
        nested_boundary = _make_skill(f, ".agents/skills/nested-git")
        (nested_boundary / "component").mkdir()
        (nested_boundary / "component" / ".git").write_text(
            "gitdir: /outside/not-owned\n")
        gitlink_skill = _make_skill(f, ".agents/skills/indexed-gitlink")
        gitlink_component = gitlink_skill / "component"
        gitlink_component.mkdir()
        _init_repo(gitlink_component)
        (gitlink_component / "owned-by-inner.txt").write_text("inner\n")
        subprocess.run(["git", "-C", str(gitlink_component), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(
            ["git", "-C", str(gitlink_component), "-c", "user.email=t@t",
             "-c", "user.name=t", "commit", "-qm", "inner"],
            check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(f), "add", ".agents/skills/indexed-gitlink"],
            check=True, capture_output=True)
        # Prove the outer index's 160000 mode is independently refused, even after the
        # worktree marker disappears (the deinitialized-gitlink shape).
        shutil.rmtree(gitlink_component / ".git")
        ti_g, ti_d, ti_e = (target_info(f, name)
                            for name in ("gamma", "delta", "epsilon"))
        ok.append(("skills/<n> in a non-khenrix repo is council-only",
                   ti_g["tier"] == "council-only"))
        ok.append((".claude/skills/<n> in a non-khenrix repo also resolves",
                   ti_d["found"] and ti_d["tier"] == "council-only"))
        ok.append((".agents/skills/<n> in a non-khenrix repo also resolves",
                   ti_e["paths"] == [".agents/skills/epsilon"]
                   and ti_e["tier"] == "council-only"))
        ok.append(("receipt function refuses a council-only target",
                   _raises(lambda: verify_final_receipt(
                       f, "gamma", list(FINAL_PANEL)), ValueError)))
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _rc = main(["verify-final-receipt", "--repo", str(f), "--skill", "gamma"])
        ok.append(("CLI receipt gate explains that council-only has no khenrix receipt",
                   _rc == 2 and "council-only" in _buf.getvalue()
                   and "no khenrix receipt gate" in _buf.getvalue()
                   and "Traceback" not in _buf.getvalue()))
        for skill, boundary_label in (("nested-git", ".git boundary"),
                                      ("indexed-gitlink", "gitlink")):
            boundary_info = target_info(f, skill)
            ok.append((f"{skill} is refused as a nested Git ownership boundary",
                       not boundary_info["found"]
                       and boundary_info["git_boundary_near_misses"]
                       and boundary_label
                       in boundary_info["git_boundary_near_misses"][0]))
            for command in ("target-info", "baseline", "stale-models",
                            "verify-final-receipt"):
                _buf = io.StringIO()
                with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                    _rc = main([command, "--repo", str(f), "--skill", skill])
                ok.append((f"CLI {command} refuses {skill} before reading inner contents",
                           _rc == 2 and "outer repository commit does not own"
                           in _buf.getvalue() and "Traceback" not in _buf.getvalue()))
        ok.append(("bare foreign directory is not a skill",
                   target_info(f, "empty")["found"] is False))
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _rc = main(["target-info", "--repo", str(f), "--skill", "empty"])
        ok.append(("missing manifest diagnostic names the existing directory and manifest",
                   _rc == 2
                   and ".agents/skills/empty exists but has no SKILL.md" in _buf.getvalue()))
        ok.append(("foreign template-only directory is not a skill",
                   target_info(f, "template-only")["found"] is False))
        for linked in ("linked-out", "linked-in", "linked-manifest"):
            linked_info = target_info(f, linked)
            ok.append((f"symlink-backed foreign skill {linked} is refused",
                       not linked_info["found"]
                       and any("symlink-backed" in miss
                               for miss in linked_info["near_misses"])))
        nested_info = target_info(f, "nested-linked")
        ok.append(("a linked descendant refuses the whole foreign skill",
                   not nested_info["found"]
                   and any("references/outside.md" in miss
                           for miss in nested_info["near_misses"])))
        ok.append(("stale-model scanning cannot dereference a linked descendant",
                   _raises(lambda: scan_stale_models(f, "nested-linked", set()),
                           ValueError)))
        shadowed_info = target_info(f, "shadowed")
        ok.append(("a linked duplicate cannot hide behind one valid layout",
                   shadowed_info["found"] and shadowed_info["ambiguous"]
                   and shadowed_info["paths"] == ["skills/shadowed"]))
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _rc = main(["target-info", "--repo", str(f), "--skill", "shadowed"])
        ok.append(("linked-plus-valid ambiguity is refused with both candidates named",
                   _rc == 2 and "skills/shadowed" in _buf.getvalue()
                   and ".agents/skills/shadowed is symlink-backed" in _buf.getvalue()))
        for linked in ("dangling-dir", "dangling-manifest"):
            linked_info = target_info(f, linked)
            ok.append((f"standalone dangling candidate {linked} is refused",
                       not linked_info["found"]
                       and any("symlink-backed" in miss
                               for miss in linked_info["linked_near_misses"])))
        for shadow in ("dangling-shadow-dir", "dangling-shadow-manifest"):
            shadow_info = target_info(f, shadow)
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf):
                _rc = main(["target-info", "--repo", str(f), "--skill", shadow])
            ok.append((f"valid plus dangling candidate {shadow} is ambiguous",
                       shadow_info["found"] and shadow_info["ambiguous"] and _rc == 2
                       and f"skills/{shadow}" in _buf.getvalue()
                       and "symlink-backed" in _buf.getvalue()))
        for linked in ("linked-out", "linked-in", "linked-manifest", "nested-linked",
                       "dangling-dir", "dangling-manifest", "shadowed",
                       "dangling-shadow-dir", "dangling-shadow-manifest"):
            for command in ("baseline", "stale-models"):
                _buf = io.StringIO()
                with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                    _rc = main([command, "--repo", str(f), "--skill", linked])
                ok.append((f"{command} refuses linked candidate {linked} without traceback",
                           _rc == 2 and "symlink-backed" in _buf.getvalue()
                           and "Traceback" not in _buf.getvalue()))
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _rc = main(["target-info", "--repo", str(f), "--skill", "three-layouts"])
        _lines = _buf.getvalue().splitlines()
        _matched = _lines[0].split(": ", 1)[1].split(" — refusing", 1)[0].split(", ")
        ok.append(("manifest-less third layout is not described as a match",
                   _rc == 2 and "skills/three-layouts" not in _matched))
        ok.append(("path-like skill name is rejected before layout interpolation",
                   _raises(lambda: target_info(f, "../../outside"), ValueError)))
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _rc = main(["target-info", "--repo", str(f), "--skill", "../../outside"])
        ok.append(("CLI rejects path-like skill name cleanly",
                   _rc == 2 and "invalid skill name" in _buf.getvalue()))
        for command in ("target-info", "baseline", "stale-models", "verify-final-receipt"):
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf):
                _rc = main([command, "--repo", str(f), "--skill", ""])
            ok.append((f"{command} rejects an explicitly empty skill",
                       _rc == 2 and "invalid skill name" in _buf.getvalue()))
        ok.append(("foreign log target is repo-qualified",
                   bool(re.match(r"foreign@[0-9a-f]{8}:gamma$", ti_g["log_target"]))))
        triage_linked = _make_skill(r, "shared/skills/triage-linked")
        (triage_linked / "references").mkdir()
        (triage_linked / "references" / "outside.md").symlink_to(outside_note)
        triage_dangling = r / "shared" / "skills" / "triage-dangling"
        triage_dangling.mkdir(parents=True)
        (triage_dangling / "SKILL.md").symlink_to(r / "missing-triage-SKILL.md")
        _make_skill(r, "shared/skills/triage-shadow")
        (r / "shared" / "skill-templates" / "triage-shadow").symlink_to(
            external / "source", target_is_directory=True)
        try:
            triage(r)
            triage_safe = False
        except ValueError as ex:
            triage_safe = (
                "target-info refuses" in str(ex)
                and all(name in str(ex) for name in (
                    "triage-linked", "triage-dangling", "triage-shadow")))
        except (FileNotFoundError, OSError):
            triage_safe = False
        ok.append(("triage reports every symlink-backed target-info refusal",
                   triage_safe))
        # verify_final_receipt: assert on the REAL producer strings
        def _receipt_eval(eval_id):
            return {"id": eval_id, "name": f"case-{eval_id}", "prompt": "fixture",
                    "assertions": ["fixture"], "files": []}

        ev = r / "evals" / "alpha"
        ev.mkdir(parents=True)
        ev.joinpath("evals.json").write_text(json.dumps(
            {"evals": [_receipt_eval(i) for i in range(7)]}))
        _chk = _load_checks()

        def _current_receipt(**overrides):
            rec = {
                "schema_version": _chk.CURRENT_RECEIPT_SCHEMA,
                "skill": "alpha",
                "providers": list(FINAL_PANEL),
                "judge": "codex",
                "mode": "normal",
                "models": {"judge": "gpt-test"},
                "per_provider": {
                    provider: {"delta": 0.1, "n_evals": 7, "status": "ok"}
                    for provider in FINAL_PANEL
                },
                "provenance": "eval",
                "certified_by": "delta-gate",
                "source_hash": _chk.source_hash(r, "alpha"),
                "eval_set_hash": _chk.eval_set_hash(r, "alpha"),
                "eval_policy_hash": _chk.eval_policy_hash(r),
            }
            rec.update(overrides)
            return rec

        def _probs(rec):
            ev.joinpath("receipt.json").write_text(json.dumps(rec))
            return " ".join(verify_final_receipt(r, "alpha", list(FINAL_PANEL)))
        ok.append(("seeded receipt is rejected",
                   "seeded, not earned" in _probs(
                       _current_receipt(
                           provenance="seeded: blessed current committed state"))))
        ok.append(("schema-3 single-provider receipt is rejected by Codex+agy policy",
                   "provider order" in _probs(
                       _current_receipt(
                           providers=["codex"],
                           per_provider={
                               "codex": {"delta": 0.1, "n_evals": 7, "status": "ok"}
                           }))))
        ok.append(("ordinary receipt cannot claim a self-test panel exemption",
                   "not eligible" in _probs(
                       _current_receipt(
                           self_test=True, certified_by="some --self-test"))))
        _make_skill(r, "shared/skills/zeta")
        _fixture_git("add", "shared/skills/zeta/SKILL.md")
        _fixture_git("commit", "-qm", "add zeta",
                     date="2026-07-01T00:00:00+00:00")
        ok.append(("missing receipt is reported",
                   "no receipt" in " ".join(
                       verify_final_receipt(r, "zeta", list(FINAL_PANEL)))))
        ok.append(("receipt function refuses a narrowed final panel",
                   _raises(lambda: verify_final_receipt(r, "alpha", ["claude"]),
                           ValueError)))
        # THE SUCCESS LINE IS THE BEHAVIOUR, so assert it through the CLI. The checks above
        # call verify_final_receipt() directly and only ever inspect PROBLEMS, so the
        # message printed when there are none was unpinned: this command spent a long time
        # announcing "receipt is full-panel" for the very skills it EXEMPTS from the panel
        # check, and reverting to that wording would still leave every gate green. Only
        # main() chooses the wording, so only a CLI-level run can witness it.
        ok.append(("receipt validator dynamic import supports registered dataclasses",
                   hasattr(_chk, "EvalInputSnapshot")))

        def _invoke_receipt_cli(
            rec, skill: str = "alpha", repo: Path = r
        ) -> tuple[int, str]:
            receipt_dir = repo / "evals" / skill
            receipt_dir.mkdir(parents=True, exist_ok=True)
            receipt_dir.joinpath("receipt.json").write_text(json.dumps(rec))
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                rc = main(
                    ["verify-final-receipt", "--repo", str(repo), "--skill", skill])
            return rc, output.getvalue()

        def _say(extra: dict, per_provider: dict | None = None,
                 schema_version: int | None = _chk.CURRENT_RECEIPT_SCHEMA) -> tuple[int, str]:
            complete = {provider: {"delta": 0.1, "n_evals": 7, "status": "ok"}
                        for provider in FINAL_PANEL}
            rec = {"skill": "alpha", "providers": list(FINAL_PANEL),
                   "provenance": "eval", "certified_by": "delta-gate",
                   "per_provider": complete if per_provider is None else per_provider,
                   "judge": "codex", "mode": "normal",
                   "models": {"codex": "gpt-test", "agy": "gemini-test",
                              "judge": "gpt-test"},
                   "eval_policy_hash": _chk.eval_policy_hash(r),
                   "source_hash": _chk.source_hash(r, "alpha"),
                   "eval_set_hash": _chk.eval_set_hash(r, "alpha"), **extra}
            if schema_version is not None:
                rec["schema_version"] = schema_version
            return _invoke_receipt_cli(rec)
        _rc_partial, _out_partial = _say({}, {"claude": {}})
        _rc_panel, _out_panel = _say({})
        deterministic_skill = "khenrix-wiki-add"
        with tempfile.TemporaryDirectory() as deterministic_td:
            deterministic_root = _mark_khenrix(Path(deterministic_td))
            _make_skill(
                deterministic_root, f"shared/skills/{deterministic_skill}")
            (deterministic_root / "capabilities.toml").write_text(
                "[models]\n" + policy_toml)
            _init_repo(deterministic_root)
            _commit_fixture(deterministic_root)
            deterministic_evals = deterministic_root / "evals" / deterministic_skill
            deterministic_evals.mkdir(parents=True)
            deterministic_evals.joinpath("evals.json").write_text(json.dumps(
                {"evals": [_receipt_eval(i) for i in range(7)]}))
            deterministic_receipt = {
                "schema_version": _chk.CURRENT_RECEIPT_SCHEMA,
                "skill": deterministic_skill,
                "providers": list(FINAL_PANEL),
                "judge": "codex",
                "mode": "normal",
                "models": {"codex": "gpt-test", "agy": "gemini-test",
                           "judge": "gpt-test"},
                "per_provider": {
                    provider: {"delta": 0.0, "n_evals": 7, "status": "ok"}
                    for provider in FINAL_PANEL},
                "provenance": "eval",
                "self_test": True,
                "certified_by": "wikisync-unittests",
                "deterministic_gate": "wikisync-unittests",
                "gate_command": _chk.deterministic_gate_command(
                    deterministic_root, deterministic_skill),
                "gate_tree_hash": _chk.gate_tree_snapshot(
                    deterministic_root).gate_tree_hash,
                "gate_counts": {"tests_run": 1, "skipped": 0, "failed": 0},
                "source_hash": _chk.source_hash(
                    deterministic_root, deterministic_skill),
                "eval_set_hash": _chk.eval_set_hash(
                    deterministic_root, deterministic_skill),
                "eval_policy_hash": _chk.eval_policy_hash(deterministic_root),
            }
            _rc_self, _out_self = _invoke_receipt_cli(
                deterministic_receipt, deterministic_skill, deterministic_root)
        _rc_v1, _out_v1 = _say({}, {}, schema_version=None)
        _rc_blind, _out_blind = _say(
            {"providers": ["codex"], "blind_winner": "n/a-deterministic"},
            {"codex": {"delta": 0.1, "n_evals": 7, "status": "ok"}})
        _say({})
        _narrowed_output = io.StringIO()
        with contextlib.redirect_stdout(_narrowed_output):
            _narrowed_rc = main(
                ["verify-final-receipt", "--repo", str(r), "--skill", "alpha",
                 "--panel", "claude"])
        ok.append(("CLI: a partial per-provider block cannot prove a full panel",
                   _rc_partial == 1 and "no completed codex evidence"
                   in _out_partial))
        for label, bad_delta in (("NaN", float("nan")),
                                 ("infinite", float("inf")),
                                 ("out-of-range", 999.0)):
            rows = {provider: {"delta": 0.1, "n_evals": 7, "status": "ok"}
                    for provider in FINAL_PANEL}
            rows["codex"]["delta"] = bad_delta
            _rc_bad, _out_bad = _say({}, rows)
            ok.append((f"CLI: {label} provider delta cannot prove a full panel",
                       _rc_bad == 1 and "no completed codex evidence"
                       in _out_bad))
        for provider, bad_count in (("codex", 8), ("agy", 10 ** 21)):
            rows = {name: {"delta": 0.1, "n_evals": 7, "status": "ok"}
                    for name in FINAL_PANEL}
            rows[provider]["n_evals"] = bad_count
            _rc_bad, _out_bad = _say({}, rows)
            ok.append((f"CLI: provider {provider} must report all 7 distinct eval ids",
                       _rc_bad == 1 and provider in _out_bad
                       and "all 7 current evals" in _out_bad))
        ok.append(("CLI: manifest-matching n_evals=7 proves the canonical panel",
                   _rc_panel == 0 and "canonical-panel and matches source" in _out_panel))
        ok.append(("CLI: deterministic receipt proves both authorities",
                   _rc_self == 0
                   and "deterministic-certifier + canonical-panel" in _out_self))
        ok.append(("CLI: a v1 receipt cannot bypass per-provider eval counts",
                   _rc_v1 == 1 and "legacy schema v1" in _out_v1))
        ok.append(("CLI: n/a blind_winner without self_test cannot bypass the panel",
                   _rc_blind == 1 and "provider order" in _out_blind
                   and "Traceback" not in _out_blind))
        ok.append(("CLI: --panel cannot narrow the canonical final panel",
                   _narrowed_rc == 2 and "at codex,agy"
                   in _narrowed_output.getvalue()
                   and "Traceback" not in _narrowed_output.getvalue()))
        for malformed_root in ([], "x", None):
            _rc_bad, _out_bad = _invoke_receipt_cli(malformed_root)
            ok.append((f"CLI: {type(malformed_root).__name__} receipt root is cleanly refused",
                       _rc_bad == 1 and "root must be a JSON object" in _out_bad
                       and "Traceback" not in _out_bad))
        for label, malformed_providers in (
            ("mapping", {"claude": True}),
            ("unhashable", ["claude", ["agy"]]),
        ):
            _rc_bad, _out_bad = _say({"providers": malformed_providers})
            ok.append((f"CLI: {label} providers are cleanly refused",
                       _rc_bad == 1 and "providers must be a list" in _out_bad
                       and "Traceback" not in _out_bad))
        for bad_version in (True, False, 0, 1, -1):
            _rc_bad, _out_bad = _say({}, schema_version=bad_version)
            ok.append((f"CLI: explicit schema_version={bad_version!r} is refused",
                       _rc_bad == 1 and "schema_version" in _out_bad
                       and "Traceback" not in _out_bad))
        _say({})
        _valid_receipt = json.loads(ev.joinpath("receipt.json").read_text())
        ev.joinpath("evals.json").write_text(json.dumps(
            {"evals": [_receipt_eval(1), _receipt_eval("1")]}))
        _rc_collision, _out_collision = _invoke_receipt_cli(_valid_receipt)
        ok.append(("CLI: rendered duplicate eval ids cannot prove the final gate",
                   _rc_collision == 1 and "duplicate rendered eval id" in _out_collision
                   and "Traceback" not in _out_collision))
        ev.joinpath("evals.json").write_text(json.dumps(
            {"evals": [_receipt_eval(i) for i in range(7)]}))
    # severity-gated convergence: the rule that replaced the fixed cycle cap
    def _hist(counts, tail=0):
        e = [{"finding_id": RUN_START, "decision": "applied"}]
        for c in counts:
            e += [{"finding_id": f"f{i}", "decision": "applied", "severity": "serious"}
                  for i in range(c)]
            e.append({"finding_id": CYCLE_END, "decision": "applied",
                      "cycle": len([x for x in e if x["finding_id"] == CYCLE_END]) + 1})
        e += [{"finding_id": f"t{i}", "decision": "applied", "severity": "serious"}
              for i in range(tail)]
        return e
    for label, terminal in (
        ("converged", {"finding_id": RUN_END, "decision": "applied",
                       "converged": True, "cycles": 2}),
        ("stalled", {"finding_id": RUN_END, "decision": "deferred",
                     "converged": False, "cycles": 2}),
        ("reader-only legacy stall", {"finding_id": RUN_END,
                                      "decision": "deferred"}),
    ):
        ok.append((f"{label} run-convergence is a trusted reader boundary",
                   _run_end_problem(terminal, allow_legacy=True) is None))
    ok.append(("legacy deferred run-convergence is refused for new writes",
               _run_end_problem(
                   {"finding_id": RUN_END, "decision": "deferred"},
                   allow_legacy=False) is not None))
    malformed_terminals = (
        ("missing fields", {"finding_id": RUN_END, "decision": "applied"}),
        ("contradictory applied", {"finding_id": RUN_END, "decision": "applied",
                                   "converged": False, "cycles": 1}),
        ("contradictory deferred", {"finding_id": RUN_END, "decision": "deferred",
                                    "converged": True, "cycles": 1}),
        ("rejected", {"finding_id": RUN_END, "decision": "rejected",
                      "converged": False, "cycles": 1}),
        ("boolean cycles", {"finding_id": RUN_END, "decision": "applied",
                            "converged": True, "cycles": True}),
        ("negative cycles", {"finding_id": RUN_END, "decision": "applied",
                             "converged": True, "cycles": -1}),
    )
    for label, terminal in malformed_terminals:
        status = convergence_status([
            {"finding_id": RUN_START, "decision": "applied"},
            {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
            terminal,
            {"finding_id": "after-terminal", "decision": "applied",
             "severity": "serious"},
            {"finding_id": CYCLE_END, "decision": "applied", "cycle": 2},
        ])
        ok.append((f"malformed {label} run-convergence is serious and never delimits",
                   status["counts"] == [0, 2] and status["converged"] is False))
    rollover_after_malformed_terminal = convergence_status([
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "a"},
        {"target": "x", "finding_id": RUN_END, "decision": "applied",
         "converged": False, "cycles": 1},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "b"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
    ])
    ok.append(("a later run cannot inherit or erase malformed terminal debt",
               rollover_after_malformed_terminal["converged"] is False
               and rollover_after_malformed_terminal["run_gap"] is not None
               and rollover_after_malformed_terminal["run_gap"]["applied_records"][0]
                   ["finding_id"] == RUN_END))
    ok.append(("work appended after a completed run WARNS and blocks convergence",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                    {"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
                    {"finding_id": "f", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 2}]
               )["converged"] is False))
    for decision in ("deferred", "rejected"):
        _post_terminal_cycle = convergence_status(
            [{"finding_id": RUN_START, "decision": "applied"},
             {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
             {"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
             {"finding_id": CYCLE_END, "decision": decision, "cycle": 2}])
        ok.append((f"a post-terminal {decision} cycle-end warns and blocks convergence",
                   _post_terminal_cycle["converged"] is False
                   and any(CYCLE_END in warning
                           for warning in _post_terminal_cycle["warnings"])))
    _post_terminal_malformed_cycle = convergence_status(
        [{"finding_id": RUN_START, "decision": "applied"},
         {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
         {"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
         {"finding_id": CYCLE_END, "decision": "applied"}])
    ok.append(("a post-terminal malformed cycle-end warns and blocks convergence",
               _post_terminal_malformed_cycle["converged"] is False
               and any(CYCLE_END in warning
                       for warning in _post_terminal_malformed_cycle["warnings"])))
    for decision in ("deferred", "rejected"):
        _post_terminal_start = convergence_status(
            [{"finding_id": RUN_START, "decision": "applied"},
             {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
             {"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
             {"finding_id": RUN_START, "decision": decision}])
        ok.append((f"a post-terminal {decision} run-start warns and blocks convergence",
                   _post_terminal_start["converged"] is False
                   and any(RUN_START in warning
                           for warning in _post_terminal_start["warnings"])))
    for decision in ("applied", "deferred", "rejected"):
        _duplicate_terminal = convergence_status(
            [{"finding_id": RUN_START, "decision": "applied"},
             {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
             {"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
             {"finding_id": RUN_END, "decision": decision}])
        ok.append((f"a post-terminal {decision} run-convergence warns and blocks",
                   _duplicate_terminal["converged"] is False
                   and any(RUN_END in warning
                           for warning in _duplicate_terminal["warnings"])))
    ok.append(("a properly closed run followed by a new one still converges",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                    {"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
                    {"finding_id": RUN_START, "decision": "applied"},
                   {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["converged"] is True))
    for malformed_decision in ("rejected", "deferred"):
        malformed_current = convergence_status(
            [{"finding_id": RUN_START, "decision": "applied"},
             {"finding_id": RUN_START, "decision": malformed_decision},
             {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}])
        ok.append((f"a persisted {malformed_decision} run-start is serious, not structural",
                   malformed_current["converged"] is False
                   and malformed_current["counts"] == [1]))
        malformed_rollover = convergence_status(
            [{"finding_id": RUN_START, "decision": "applied"},
             {"finding_id": RUN_START, "decision": malformed_decision},
             {"finding_id": RUN_START, "decision": "applied"},
             {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}])
        ok.append((f"a later valid start cannot erase a {malformed_decision} start",
                   malformed_rollover["converged"] is False
                   and bool(malformed_rollover["warnings"])))
    ok.append(("a log with no valid applied run-start is refused",
               _raises(lambda: convergence_status(
                   [{"finding_id": RUN_START, "decision": "rejected"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]),
                       ValueError)))
    ok.append(("a non-object log record is refused without an attribute crash",
               _raises(lambda: convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"}, None]), ValueError)))
    ok.append(("a clean final cycle converges",
               convergence_status(_hist([3, 1, 0]))["verdict"] == "converged"))
    ok.append(("a declining rate keeps iterating",
               convergence_status(_hist([6, 3, 1]))["verdict"] == "keep-iterating"))
    ok.append(("a flat/rising rate stalls and hands over",
               convergence_status(_hist([2, 2, 3]))["verdict"].startswith("stalled")))
    ok.append(("minor findings do not block convergence",
               convergence_status(
                   _hist([2]) + [{"finding_id": "m", "decision": "applied", "severity": "minor"},
                                 {"finding_id": CYCLE_END, "decision": "applied", "cycle": 2}]
               )["verdict"] == "converged"))
    ok.append(("an unsevered applied finding counts as serious (fail closed)",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "x", "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["verdict"] == "keep-iterating"))
    ok.append(("deferred findings never block convergence",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "d", "decision": "deferred", "severity": "blocking"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["verdict"] == "converged"))
    ok.append(("an oscillating rate stalls (the infinite loop the old rule allowed)",
               convergence_status(_hist([2, 1, 2, 1, 2, 1]))["verdict"].startswith("stalled")))
    # A SECOND run-start inside one run (the natural resume point) used to drop that run's
    # findings and report converged — but only on a target with a completed run behind it,
    # so every real target was in the unguarded regime and the self-test's first-run
    # fixtures never saw it. Assert on the shape that actually shipped.
    _closed_run = [{"finding_id": RUN_START, "decision": "applied"},
                   {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                   {"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1}]

    def _terminal_rollover(prior: list, inter_run: list | None = None) -> dict:
        return convergence_status(
            [{"target": "x", "finding_id": RUN_START, "decision": "applied",
              "ts": "terminal-a"}]
            + prior
            + [{"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1}]
            + (inter_run or [])
            + [{"target": "x", "finding_id": RUN_START, "decision": "applied",
                "ts": "terminal-b"},
               {"target": "x", "finding_id": CYCLE_END, "decision": "applied",
                "cycle": 1}])

    _pre_terminal_open = _terminal_rollover([
        {"target": "x", "finding_id": "pre-terminal-f", "decision": "applied",
         "severity": "blocking"}])
    ok.append(("a terminal cannot account for an open pre-terminal finding",
               _pre_terminal_open["converged"] is False
               and [record["finding_id"] for record
                    in _pre_terminal_open["run_gap"]["applied_records"]]
               == ["pre-terminal-f"]))
    _pre_terminal_after_cycle = _terminal_rollover([
        {"target": "x", "finding_id": "counted", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
        {"target": "x", "finding_id": "pre-terminal-g", "decision": "applied",
         "severity": "blocking"},
    ])
    ok.append(("only the open tail after the last cycle survives a terminal rollover",
               _pre_terminal_after_cycle["converged"] is False
               and [record["finding_id"] for record
                    in _pre_terminal_after_cycle["run_gap"]["applied_records"]]
               == ["pre-terminal-g"]))
    _post_terminal_bookkeeping = _terminal_rollover([
        {"target": "x", "finding_id": "counted", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
    ], [{"target": "x", "finding_id": "ordinary-bookkeeping",
         "decision": "applied", "severity": "minor"}])
    ok.append(("valid post-terminal bookkeeping remains outside the next run",
               _post_terminal_bookkeeping["converged"] is True
               and _post_terminal_bookkeeping["run_gap"] is None
               and not _post_terminal_bookkeeping["warnings"]))
    _open_plus_bookkeeping = _terminal_rollover([
        {"target": "x", "finding_id": "pre-terminal-f", "decision": "applied",
         "severity": "blocking"},
    ], [{"target": "x", "finding_id": "ordinary-bookkeeping",
         "decision": "applied", "severity": "minor"}])
    ok.append(("post-terminal bookkeeping is excluded without hiding the open tail",
               _open_plus_bookkeeping["converged"] is False
               and [record["finding_id"] for record
                    in _open_plus_bookkeeping["run_gap"]["applied_records"]]
               == ["pre-terminal-f"]))
    _duplicate_occurrences = _terminal_rollover([
        {"target": "x", "finding_id": "duplicate", "decision": "applied",
         "severity": "blocking"},
        {"target": "x", "finding_id": "duplicate", "decision": "applied",
         "severity": "blocking"},
    ])
    ok.append(("duplicate pre-terminal ids remain separate exact occurrences",
               [(record["finding_id"], record["gap_index"])
                for record in _duplicate_occurrences["run_gap"]["applied_records"]]
               == [("duplicate", 0), ("duplicate", 1)]))
    # The run writes its OWN run-start, does blocking work, then re-writes run-start
    # (Step 1 is the natural resume point after an interruption). The findings are then
    # before the newest marker and drop out of the count.
    _reopened = (_closed_run
                 + [{"finding_id": RUN_START, "decision": "applied"}]
                 + [{"finding_id": f"bug{i}", "decision": "applied", "severity": "blocking"}
                    for i in range(2)]
                 + [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}])
    _reopened_warnings = cycle_severity_counts(_reopened)[2]
    ok.append(("a re-opened run cannot converge over its dropped findings",
               convergence_status(_reopened)["converged"] is False))
    ok.append(("re-opened run names the dropped findings",
               any("bug0" in w for w in _reopened_warnings)))
    ok.append(("gap warning names an unresolved prefix, not normal bookkeeping",
               any("unresolved prefix" in w for w in _reopened_warnings)
               and not any("inter-run bookkeeping" in w for w in _reopened_warnings)))
    # A warning over the immutable prefix cannot be repaired by an ordinary append, while a
    # new run-start discards already-reviewed cycles. The exact resolution marker is the one
    # append-only escape: bind it to the old bytes, keep the current cycle scan untouched.
    _resolvable = [
        {"target": "x", "finding_id": RUN_START, "decision": "applied",
         "ts": "2026-01-01T00:00:00+00:00"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
        {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1,
         "ts": "2026-01-01T00:01:00+00:00"},
        {"target": "x", "finding_id": RUN_START, "decision": "applied",
         "ts": "2026-01-01T00:02:00+00:00"},
        {"target": "x", "finding_id": "old-work", "decision": "applied",
         "severity": "blocking"},
        {"target": "x", "finding_id": RUN_START, "decision": "applied",
         "ts": "2026-01-01T00:03:00+00:00"},
    ]
    for cycle, count in ((1, 12), (2, 5)):
        _resolvable += [
            {"target": "x", "finding_id": f"c{cycle}-{i}", "decision": "applied",
             "severity": "serious"} for i in range(count)
        ]
        _resolvable.append(
            {"target": "x", "finding_id": CYCLE_END, "decision": "applied",
             "cycle": cycle})

    def _resolution_for(entries: list, **overrides) -> dict:
        """Historical v2 marker fixture — reader compatibility, never a new write."""
        gap = _pre_start_gap(entries)
        marker = {
            "target": "x",
            "finding_id": RUN_GAP_RESOLUTION,
            "decision": "applied",
            "run_start_ts": gap["run_start_ts"],
            "gap_sha256": gap["gap_sha256"],
            "gap_records": gap["gap_records"],
            "applied_findings": gap["applied_findings"],
            "resolution": RUN_GAP_RESOLUTION_VALUE,
            "re_recorded_gap_indices": [],
            "title": "resolved exact pre-start gap",
            "reason": "the old record belongs to the abandoned prior run",
        }
        marker.update(overrides)
        return marker

    def _v3_resolution_for(entries: list, *, prior: list[int] | None = None,
                           current: list[int] | None = None, **overrides) -> dict:
        gap = _pre_start_gap(entries)
        emitted = sorted(record["gap_index"] for record in gap["applied_records"])
        current = [] if current is None else current
        prior = ([index for index in emitted if index not in current]
                 if prior is None else prior)
        marker = {
            "target": "x",
            "finding_id": RUN_GAP_RESOLUTION,
            "decision": "applied",
            "run_start_ts": gap["run_start_ts"],
            "gap_sha256": gap["gap_sha256"],
            "gap_records": gap["gap_records"],
            "applied_findings": gap["applied_findings"],
            "resolution": RUN_GAP_RESOLUTION_VALUE,
            "schema_version": RUN_GAP_RESOLUTION_SCHEMA,
            "prior_gap_indices": prior,
            "current_gap_indices": current,
            "title": "resolved exact pre-start gap",
            "reason": "each emitted occurrence was classified from the raw history",
        }
        marker.update(overrides)
        return marker

    def _v3_surrogate_for(entries: list, index: int, **overrides) -> dict:
        gap = _pre_start_gap(entries)
        record = next(record for record in gap["applied_records"]
                      if record["gap_index"] == index)
        surrogate = _gap_replay_entry(gap, record)
        surrogate["reason"] = "this exact lifecycle occurrence belongs to the current run"
        surrogate.update(overrides)
        return surrogate

    _resolution = _resolution_for(_resolvable)
    _before_resolution = convergence_status(_resolvable)
    _after_resolution = convergence_status(_resolvable + [_resolution])
    ok.append(("an unresolved exact pre-start gap warns",
               bool(_before_resolution["warnings"])
               and _before_resolution["run_gap"]["resolved"] is False))
    ok.append(("a matching gap resolution preserves both reviewed cycles",
               _after_resolution["counts"] == [12, 5]
               and not _after_resolution["warnings"]
               and _after_resolution["run_gap"]["resolved"] is True))
    ok.append(("a gap resolution is structural — it does not open the cycle tail",
               _after_resolution["tail_open"] is False))
    ok.append(("a resolved gap permits a later genuinely clean cycle to converge",
               convergence_status(
                   _resolvable + [_resolution,
                                  {"target": "x", "finding_id": CYCLE_END,
                                   "decision": "applied", "cycle": 3}]
               )["converged"] is True))
    _legacy_resolution = _resolution_for(
        _resolvable, resolution=RUN_GAP_RESOLUTION_LEGACY_VALUE)
    _legacy_resolution.pop("re_recorded_gap_indices")
    ok.append(("the exact legacy all-prior marker remains structural",
               convergence_status(_resolvable + [_legacy_resolution])["run_gap"]["resolved"]
               is True))

    def _gap_fixture(records: list[dict]) -> list[dict]:
        return [
            {"target": "x", "finding_id": RUN_START, "decision": "applied",
             "ts": "2026-01-01T00:00:00+00:00"},
            {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
            {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
            {"target": "x", "finding_id": RUN_START, "decision": "applied",
             "ts": "2026-01-01T00:01:00+00:00"},
            *records,
            {"target": "x", "finding_id": RUN_START, "decision": "applied",
             "ts": "2026-01-01T00:02:00+00:00"},
        ]

    _all_current = _gap_fixture([
        {"target": "x", "finding_id": "same", "decision": "applied",
         "severity": "serious"},
    ])
    _all_current += [
        {"target": "x", "finding_id": "same", "decision": "applied",
         "severity": "blocking"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
    ]
    _all_current_resolution = _resolution_for(
        _all_current, re_recorded_gap_indices=[1], reason="the selected occurrence was re-recorded")
    _all_current += [
        _all_current_resolution,
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 2},
    ]
    _all_current_status = convergence_status(_all_current)
    ok.append(("an all-current gap clears after exact re-record accounting",
               _all_current_status["counts"] == [1, 0]
               and _all_current_status["converged"] is True
               and not _all_current_status["warnings"]))

    _mixed = _gap_fixture([
        {"target": "x", "finding_id": "prior-minor", "decision": "applied",
         "severity": "minor"},
        {"target": "x", "finding_id": "current-serious", "decision": "applied",
         "severity": "serious"},
    ])
    _mixed += [
        {"target": "x", "finding_id": "current-serious", "decision": "applied",
         "severity": "blocking"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
    ]
    _mixed += [
        _resolution_for(_mixed, re_recorded_gap_indices=[2],
                        reason="index 1 is prior; index 2 was re-recorded"),
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 2},
    ]
    _mixed_status = convergence_status(_mixed)
    ok.append(("a mixed gap accounts prior and re-recorded occurrences independently",
               _mixed_status["counts"] == [1, 0]
               and _mixed_status["converged"] is True
               and not _mixed_status["warnings"]))

    _duplicates = _gap_fixture([
        {"target": "x", "finding_id": "duplicate", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": "duplicate", "decision": "applied",
         "severity": "blocking"},
    ])
    _one_copy = _duplicates + [
        {"target": "x", "finding_id": "duplicate", "decision": "applied",
         "severity": "serious"},
    ]
    _one_marker = _resolution_for(_one_copy, re_recorded_gap_indices=[1, 2])
    _one_copy_status = convergence_status(
        _one_copy + [_one_marker,
                     {"target": "x", "finding_id": CYCLE_END,
                      "decision": "applied", "cycle": 1}])
    _two_copies = _one_copy + [
        {"target": "x", "finding_id": "duplicate", "decision": "applied",
         "severity": "blocking"},
    ]
    _two_marker = _resolution_for(_two_copies, re_recorded_gap_indices=[1, 2])
    _two_copy_status = convergence_status(
        _two_copies + [_two_marker,
                       {"target": "x", "finding_id": CYCLE_END,
                        "decision": "applied", "cycle": 1}])
    ok.append(("duplicate gap IDs require duplicate re-recorded occurrences",
               _one_copy_status["run_gap"]["resolved"] is False
               and _one_copy_status["counts"] == [2]
               and _two_copy_status["run_gap"]["resolved"] is True
               and _two_copy_status["counts"] == [2]))

    # Schema v3 has to represent every truthful ownership partition, including lifecycle
    # records whose reserved finding ids cannot safely be replayed. Their fingerprint/index-
    # bound surrogate is deliberately an ordinary serious finding: the existing cycle, open-
    # tail and rollover machinery then preserves the debt without a parallel accounting path.
    _lifecycle_shapes = (
        ("invalid cycle-end",
         {"target": "x", "finding_id": CYCLE_END, "decision": "applied",
          "title": "missing cycle number"}),
        ("stray run-gap-resolution",
         {"target": "wrong-target", "finding_id": RUN_GAP_RESOLUTION,
          "decision": "applied",
          "title": "malformed stray resolution"}),
    )
    _invalid_cycle_v3 = None
    for label, lifecycle in _lifecycle_shapes:
        prefix = [
            {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "v3-old"},
            lifecycle,
            {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "v3-now"},
        ]
        gap = _pre_start_gap(prefix)
        lifecycle_index = next(record["gap_index"] for record in gap["applied_records"]
                               if record["finding_id"] == lifecycle["finding_id"])

        all_prior_marker = _v3_resolution_for(
            prefix, prior=[lifecycle_index], current=[],
            reason="the exact lifecycle occurrence belongs to the abandoned prior run")
        all_prior_status = convergence_status(
            prefix
            + [{"target": "x", "finding_id": CYCLE_END,
                "decision": "applied", "cycle": 1},
               all_prior_marker])
        ok.append((f"v3 all-prior {label} is explicitly classified and can converge",
                   all_prior_status["converged"] is True
                   and all_prior_status["run_gap"]["resolved"] is True
                   and not all_prior_status["warnings"]))

        omitted_marker = _v3_resolution_for(prefix, prior=[], current=[])
        omitted_problem = _gap_resolution_problem(
            prefix + [omitted_marker], len(prefix), gap)
        ok.append((f"v3 {label} cannot be silently omitted from ownership",
                   omitted_problem is not None and "classify every" in omitted_problem))

        uncovered_marker = _v3_resolution_for(
            prefix, prior=[], current=[lifecycle_index],
            reason="truthfully current, but the required surrogate is absent")
        uncovered_status = convergence_status(
            prefix
            + [uncovered_marker,
               {"target": "x", "finding_id": CYCLE_END,
                "decision": "applied", "cycle": 1}])
        ok.append((f"v3 current {label} without its bound surrogate fails closed",
                   uncovered_status["converged"] is False
                   and uncovered_status["run_gap"]["resolved"] is False
                   and uncovered_status["counts"] == [1]))

        surrogate = _v3_surrogate_for(prefix, lifecycle_index)
        covered = prefix + [surrogate]
        current_marker = _v3_resolution_for(
            covered, prior=[], current=[lifecycle_index],
            reason="the lifecycle occurrence is current and its bound surrogate was recorded")
        current_cycle = covered + [
            current_marker,
            {"target": "x", "finding_id": CYCLE_END,
             "decision": "applied", "cycle": 1},
        ]
        current_status = convergence_status(current_cycle)
        clean_after_current = convergence_status(
            current_cycle
            + [{"target": "x", "finding_id": CYCLE_END,
                "decision": "applied", "cycle": 2}])
        ok.append((f"v3 current {label} is ordinary serious debt, never a clean cycle",
                   surrogate["target"] == "x"
                   and surrogate["replays_target"] == lifecycle["target"]
                   and current_status["run_gap"]["resolved"] is True
                   and current_status["counts"] == [1]
                   and current_status["converged"] is False
                   and not current_status["warnings"]))
        ok.append((f"v3 current {label} converges only after a later clean cycle",
                   clean_after_current["counts"] == [1, 0]
                   and clean_after_current["converged"] is True))

        mixed_prefix = [
            {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "mix-old"},
            {"target": "x", "finding_id": "prior-minor", "decision": "applied",
             "severity": "minor"},
            {"target": "x", "finding_id": "current-minor", "decision": "applied",
             "severity": "minor"},
            dict(lifecycle),
            {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "mix-now"},
        ]
        mixed_gap = _pre_start_gap(mixed_prefix)
        indices = {record["finding_id"]: record["gap_index"]
                   for record in mixed_gap["applied_records"]}
        mixed_replays = mixed_prefix + [
            {"target": "x", "finding_id": "current-minor", "decision": "applied",
             "severity": "minor"},
            _v3_surrogate_for(mixed_prefix, indices[lifecycle["finding_id"]]),
        ]
        mixed_marker = _v3_resolution_for(
            mixed_replays,
            prior=[indices["prior-minor"]],
            current=sorted([indices["current-minor"], indices[lifecycle["finding_id"]]]))
        mixed_status = convergence_status(
            mixed_replays
            + [mixed_marker,
               {"target": "x", "finding_id": CYCLE_END,
                "decision": "applied", "cycle": 1}])
        partial_marker = _v3_resolution_for(
            mixed_prefix
            + [{"target": "x", "finding_id": "current-minor", "decision": "applied",
                "severity": "minor"}],
            prior=[indices["prior-minor"]], current=[indices["current-minor"]])
        partial_problem = _gap_resolution_problem(
            mixed_prefix
            + [{"target": "x", "finding_id": "current-minor", "decision": "applied",
                "severity": "minor"}, partial_marker],
            len(mixed_prefix) + 1, mixed_gap)
        ok.append((f"v3 mixed ordinary/{label} ownership preserves serious lifecycle debt",
                   mixed_status["run_gap"]["resolved"] is True
                   and mixed_status["counts"] == [1]
                   and mixed_status["converged"] is False))
        ok.append((f"v3 mixed {label} rejects a truthful-but-partial ownership list",
                   partial_problem is not None and "classify every" in partial_problem))

        if lifecycle["finding_id"] == CYCLE_END:
            _invalid_cycle_v3 = (prefix, lifecycle_index, surrogate, current_marker)

    # A surrogate before a structural marker is still unreviewed work. Starting another run
    # before cycle-end must promote that ordinary record into the next exact gap, not lose a
    # synthetic charge attached only to the old marker.
    _v3_prefix, _v3_lifecycle_index, _v3_surrogate, _v3_marker = _invalid_cycle_v3
    _v3_rollover_open = (
        _v3_prefix
        + [_v3_surrogate, _v3_marker,
           {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "v3-next"}]
    )
    _v3_rollover = _v3_rollover_open + [
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
    _v3_rollover_status = convergence_status(_v3_rollover)
    ok.append(("v3 lifecycle surrogate survives rollover before cycle-end",
               _v3_rollover_status["converged"] is False
               and _v3_rollover_status["run_gap"]["applied_findings"] == 1
               and _v3_rollover_status["run_gap"]["applied_records"][0]["finding_id"]
               == _v3_surrogate["finding_id"]))

    # Promotion means this provenance was explicitly current in an earlier v3 marker. A
    # later run boundary cannot turn it into prior history; only a valid cycle can account
    # for it. Otherwise repeated all-prior markers erase serious work one rollover at a time.
    _promoted_gap = _pre_start_gap(_v3_rollover_open)
    _promoted_indices = [record["gap_index"] for record in _promoted_gap["applied_records"]]
    _reclassified_marker = _v3_resolution_for(
        _v3_rollover_open, prior=_promoted_indices, current=[],
        reason="attempted rollover reclassification")
    _reclassified_problem = _gap_resolution_problem(
        _v3_rollover_open + [_reclassified_marker], len(_v3_rollover_open), _promoted_gap)
    _reclassified_status = convergence_status(
        _v3_rollover_open
        + [_reclassified_marker,
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    ok.append(("v3 promoted current lifecycle debt cannot be reclassified prior",
               _promoted_indices == [0]
               and "cannot be reclassified prior" in _reclassified_problem
               and _reclassified_status["run_gap"]["resolved"] is False
               and _reclassified_status["counts"] == [1]
               and _reclassified_status["converged"] is False))

    _carried_v2_marker = _resolution_for(_v3_rollover_open)
    _carried_v1_marker = dict(
        _carried_v2_marker, resolution=RUN_GAP_RESOLUTION_LEGACY_VALUE)
    _carried_v1_marker.pop("re_recorded_gap_indices")
    ok.append(("legacy v1/v2 cannot erase carried schema-v3 lifecycle debt",
               _gap_resolution_problem(
                   _v3_rollover_open + [_carried_v1_marker],
                   len(_v3_rollover_open), _promoted_gap) is not None
               and _gap_resolution_problem(
                   _v3_rollover_open + [_carried_v2_marker],
                   len(_v3_rollover_open), _promoted_gap) is not None))

    _promoted_replay = dict(_v3_surrogate)
    _promoted_replay["reason"] = "current lifecycle debt carried into this run"
    _promoted_entries = _v3_rollover_open + [_promoted_replay]
    _promoted_current_marker = _v3_resolution_for(
        _promoted_entries, prior=[], current=_promoted_indices,
        reason="carried provenance remains current")
    _promoted_cycle = _promoted_entries + [
        _promoted_current_marker,
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
    ]
    _promoted_cycle_status = convergence_status(_promoted_cycle)
    _promoted_clean_status = convergence_status(
        _promoted_cycle
        + [{"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 2}])
    ok.append(("v3 promoted lifecycle debt remains serious until cycle-accounted",
               _promoted_cycle_status["run_gap"]["resolved"] is True
               and _promoted_cycle_status["counts"] == [1]
               and _promoted_cycle_status["converged"] is False
               and _promoted_clean_status["counts"] == [1, 0]
               and _promoted_clean_status["converged"] is True))

    _accounted_before_rollover = (
        _v3_prefix
        + [_v3_surrogate, _v3_marker,
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1},
           {"target": "x", "finding_id": RUN_START,
            "decision": "applied", "ts": "v3-accounted-next"},
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}]
    )
    _accounted_status = convergence_status(_accounted_before_rollover)
    ok.append(("a valid cycle accounts lifecycle provenance before rollover",
               _accounted_status["run_gap"] is None
               and _accounted_status["counts"] == [0]
               and _accounted_status["converged"] is True))

    # The reserved id grammar fails closed under a different valid digest/index, while a
    # malformed lookalike remains an ordinary id and preserves the all-prior control.
    _mutated_provenance_id = f"{RUN_GAP_REPLAY_PREFIX}-{'a' * 64}-999"
    _mutated_provenance_prefix = [
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "mp-old"},
        {"target": "x", "finding_id": _mutated_provenance_id,
         "decision": "applied", "severity": "serious"},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "mp-now"},
    ]
    _mutated_provenance_gap = _pre_start_gap(_mutated_provenance_prefix)
    _mutated_provenance_index = _mutated_provenance_gap["applied_records"][0]["gap_index"]
    _mutated_provenance_marker = _v3_resolution_for(
        _mutated_provenance_prefix, prior=[_mutated_provenance_index], current=[])
    _lookalike_prefix = [dict(entry) for entry in _mutated_provenance_prefix]
    _lookalike_prefix[1]["finding_id"] += "-extra"
    _lookalike_gap = _pre_start_gap(_lookalike_prefix)
    _lookalike_index = _lookalike_gap["applied_records"][0]["gap_index"]
    _lookalike_marker = _v3_resolution_for(
        _lookalike_prefix, prior=[_lookalike_index], current=[])
    ok.append(("v3 provenance-id mutations fail closed without capturing lookalikes",
               _gap_resolution_problem(
                   _mutated_provenance_prefix + [_mutated_provenance_marker],
                   len(_mutated_provenance_prefix), _mutated_provenance_gap) is not None
               and _gap_resolution_problem(
                   _lookalike_prefix + [_lookalike_marker],
                   len(_lookalike_prefix), _lookalike_gap) is None))

    # Duplicate lifecycle ids are occurrences, not a set. The digest-bound surrogate id also
    # includes the exact gap index, so one surrogate cannot cover its equal-looking sibling.
    _duplicate_lifecycle_prefix = [
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "dup-old"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "title": "bad one"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "title": "bad two"},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "dup-now"},
    ]
    _duplicate_lifecycle_gap = _pre_start_gap(_duplicate_lifecycle_prefix)
    _duplicate_lifecycle_indices = [record["gap_index"]
                                    for record in _duplicate_lifecycle_gap["applied_records"]]
    _one_lifecycle_surrogate = _v3_surrogate_for(
        _duplicate_lifecycle_prefix, _duplicate_lifecycle_indices[0])
    _one_lifecycle_entries = _duplicate_lifecycle_prefix + [_one_lifecycle_surrogate]
    _one_lifecycle_marker = _v3_resolution_for(
        _one_lifecycle_entries, prior=[], current=_duplicate_lifecycle_indices)
    _two_lifecycle_entries = _one_lifecycle_entries + [
        _v3_surrogate_for(_duplicate_lifecycle_prefix, _duplicate_lifecycle_indices[1])]
    _two_lifecycle_marker = _v3_resolution_for(
        _two_lifecycle_entries, prior=[], current=_duplicate_lifecycle_indices)
    _two_lifecycle_status = convergence_status(
        _two_lifecycle_entries
        + [_two_lifecycle_marker,
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    ok.append(("v3 duplicate lifecycle ids require one bound surrogate per occurrence",
               _gap_resolution_problem(
                   _one_lifecycle_entries + [_one_lifecycle_marker],
                   len(_one_lifecycle_entries), _duplicate_lifecycle_gap) is not None
               and _two_lifecycle_status["run_gap"]["resolved"] is True
               and _two_lifecycle_status["counts"] == [2]))

    _duplicate_carry_head = _v3_prefix + [_v3_surrogate, dict(_v3_surrogate)]
    _duplicate_carry_first_marker = _v3_resolution_for(
        _duplicate_carry_head, prior=[], current=[_v3_lifecycle_index])
    _duplicate_carry_open = _duplicate_carry_head + [
        _duplicate_carry_first_marker,
        {"target": "x", "finding_id": RUN_START,
         "decision": "applied", "ts": "duplicate-carry-next"},
    ]
    _duplicate_carry_gap = _pre_start_gap(_duplicate_carry_open)
    _duplicate_carry_indices = [record["gap_index"]
                                for record in _duplicate_carry_gap["applied_records"]]
    _duplicate_carry_one = _duplicate_carry_open + [dict(_v3_surrogate)]
    _duplicate_carry_one_marker = _v3_resolution_for(
        _duplicate_carry_one, prior=[], current=_duplicate_carry_indices)
    _duplicate_carry_two = _duplicate_carry_one + [dict(_v3_surrogate)]
    _duplicate_carry_two_marker = _v3_resolution_for(
        _duplicate_carry_two, prior=[], current=_duplicate_carry_indices)
    _duplicate_carry_two_status = convergence_status(
        _duplicate_carry_two
        + [_duplicate_carry_two_marker,
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    ok.append(("duplicate carried lifecycle surrogates each stay current and require replay",
               _duplicate_carry_indices == [0, 1]
               and _gap_resolution_problem(
                   _duplicate_carry_one + [_duplicate_carry_one_marker],
                   len(_duplicate_carry_one), _duplicate_carry_gap) is not None
               and _duplicate_carry_two_status["run_gap"]["resolved"] is True
               and _duplicate_carry_two_status["counts"] == [2]
               and _duplicate_carry_two_status["converged"] is False))

    # Every binding field is semantic. A mutation leaves the surrogate as ordinary work and
    # the marker non-structural, so both the current cycle and the original gap stay visible.
    _surrogate_mutations = (
        {"gap_sha256": "0" * 64},
        {"gap_index": _v3_lifecycle_index + 1},
        {"replays_target": "not-the-original-target"},
        {"replays_finding_id": "not-cycle-end"},
        {"finding_id": "not-the-bound-surrogate"},
        {"severity": "minor"},
        {"title": ""},
        {"reason": ""},
    )
    _mutations_fail = True
    for mutation in _surrogate_mutations:
        bad_surrogate = _v3_surrogate_for(_v3_prefix, _v3_lifecycle_index, **mutation)
        bad_entries = _v3_prefix + [bad_surrogate]
        bad_marker = _v3_resolution_for(
            bad_entries, prior=[], current=[_v3_lifecycle_index])
        _mutations_fail &= _gap_resolution_problem(
            bad_entries + [bad_marker], len(bad_entries), _pre_start_gap(bad_entries)) is not None
    ok.append(("v3 lifecycle surrogate rejects every binding/audit mutation",
               _mutations_fail))
    _mutated_v3_gap = [dict(entry) for entry in _v3_prefix]
    _mutated_v3_gap[1]["title"] = "mutated historical bytes"
    ok.append(("v3 marker and surrogate cannot survive a mutated fingerprinted gap",
               convergence_status(
                   _mutated_v3_gap
                   + [_v3_surrogate, _v3_marker,
                      {"target": "x", "finding_id": CYCLE_END,
                       "decision": "applied", "cycle": 1}]
               )["run_gap"]["resolved"] is False))

    ok.append(("v1 and v2 gap markers remain reader-compatible after v3",
               convergence_status(_resolvable + [_legacy_resolution])["run_gap"]["resolved"]
               is True
               and convergence_status(_resolvable + [_resolution])["run_gap"]["resolved"]
               is True
               and "schema_version" not in _legacy_resolution
               and "schema_version" not in _resolution))
    _null_schema_marker = _v3_resolution_for(
        _v3_prefix, prior=[_v3_lifecycle_index], current=[], schema_version=None)
    ok.append(("an explicit null schema cannot masquerade as a legacy v2 marker",
               _gap_resolution_problem(
                   _v3_prefix + [_null_schema_marker], len(_v3_prefix),
                   _pre_start_gap(_v3_prefix)) is not None))

    for label, overrides in (
        ("digest", {"gap_sha256": "0" * 64}),
        ("run start", {"run_start_ts": "2026-01-01T09:00:00+00:00"}),
        ("record count", {"gap_records": 99}),
        ("applied count", {"applied_findings": 99}),
        ("decision", {"decision": "deferred"}),
        ("resolution value", {"resolution": "ignore-it"}),
        ("wrong target", {"target": "y"}),
        ("title", {"title": ""}),
        ("reason", {"reason": "  "}),
        ("hand-edited severity", {"severity": "minor"}),
    ):
        bad = _resolution_for(_resolvable, **overrides)
        ok.append((f"a wrong gap-resolution {label} cannot suppress the warning",
                   bool(convergence_status(_resolvable + [bad])["warnings"])))

    _wrong_target_marker = _resolution_for(_resolvable, target="y", severity="minor")
    _wrong_target_status = convergence_status(
        _resolvable + [_wrong_target_marker,
                       {"target": "x", "finding_id": CYCLE_END,
                        "decision": "applied", "cycle": 3}])
    ok.append(("a hand-edited wrong-target marker is forced serious",
               _wrong_target_status["run_gap"]["resolved"] is False
               and _wrong_target_status["counts"][-1] == 1))

    for label, selected in (
        ("boolean index", [True]),
        ("negative index", [-1]),
        ("out-of-range index", [99]),
        ("lifecycle index", [0]),
        ("duplicate indices", [1, 1]),
        ("unsorted indices", [2, 1]),
    ):
        bad = _resolution_for(_resolvable, re_recorded_gap_indices=selected)
        status = convergence_status(
            _resolvable + [bad,
                           {"target": "x", "finding_id": CYCLE_END,
                            "decision": "applied", "cycle": 3}])
        ok.append((f"a malformed gap-resolution {label} counts serious",
                   status["run_gap"]["resolved"] is False
                   and status["counts"][-1] == 1))

    _missing_indices = dict(_resolution)
    _missing_indices.pop("re_recorded_gap_indices")
    _missing_indices_status = convergence_status(
        _resolvable + [_missing_indices,
                       {"target": "x", "finding_id": CYCLE_END,
                        "decision": "applied", "cycle": 3}])
    ok.append(("a v2 marker missing its index list counts serious",
               _missing_indices_status["run_gap"]["resolved"] is False
               and _missing_indices_status["counts"][-1] == 1))

    _no_coverage = _resolution_for(_resolvable, re_recorded_gap_indices=[1])
    _no_coverage_status = convergence_status(
        _resolvable + [_no_coverage,
                       {"target": "x", "finding_id": CYCLE_END,
                        "decision": "applied", "cycle": 3}])
    ok.append(("a selected occurrence without a pre-marker re-record fails closed",
               _no_coverage_status["run_gap"]["resolved"] is False
               and _no_coverage_status["counts"][-1] == 1))
    _wrong_severity_entries = _resolvable + [
        {"target": "x", "finding_id": "old-work", "decision": "applied",
         "severity": "minor"},
    ]
    _wrong_severity = _resolution_for(
        _wrong_severity_entries, re_recorded_gap_indices=[1])
    ok.append(("a serious gap occurrence cannot be covered by a minor re-record",
               convergence_status(
                   _wrong_severity_entries + [_wrong_severity,
                                              {"target": "x", "finding_id": CYCLE_END,
                                               "decision": "applied", "cycle": 3}]
               )["counts"][-1] == 1))
    _after_marker = _resolvable + [_no_coverage,
                                   {"target": "x", "finding_id": "old-work",
                                    "decision": "applied", "severity": "serious"},
                                   {"target": "x", "finding_id": CYCLE_END,
                                    "decision": "applied", "cycle": 3}]
    ok.append(("a re-record after its marker cannot validate it retroactively",
               convergence_status(_after_marker)["run_gap"]["resolved"] is False))

    _valid_plus_bad = _resolvable + [
        _resolution,
        _resolution_for(_resolvable, reason="", severity="minor"),
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 3},
    ]
    _valid_plus_bad_status = convergence_status(_valid_plus_bad)
    ok.append(("one valid plus one malformed marker resolves but counts the malformed one",
               _valid_plus_bad_status["run_gap"]["resolved"] is True
               and _valid_plus_bad_status["counts"][-1] == 1))
    _duplicate_markers_status = convergence_status(
        _resolvable + [_resolution, dict(_resolution),
                       {"target": "x", "finding_id": CYCLE_END,
                        "decision": "applied", "cycle": 3}])
    ok.append(("duplicate valid markers fail closed as two serious records",
               _duplicate_markers_status["run_gap"]["resolved"] is False
               and _duplicate_markers_status["counts"][-1] == 2))

    _no_gap_bad = convergence_status([
        {"target": "x", "finding_id": RUN_START, "decision": "applied"},
        {**_resolution, "severity": "minor"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
    ])
    ok.append(("an applied resolution with no gap is an ordinary serious failure",
               _no_gap_bad["counts"] == [1] and _no_gap_bad["converged"] is False))

    _mutated_gap = [dict(entry) for entry in _resolvable]
    _mutated_gap[4]["title"] = "the exact old bytes changed"
    ok.append(("mutating any gap record invalidates an earlier resolution",
               bool(convergence_status(_mutated_gap + [_resolution])["warnings"])))
    _later_gap = (_resolvable + [_resolution,
                                {"target": "x", "finding_id": RUN_END,
                                 "decision": "applied", "converged": True,
                                 "cycles": 1},
                                {"target": "x", "finding_id": RUN_START,
                                 "decision": "applied",
                                 "ts": "2026-01-01T00:04:00+00:00"},
                                {"target": "x", "finding_id": "later-old-work",
                                 "decision": "applied", "severity": "blocking"},
                                {"target": "x", "finding_id": RUN_START,
                                 "decision": "applied",
                                 "ts": "2026-01-01T00:05:00+00:00"},
                                {"target": "x", "finding_id": CYCLE_END,
                                 "decision": "applied", "cycle": 1}])
    ok.append(("an old resolution cannot suppress a later run's gap",
               bool(convergence_status(_later_gap)["warnings"])))
    ok.append(("a resolution appended after run-convergence suppresses nothing",
               bool(convergence_status(
                   _resolvable
                   + [{"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
                      _resolution]
               )["warnings"])))
    ok.append(("a resolution after run-convergence is stray work",
               bool(convergence_status(
                   [{"target": "x", "finding_id": RUN_START, "decision": "applied",
                     "ts": "2026-01-01T00:00:00+00:00"},
                    {"target": "x", "finding_id": CYCLE_END, "decision": "applied",
                     "cycle": 1},
                    {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
                    _resolution]
               )["warnings"])))
    _rollover_prefix = [
        {"target": "x", "finding_id": RUN_START, "decision": "applied",
         "ts": "2026-01-01T00:00:00+00:00"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
        {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        _resolution,
        {"target": "x", "finding_id": RUN_START, "decision": "applied",
         "ts": "2026-01-01T00:06:00+00:00"},
    ]
    _rollover_unresolved = convergence_status(
        _rollover_prefix
        + [{"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    ok.append(("a post-terminal resolution stays ambiguous after the next run starts",
               bool(_rollover_unresolved["warnings"])
               and _rollover_unresolved["converged"] is False))
    _rollover_resolution = _resolution_for(
        _rollover_prefix,
        reason="the reserved stray record belongs to the earlier completed run")
    ok.append(("the next run can explicitly account for that exact stray record",
               convergence_status(
                   _rollover_prefix
                   + [_rollover_resolution,
                      {"target": "x", "finding_id": CYCLE_END,
                       "decision": "applied", "cycle": 1}]
               )["converged"] is True))
    _duplicate_terminal_prefix = [
        {"target": "x", "finding_id": RUN_START, "decision": "applied",
         "ts": "dup-r1"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
        {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        {"target": "x", "finding_id": RUN_GAP_RESOLUTION, "decision": "applied"},
        {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        {"target": "x", "finding_id": RUN_START, "decision": "applied",
         "ts": "dup-r2"},
    ]
    _duplicate_terminal_cycle = _duplicate_terminal_prefix + [
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
    _duplicate_terminal_status = convergence_status(_duplicate_terminal_cycle)
    ok.append(("a duplicate terminal cannot erase intervening lifecycle debt",
               _duplicate_terminal_status["converged"] is False
               and _duplicate_terminal_status["run_gap"]["resolution_open"] is True
               and [(record["finding_id"], record["effective_severity"])
                    for record in _duplicate_terminal_status["run_gap"]["applied_records"]]
               == [(RUN_GAP_RESOLUTION, "serious"), (RUN_END, "serious")]))
    _duplicate_terminal_resolution = _v3_resolution_for(
        _duplicate_terminal_prefix,
        reason="both malformed lifecycle records belong to the earlier completed run")
    _duplicate_terminal_resolved = convergence_status(
        _duplicate_terminal_cycle + [_duplicate_terminal_resolution])
    ok.append(("duplicate-terminal lifecycle debt remains exactly recoverable",
               _duplicate_terminal_resolved["converged"] is True
               and _duplicate_terminal_resolved["run_gap"]["resolved"] is True
               and not _duplicate_terminal_resolved["warnings"]))
    for decision in ("deferred", "rejected"):
        _malformed_start_prefix = [
            {"target": "x", "finding_id": RUN_START, "decision": "applied",
             "ts": f"{decision}-r1"},
            {"target": "x", "finding_id": CYCLE_END,
             "decision": "applied", "cycle": 1},
            {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
            {"target": "x", "finding_id": RUN_START, "decision": decision},
            {"target": "x", "finding_id": RUN_START, "decision": "applied",
             "ts": f"{decision}-r2"},
        ]
        _malformed_start_cycle = _malformed_start_prefix + [
            {"target": "x", "finding_id": CYCLE_END,
             "decision": "applied", "cycle": 1}]
        _malformed_start_status = convergence_status(_malformed_start_cycle)
        _malformed_start_resolution = _v3_resolution_for(
            _malformed_start_prefix,
            reason=f"the {decision} marker belongs to prior malformed history")
        _malformed_start_resolved = convergence_status(
            _malformed_start_cycle + [_malformed_start_resolution])
        ok.append((f"a post-terminal {decision} run-start is recoverable after rollover",
                   _malformed_start_status["converged"] is False
                   and [(record["finding_id"], record["effective_severity"])
                        for record in _malformed_start_status["run_gap"]["applied_records"]]
                   == [(RUN_START, "serious")]
                   and _malformed_start_resolved["converged"] is True
                   and _malformed_start_resolved["run_gap"]["resolved"] is True))
    _third_run_unresolved = (
        _rollover_prefix
        + [{"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1},
           {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
           {"target": "x", "finding_id": RUN_START, "decision": "applied",
            "ts": "2026-01-01T00:07:00+00:00"},
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    ok.append(("an unresolved gap remains sticky across another closed run",
               bool(convergence_status(_third_run_unresolved)["warnings"])
               and convergence_status(_third_run_unresolved)["converged"] is False))
    _five_rollovers = [
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "r1"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
        {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        _resolution,
    ]
    for i in range(2, 7):
        _five_rollovers.append(
            {"target": "x", "finding_id": RUN_START, "decision": "applied",
             "ts": f"r{i}"})
        if i == 2:
            _five_rollovers.append(
                {"target": "x", "finding_id": "counted-intermediate-work",
                 "decision": "applied", "severity": "serious"})
        _five_rollovers.append(
            {"target": "x", "finding_id": CYCLE_END,
             "decision": "applied", "cycle": 1})
        if i < 6:
            _five_rollovers.append(
                {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1})
    _five_rollover_status = convergence_status(_five_rollovers)
    ok.append(("an unresolved gap remains sticky across five later runs",
               bool(_five_rollover_status["warnings"])
               and _five_rollover_status["converged"] is False
               and _five_rollover_status["run_gap"]["applied_findings"] == 1
               and _five_rollover_status["run_gap"]["applied_records"][0]["finding_id"]
               == RUN_GAP_RESOLUTION))
    _third_run_after_resolution = (
        _rollover_prefix
        + [_rollover_resolution,
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1},
           {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
           {"target": "x", "finding_id": RUN_START, "decision": "applied",
            "ts": "2026-01-01T00:07:00+00:00"},
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    ok.append(("an exactly resolved gap does not propagate into a later run",
               convergence_status(_third_run_after_resolution)["converged"] is True
               and convergence_status(_third_run_after_resolution)["run_gap"] is None))
    _resumed_after_resolution = (
        _rollover_prefix
        + [_rollover_resolution,
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1},
           {"target": "x", "finding_id": RUN_START, "decision": "applied",
            "ts": "2026-01-01T00:07:00+00:00"},
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    _resumed_after_resolution_status = convergence_status(_resumed_after_resolution)
    ok.append(("an exact resolution remains accounted across a start without a terminal",
               _resumed_after_resolution_status["converged"] is True
               and _resumed_after_resolution_status["run_gap"] is None))
    _malformed_before_resolution = dict(_rollover_resolution)
    _malformed_before_resolution["gap_sha256"] = "0" * 64
    _open_tail_before_resolution = (
        _rollover_prefix
        + [_malformed_before_resolution,
           _rollover_resolution,
           {"target": "x", "finding_id": RUN_START, "decision": "applied",
            "ts": "2026-01-01T00:07:00+00:00"},
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    _open_tail_status = convergence_status(_open_tail_before_resolution)
    ok.append(("a malformed pre-resolution tail survives a start without a terminal",
               _open_tail_status["converged"] is False
               and _open_tail_status["run_gap"]["gap_records"] == 2
               and _open_tail_status["run_gap"]["applied_records"] == [{
                   "gap_index": 0,
                   "target": "x",
                   "finding_id": RUN_GAP_RESOLUTION,
                   "effective_severity": "serious",
               }]))
    _tail_resolution = _resolution_for(
        _open_tail_before_resolution,
        reason="the malformed pre-resolution marker belongs to the interrupted prior run")
    ok.append(("the carried pre-resolution tail remains exactly resolvable",
               convergence_status(
                   _open_tail_before_resolution + [_tail_resolution]
               )["converged"] is True))
    _closed_tail_before_resolution = (
        _rollover_prefix
        + [_malformed_before_resolution,
           _rollover_resolution,
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1},
           {"target": "x", "finding_id": RUN_START, "decision": "applied",
            "ts": "2026-01-01T00:07:00+00:00"},
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    ok.append(("a valid monotonic cycle-end closes the pre-resolution tail",
               convergence_status(_closed_tail_before_resolution)["converged"] is True
               and convergence_status(_closed_tail_before_resolution)["run_gap"] is None))
    _invalid_cycle_gap_records = [
        {
            "gap_index": 0,
            "target": "x",
            "finding_id": RUN_GAP_RESOLUTION,
            "effective_severity": "serious",
        },
        {
            "gap_index": 2,
            "target": "x",
            "finding_id": CYCLE_END,
            "effective_severity": "serious",
        },
    ]
    _missing_cycle_before_rollover = (
        _rollover_prefix
        + [_malformed_before_resolution,
           _rollover_resolution,
           {"target": "x", "finding_id": CYCLE_END, "decision": "applied"},
           {"target": "x", "finding_id": RUN_START, "decision": "applied",
            "ts": "2026-01-01T00:07:00+00:00"},
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    _missing_cycle_rollover_status = convergence_status(_missing_cycle_before_rollover)
    ok.append(("a missing-cycle marker stays serious across a rollover",
               _missing_cycle_rollover_status["converged"] is False
               and _missing_cycle_rollover_status["run_gap"]["applied_records"]
               == _invalid_cycle_gap_records))
    _duplicate_cycle_before_rollover = (
        _rollover_prefix
        + [{"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1},
           _malformed_before_resolution,
           _rollover_resolution,
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1},
           {"target": "x", "finding_id": RUN_START, "decision": "applied",
            "ts": "2026-01-01T00:07:00+00:00"},
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    _duplicate_cycle_rollover_status = convergence_status(_duplicate_cycle_before_rollover)
    ok.append(("a duplicate cycle marker stays serious across a rollover",
               _duplicate_cycle_rollover_status["converged"] is False
               and _duplicate_cycle_rollover_status["run_gap"]["applied_records"]
               == _invalid_cycle_gap_records))
    _tainted_missing_history = [
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "a"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied"},
        {"target": "x", "finding_id": "lost", "decision": "applied",
         "severity": "blocking"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
        {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "b"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
    ]
    _tainted_missing_status = convergence_status(_tainted_missing_history)
    ok.append(("a later shaped cycle cannot repair a missing-cycle segment",
               _tainted_missing_status["converged"] is False
               and [(record["finding_id"], record["effective_severity"])
                    for record in _tainted_missing_status["run_gap"]["applied_records"]]
               == [(CYCLE_END, "serious"), ("lost", "serious"),
                   (CYCLE_END, "serious")]))
    _tainted_duplicate_history = [
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "a"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
        {"target": "x", "finding_id": "x", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
        {"target": "x", "finding_id": "lost", "decision": "applied",
         "severity": "blocking"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 2},
        {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "b"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
    ]
    _tainted_duplicate_status = convergence_status(_tainted_duplicate_history)
    ok.append(("a later increasing cycle cannot repair a duplicate-cycle segment",
               _tainted_duplicate_status["converged"] is False
               and [record["finding_id"] for record
                    in _tainted_duplicate_status["run_gap"]["applied_records"]]
               == ["x", CYCLE_END, "lost", CYCLE_END]
               and all(record["effective_severity"] == "serious" for record
                       in _tainted_duplicate_status["run_gap"]["applied_records"])))
    _valid_multicycle_terminal = [
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "a"},
        {"target": "x", "finding_id": "reviewed", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 4},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 9},
        {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "b"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
    ]
    ok.append(("valid nonconsecutive multi-cycle history stays trusted across a terminal",
               convergence_status(_valid_multicycle_terminal)["converged"] is True
               and convergence_status(_valid_multicycle_terminal)["run_gap"] is None))
    # A valid cycle-end after an exact resolution closes all preceding interrupted-run
    # work. The paired no-cycle case below must retain that work as exact next-gap debt.
    _work_after_resolution_with_cycle = (
        _rollover_prefix
        + [_rollover_resolution,
           {"target": "x", "finding_id": "post-resolution-work",
            "decision": "applied", "severity": "serious"},
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1},
           {"target": "x", "finding_id": RUN_START, "decision": "applied",
            "ts": "2026-01-01T00:07:00+00:00"},
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    _work_after_resolution_with_cycle_status = convergence_status(
        _work_after_resolution_with_cycle)
    ok.append(("a valid cycle-end after a resolution closes preceding work",
               _work_after_resolution_with_cycle_status["converged"] is True
               and _work_after_resolution_with_cycle_status["run_gap"] is None))
    _work_after_resolution_without_cycle = (
        _rollover_prefix
        + [_rollover_resolution,
           {"target": "x", "finding_id": "post-resolution-work",
            "decision": "applied", "severity": "serious"},
           {"target": "x", "finding_id": RUN_START, "decision": "applied",
            "ts": "2026-01-01T00:07:00+00:00"},
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    _work_after_resolution_without_cycle_status = convergence_status(
        _work_after_resolution_without_cycle)
    ok.append(("post-resolution work without a cycle-end remains exact gap debt",
               _work_after_resolution_without_cycle_status["converged"] is False
               and _work_after_resolution_without_cycle_status["run_gap"]["gap_records"] == 1
               and _work_after_resolution_without_cycle_status["run_gap"]["applied_records"] == [{
                   "gap_index": 0,
                   "target": "x",
                   "finding_id": "post-resolution-work",
                   "effective_severity": "serious",
               }]))
    _work_resolution = _resolution_for(
        _work_after_resolution_without_cycle,
        reason="the post-resolution work belongs to the interrupted prior run")
    ok.append(("the fresh post-resolution gap remains exactly resolvable",
               convergence_status(
                   _work_after_resolution_without_cycle + [_work_resolution]
               )["converged"] is True))
    # A frozen head gap can outlive many interrupted runs. Their unclosed occurrences are
    # queued behind it: resolving the head promotes exactly that sparse debt, while work
    # already closed by a valid cycle-end remains accounted and is never added again.
    _queued_prefix = [
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "q0"},
        {"target": "x", "finding_id": "old-queued-gap", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "q1"},
        {"target": "x", "finding_id": "counted-q1", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 4},
        {"target": "x", "finding_id": "open-a", "decision": "applied",
         "severity": "minor"},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "q2"},
        {"target": "x", "finding_id": "counted-q2", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 9},
        {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        {"target": "x", "finding_id": "post-terminal-q2", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "q3"},
        {"target": "x", "finding_id": "counted-q3", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
        {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "q4"},
        {"target": "x", "finding_id": "open-b", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "q5"},
        {"target": "x", "finding_id": "counted-q5", "decision": "applied",
         "severity": "serious"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 2},
        {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        {"target": "x", "finding_id": RUN_START, "decision": "applied", "ts": "q6"},
    ]
    _queued_original = _pre_start_gap(_queued_prefix)
    _queued_before_first_marker = _queued_prefix + [
        {"target": "x", "finding_id": "open-a", "decision": "applied",
         "severity": "minor"}]
    _queued_first_resolution = _resolution_for(
        _queued_before_first_marker,
        reason="the immutable original batch belongs to its interrupted run")
    _queued_once = (
        _queued_before_first_marker
        + [_queued_first_resolution,
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1}])
    _queued_once_status = convergence_status(_queued_once)
    _queued_records = [
        {"gap_index": 0, "target": "x", "finding_id": "open-a",
         "effective_severity": "minor"},
        {"gap_index": 5, "target": "x", "finding_id": "post-terminal-q2",
         "effective_severity": "serious"},
        {"gap_index": 11, "target": "x", "finding_id": "open-b",
         "effective_severity": "serious"},
    ]
    ok.append(("a carried head preserves only its original batch before resolution",
               _queued_original["finding_ids"] == ["old-queued-gap"]
               and _queued_original["applied_findings"] == 1))
    ok.append(("resolving a carried head promotes every unaccounted later occurrence",
               _queued_once_status["converged"] is False
               and _queued_once_status["run_gap"]["applied_records"] == _queued_records
               and not any("counted-" in record["finding_id"]
                           for record in _queued_once_status["run_gap"]["applied_records"])))
    _prior_replay_second_marker = _resolution_for(
        _queued_once, re_recorded_gap_indices=[0],
        reason="this must not reuse a replay from before the first marker")
    ok.append(("a replay before marker one cannot account the promoted batch",
               _gap_resolution_problem(
                   _queued_once + [_prior_replay_second_marker], len(_queued_once),
                   _pre_start_gap(_queued_once)) is not None))
    _same_run_second_resolution = _resolution_for(
        _queued_once, reason="the promoted sparse batch is prior history")
    _twice_resolved_same_run = convergence_status(
        _queued_once
        + [_same_run_second_resolution,
           {"target": "x", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 2}])
    ok.append(("two same-run exact resolutions are both structural",
               _twice_resolved_same_run["converged"] is True
               and _twice_resolved_same_run["counts"] == [0, 0]))
    _queued_rollovers = list(_queued_once)
    _queued_fingerprints = [_pre_start_gap(_queued_rollovers)["gap_sha256"]]
    _queued_stable = True
    for i in range(7, 13):
        if i % 2:
            _queued_rollovers.append(
                {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1})
        _queued_rollovers += [
            {"target": "x", "finding_id": RUN_START,
             "decision": "applied", "ts": f"q{i}"},
            {"target": "x", "finding_id": CYCLE_END,
             "decision": "applied", "cycle": 1},
        ]
        _rolled_gap = _pre_start_gap(_queued_rollovers)
        _queued_stable &= _rolled_gap["applied_records"] == _queued_records
        _queued_fingerprints.append(_rolled_gap["gap_sha256"])
    ok.append(("the promoted sparse batch survives six more mixed rollovers",
               _queued_stable
               and len(set(_queued_fingerprints)) == len(_queued_fingerprints)
               and convergence_status(_queued_rollovers)["converged"] is False))
    _long_closed_history = []
    for i in range(1100):
        _long_closed_history += [
            {"target": "x", "finding_id": RUN_START, "decision": "applied",
             "ts": f"historical-{i}"},
            {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
            {"target": "x", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        ]
    _long_closed_history += [
        {"target": "x", "finding_id": RUN_START, "decision": "applied",
         "ts": "current"},
        {"target": "x", "finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
    ]
    ok.append(("long append-only history has no recursion limit",
               convergence_status(_long_closed_history)["converged"] is True))
    # The discriminating half: closing out a deferred finding BETWEEN runs is this log's
    # established practice and must stay silent, or no run that tidies up could converge.
    ok.append(("inter-run bookkeeping still converges",
               convergence_status(
                   _closed_run
                   + [{"finding_id": "closeout", "decision": "applied", "severity": "minor"},
                      {"finding_id": RUN_START, "decision": "applied"},
                      {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["converged"] is True))
    ok.append(("a strictly declining rate keeps iterating however long",
               convergence_status(_hist([9, 8, 7, 6, 5]))["verdict"] == "keep-iterating"))
    ok.append(("an OPEN cycle never converges",
               convergence_status(_hist([3, 0], tail=1))["converged"] is False))
    ok.append(("a run that ends mid-cycle leaves the cycle OPEN, never converged",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "a", "decision": "applied", "severity": "serious"},
                    {"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["converged"] is False))
    ok.append(("a fresh run does not inherit the prior run's history",
               convergence_status(
                   _hist([5, 5, 5]) + [{"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1}]
                   + _hist([0]))["verdict"] == "converged"))
    ok.append(("findings before run-start WARN but do not lock the target",
               bool(convergence_status(
                   [{"finding_id": "f", "decision": "applied", "severity": "blocking"},
                    {"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["warnings"])))
    ok.append(("legitimate inter-run bookkeeping does not lock the target",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "f", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                    {"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
                    {"finding_id": "closed-old", "decision": "applied", "severity": "minor"},
                    {"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["verdict"] == "converged"))
    ok.append(("run-convergence is TERMINAL — a later run without run-start cannot inherit",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "a", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                    {"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
                    {"finding_id": "b", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 4}]
               )["counts"] == [1]))
    ok.append(("a missing run-start is REFUSED (would inherit all history)",
               _raises(lambda: convergence_status(
                   [{"finding_id": "f", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]), ValueError)))
    _persisted_duplicate = convergence_status(
        [{"finding_id": RUN_START, "decision": "applied"},
         {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
         {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
         {"finding_id": CYCLE_END, "decision": "applied", "cycle": 2}])
    ok.append(("a persisted duplicate cycle-end is forced serious",
               _persisted_duplicate["counts"] == [0]
               and _persisted_duplicate["tail_open"] is True
               and _persisted_duplicate["converged"] is False))
    _persisted_descending = convergence_status(
        [{"finding_id": RUN_START, "decision": "applied"},
         {"finding_id": CYCLE_END, "decision": "applied", "cycle": 8},
         {"finding_id": CYCLE_END, "decision": "applied", "cycle": 3},
         {"finding_id": CYCLE_END, "decision": "applied", "cycle": 9}])
    ok.append(("a persisted descending cycle-end is forced serious",
               _persisted_descending["counts"] == [0]
               and _persisted_descending["tail_open"] is True
               and _persisted_descending["converged"] is False))
    for decision in ("deferred", "rejected"):
        _non_applied_cycle = convergence_status(
            [{"finding_id": RUN_START, "decision": "applied"},
             {"finding_id": CYCLE_END, "decision": decision, "cycle": 1}])
        ok.append((f"a persisted {decision} cycle-end cannot make a clean cycle",
                   _non_applied_cycle["counts"] == []
                   and _non_applied_cycle["tail_open"] is True
                   and _non_applied_cycle["converged"] is False))
    _persisted_malformed_cycle = convergence_status(
        [{"finding_id": RUN_START, "decision": "applied"},
         {"finding_id": CYCLE_END, "decision": "applied"},
         {"finding_id": CYCLE_END, "decision": "applied", "cycle": 4}])
    ok.append(("a persisted malformed cycle-end is forced serious",
               _persisted_malformed_cycle["counts"] == []
               and _persisted_malformed_cycle["tail_open"] is True
               and _persisted_malformed_cycle["converged"] is False))
    ok.append(("nonconsecutive monotonic cycle numbers remain valid",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "f", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 4},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 9}]
               )["counts"] == [1, 0]))
    ok.append(("a prior COMPLETED run before run-start is fine",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "old", "decision": "applied", "severity": "serious"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1},
                    {"finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
                    {"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["verdict"] == "converged"))
    with tempfile.TemporaryDirectory() as td:  # khenrix-shaped, or the LOG-KEY check raises
        kr = _mark_khenrix(Path(td))           # first and the assertion passes vacuously
        (kr / "shared" / "skills").mkdir(parents=True)
        (kr / "capabilities.toml").write_text("[models]\n")
        def _sev(v):
            e = {"target": "x", "finding_id": "y", "decision": "applied"}
            if v is not _MISSING:
                e["severity"] = v
            try:
                log_append(kr, "x", e)
                return None
            except ValueError as ex:
                return str(ex)
        ok.append(("a bad severity is rejected at write time — for the RIGHT reason",
                   "severity must be one of" in (_sev("P0") or "")))
        # "severity must be one of" predates the SEVERITY_TESTS wiring, so asserting only
        # that would stay green if the tests were deleted again — dead code restored.
        ok.append(("bad severity prints the objective TESTS, not just the labels",
                   all(f"  {k}: {v}" in (_sev("P0") or "")
                       for k, v in SEVERITY_TESTS.items())))
        ok.append(("an explicit null severity is rejected (absent != null)",
                   "severity must be one of" in (_sev(None) or "")))
        ok.append(("an omitted severity is accepted", _sev(_MISSING) is None))
    with tempfile.TemporaryDirectory() as td:
        kr = _mark_khenrix(Path(td))
        (kr / "shared" / "skills").mkdir(parents=True)
        (kr / "capabilities.toml").write_text("[models]\n")
        _init_repo(kr)
        target = "gap-fixture"

        def _append(fid: str, **extra) -> dict:
            return log_append(kr, target, {
                "target": target, "finding_id": fid, "decision": "applied", **extra})

        _append(RUN_START, ts="2026-01-01T00:00:00+00:00")
        _append(CYCLE_END, cycle=1)
        _append(RUN_END, converged=True, cycles=1,
                ts="2026-01-01T00:01:00+00:00")
        _append(RUN_START, ts="2026-01-01T00:02:00+00:00")
        _append("old-work", severity="blocking")
        _append(RUN_START, ts="2026-01-01T00:03:00+00:00")
        _append("current-work", severity="serious")
        _append(CYCLE_END, cycle=1)

        def _persisted_resolution(**overrides) -> dict:
            gap = _pre_start_gap(log_entries(kr, target))
            applied_indices = sorted(record["gap_index"]
                                     for record in gap["applied_records"])
            marker = {
                "target": target,
                "finding_id": RUN_GAP_RESOLUTION,
                "decision": "applied",
                "run_start_ts": gap["run_start_ts"],
                "gap_sha256": gap["gap_sha256"],
                "gap_records": gap["gap_records"],
                "applied_findings": gap["applied_findings"],
                "resolution": RUN_GAP_RESOLUTION_VALUE,
                "schema_version": RUN_GAP_RESOLUTION_SCHEMA,
                "prior_gap_indices": applied_indices,
                "current_gap_indices": [],
                "title": "resolved exact pre-start gap",
                "reason": "verified as prior-run history",
            }
            marker.update(overrides)
            return marker

        def _resolution_error(**overrides) -> str:
            try:
                log_append(kr, target, _persisted_resolution(**overrides))
            except ValueError as ex:
                return str(ex)
            return ""

        current_gap = _pre_start_gap(log_entries(kr, target))
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _gap_rc = main(["convergence-status", "--repo", str(kr), "--target", target])
        _gap_output = _buf.getvalue()
        ok.append(("convergence-status emits every exact resolution field",
                   _gap_rc == 1
                   and RUN_GAP_RESOLUTION in _gap_output
                   and current_gap["applied_records"][0]["gap_index"] == 1
                   and all(str(current_gap[key]) in _gap_output for key in (
                       "run_start_ts", "gap_sha256", "gap_records", "applied_findings"))))
        recipe = json.loads(next(line.strip() for line in _gap_output.splitlines()
                                 if line.strip().startswith("{")))
        recipe["reason"] = "verified as prior-run history"
        recipe["prior_gap_indices"] = [record["gap_index"]
                                       for record in current_gap["applied_records"]]
        try:
            _validate_run_gap_resolution(kr, target, recipe)
            recipe_error = ""
        except ValueError as ex:
            recipe_error = str(ex)
        ok.append(("the printed v3 recipe accepts one exhaustive ownership partition",
                   not recipe_error
                   and recipe["resolution"] == RUN_GAP_RESOLUTION_VALUE
                   and recipe["schema_version"] == RUN_GAP_RESOLUTION_SCHEMA
                   and recipe["current_gap_indices"] == []
                   and convergence_status(log_entries(kr, target))["run_gap"]
                   ["resolution_open"] is True))

        lifecycle_target = "lifecycle-gap-fixture"
        lifecycle_entries = [
            {"target": lifecycle_target, "finding_id": RUN_START,
             "decision": "applied", "ts": "2026-01-01T01:00:00+00:00"},
            {"target": lifecycle_target, "finding_id": CYCLE_END,
             "decision": "applied", "title": "historical missing cycle"},
            {"target": lifecycle_target, "finding_id": RUN_START,
             "decision": "applied", "ts": "2026-01-01T02:00:00+00:00"},
        ]
        lifecycle_path = log_path(kr, lifecycle_target)
        lifecycle_path.write_text(
            "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in lifecycle_entries),
            encoding="utf-8")
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            lifecycle_rc = main(
                ["convergence-status", "--repo", str(kr), "--target", lifecycle_target])
        lifecycle_output = _buf.getvalue()
        lifecycle_recipes = [json.loads(line.strip()) for line in lifecycle_output.splitlines()
                             if line.strip().startswith("{")]
        lifecycle_resolution = next(entry for entry in lifecycle_recipes
                                    if entry["finding_id"] == RUN_GAP_RESOLUTION)
        lifecycle_surrogate = next(entry for entry in lifecycle_recipes
                                   if entry["finding_id"].startswith(RUN_GAP_REPLAY_PREFIX))
        lifecycle_gap = _pre_start_gap(log_entries(kr, lifecycle_target))
        lifecycle_index = lifecycle_gap["applied_records"][0]["gap_index"]
        ok.append(("CLI v3 recipe is intentionally incomplete until ownership is exhaustive",
                   lifecycle_rc == 1
                   and lifecycle_resolution["schema_version"] == RUN_GAP_RESOLUTION_SCHEMA
                   and lifecycle_resolution["prior_gap_indices"] == []
                   and lifecycle_resolution["current_gap_indices"] == []
                   and "classify EVERY" in lifecycle_output))
        ok.append(("CLI emits a bound ordinary serious recipe for lifecycle ownership",
                   lifecycle_surrogate["severity"] == "serious"
                   and lifecycle_surrogate["gap_sha256"] == lifecycle_gap["gap_sha256"]
                   and lifecycle_surrogate["gap_index"] == lifecycle_index
                   and lifecycle_surrogate["replays_target"] == lifecycle_target
                   and lifecycle_surrogate["replays_finding_id"] == CYCLE_END
                   and "reason" not in lifecycle_surrogate))
        lifecycle_surrogate["reason"] = "the malformed delimiter belongs to this run"
        log_append(kr, lifecycle_target, lifecycle_surrogate)
        lifecycle_resolution.update({
            "prior_gap_indices": [],
            "current_gap_indices": [lifecycle_index],
            "reason": "the current lifecycle occurrence has its exact serious surrogate",
        })
        log_append(kr, lifecycle_target, lifecycle_resolution)
        log_append(kr, lifecycle_target, {
            "target": lifecycle_target, "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1})
        lifecycle_written_status = convergence_status(log_entries(kr, lifecycle_target))
        ok.append(("public log writer accepts the emitted surrogate plus exhaustive v3 marker",
                   lifecycle_written_status["run_gap"]["resolved"] is True
                   and lifecycle_written_status["counts"] == [1]
                   and lifecycle_written_status["converged"] is False
                   and not lifecycle_written_status["warnings"]))

        missing_indices_recipe = dict(recipe)
        missing_indices_recipe.pop("prior_gap_indices")
        try:
            _validate_run_gap_resolution(kr, target, missing_indices_recipe)
            missing_indices_error = ""
        except ValueError as ex:
            missing_indices_error = str(ex)
        ok.append(("log append refuses v3 without both ownership lists",
                   "is required" in missing_indices_error))

        legacy_write = _persisted_resolution(resolution=RUN_GAP_RESOLUTION_LEGACY_VALUE)
        for key in ("schema_version", "prior_gap_indices", "current_gap_indices"):
            legacy_write.pop(key)
        try:
            log_append(kr, target, legacy_write)
            legacy_write_error = ""
        except ValueError as ex:
            legacy_write_error = str(ex)
        ok.append(("log append rejects new v1 resolution markers",
                   "reader-only" in legacy_write_error))

        v2_write = _persisted_resolution()
        for key in ("schema_version", "prior_gap_indices", "current_gap_indices"):
            v2_write.pop(key)
        v2_write["re_recorded_gap_indices"] = []
        try:
            log_append(kr, target, v2_write)
            v2_write_error = ""
        except ValueError as ex:
            v2_write_error = str(ex)
        ok.append(("log append rejects new v2 resolution markers",
                   "reader-only" in v2_write_error))

        for label, overrides in (
            ("digest", {"gap_sha256": "0" * 64}),
            ("run start", {"run_start_ts": "wrong"}),
            ("record count", {"gap_records": 99}),
            ("applied count", {"applied_findings": 99}),
            ("boolean record count", {"gap_records": True}),
            ("decision", {"decision": "deferred"}),
            ("resolution value", {"resolution": "ignore-it"}),
            ("schema version", {"schema_version": 2}),
            ("boolean ownership index", {"prior_gap_indices": [True]}),
            ("negative ownership index", {"prior_gap_indices": [-1]}),
            ("out-of-range ownership index", {"prior_gap_indices": [99]}),
            ("duplicate ownership indices", {"prior_gap_indices": [1, 1]}),
            ("unsorted ownership indices", {"prior_gap_indices": [2, 1]}),
            ("overlapping ownership", {"current_gap_indices": [1]}),
            ("incomplete ownership", {"prior_gap_indices": []}),
            ("v2 field in v3", {"re_recorded_gap_indices": []}),
            ("title", {"title": ""}),
            ("reason", {"reason": ""}),
            ("severity", {"severity": "minor"}),
        ):
            ok.append((f"log append refuses a wrong gap-resolution {label}",
                       bool(_resolution_error(**overrides))))

        clean_target = "clean-fixture"
        log_append(kr, clean_target, {
            "target": clean_target, "finding_id": RUN_START, "decision": "applied",
            "ts": "2026-01-01T00:00:00+00:00"})
        no_gap = {**_persisted_resolution(), "target": clean_target}
        try:
            log_append(kr, clean_target, no_gap)
            _no_gap_error = ""
        except ValueError as ex:
            _no_gap_error = str(ex)
        ok.append(("log append refuses a resolution when no ambiguity exists",
                   "no pre-start ambiguity" in _no_gap_error))

        terminal_target = "terminal-gap-fixture"
        for entry in (
            {"target": terminal_target, "finding_id": RUN_START, "decision": "applied",
             "ts": "2026-01-01T00:00:00+00:00"},
            {"target": terminal_target, "finding_id": "stranded", "decision": "applied",
             "severity": "serious"},
            {"target": terminal_target, "finding_id": RUN_START, "decision": "applied",
             "ts": "2026-01-01T00:01:00+00:00"},
            {"target": terminal_target, "finding_id": CYCLE_END, "decision": "applied",
             "cycle": 1},
            {"target": terminal_target, "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1},
        ):
            log_append(kr, terminal_target, entry)
        terminal_entries = log_entries(kr, terminal_target)
        terminal_gap = _pre_start_gap(terminal_entries)
        terminal_indices = sorted(record["gap_index"]
                                  for record in terminal_gap["applied_records"])
        terminal_marker = {
            "target": terminal_target,
            "finding_id": RUN_GAP_RESOLUTION,
            "decision": "applied",
            "run_start_ts": terminal_gap["run_start_ts"],
            "gap_sha256": terminal_gap["gap_sha256"],
            "gap_records": terminal_gap["gap_records"],
            "applied_findings": terminal_gap["applied_findings"],
            "resolution": RUN_GAP_RESOLUTION_VALUE,
            "schema_version": RUN_GAP_RESOLUTION_SCHEMA,
            "prior_gap_indices": terminal_indices,
            "current_gap_indices": [],
            "title": RUN_GAP_RESOLUTION_TITLE,
            "reason": "the stranded record was prior-run work",
        }
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _terminal_rc = main(
                ["convergence-status", "--repo", str(kr), "--target", terminal_target])
        _terminal_output = _buf.getvalue()
        try:
            _validate_run_gap_resolution(kr, terminal_target, terminal_marker)
            _terminal_error = ""
        except ValueError as ex:
            _terminal_error = str(ex)
        ok.append(("terminal unresolved gaps expose no appendable CLI recipe",
                   _terminal_rc == 1
                   and convergence_status(terminal_entries)["run_gap"]["resolution_open"]
                   is False
                   and "no resolution recipe" in _terminal_output
                   and not any(line.strip().startswith("{")
                               for line in _terminal_output.splitlines())
                   and "already terminal" in _terminal_error))

        accepted = log_append(kr, target, _persisted_resolution())
        ok.append(("log append accepts the exact current gap resolution",
                   accepted["finding_id"] == RUN_GAP_RESOLUTION
                   and not convergence_status(log_entries(kr, target))["warnings"]))
        ok.append(("log append refuses a duplicate exact resolution",
                   "already resolved" in _resolution_error()))
        _append(RUN_END, converged=True, cycles=1)
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _resolved_terminal_rc = main(
                ["convergence-status", "--repo", str(kr), "--target", target])
        _resolved_terminal_output = _buf.getvalue()
        ok.append(("a valid pre-terminal marker stays resolved without a terminal recipe",
                   convergence_status(log_entries(kr, target))["run_gap"]["resolved"] is True
                   and "required resolution fields" not in _resolved_terminal_output
                   and not any(line.strip().startswith("{")
                               for line in _resolved_terminal_output.splitlines())))
        ok.append(("log append refuses a resolution after the current run ended",
                   "already terminal" in _resolution_error()))

        # Historical logs may contain an explicit null timestamp from before writer
        # validation. The exact fingerprint still binds that anchor, so reading and
        # resolving it must remain possible even though new null timestamps are refused.
        null_target = "null-ts-gap-fixture"
        null_entries = [
            {"target": null_target, "finding_id": RUN_START, "decision": "applied",
             "ts": "2026-01-01T00:00:00+00:00"},
            {"target": null_target, "finding_id": CYCLE_END, "decision": "applied",
             "cycle": 1},
            {"target": null_target, "finding_id": "historical-work",
             "decision": "applied", "severity": "serious"},
            {"target": null_target, "finding_id": RUN_START, "decision": "applied",
             "ts": None},
            {"target": null_target, "finding_id": CYCLE_END, "decision": "applied",
             "cycle": 1},
        ]
        null_path = log_path(kr, null_target)
        null_path.parent.mkdir(parents=True, exist_ok=True)
        null_path.write_text(
            "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in null_entries),
            encoding="utf-8")
        null_gap = _pre_start_gap(log_entries(kr, null_target))
        null_marker = {
            "target": null_target,
            "finding_id": RUN_GAP_RESOLUTION,
            "decision": "applied",
            "run_start_ts": null_gap["run_start_ts"],
            "gap_sha256": null_gap["gap_sha256"],
            "gap_records": null_gap["gap_records"],
            "applied_findings": null_gap["applied_findings"],
            "resolution": RUN_GAP_RESOLUTION_VALUE,
            "schema_version": RUN_GAP_RESOLUTION_SCHEMA,
            "prior_gap_indices": [record["gap_index"]
                                  for record in null_gap["applied_records"]],
            "current_gap_indices": [],
            "title": RUN_GAP_RESOLUTION_TITLE,
            "reason": "the exact null-anchored record is verified prior history",
        }
        log_append(kr, null_target, null_marker)
        ok.append(("a historical null run-start remains exactly resolvable",
                   convergence_status(log_entries(kr, null_target))["converged"] is True))
    ok.append(("a null severity counts as serious, not zero",
               convergence_status(
                   [{"finding_id": RUN_START, "decision": "applied"},
                    {"finding_id": "f", "decision": "applied", "severity": None},
                    {"finding_id": CYCLE_END, "decision": "applied", "cycle": 1}]
               )["verdict"] == "keep-iterating"))
    # lock: the token is what makes a steal detectable — touch -c could not
    _saved = globals()["LOCK_DIR"]
    with tempfile.TemporaryDirectory() as td:
        globals()["LOCK_DIR"] = Path(td) / "lock.d"
        got, owner = lock_acquire()
        ok.append(("lock acquires", got))
        ok.append(("second acquire is refused", lock_acquire()[0] is False))
        ok.append(("refresh with the owner token succeeds", lock_refresh(owner)[0]))
        # exercise the token in the shape acquire PRINTS, not just the shape it returns:
        # the operator only ever sees `OWNER=<token>`, so testing the bare form alone
        # left the real interface broken while the suite stayed green.
        ok.append(("refresh accepts the printed OWNER= form",
                   lock_refresh(f"OWNER={owner}")[0]))
        ok.append(("refresh accepts a trailing newline",
                   lock_refresh(f"OWNER={owner}\n")[0]))
        ok.append(("refresh with a wrong token reports a steal",
                   lock_refresh("bogus")[0] is False))
        ok.append(("OWNER= prefix does not mask a wrong token",
                   lock_refresh("OWNER=bogus")[0] is False))
        ok.append(("non-owner cannot release", lock_release("bogus")[0] is False))
        ok.append(("release accepts the printed OWNER= form",
                   lock_release(f"OWNER={owner}")[0]))
        got, owner = lock_acquire()  # re-take: the line above released it
        ok.append(("re-acquire after release succeeds", got))
        ok.append(("owner releases", lock_release(owner)[0]))
        ok.append(("refresh after release reports it gone", lock_refresh(owner)[0] is False))
    globals()["LOCK_DIR"] = _saved
    # triage must not recommend work on zero evidence — the sort is (-score, skill), so an
    # all-zero board would otherwise crown whichever skill sorts first alphabetically.
    # The union, not concatenation: a name under BOTH source dirs is one skill. Reverting
    # to `sorted(a) + sorted(b)` leaves every other triage assertion green, so this is the
    # only thing standing between that revert and a duplicated board.
    with tempfile.TemporaryDirectory() as _td:
        _r = _mark_khenrix(Path(_td))
        _make_skill(_r, "shared/skills/dup")
        (_r / "shared" / "skill-templates" / "dup").mkdir(parents=True)
        _make_skill(_r, "shared/skills/Bad_Name")
        (_r / "shared" / "skills" / "bare").mkdir(parents=True)
        _make_skill(_r, "shared/skill-templates/wrong-manifest")
        (_r / "capabilities.toml").write_text("[models]\nclaude = []\n")
        _init_repo(_r)
        _commit_fixture(_r)
        try:
            _rows = triage(_r)
            ok.append(("triage: a usable name plus a manifest-less duplicate yields ONE row",
                       [r["skill"] for r in _rows].count("dup") == 1))
            ok.append(("triage: directories without the proper manifest are ignored",
                       not {"bare", "wrong-manifest", "Bad_Name"}
                       & {row["skill"] for row in _rows}))
        except Exception as _e:  # noqa: BLE001
            ok.append(((f"triage: a usable name plus a manifest-less duplicate yields "
                        f"ONE row ({_e})"), False))
            ok.append((f"triage: invalid directories are ignored ({_e})", False))
    with tempfile.TemporaryDirectory() as _td:
        _r = _mark_khenrix(Path(_td))
        _make_skill(_r, "shared/skills/ambiguous")
        _make_skill(_r, "shared/skill-templates/ambiguous", template=True)
        (_r / "capabilities.toml").write_text("[models]\nclaude = []\n")
        _init_repo(_r)
        _commit_fixture(_r)
        try:
            triage(_r)
            _ambiguous_visible = False
        except ValueError as _e:
            _ambiguous_visible = (
                "target-info refuses" in str(_e)
                and "ambiguous" in str(_e)
                and "shared/skills/ambiguous" in str(_e)
                and "shared/skill-templates/ambiguous" in str(_e))
        ok.append(("triage: two usable layouts are reported, never silently omitted",
                   _ambiguous_visible))
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            _rc = main(["verify-final-receipt", "--repo", str(_r),
                        "--skill", "ambiguous"])
        ok.append(("final receipt cannot bypass an ambiguous full-gate target",
                   _rc == 2 and "MORE THAN ONE layout" in _buf.getvalue()
                   and "Traceback" not in _buf.getvalue()))
    ok.append(("triage: a signal-free board scores 0 for every row",
               triage_score("fresh", 0, 0, 100) == 0))
    ok.append(("triage: the line-budget rule is what lifts a fresh skill off 0",
               triage_score("fresh", 0, 0, 480) == 10))
    ok.append(("triage: a stale receipt still outranks a line budget",
               triage_score("stale-source", 0, 0, 100) > triage_score("fresh", 0, 0, 480)))
    # An UNKNOWN baseline age must not read as a fresh one. triage() swallows git errors,
    # so without this a checkout where git fails for every skill scores an all-zero board
    # and triage_recommendation reports "nothing to tune up" — a confident wrong all-clear.
    # Missing evidence must not become staleness points, and must not become a winner.
    ok.append(("triage: an unknown age scores the SAME as a known-fresh one (no points)",
               triage_score("fresh", None, 0, 100) == triage_score("fresh", 0, 0, 100)))
    ok.append(("triage: 70 days of real neglect still outranks an unknown age",
               triage_score("fresh", 70, 0, 100) > triage_score("fresh", None, 0, 100)))
    _unk = [{"skill": "aaa", "score": 0, "age_days": None},
            {"skill": "zzz", "score": 0, "age_days": None}]
    ok.append(("triage: an all-unknown board reports the DIAGNOSIS, not a winner",
               "recommend" not in triage_recommendation(_unk)
               and "UNKNOWN for every skill" in triage_recommendation(_unk)))
    ok.append(("triage: an all-unknown board is not reported as a clean all-clear",
               "nothing to tune up" not in triage_recommendation(_unk)))
    # A MIXED board must still rank. `all(...)` -> `any(...)` survives every assertion
    # above, and under `any` a single skill with no history would suppress a valid
    # recommendation for the whole board.
    _mixed = [{"skill": "hot", "score": 40, "age_days": None},
              {"skill": "cold", "score": 2, "age_days": 12.0}]
    ok.append(("triage: a MIXED board still recommends, not diagnoses",
               "recommend" in triage_recommendation(_mixed)
               and "UNKNOWN for every skill" not in triage_recommendation(_mixed)))
    # A real signal must survive a TOTAL age blackout: score 55 comes from receipt state,
    # stale model ids and the line budget, none of which touch git. Suppressing the
    # recommendation there withheld an answer the tool had good grounds for.
    _blackout = [{"skill": "stale-one", "score": 55, "age_days": None},
                 {"skill": "other", "score": 10, "age_days": None}]
    ok.append(("triage: a decisive signal SURVIVES an all-unknown age board",
               "recommend" in triage_recommendation(_blackout)
               and "stale-one" in triage_recommendation(_blackout)))
    ok.append(("triage: and it discloses that the age component is missing",
               "age component" in triage_recommendation(_blackout)))
    ok.append(("triage: an all-unknown board with NO signal is still the bare diagnosis",
               "recommend" not in triage_recommendation(
                   [{"skill": "a", "score": 0, "age_days": None}])))
    ok.append(("triage: a partial blackout with no signal refuses a clean all-clear",
               "INCOMPLETE" in triage_recommendation(
                   [{"skill": "a", "score": 0, "age_days": None},
                    {"skill": "b", "score": 0, "age_days": 3.0}])))
    ok.append(("triage: a fully-known board with no signal IS a clean all-clear",
               triage_recommendation([{"skill": "a", "score": 0, "age_days": 3.0}])
               == "no skill shows a staleness signal — nothing to tune up."))
    ok.append(("triage: a partial blackout WITH a signal names how many are unknown",
               "1 of 2" in triage_recommendation(
                   [{"skill": "a", "score": 40, "age_days": None},
                    {"skill": "b", "score": 0, "age_days": 3.0}])))
    with tempfile.TemporaryDirectory() as td:
        try:
            triage(Path(td))
            ok.append(("triage: refuses a non-khenrix repo instead of reporting nothing", False))
        except ValueError:
            ok.append(("triage: refuses a non-khenrix repo instead of reporting nothing", True))
    # Assert the DECISION, not the score: a score assertion still passes with the
    # threshold removed, which is exactly how this first shipped insensitive.
    _zero = [{"skill": "aaa-first", "score": 0}, {"skill": "zzz-last", "score": 0}]
    ok.append(("triage: an all-zero board recommends NOTHING",
               "recommend" not in triage_recommendation(_zero)))
    ok.append(("triage: an all-zero board says so explicitly",
               "nothing to tune up" in triage_recommendation(_zero)))
    ok.append(("triage: a real signal still produces a recommendation",
               "recommend: deep tune-up of 'x'" in
               triage_recommendation([{"skill": "x", "score": 10}])))
    # the staleness window must exceed the longest step this skill's own guidance produces:
    # deep timeout 1800s x (retries 1 + 1) = 60 min for a self-tuneup, 90 at the default.
    # Assert the DOCUMENTED 135, not a weaker bound. A default deep fan-out is already
    # 3 x 1800s plus backoff = 90 min 15 s before teardown and worktree setup, so `> 90`
    # still admitted values a single legal fan-out can exhaust — which is exactly how the
    # previous 90 became wrong when MODE_TIMEOUT["deep"] moved 1200 -> 1800 and nothing
    # here noticed. The number in the failure table and the number here have to be the
    # same number.
    ok.append(("lock: the staleness window is the documented 135 min", LOCK_STALE_MIN == 135))
    # AND ENFORCE THE INVARIANT MECHANICALLY, not just in the comment above the constant.
    # This is the defect that produced the 135: LOCK_STALE_MIN was coupled to
    # MODE_TIMEOUT["deep"] by PROSE ONLY, the council engine moved 1200 -> 1800 on
    # 2026-08-13, and nothing failed — the window silently stopped exceeding a single
    # legal fan-out. A comment cannot notice a number changing in another file; this can.
    # Skipped (not failed) when the engine is unreachable, because tuneup.py also runs
    # against non-khenrix repos that have no council engine.
    _eng = Path(__file__).resolve().parents[3] / "lib" / "council" / "engine.py"
    if _eng.is_file():
        _ns: dict = {}
        for _ln in _eng.read_text().splitlines():
            if _ln.startswith("MODE_TIMEOUT"):
                exec(_ln, _ns)  # noqa: S102 - a literal dict assignment from our own repo
                break
        _deep = (_ns.get("MODE_TIMEOUT") or {}).get("deep")
        if _deep:
            _longest = 3 * _deep / 60          # default --retries 2 => 3 attempts
            ok.append(((f"lock: window {LOCK_STALE_MIN} min strictly exceeds one default "
                        f"deep fan-out ({_longest:.0f} min at MODE_TIMEOUT deep={_deep}s)"),
                       LOCK_STALE_MIN > _longest))
    # lock status must be a QUESTION, never an action: it is the answer to "is the holder
    # alive?", and the old answer (`lock acquire`) destroyed the lock past the window.
    with tempfile.TemporaryDirectory() as _std:
        _saved = LOCK_DIR
        try:
            globals()["LOCK_DIR"] = Path(_std) / "lock.d"
            ok.append(("lock status: reports not-held without creating anything",
                       lock_status() == {"held": False} and not LOCK_DIR.exists()))
            _got, _tok = lock_acquire()
            _st = lock_status()
            ok.append(("lock status: reports the holder and an age",
                       _st["held"] and _st["owner"] == _tok and _st["age_min"] >= 0))
            _old = time.time() - 999 * 60
            os.utime(LOCK_DIR, (_old, _old))
            ok.append(("lock status: does NOT steal a lock far past the stale window",
                       lock_status()["held"] and LOCK_DIR.is_dir()
                       and (LOCK_DIR / "owner").read_text().strip() == _tok))
        finally:
            shutil.rmtree(LOCK_DIR, ignore_errors=True)
            globals()["LOCK_DIR"] = _saved
    # Exercise the boundary through lock_acquire's DEFAULT, so the failure-table's "older
    # than 135 min" and the constant cannot drift apart, and so a change to the default
    # argument is caught too. 134 -> still held; 136 -> stolen.
    with tempfile.TemporaryDirectory() as _ltd:
        _saved = LOCK_DIR
        try:
            globals()["LOCK_DIR"] = Path(_ltd) / "lock.d"
            for _age, _want_held in ((134, True), (136, False)):
                shutil.rmtree(LOCK_DIR, ignore_errors=True)
                lock_acquire()
                _old = time.time() - _age * 60
                os.utime(LOCK_DIR, (_old, _old))
                _got, _ = lock_acquire()          # default stale_min, not an override
                ok.append(((f"lock: a {_age}-min-old lock is "
                            f"{'still held' if _want_held else 'stealable'}"),
                           _got is not _want_held))
        finally:
            shutil.rmtree(LOCK_DIR, ignore_errors=True)
            globals()["LOCK_DIR"] = _saved

    # Ambiguous target: every foreign-layout combination must be REFUSED, not silently
    # resolved. Testing only one pair let a newly added third layout bypass this contract.
    with tempfile.TemporaryDirectory() as td:
        layouts = (".agents/skills/x", ".claude/skills/x", "skills/x")
        combinations = (
            layouts[:2],
            (layouts[0], layouts[2]),
            layouts[1:],
            layouts,
        )
        for index, selected in enumerate(combinations):
            fr = Path(td) / f"foreign-{index}"
            for layout in selected:
                _make_skill(fr, layout)
            _init_repo(fr)
            _commit_fixture(fr)
            info = target_info(fr, "x")
            label = f"{len(selected)}-layout combination {index}"
            ok.append((f"{label} is reported ambiguous", info["ambiguous"] is True))
            ok.append((f"{label} reports its exact paths", set(info["paths"]) == set(selected)))
            # Capture the expected refusal so the suite's output stays readable.
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf):
                _rc = main(["target-info", "--repo", str(fr), "--skill", "x"])
            refusal = _buf.getvalue()
            ok.append((f"{label} exits nonzero", _rc != 0))
            ok.append((f"{label} refusal explains itself",
                       "MORE THAN ONE layout" in refusal
                       and all(path in refusal for path in selected)))
            for command in ("baseline", "stale-models", "verify-final-receipt"):
                _buf = io.StringIO()
                with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                    _direct_rc = main(
                        [command, "--repo", str(fr), "--skill", "x"])
                _direct = _buf.getvalue()
                ok.append((f"{command} shares the {label} refusal without a traceback",
                           _direct_rc == 2 and "MORE THAN ONE layout" in _direct
                           and "Traceback" not in _direct
                           and all(path in _direct for path in selected)))

        # A plain manifest-less sibling is not a second skill and must not poison the one
        # real source. This is the deliberate distinction from every linked candidate.
        valid = Path(td) / "valid-plus-bare"
        _make_skill(valid, "skills/x")
        (valid / ".agents" / "skills" / "x").mkdir(parents=True)
        subprocess.run(["git", "-C", str(valid), "init", "-q", "."], check=True)
        subprocess.run(["git", "-C", str(valid), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(valid), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "add x"], check=True)
        ok.append(("one valid skill plus a plain manifest-less sibling resolves",
                   _require_skill_paths(valid, "x")[1] == [valid / "skills" / "x"]
                   and baseline(valid, "x") is not None
                   and isinstance(scan_stale_models(valid, "x", set()), list)))

    # Repository ownership is a whole route, not merely a filesystem containment check.
    # Exercise the public commands because a private classifier test would not prove that
    # review-material and receipt verification share the same fail-closed resolver.
    with tempfile.TemporaryDirectory() as td:
        ownership_root = Path(td)

        def _base_repo(name: str) -> Path:
            repo = ownership_root / name
            repo.mkdir()
            (repo / "README.md").write_text("fixture\n")
            _init_repo(repo)
            _commit_fixture(repo, "README.md", message="base")
            return repo

        ancestor_dotgit = _base_repo("ancestor-dotgit")
        _make_skill(ancestor_dotgit, ".agents/skills/x")
        _init_repo(ancestor_dotgit / ".agents" / "skills")
        _commit_fixture(
            ancestor_dotgit / ".agents" / "skills", "x/SKILL.md", message="inner")

        ancestor_gitlink = _base_repo("ancestor-gitlink")
        _make_skill(ancestor_gitlink, ".agents/skills/x")
        gitlink_oid = _git(ancestor_gitlink, "rev-parse", "HEAD").strip()
        subprocess.run(
            ["git", "-C", str(ancestor_gitlink), "update-index", "--add", "--cacheinfo",
             f"160000,{gitlink_oid},.agents/skills"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(ancestor_gitlink), "-c", "user.email=t@t",
             "-c", "user.name=t", "commit", "-qm", "ancestor gitlink"],
            check=True, capture_output=True)

        untracked_manifest = _base_repo("untracked-manifest")
        _make_skill(untracked_manifest, ".agents/skills/x")

        ignored_manifest = _base_repo("ignored-manifest")
        (ignored_manifest / ".gitignore").write_text(".agents/skills/x/\n")
        _commit_fixture(ignored_manifest, ".gitignore", message="ignore target")
        _make_skill(ignored_manifest, ".agents/skills/x")

        index_only = _base_repo("index-only-manifest")
        _make_skill(index_only, ".agents/skills/x")
        subprocess.run(
            ["git", "-C", str(index_only), "add", ".agents/skills/x/SKILL.md"],
            check=True, capture_output=True)

        head_only = _base_repo("head-only-manifest")
        _make_skill(head_only, ".agents/skills/x")
        _commit_fixture(head_only, ".agents/skills/x/SKILL.md", message="add x")
        subprocess.run(
            ["git", "-C", str(head_only), "rm", "--cached", "-q",
             ".agents/skills/x/SKILL.md"], check=True, capture_output=True)

        ignored_source = _base_repo("ignored-source")
        skill_source = _make_skill(ignored_source, ".agents/skills/x")
        (ignored_source / ".gitignore").write_text(
            ".agents/skills/x/scripts/hidden.py\n")
        _commit_fixture(
            ignored_source, ".gitignore", ".agents/skills/x/SKILL.md", message="add x")
        (skill_source / "scripts").mkdir()
        (skill_source / "scripts" / "hidden.py").write_text("MODEL = 'gpt-9.9'\n")

        ignored_extensionless = _base_repo("ignored-extensionless")
        skill_source = _make_skill(ignored_extensionless, ".agents/skills/x")
        (ignored_extensionless / ".gitignore").write_text(
            ".agents/skills/x/scripts/validate\n")
        _commit_fixture(ignored_extensionless, ".gitignore", ".agents/skills/x/SKILL.md",
                        message="add ignored extensionless source")
        (skill_source / "scripts").mkdir()
        executable = skill_source / "scripts" / "validate"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)

        ignored_yaml = _base_repo("ignored-yaml")
        skill_source = _make_skill(ignored_yaml, ".agents/skills/x")
        (ignored_yaml / ".gitignore").write_text(
            ".agents/skills/x/references/policy.yaml\n")
        _commit_fixture(ignored_yaml, ".gitignore", ".agents/skills/x/SKILL.md",
                        message="add ignored yaml source")
        (skill_source / "references").mkdir()
        (skill_source / "references" / "policy.yaml").write_text("mode: strict\n")

        ignored_symlink = _base_repo("ignored-symlink")
        skill_source = _make_skill(ignored_symlink, ".agents/skills/x")
        (ignored_symlink / ".gitignore").write_text(
            ".agents/skills/x/references/external\n")
        _commit_fixture(ignored_symlink, ".gitignore", ".agents/skills/x/SKILL.md",
                        message="add ignored symlink source")
        (skill_source / "references").mkdir()
        (skill_source / "references" / "external").symlink_to("/outside")

        ignored_disposable = _base_repo("ignored-disposable")
        skill_source = _make_skill(ignored_disposable, ".agents/skills/x")
        (ignored_disposable / ".gitignore").write_text(
            ".agents/skills/x/__pycache__/\n.agents/skills/x/legacy.pyc\n")
        _commit_fixture(ignored_disposable, ".gitignore", ".agents/skills/x/SKILL.md",
                        message="add disposable ignores")
        (skill_source / "__pycache__").mkdir()
        (skill_source / "__pycache__" / "module.data").write_bytes(b"cache")
        (skill_source / "legacy.pyc").write_bytes(b"cache")

        refusal_cases = (
            ("ancestor .git", ancestor_dotgit, ".git boundary"),
            ("ancestor gitlink", ancestor_gitlink, "gitlink"),
            ("untracked manifest", untracked_manifest, "outer HEAD and outer index"),
            ("ignored manifest", ignored_manifest, "outer HEAD and outer index"),
            ("index-only manifest", index_only, "outer HEAD"),
            ("HEAD-only manifest", head_only, "outer index"),
            ("ignored consumable source", ignored_source, "contains ignored source"),
            ("ignored extensionless executable", ignored_extensionless,
             "contains ignored source"),
            ("ignored YAML source", ignored_yaml, "contains ignored source"),
        )
        for label, repo, expected in refusal_cases:
            info = target_info(repo, "x")
            diagnostic = "\n".join(info["near_misses"])
            ok.append((f"ownership: {label} is not a usable target",
                       not info["found"] and expected in diagnostic))
            target = log_target_key(repo, "x")
            commands = (
                ["target-info", "--repo", str(repo), "--skill", "x"],
                ["baseline", "--repo", str(repo), "--skill", "x"],
                ["stale-models", "--repo", str(repo), "--skill", "x"],
                ["verify-final-receipt", "--repo", str(repo), "--skill", "x"],
                ["review-material", "--repo", str(repo), "--skill", "x",
                 "--target", target],
            )
            for command in commands:
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    rc = main(command)
                ok.append((f"ownership: {command[0]} refuses {label} centrally",
                           rc == 2 and expected in output.getvalue()
                           and "Traceback" not in output.getvalue()))

        ignored_symlink_problem = _ignored_consumable_problem(
            ignored_symlink, ignored_symlink / ".agents" / "skills" / "x")
        ok.append(("ownership: ignored symlink is rejected as consumable source",
                   ignored_symlink_problem is not None
                   and "contains ignored source" in ignored_symlink_problem))
        ok.append(("ownership: ignored bytecode cache is disposable",
                   _ignored_consumable_problem(
                       ignored_disposable,
                       ignored_disposable / ".agents" / "skills" / "x") is None))

        new_source = _base_repo("new-visible-source")
        visible_skill = _make_skill(new_source, ".agents/skills/x")
        foreign_new_log_skill = _make_skill(
            new_source, ".agents/skills/foreign-new-log")
        foreign_marketplace = new_source / "marketplaces" / "owned.md"
        foreign_marketplace.parent.mkdir()
        foreign_marketplace.write_text("base marketplace\n")
        _commit_fixture(new_source, ".agents/skills/x/SKILL.md",
                        ".agents/skills/foreign-new-log/SKILL.md",
                        "marketplaces/owned.md", message="add x")
        (visible_skill / "references").mkdir()
        new_reference = visible_skill / "references" / "new.md"
        new_reference.write_text("model: gpt-9.9\n")
        foreign_marketplace.write_text("FOREIGN-TRACKED-MARKETPLACE\n")
        (new_source / "marketplaces" / "new.md").write_text(
            "FOREIGN-UNTRACKED-MARKETPLACE\n")
        visible_target = log_target_key(new_source, "x")
        log_append(new_source, visible_target, {
            "target": visible_target, "finding_id": RUN_START,
            "decision": "applied", "title": "fixture"})
        visible_log = log_path(new_source, visible_target)
        _commit_fixture(
            self_test_registry, str(visible_log.relative_to(self_test_registry)),
            message="commit foreign x log")
        log_append(new_source, visible_target, {
            "target": visible_target, "finding_id": "append-only-growth",
            "decision": "rejected"})
        visible_info = target_info(new_source, "x")
        visible_review = review_material(new_source, "x", visible_target)
        ok.append(("self-test foreign logs stay in the injected registry",
                   visible_log.is_relative_to(self_test_registry)
                   and visible_log.is_file()))
        ok.append(("ownership: a new nonignored descendant remains a usable candidate",
                   visible_info["found"] and not visible_info["near_misses"]))
        ok.append(("ownership: stale-models reads the new visible descendant",
                   any(hit["file"].endswith("references/new.md")
                       for hit in scan_stale_models(new_source, "x", set()))))
        ok.append(("ownership: review-material transmits the new visible descendant",
                   "references/new.md" in visible_review and "gpt-9.9" in visible_review))
        ok.append(("council-only review includes ordinary tracked marketplaces content",
                   "FOREIGN-TRACKED-MARKETPLACE" in visible_review))
        ok.append(("council-only review includes ordinary untracked marketplaces content",
                   "FOREIGN-UNTRACKED-MARKETPLACE" in visible_review))
        saved_visible_log = visible_log.read_bytes()
        rewritten_visible_log = bytearray(saved_visible_log)
        rewritten_visible_log[0] = (
            ord("[") if rewritten_visible_log[0] != ord("[") else ord("{"))
        visible_log.write_bytes(bytes(rewritten_visible_log))
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
            _foreign_rewrite_rc = main([
                "review-material", "--repo", str(new_source), "--skill", "x",
                "--target", visible_target])
        ok.append(("public foreign review rejects a committed registry-log rewrite",
                   _foreign_rewrite_rc == 2
                   and "not append-only from HEAD" in _buf.getvalue()
                   and "Traceback" not in _buf.getvalue()))
        visible_log.write_bytes(saved_visible_log)
        visible_log.unlink()
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
            _foreign_delete_rc = main([
                "review-material", "--repo", str(new_source), "--skill", "x",
                "--target", visible_target])
        ok.append(("public foreign review rejects a deleted committed registry log",
                   _foreign_delete_rc == 2 and "missing or unreadable" in _buf.getvalue()
                   and "Traceback" not in _buf.getvalue()))
        visible_log.write_bytes(saved_visible_log)
        foreign_new_target = log_target_key(new_source, "foreign-new-log")
        log_append(new_source, foreign_new_target, {
            "target": foreign_new_target, "finding_id": RUN_START,
            "decision": "applied"})
        foreign_new_log_skill.joinpath("SKILL.md").write_text(
            "---\nname: foreign-new-log\n---\n")
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
            _foreign_new_rc = main([
                "review-material", "--repo", str(new_source), "--skill",
                "foreign-new-log", "--target", foreign_new_target])
        ok.append(("public foreign review permits a registry log absent from HEAD",
                   _foreign_new_rc == 0 and "foreign-new-log" in _buf.getvalue()
                   and "Traceback" not in _buf.getvalue()))

    # review-material: every case the shell loop it replaced got wrong, verified live
    with tempfile.TemporaryDirectory() as td:
        r = _mark_khenrix(Path(td) / "repo"); r.mkdir()
        (r / "shared" / "skills").mkdir(parents=True)
        (r / "capabilities.toml").write_text(
            "[models]\n[skill_facts.review-test.codex]\nheadline = 'base'\n")
        target = "review-test"
        target_source = _make_skill(
            r, f"shared/skill-templates/{target}", template=True)
        target_script = target_source / "scripts" / "tuneup.py"
        target_script.parent.mkdir()
        target_script.write_text("print('base')\n")
        special_relpath = (
            "shared/skill-templates/review-test/scripts/zz name with spaces "
            "\"and\" unicode-\u0394.py")
        special_script = r / special_relpath
        special_script.write_text("print('base special')\n")
        new_log_source = _make_skill(r, "shared/skills/new-log")
        generated_marketplace = r / "marketplaces" / "generated.md"
        generated_marketplace.parent.mkdir()
        generated_marketplace.write_text("generated base\n")
        subprocess.run(["git", "-C", str(r), "init", "-q", "."], check=True)
        textconv = Path(td) / "review-material-textconv"
        textconv.write_text("#!/bin/sh\nprintf 'TEXTCONV-SENTINEL\\n'\n")
        textconv.chmod(0o755)
        (r / ".gitattributes").write_text("t.txt diff=review-material-test\n")
        subprocess.run(
            ["git", "-C", str(r), "config", "diff.review-material-test.textconv",
             str(textconv)], check=True)
        (r / "t.txt").write_text("tracked\n")
        log_append(r, target, {
            "target": target, "finding_id": RUN_START, "decision": "applied",
            "title": "run start"})
        log_append(r, target, {
            "target": target, "finding_id": "alpha", "decision": "applied",
            "severity": "minor", "title": "first alpha"})
        log_append(r, target, {
            "target": target, "finding_id": "beta", "decision": "deferred",
            "severity": "minor", "title": "beta"})
        log_append(r, target, {
            "target": target, "finding_id": "alpha", "decision": "rejected",
            "severity": "minor", "title": "latest alpha"})
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "i"], check=True)
        ok.append(("review-material returns exactly empty for an unchanged candidate",
                   review_material(r, target, target) == ""))
        log_append(r, "new-log", {
            "target": "new-log", "finding_id": RUN_START, "decision": "applied"})
        new_log_source.joinpath("SKILL.md").write_text("# new-log changed\n")
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
            _new_log_rc = main([
                "review-material", "--repo", str(r), "--skill", "new-log",
                "--target", "new-log"])
        ok.append(("public review CLI permits a new active log absent from HEAD",
                   _new_log_rc == 0 and "new-log changed" in _buf.getvalue()))
        new_log_source.joinpath("SKILL.md").write_text(
            "---\nname: test-skill\n---\n")
        (r / "t.txt").write_text("tracked CANONICAL-DIFF-CONTENT\n")
        textconv_material = review_material(r, target, target)
        ok.append(("review-material ignores .gitattributes textconv",
                   "TEXTCONV-SENTINEL" not in textconv_material
                   and "CANONICAL-DIFF-CONTENT" in textconv_material))
        external_diff = Path(td) / "review-material-external-diff"
        external_diff.write_text("#!/bin/sh\nprintf 'EXTERNAL-DIFF-SENTINEL\\n'\n")
        external_diff.chmod(0o755)
        saved_external_diff = os.environ.get("GIT_EXTERNAL_DIFF")
        os.environ["GIT_EXTERNAL_DIFF"] = str(external_diff)
        try:
            external_diff_material = review_material(r, target, target)
        finally:
            if saved_external_diff is None:
                os.environ.pop("GIT_EXTERNAL_DIFF", None)
            else:
                os.environ["GIT_EXTERNAL_DIFF"] = saved_external_diff
        ok.append(("review-material ignores GIT_EXTERNAL_DIFF",
                   "EXTERNAL-DIFF-SENTINEL" not in external_diff_material
                   and "CANONICAL-DIFF-CONTENT" in external_diff_material))
        (r / "t.txt").write_text("tracked\n")
        with log_path(r, target).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "target": target, "finding_id": RUN_START,
                "decision": "rejected", "title": "malformed trailing start"}) + "\n")
            handle.write(json.dumps({
                "target": target, "finding_id": RUN_START,
                "decision": "deferred", "title": "another malformed start"}) + "\n")
        log_append(r, target, {
            "target": target, "finding_id": "large-ledger-entry", "decision": "rejected",
            "severity": "minor", "title": "LOG-DUPLICATE-SENTINEL" + "L" * 70000})
        target_script.write_text(
            "print('TARGET-EXECUTABLE-SENTINEL')\n" + "# " + "s" * 4000 + "\n")
        special_script.write_text(
            "print('SPECIAL-PATH-SENTINEL')\n" + "# " + "q" * 200000 + "\n")
        (r / "capabilities.toml").write_text(
            "[models]\n[skill_facts.review-test.codex]\n"
            "headline = 'TARGET-FACTS-SENTINEL'\n")
        secret = Path(td) / "outside-secret.txt"
        secret.write_text("SECRET-OUTSIDE-REPO\n")
        (r / "leak.txt").symlink_to(secret)          # points OUTSIDE the repo
        (r / "broken.txt").symlink_to(Path(td) / "nope")
        (r / "newline-only.md").write_text("\n")     # grep -Iq . called this BINARY
        (r / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00data")
        (r / "nul-8191.bin").write_bytes(b"A" * 8191 + b"\0TAIL")
        (r / "nul-8192.bin").write_bytes(b"A" * 8192 + b"\0TAIL")
        (r / "nul-final.bin").write_bytes(b"A" * 9000 + b"\0")
        (r / "invalid-utf8.bin").write_bytes(b"PAYLOAD\xffEND")
        (r / "large-nul-early.bin").write_bytes(
            b"\0" + b"A" * (REVIEW_TOTAL_CAP + 9000))
        (r / "large-nul-8192.bin").write_bytes(
            b"A" * 8192 + b"\0" + b"A" * REVIEW_TOTAL_CAP)
        (r / "large-nul-final.bin").write_bytes(
            b"A" * (REVIEW_TOTAL_CAP + 9000) + b"\0")
        (r / "large-invalid-8192.bin").write_bytes(
            b"A" * 8192 + b"\xff" + b"A" * REVIEW_TOTAL_CAP)
        (r / "name with spaces.md").write_text("spaced\n")
        (r / "untracked-sentinel.md").write_text("UNTRACKED-SENTINEL\n")
        generated_marketplace.write_text("FULL-GATE-TRACKED-MARKETPLACE-OMIT\n")
        (r / "marketplaces" / "new.md").write_text(
            "FULL-GATE-UNTRACKED-MARKETPLACE-OMIT\n")
        (r / "t.txt").write_text("tracked MODIFIED\n" + "a" * 200000 + "é\n")
        out = review_material(r, target, target)
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
            _special_path_rc = main([
                "review-material", "--repo", str(r), "--skill", target,
                "--target", target])
        public_special_review = _buf.getvalue()
        ok.append(("public review CLI preserves the exact special-character path",
                   _special_path_rc == 0
                   and json.dumps(special_relpath, ensure_ascii=True)
                       in public_special_review
                   and "Traceback" not in public_special_review))
        real_subprocess_run = subprocess.run
        fault_injected = False

        def _same_count_wrong_path_run(*args, **kwargs):
            nonlocal fault_injected
            result = real_subprocess_run(*args, **kwargs)
            command = args[0] if args else kwargs.get("args", [])
            if (not fault_injected and result.returncode == 0 and result.stdout
                    and "--raw" in command and "-z" in command and "-p" in command):
                payload = bytearray(result.stdout)
                metadata_end = payload.find(b"\0")
                path_end = payload.find(b"\0", metadata_end + 1)
                if metadata_end >= 0 and path_end > metadata_end + 1:
                    path_start = metadata_end + 1
                    payload[path_start] = (
                        ord("z") if payload[path_start] != ord("z") else ord("y"))
                    fault_injected = True
                    return subprocess.CompletedProcess(
                        result.args, result.returncode, bytes(payload), result.stderr)
            return result

        subprocess.run = _same_count_wrong_path_run
        try:
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                _wrong_identity_rc = main([
                    "review-material", "--repo", str(r), "--skill", target,
                    "--target", target])
        finally:
            subprocess.run = real_subprocess_run
        ok.append(("public review CLI fails closed on a same-count wrong path identity",
                   fault_injected and _wrong_identity_rc == 2
                   and "path identity mismatch" in _buf.getvalue()
                   and "Traceback" not in _buf.getvalue()))
        ok.append(("review-material never dereferences a symlink (exfiltration guard)",
                   "SECRET-OUTSIDE-REPO" not in out))
        ok.append(("review-material names the skipped symlink", "SYMLINK — not followed" in out))
        ok.append(("review-material survives a broken symlink", "broken.txt" in out))
        ok.append(("review-material keeps a newline-only text file",
                   "newline-only.md" in out and 'newline-only.md" ===' in out
                   and "BINARY" not in out.split("newline-only.md")[1][:40]))
        ok.append(("review-material omits a real binary", "shot.png" in out and "BINARY" in out))
        for name in ("nul-8191.bin", "nul-8192.bin", "nul-final.bin",
                     "invalid-utf8.bin", "large-nul-early.bin",
                     "large-nul-8192.bin", "large-nul-final.bin",
                     "large-invalid-8192.bin"):
            section = out.split(name, 1)[0].rsplit("=== NEW FILE", 1)[-1]
            ok.append((f"review-material classifies all of {name} as binary metadata",
                       "BINARY — metadata only" in section))
        ok.append(("complete review prompt contains no embedded NUL", "\0" not in out))
        ok.append(("invalid UTF-8 is not silently replaced", "PAYLOAD" not in out
                   and "\ufffd" not in out))
        ok.append(("review-material handles spaces in a path", "name with spaces.md" in out))
        ok.append(("review-material omits generated full-gate marketplaces only",
                   "FULL-GATE-TRACKED-MARKETPLACE-OMIT" not in out
                   and "FULL-GATE-UNTRACKED-MARKETPLACE-OMIT" not in out))
        ok.append(("review-material includes the complete scoped decision ledger",
                   '"alpha"=rejected' in out and '"beta"=deferred' in out
                   and RUN_START not in out.split("CURRENT RUN DECISIONS", 1)[1]
                                    .split("=== NEW FILE", 1)[0]))
        ok.append(("review-material sends untracked content before tracked material",
                   out.index("UNTRACKED-SENTINEL")
                   < out.index("=== TRACKED DIFF (target source first) ===")))
        ok.append(("review-material deduplicates the active JSONL already in its ledger",
                   "LOG-DUPLICATE-SENTINEL" not in out))
        ok.append(("review-material keeps executable target source ahead of overflow",
                   "TARGET-EXECUTABLE-SENTINEL" in out
                   and out.index("TARGET-EXECUTABLE-SENTINEL")
                       < out.index("SPECIAL-PATH-SENTINEL")))
        ok.append(("review-material keeps templated skill facts ahead of overflow",
                   "TARGET-FACTS-SENTINEL" in out
                   and out.index("TARGET-FACTS-SENTINEL")
                       < out.index("TARGET-EXECUTABLE-SENTINEL")))
        inventory = out.split("=== TRACKED DIFF INVENTORY", 1)[1].split(
            "=== NEW FILE", 1)[0]
        ok.append(("review-material inventories every changed tracked path",
                   all(json.dumps(path) in inventory
                       for path in ("capabilities.toml",
                                    "shared/skill-templates/review-test/scripts/tuneup.py",
                                    "t.txt", special_relpath))))
        ok.append(("NUL inventory preserves spaces, quotes, and Unicode exactly",
                   json.dumps(special_relpath, ensure_ascii=True) in inventory))
        script_relpath = "shared/skill-templates/review-test/scripts/tuneup.py"
        expected_script_diff_bytes = len(subprocess.run(
            ["git", "-C", str(r), "diff", "--no-ext-diff", "--no-textconv",
             "--no-color", "--default-prefix", "HEAD", "--", script_relpath],
            capture_output=True, check=True).stdout)
        ok.append(("review-material inventory byte counts exclude injected headings",
                   f'{json.dumps(script_relpath)}: {expected_script_diff_bytes} diff bytes'
                   in inventory))
        ok.append(("review-material truncates only the recoverable tracked diff",
                   TRACKED_TRUNCATION_MARKER in out
                   and "untracked" not in TRACKED_TRUNCATION_MARKER.lower()))
        ok.append(("tracked truncation names a truthful filtered recovery boundary",
                   "bytes omitted starting in" in out
                   and "working tree (agy uses its mirror)" in out
                   and "generated marketplaces are excluded" in out))
        ok.append(("tracked truncation names the exact special-character path",
                   ("bytes omitted starting in "
                    + json.dumps(special_relpath, ensure_ascii=True)) in out))
        first_header = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
        second_start = 90984
        synthetic = (
            first_header
            + "a" * (second_start - len(first_header) - 1)
            + "\ndiff --git a/bbbbbbbbbb.txt b/bbbbbbbbbb.txt\n"
            + "b" * 5000)
        synthetic_paths = ["a.txt", "bbbbbbbbbb.txt"]
        bounded = _bounded_tracked_diff(
            synthetic, synthetic_paths, 91200, "synthetic exclusion")
        bounded_raw = bounded.encode("utf-8")
        marker_at = bounded_raw.index(("\n" + TRACKED_TRUNCATION_MARKER).encode())
        synthetic_raw = synthetic.encode("utf-8")
        boundary_path, boundary_offset = _diff_path_at(
            synthetic_raw, synthetic_paths, marker_at)
        exact_boundary = (
            f"{len(synthetic_raw) - marker_at} bytes omitted starting in "
            f"{json.dumps(boundary_path, ensure_ascii=True)} at diff byte "
            f"+{boundary_offset}")
        ok.append(("tracked truncation reports its exact final emitted boundary",
                   exact_boundary in bounded))
        preamble_synthetic = "=== PREAMBLE ===\n" + synthetic
        preamble_bounded = _bounded_tracked_diff(
            preamble_synthetic, synthetic_paths, 2055, "synthetic exclusion")
        ok.append(("tracked truncation labels a pre-header boundary as preamble",
                   '"<tracked-diff-preamble>"' in preamble_bounded))
        ok.append(("diff/path section mismatch fails closed",
                   _raises(lambda: _diff_inventory(synthetic, ["only-one.txt"]),
                           RuntimeError)))
        ok.append(("tracked truncation is valid UTF-8 without a replacement codepoint",
                   "\ufffd" not in out))
        ok.append(("complete review prompt obeys its aggregate cap",
                   len(out.encode("utf-8")) <= REVIEW_TOTAL_CAP))
        active_log = log_path(r, target)
        active_log_bytes = active_log.read_bytes()
        rewritten = bytearray(active_log_bytes)
        rewritten[0] = ord("[") if rewritten[0] != ord("[") else ord("{")
        active_log.write_bytes(bytes(rewritten))
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
            _rewrite_rc = main([
                "review-material", "--repo", str(r), "--skill", target,
                "--target", target])
        ok.append(("public review CLI rejects a same-length committed-log rewrite",
                   _rewrite_rc == 2 and "not append-only from HEAD" in _buf.getvalue()
                   and "Traceback" not in _buf.getvalue()))
        active_log.write_bytes(active_log_bytes)
        active_log.unlink()
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
            _deleted_rc = main([
                "review-material", "--repo", str(r), "--skill", target,
                "--target", target])
        ok.append(("public review CLI rejects deletion of the committed active log",
                   _deleted_rc == 2 and "missing or unreadable" in _buf.getvalue()
                   and "Traceback" not in _buf.getvalue()))
        active_log.write_bytes(active_log_bytes)
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
        ok.append(("review-material sees a STAGED tree (diff HEAD, not bare diff)",
                   "diff --git" in review_material(r, target, target)))
        # a failed git must RAISE, not return "" — an empty result is what tells Step 9
        # there is nothing to review, and a skipped review reads as a converged cycle
        broken = _mark_khenrix(Path(td) / "not-a-repo-at-all")
        (broken / "shared" / "skills").mkdir(parents=True)
        (broken / "capabilities.toml").write_text("[models]\n")
        _make_skill(broken, f"shared/skills/{target}")
        log_append(broken, target, {
            "target": target, "finding_id": RUN_START, "decision": "applied",
            "title": "run start"})
        try:
            review_material(broken, target, target)
            ok.append(("review-material fails CLOSED on a broken repo", False))
        except (RuntimeError, ValueError):
            ok.append(("review-material fails CLOSED on a broken repo", True))
        (r / "too-big-untracked.md").write_text("x" * 7000)
        try:
            review_material(r, target, target, total_cap=6000)
            _overflow = ""
        except RuntimeError as ex:
            _overflow = str(ex)
        ok.append(("review-material fails closed instead of truncating untracked text",
                   "too-big-untracked.md" in _overflow and "review cap" in _overflow))
        try:
            _assert_review_prompt_bound(
                r, "x" * REVIEW_TOTAL_CAP,
                reviewer=_capture_council_reviewer(r))
            _argv_safe = True
        except RuntimeError:
            _argv_safe = False
        ok.append(("real council wrappers plus headroom stay below MAX_ARG_STRLEN",
                   _argv_safe
                   and REVIEW_TOTAL_CAP + REVIEW_WRAPPER_HEADROOM < REVIEW_SINGLE_ARG_MAX))
        ok.append(("argv bound refuses an embedded NUL independently",
                   _raises(lambda: _assert_review_prompt_bound(r, "x\0y"), RuntimeError)))
    # The ledger is read before untracked enumeration, and the wrapper is imported after
    # prompt assembly. Both are review inputs too: their own paths must be repository-owned
    # or the later untracked-symlink guard arrives too late.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        outside_ledger = root / "outside-ledger.jsonl"
        outside_ledger.write_text(
            json.dumps({"target": "demo", "finding_id": RUN_START,
                        "decision": "applied", "note": "EXFIL_FROM_LEDGER"}) + "\n")
        for mode in ("tracked", "untracked", "dangling"):
            repo = _mark_khenrix(root / f"ledger-{mode}")
            (repo / "shared" / "skills").mkdir(parents=True)
            (repo / "capabilities.toml").write_text("[models]\n")
            _make_skill(repo, "shared/skills/demo")
            (repo / "tracked.txt").write_text("base\n")
            log = log_path(repo, "demo")
            log.parent.mkdir(parents=True)
            if mode == "tracked":
                log.symlink_to(outside_ledger)
            subprocess.run(["git", "-C", str(repo), "init", "-q", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                            "-c", "user.name=t", "commit", "-qm", "base"], check=True)
            if mode != "tracked":
                log.symlink_to(outside_ledger if mode == "untracked"
                               else root / "missing-ledger.jsonl")
            (repo / "tracked.txt").write_text("changed\n")
            try:
                review_material(repo, "demo", "demo")
                problem = ""
            except (RuntimeError, ValueError) as ex:
                problem = str(ex)
            ok.append((f"review-material refuses a {mode} symlink-backed ledger",
                       "symlink-backed" in problem
                       and "EXFIL_FROM_LEDGER" not in problem))
            for command in (
                ["log", "list", "--repo", str(repo), "--target", "demo"],
                ["convergence-status", "--repo", str(repo), "--target", "demo"],
            ):
                _buf = io.StringIO()
                with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                    _rc = main(command)
                ok.append((f"{command[0]} refuses a {mode} linked ledger cleanly",
                           _rc == 2 and "symlink-backed" in _buf.getvalue()
                           and "EXFIL_FROM_LEDGER" not in _buf.getvalue()
                           and "Traceback" not in _buf.getvalue()))
            ok.append((f"log writes refuse a {mode} symlink-backed ledger",
                       _raises(lambda repo=repo: log_append(repo, "demo", {
                           "target": "demo", "finding_id": "new", "decision": "applied"
                       }), ValueError)))

        outside_engine = root / "outside-engine.py"
        outside_engine.write_text(
            'raise RuntimeError("EXTERNAL_ENGINE_WAS_IMPORTED")\n')
        saved_engine = _COUNCIL_ENGINE
        try:
            for mode in ("tracked", "untracked", "dangling"):
                repo = _mark_khenrix(root / f"engine-{mode}")
                (repo / "shared" / "skills").mkdir(parents=True)
                (repo / "capabilities.toml").write_text("[models]\n")
                _make_skill(repo, "shared/skills/demo")
                (repo / "tracked.txt").write_text("base\n")
                log_append(repo, "demo", {
                    "target": "demo", "finding_id": RUN_START,
                    "decision": "applied"})
                engine = repo / "shared" / "lib" / "council" / "engine.py"
                engine.parent.mkdir(parents=True)
                if mode == "tracked":
                    engine.symlink_to(outside_engine)
                subprocess.run(["git", "-C", str(repo), "init", "-q", "."], check=True)
                subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
                subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                                "-c", "user.name=t", "commit", "-qm", "base"], check=True)
                if mode != "tracked":
                    engine.symlink_to(outside_engine if mode == "untracked"
                                      else root / "missing-engine.py")
                (repo / "tracked.txt").write_text("changed\n")
                try:
                    review_material(repo, "demo", "demo")
                    problem = ""
                except (RuntimeError, ValueError) as ex:
                    problem = str(ex)
                ok.append((f"review-material refuses a {mode} symlink-backed wrapper",
                           ("symlink-backed" in problem
                            or "missing or ambiguous" in problem)
                           and "EXTERNAL_ENGINE_WAS_IMPORTED" not in problem))
        finally:
            globals()["_COUNCIL_ENGINE"] = saved_engine

        # Dirty council machinery is under test regardless of the target's name. Even argv
        # sizing for an ordinary target must use HEAD: importing the candidate here would
        # let the implementation being reviewed lie about (or crash) its own review gate.
        repo = _mark_khenrix(root / "committed-reviewer")
        (repo / "shared" / "skills").mkdir(parents=True)
        (repo / "capabilities.toml").write_text("[models]\n")
        council_skill = _make_skill(repo, "shared/skills/llm-council")
        _make_skill(repo, "shared/skills/ordinary")
        for target in ("llm-council", "ordinary"):
            log_append(repo, target, {
                "target": target, "finding_id": RUN_START, "decision": "applied"})
        engine = repo / "shared" / "lib" / "council" / "engine.py"
        engine.parent.mkdir(parents=True)
        engine.write_text(
            "SENTINEL_PREFIX = 'sentinel:'\n"
            "def apply_member_note(prompt): return prompt + ('H' * 40000)\n"
            "def apply_readonly_posture(prompt): return prompt\n"
            "def apply_sentinel(prompt, token): return prompt\n")
        subprocess.run(["git", "-C", str(repo), "init", "-q", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "base"], check=True)
        engine.write_text('raise RuntimeError("WORKING_COUNCIL_ENGINE_EXECUTED")\n')
        council_skill.joinpath("SKILL.md").write_text("# changed council\n")
        saved_engine = _COUNCIL_ENGINE
        try:
            _review = review_material(repo, "llm-council", "llm-council")
            ok.append(("llm-council review sizing uses the committed wrapper",
                       "WORKING_COUNCIL_ENGINE_EXECUTED" in _review))
            ok.append(("llm-council argv sizing applies the committed wrapper behavior",
                       _raises(lambda: _assert_review_prompt_bound(
                           repo, "x" * REVIEW_TOTAL_CAP, committed=True), RuntimeError)))
            _ordinary_reviewer = _capture_council_reviewer(repo)
            _assert_review_prompt_bound(
                repo, "ordinary review", reviewer=_ordinary_reviewer)
            ok.append(("ordinary targets also substitute HEAD when council code is dirty",
                       _ordinary_reviewer.selection == "committed-head"
                       and REVIEWER_ENGINE_RELPATH in _ordinary_reviewer.dirty_paths))
        finally:
            globals()["_COUNCIL_ENGINE"] = saved_engine

        # BOTH implementation trees are reviewer authority for EVERY target. A staged
        # skill edit or an untracked helper beneath llm-council must substitute HEAD even
        # while the skill being tuned is entirely unrelated.
        selection_repo = _mark_khenrix(root / "reviewer-selection")
        (selection_repo / "shared" / "skills").mkdir(parents=True)
        (selection_repo / "capabilities.toml").write_text("[models]\n")
        selection_council = _make_skill(
            selection_repo, "shared/skills/llm-council")
        selection_engine = selection_repo / REVIEWER_ENGINE_RELPATH
        selection_engine.parent.mkdir(parents=True)
        selection_engine.write_text(
            "SENTINEL_PREFIX = 'x'\n"
            "MODES = {'normal': {}}\n"
            "def apply_member_note(prompt): return prompt\n"
            "def apply_readonly_posture(prompt): return prompt\n"
            "def apply_sentinel(prompt, token): return prompt\n")
        subprocess.run(["git", "-C", str(selection_repo), "init", "-q", "."], check=True)
        subprocess.run(["git", "-C", str(selection_repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(selection_repo), "-c", "user.email=t@t",
             "-c", "user.name=t", "commit", "-qm", "base"], check=True)
        clean_reviewer = _capture_council_reviewer(selection_repo)
        ok.append(("a clean reviewer capture records working-tree bytes and provenance",
                   clean_reviewer.selection == "working-tree-clean"
                   and not clean_reviewer.dirty_paths
                   and clean_reviewer.engine_sha256 == hashlib.sha256(
                       selection_engine.read_bytes()).hexdigest()))
        planted = selection_council / "scripts" / "planted.py"
        planted.parent.mkdir()
        planted.write_text("raise RuntimeError('candidate reviewer')\n")
        untracked_reviewer = _capture_council_reviewer(selection_repo)
        ok.append(("an untracked llm-council descendant selects committed HEAD",
                   untracked_reviewer.selection == "committed-head"
                   and "shared/skills/llm-council/scripts/planted.py"
                   in untracked_reviewer.dirty_paths))
        planted.unlink()
        selection_council.joinpath("SKILL.md").write_text("# staged candidate\n")
        subprocess.run(
            ["git", "-C", str(selection_repo), "add",
             "shared/skills/llm-council/SKILL.md"], check=True)
        staged_reviewer = _capture_council_reviewer(selection_repo)
        ok.append(("a staged llm-council skill edit selects committed HEAD",
                   staged_reviewer.selection == "committed-head"
                   and "shared/skills/llm-council/SKILL.md"
                   in staged_reviewer.dirty_paths))

        # One module instance crosses the boundary calculation and the fanout call. The
        # fake has no providers behind it: its state counter exposes a second import, and
        # its argv record proves the production command fixes the panel to Codex+agy.
        exact_repo = _mark_khenrix(root / "exact-reviewer-capture")
        (exact_repo / "shared" / "skills").mkdir(parents=True)
        (exact_repo / "capabilities.toml").write_text("[models]\n")
        exact_target = _make_skill(exact_repo, "shared/skills/ordinary")
        exact_engine = exact_repo / REVIEWER_ENGINE_RELPATH
        exact_engine.parent.mkdir(parents=True)
        exact_engine.write_text(
            "import json\n"
            "from pathlib import Path\n"
            "SENTINEL_PREFIX = 'x'\n"
            "MODES = {'normal': {}}\n"
            "BOUND_CALLS = 0\n"
            "def apply_member_note(prompt):\n"
            "    global BOUND_CALLS\n"
            "    BOUND_CALLS += 1\n"
            "    return prompt\n"
            "def apply_readonly_posture(prompt): return prompt\n"
            "def apply_sentinel(prompt, token): return prompt\n"
            "def main(argv):\n"
            "    before = BOUND_CALLS\n"
            "    providers = argv[argv.index('--providers') + 1]\n"
            "    workdir = Path(argv[argv.index('--workdir') + 1])\n"
            "    manifest = {'schema': 1, 'workdir': str(workdir),\n"
            "                'summary': {'valid': 2},\n"
            "                'bound_calls_before_fanout': before,\n"
            "                'providers_arg': providers}\n"
            "    (workdir / 'manifest.json').write_text(json.dumps(manifest))\n"
            "    print(json.dumps(manifest))\n"
            "    return 0\n")
        log_append(exact_repo, "ordinary", {
            "target": "ordinary", "finding_id": RUN_START, "decision": "applied"})
        subprocess.run(["git", "-C", str(exact_repo), "init", "-q", "."], check=True)
        subprocess.run(["git", "-C", str(exact_repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(exact_repo), "-c", "user.email=t@t",
             "-c", "user.name=t", "commit", "-qm", "base"], check=True)
        exact_target.joinpath("SKILL.md").write_text("# candidate\n")
        exact_capture = _capture_council_reviewer(exact_repo)
        exact_outdir = root / "exact-review-output"
        exact_rc, exact_manifest = run_diff_review(
            exact_repo, "ordinary", "ordinary", workdir=exact_outdir,
            reviewer=exact_capture)
        persisted_exact = _strict_json_loads(
            (exact_outdir / "manifest.json").read_bytes(), context="test manifest")
        ok.append(("review sizing and fanout use the exact same captured module",
                   exact_rc == 0
                   and exact_manifest["bound_calls_before_fanout"] == 1))
        ok.append(("final diff fanout is fixed to the Codex+agy panel",
                   exact_manifest["providers_arg"] == ",".join(FINAL_PANEL)))
        ok.append(("reviewer provenance is persisted beside the fanout manifest",
                   persisted_exact["reviewer_source"] == exact_capture.provenance()
                   and persisted_exact["review_prompt"]["sha256"]
                   == exact_manifest["review_prompt"]["sha256"]))
        exact_cli_out = io.StringIO()
        with contextlib.redirect_stdout(exact_cli_out), contextlib.redirect_stderr(
                exact_cli_out):
            exact_cli_rc = main([
                "review-diff", "--repo", str(exact_repo), "--skill", "ordinary",
                "--target", "ordinary", "--workdir", str(root / "exact-review-cli")])
        exact_cli_manifest = _strict_json_loads(
            exact_cli_out.getvalue(), context="test review-diff CLI manifest")
        ok.append(("public review-diff preserves the one-capture contract",
                   exact_cli_rc == 0
                   and exact_cli_manifest["bound_calls_before_fanout"] == 1
                   and exact_cli_manifest["providers_arg"] == ",".join(FINAL_PANEL)))

        linked_repo = _mark_khenrix(root / "committed-linked-reviewer")
        linked_engine = linked_repo / "shared" / "lib" / "council" / "engine.py"
        linked_engine.parent.mkdir(parents=True)
        linked_target = linked_repo / "regular-engine.py"
        linked_target.write_text(
            "SENTINEL_PREFIX = 'x'\n"
            "def apply_member_note(prompt): return prompt\n"
            "def apply_readonly_posture(prompt): return prompt\n"
            "def apply_sentinel(prompt, token): return prompt\n")
        linked_engine.symlink_to(linked_target)
        subprocess.run(["git", "-C", str(linked_repo), "init", "-q", "."], check=True)
        subprocess.run(["git", "-C", str(linked_repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(linked_repo), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "linked"], check=True)
        try:
            _council_engine_module(linked_repo, committed=True)
            _linked_head_problem = ""
        except RuntimeError as ex:
            _linked_head_problem = str(ex)
        ok.append(("the committed reviewer must be a regular Git blob",
                   "not a regular blob" in _linked_head_problem))
    # log round-trip in a tempdir; latest decision per finding wins
    with tempfile.TemporaryDirectory() as td:
        repo = _mark_khenrix(Path(td))
        # khenrix-shaped so registry_repo() resolves to THIS tempdir — otherwise the log
        # would route to the real checkout and the test would write into the repo.
        (repo / "shared" / "skills").mkdir(parents=True)
        (repo / "capabilities.toml").write_text("[models]\n")
        _init_repo(repo)
        for bad_target in ("../escape", "nested/name", "/absolute"):
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                _rc = main(["log", "list", "--repo", str(repo),
                            "--target", bad_target])
            ok.append((f"log target {bad_target!r} cannot escape the owned directory",
                       _rc == 2 and ("invalid log target" in _buf.getvalue()
                                    or "invalid skill name" in _buf.getvalue())
                       and "Traceback" not in _buf.getvalue()))
        e1 = {"target": "markitdown", "finding_id": "stale-flag", "decision": "deferred"}
        e2 = {"target": "markitdown", "finding_id": "stale-flag", "decision": "applied"}
        log_append(repo, "markitdown", dict(e1))
        log_append(repo, "markitdown", dict(e2))
        got = log_list(repo, "markitdown")
        ok.append(("log keeps latest decision per finding",
                   len(got) == 1 and got[0]["decision"] == "applied"))
        ok.append(("JSONL preserves distinct occurrences with the same finding id",
                   len(log_entries(repo, "markitdown")) == 2))
        ok.append(("log adds a timestamp", "ts" in got[0]))
        for label, raw in (
            ("duplicate keys", '{"target":"strict","target":"other",'
                               '"finding_id":"x","decision":"applied"}'),
            ("NaN", '{"target":"strict","finding_id":"x",'
                    '"decision":"applied","value":NaN}'),
            ("Infinity", '{"target":"strict","finding_id":"x",'
                         '"decision":"applied","value":Infinity}'),
            ("overflow", '{"target":"strict","finding_id":"x",'
                         '"decision":"applied","value":1e999}'),
        ):
            strict_path = log_path(repo, "strict")
            before = strict_path.read_bytes() if strict_path.exists() else b""
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                _rc = main(["log", "append", "--repo", str(repo),
                            "--target", "strict", "--entry", raw])
            after = strict_path.read_bytes() if strict_path.exists() else b""
            ok.append((f"strict log CLI rejects {label} without mutation",
                       _rc == 2 and before == after and "log append input" in _buf.getvalue()))
        strict_path = log_path(repo, "strict-direct")
        log_append(repo, "strict-direct", {
            "target": "strict-direct", "finding_id": "valid", "decision": "applied"})
        strict_before = strict_path.read_bytes()
        ok.append(("direct log append rejects a nested non-finite value before publication",
                   _raises(lambda: log_append(repo, "strict-direct", {
                       "target": "strict-direct", "finding_id": "bad",
                       "decision": "applied", "nested": {"value": float("nan")}}),
                           ValueError)
                   and strict_path.read_bytes() == strict_before))
        duplicate_path = log_path(repo, "strict-persisted")
        duplicate_path.write_text(
            '{"target":"strict-persisted","finding_id":"x",'
            '"finding_id":"y","decision":"applied"}\n', encoding="utf-8")
        try:
            log_entries(repo, "strict-persisted")
            duplicate_problem = ""
        except ValueError as exc:
            duplicate_problem = str(exc)
        ok.append(("persisted strict JSON errors retain their JSONL line context",
                   "run log line 1" in duplicate_problem
                   and "duplicate JSON object key" in duplicate_problem))
        for label, bad_ts in (
            ("null", None), ("empty", ""), ("blank", "  "), ("integer", 7),
            ("boolean", True), ("list", []), ("object", {}),
        ):
            try:
                log_append(repo, "markitdown", {
                    "target": "markitdown", "finding_id": f"bad-ts-{label}",
                    "decision": "applied", "ts": bad_ts})
                ok.append((f"log rejects an explicit {label} timestamp", False))
            except ValueError:
                ok.append((f"log rejects an explicit {label} timestamp", True))
        legacy_target = "legacy-timestamps"
        legacy_path = log_path(repo, legacy_target)
        legacy_path.write_text(
            json.dumps({"target": legacy_target, "finding_id": "null-ts",
                        "decision": "applied", "ts": None}) + "\n"
            + json.dumps({"target": legacy_target, "finding_id": "valid-ts",
                          "decision": "applied", "ts": "2026-01-01T00:00:00Z"}) + "\n",
            encoding="utf-8")
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            legacy_list_rc = main(
                ["log", "list", "--repo", str(repo), "--target", legacy_target])
        ok.append(("legacy invalid timestamps remain listable without a crash",
                   legacy_list_rc == 0 and "null-ts" in _buf.getvalue()))
        try:
            log_append(repo, "markitdown", {"target": "markitdown", "finding_id": "x", "decision": "maybe"})
            ok.append(("bad decision rejected", False))
        except ValueError:
            ok.append(("bad decision rejected", True))
        for label, bad_id in (
            ("null", None), ("empty", ""), ("blank", "  "), ("integer", 7),
            ("boolean", True), ("list", []), ("object", {}),
        ):
            ok.append((f"log rejects a {label} finding_id",
                       _raises(lambda bad_id=bad_id: log_append(repo, "markitdown", {
                           "target": "markitdown", "finding_id": bad_id,
                           "decision": "applied"}), ValueError)))
        try:
            log_append(repo, "markitdown", {"finding_id": "x", "decision": "applied"})
            ok.append(("missing keys rejected", False))
        except ValueError:
            ok.append(("missing keys rejected", True))
        ok.append(("non-object log append is cleanly rejected",
                   _raises(lambda: log_append(repo, "markitdown", []), ValueError)))
        log_append(repo, "markitdown",
                   {"target": "markitdown", "finding_id": RUN_START,
                    "decision": "applied"})
        for decision in ("deferred", "rejected"):
            ok.append((f"log writer rejects decision={decision} on run-start",
                       _raises(lambda decision=decision: log_append(repo, "markitdown", {
                           "target": "markitdown", "finding_id": RUN_START,
                           "decision": decision}), ValueError)))
        for malformed_target, raw_entry in (
            ("wrong-target-log", {"target": "other", "finding_id": RUN_START,
                                  "decision": "applied"}),
            ("non-object-log", ["not", "an", "object"]),
            ("missing-id-log", {"target": "missing-id-log", "decision": "applied"}),
            ("missing-decision-log", {"target": "missing-decision-log",
                                      "finding_id": "finding"}),
            ("invalid-decision-log", {"target": "invalid-decision-log",
                                      "finding_id": "finding", "decision": "applyed"}),
        ):
            malformed_path = log_path(repo, malformed_target)
            malformed_path.write_text(json.dumps(raw_entry) + "\n", encoding="utf-8")
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                _rc = main(["convergence-status", "--repo", str(repo),
                            "--target", malformed_target])
            ok.append((f"persisted malformed log {malformed_target} is cleanly refused",
                       _rc == 2 and "line 1" in _buf.getvalue()
                       and "Traceback" not in _buf.getvalue()))
        terminal_cli_target = "terminal-cli"
        log_append(repo, terminal_cli_target, {
            "target": terminal_cli_target, "finding_id": RUN_START,
            "decision": "applied"})
        for label, terminal in malformed_terminals:
            cli_entry = {"target": terminal_cli_target, **terminal}
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                _rc = main([
                    "log", "append", "--repo", str(repo), "--target",
                    terminal_cli_target, "--entry", json.dumps(cli_entry)])
            ok.append((f"public log CLI rejects malformed {label} run-convergence",
                       _rc == 2 and f"invalid {RUN_END!r}" in _buf.getvalue()
                       and "Traceback" not in _buf.getvalue()))
        valid_terminal_cli = {
            "target": terminal_cli_target, "finding_id": RUN_END,
            "decision": "deferred", "converged": False, "cycles": 0,
        }
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
            _rc = main([
                "log", "append", "--repo", str(repo), "--target",
                terminal_cli_target, "--entry", json.dumps(valid_terminal_cli)])
        ok.append(("public log CLI accepts exact modern stalled run-convergence",
                   _rc == 0 and f'"finding_id": "{RUN_END}"' in _buf.getvalue()))
        # The whole delimiter contract is validated before append-only history can be
        # poisoned: decision, exact integer type, and monotonicity in the current run.
        for label, bad in (("string", "1"), ("null", None), ("float", 2.0),
                           ("bool", True), ("absent", ...)):
            entry = {"target": "markitdown", "finding_id": CYCLE_END, "decision": "applied"}
            if bad is not ...:
                entry["cycle"] = bad
            try:
                log_append(repo, "markitdown", entry)
                ok.append((f"cycle-end rejects a {label} cycle", False))
            except ValueError:
                ok.append((f"cycle-end rejects a {label} cycle", True))
        for decision in ("deferred", "rejected"):
            try:
                log_append(repo, "markitdown", {
                    "target": "markitdown", "finding_id": CYCLE_END,
                    "decision": decision, "cycle": 4})
                ok.append((f"cycle-end rejects decision={decision}", False))
            except ValueError:
                ok.append((f"cycle-end rejects decision={decision}", True))
        log_append(repo, "markitdown",
                   {"target": "markitdown", "finding_id": CYCLE_END,
                    "decision": "applied", "cycle": 4})
        for label, bad_cycle in (("duplicate", 4), ("descending", 3)):
            try:
                log_append(repo, "markitdown", {
                    "target": "markitdown", "finding_id": CYCLE_END,
                    "decision": "applied", "cycle": bad_cycle})
                ok.append((f"cycle-end rejects a {label} cycle number", False))
            except ValueError:
                ok.append((f"cycle-end rejects a {label} cycle number", True))
        log_append(repo, "markitdown",
                   {"target": "markitdown", "finding_id": CYCLE_END,
                    "decision": "applied", "cycle": 9})
        ok.append(("cycle-end accepts nonconsecutive monotonic integers",
                   [e["cycle"] for e in log_entries(repo, "markitdown")
                    if e["finding_id"] == CYCLE_END] == [4, 9]))
        log_append(repo, "markitdown", {
            "target": "markitdown", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1})
        for decision, converged in (("applied", True), ("deferred", False)):
            try:
                log_append(repo, "markitdown", {
                    "target": "markitdown", "finding_id": RUN_END,
                    "decision": decision, "converged": converged, "cycles": 1})
                _duplicate_terminal_error = ""
            except ValueError as ex:
                _duplicate_terminal_error = str(ex)
            ok.append((f"log writer rejects a duplicate {decision} run-convergence",
                       "already terminal" in _duplicate_terminal_error))
        for label, fields in (
            ("legacy deferred", {"decision": "deferred"}),
            ("rejected", {"decision": "rejected", "converged": False, "cycles": 1}),
            ("contradictory", {"decision": "applied", "converged": False,
                               "cycles": 1}),
            ("boolean cycles", {"decision": "applied", "converged": True,
                                "cycles": True}),
        ):
            entry = {"target": "markitdown", "finding_id": RUN_END, **fields}
            ok.append((f"log writer rejects {label} run-convergence",
                       _raises(lambda entry=entry: log_append(
                           repo, "markitdown", entry), ValueError)))
        log_append(repo, "markitdown", {
            "target": "markitdown", "finding_id": RUN_START, "decision": "applied"})
        log_append(repo, "markitdown", {
            "target": "markitdown", "finding_id": CYCLE_END,
            "decision": "applied", "cycle": 1})
        _next_run_terminal = log_append(repo, "markitdown", {
            "target": "markitdown", "finding_id": RUN_END, "decision": "applied", "converged": True, "cycles": 1})
        ok.append(("log writer accepts one terminal after a new valid run-start",
                   _next_run_terminal["finding_id"] == RUN_END))
    for label, passed in ok:
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    result = 0 if all(p for _, p in ok) else 1
    globals()["_SELF_TEST_KHENRIX_ROOTS"] = None
    globals()["_SELF_TEST_REGISTRY_ROOT"] = None
    self_test_registry_td.cleanup()
    return result


def _load_checks():
    """Import the receipt validator from wherever THIS engine lives, not from the target.

    Operational runs use the source-checkout engine required by SKILL.md. The rendered
    plugin candidate exists so packaged copies can run their deterministic self-test
    against the validator bundled beside them; it is not a second full-gate identity.
    `repo` is whichever repo the TARGET lives in, so importing from `repo/scripts` failed
    outright for foreign targets. A failed import that returns early would silently skip
    provenance and panel checks and print "proven" having verified nothing.
    """
    import importlib.util
    here = Path(__file__).resolve()
    candidates = [
        here.parents[4] / "scripts" / "lib" / "checks.py",   # khenrix checkout
        here.parents[3] / "lib" / "checks.py",               # rendered plugin bundle
    ]
    for c in candidates:
        if c.is_file():
            spec = importlib.util.spec_from_file_location("_khenrix_checks", c)
            mod = importlib.util.module_from_spec(spec)
            previous = sys.modules.get(spec.name)
            sys.modules[spec.name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                if previous is None:
                    sys.modules.pop(spec.name, None)
                else:
                    sys.modules[spec.name] = previous
                raise
            return mod
    raise RuntimeError(f"receipt validator not found; looked in {[str(c) for c in candidates]}")


def verify_final_receipt(repo: Path, skill: str, panel: list) -> list:
    """Prove Step 10's schema-3 shipping policy instead of asserting it in prose.

    Common freshness, provenance, canonical Codex+agy order, judge/mode, policy digest,
    model provenance, and per-provider eval-count rules live in the single
    ``checks.validate_receipt(final=True)`` authority used by precommit too. This command
    supplies the target-scoped diagnosis needed before staging. Deterministic targets must
    prove both their named certifier and the canonical advisory panel; a historical v1
    receipt may remain usable for ordinary freshness checks but cannot prove shipping.
    """
    info, _ = _require_skill_paths(repo, skill)
    if info["tier"] != "full-gate":
        raise ValueError(
            f"verify-final-receipt applies only to full-gate khenrix targets; {skill!r} "
            "is council-only and has no khenrix receipt gate")
    checks = _load_checks()
    policy = checks.eval_policy(repo)
    canonical_panel = tuple(policy.required_providers)
    requested_panel = tuple(panel)
    if requested_panel != canonical_panel:
        raise ValueError(
            "the final receipt panel is fixed by capabilities.toml [eval] at "
            f"{','.join(canonical_panel)} and cannot be narrowed or reordered "
            f"(got {','.join(requested_panel) or '<empty>'})")
    return checks.validate_receipt(
        repo, skill, final=True, panel=list(canonical_panel))


REVIEW_TOTAL_CAP = 96 * 1024
REVIEW_SINGLE_ARG_MAX = 131071  # measured: 131071 succeeds; 131072 raises E2BIG
REVIEW_WRAPPER_HEADROOM = 4096
TRACKED_TRUNCATION_MARKER = "…[tracked diff cap reached;"
TRACKED_MARKER_RESERVE = 2048
REVIEW_INSTRUCTIONS = (
    "Adversarially review this diff (a skill-tuneup pass on {target} in {repo}) — look "
    "for the strongest reasons it should not ship; do not modify anything. Prioritize "
    "correctness, over-engineering, stale references, and missed edge cases. Give a "
    "verdict PER admissible category (Bug / Inconsistency / Stale-reference / "
    "Missing-edge-case / Eval-gap / Over-engineering) with the evidence checked for "
    "each; a clean category stated with its evidence is useful. Then list findings by "
    "severity, each tied to a file or hunk with a concrete fix; ground every claim in "
    "the supplied candidate, prefer one strong finding over several weak ones, and name "
    "residual risks separately. Never answer briefly — replies under 400 characters are "
    "scored non_substantive and dropped.")

_COUNCIL_ENGINE = None
REVIEWER_ENGINE_RELPATH = "shared/lib/council/engine.py"
REVIEWER_WATCH_PATHS = (
    "shared/lib/council",
    "shared/skills/llm-council",
)


@dataclass(frozen=True)
class CouncilReviewer:
    """One immutable engine capture used for both prompt sizing and fanout."""

    module: object
    registry: Path
    selection: str
    engine_sha256: str
    head_commit: str
    dirty_paths: tuple[str, ...]

    def provenance(self) -> dict:
        return {
            "selection": self.selection,
            "engine_path": REVIEWER_ENGINE_RELPATH,
            "engine_sha256": self.engine_sha256,
            "head_commit": self.head_commit,
            "dirty_paths": list(self.dirty_paths),
        }


def _diff_starts(raw: bytes, paths: list[str] | tuple[str, ...]) -> list[tuple[int, str]]:
    """Pair diff-section offsets with Git's matching NUL-delimited path inventory.

    Display headers are not a path protocol: an unquoted name containing spaces cannot be
    recovered by tokenizing ``diff --git a/... b/...``, while quoted names depend on Git's
    escaping rules. The caller obtains ``paths`` from the atomic raw ``-z`` prefix of the
    same patch-producing Git process. A malformed protocol or changed section roster is
    therefore a hard error rather than a plausible-but-wrong recovery marker.
    """
    offsets = []
    cursor = 0
    for line in raw.splitlines(keepends=True):
        if line.startswith(b"diff --git "):
            offsets.append(cursor)
        cursor += len(line)
    if len(offsets) != len(paths):
        raise RuntimeError(
            "tracked diff section/path inventory mismatch: "
            f"diff has {len(offsets)} section(s), NUL inventory has {len(paths)} path(s)")
    return list(zip(offsets, paths))


_GIT_C_ESCAPES = {
    ord("a"): 7, ord("b"): 8, ord("t"): 9, ord("n"): 10,
    ord("v"): 11, ord("f"): 12, ord("r"): 13,
    ord('"'): ord('"'), ord("\\"): ord("\\"),
}


def _git_c_quoted_token(raw: bytes, start: int) -> tuple[bytes, int]:
    """Decode one Git double-quoted pathname token and return its end offset."""
    if start >= len(raw) or raw[start] != ord('"'):
        raise RuntimeError("tracked diff path identity mismatch: expected quoted pathname")
    out = bytearray()
    cursor = start + 1
    while cursor < len(raw):
        value = raw[cursor]
        if value == ord('"'):
            return bytes(out), cursor + 1
        if value != ord("\\"):
            out.append(value)
            cursor += 1
            continue
        cursor += 1
        if cursor >= len(raw):
            break
        escaped = raw[cursor]
        if ord("0") <= escaped <= ord("7"):
            stop = cursor
            while (stop < len(raw) and stop < cursor + 3
                   and ord("0") <= raw[stop] <= ord("7")):
                stop += 1
            out.append(int(raw[cursor:stop], 8))
            cursor = stop
            continue
        if escaped not in _GIT_C_ESCAPES:
            raise RuntimeError(
                "tracked diff path identity mismatch: invalid Git pathname escape")
        out.append(_GIT_C_ESCAPES[escaped])
        cursor += 1
    raise RuntimeError("tracked diff path identity mismatch: unterminated quoted pathname")


def _validate_diff_header_identity(header: bytes, path: bytes) -> None:
    """Bind one patch display header to its raw ``-z`` pathname identity."""
    header = header.rstrip(b"\r\n")
    expected_plain = b"diff --git a/" + path + b" b/" + path
    if header == expected_plain:
        return
    prefix = b"diff --git "
    if not header.startswith(prefix):
        raise RuntimeError("tracked diff path identity mismatch: missing diff header")
    try:
        old_path, cursor = _git_c_quoted_token(header, len(prefix))
        if cursor >= len(header) or header[cursor] != ord(" "):
            raise RuntimeError(
                "tracked diff path identity mismatch: malformed quoted header separator")
        new_path, cursor = _git_c_quoted_token(header, cursor + 1)
    except RuntimeError:
        raise
    if cursor != len(header) or old_path != b"a/" + path or new_path != b"b/" + path:
        raise RuntimeError(
            "tracked diff path identity mismatch between atomic raw roster and patch")


def _parse_atomic_tracked_diff(payload: bytes) -> tuple[str, list[str]]:
    """Decode one ``git diff --raw -z -p`` result into a bound patch and path roster."""
    if not payload:
        return "", []
    separator = payload.find(b"\0\0diff --git ")
    if separator < 0:
        raise RuntimeError(
            "tracked diff atomic protocol is missing its raw-roster/patch boundary")
    raw_records = payload[:separator + 1]
    patch = payload[separator + 2:]
    raw_paths: list[bytes] = []
    cursor = 0
    while cursor < len(raw_records):
        metadata_end = raw_records.find(b"\0", cursor)
        if metadata_end < 0:
            raise RuntimeError("tracked diff atomic raw record is unterminated")
        metadata = raw_records[cursor:metadata_end]
        fields = metadata[1:].split() if metadata.startswith(b":") else []
        if len(fields) != 5 or fields[4][:1] in (b"R", b"C"):
            raise RuntimeError(
                "tracked diff atomic raw record is malformed or unexpectedly renamed")
        path_end = raw_records.find(b"\0", metadata_end + 1)
        if path_end < 0 or path_end == metadata_end + 1:
            raise RuntimeError("tracked diff atomic raw pathname is missing or unterminated")
        raw_paths.append(raw_records[metadata_end + 1:path_end])
        cursor = path_end + 1
    headers = [line for line in patch.splitlines(keepends=True)
               if line.startswith(b"diff --git ")]
    if len(headers) != len(raw_paths):
        raise RuntimeError(
            "tracked diff section/path inventory mismatch: "
            f"patch has {len(headers)} section(s), atomic roster has {len(raw_paths)} path(s)")
    for header, raw_path in zip(headers, raw_paths):
        _validate_diff_header_identity(header, raw_path)
    text = patch.decode("utf-8", "backslashreplace").replace("\0", "\\x00")
    paths = [os.fsdecode(path) for path in raw_paths]
    _diff_starts(text.encode("utf-8"), paths)
    return text, paths


def _diff_path_at(raw: bytes, paths: list[str] | tuple[str, ...],
                  offset: int) -> tuple[str, int]:
    """Changed path and byte offset in its diff section at one truncation boundary."""
    starts = _diff_starts(raw, paths)
    if not starts:
        return "<tracked-diff-preamble>", offset
    if offset < starts[0][0]:
        return "<tracked-diff-preamble>", offset
    section_start, path = max(item for item in starts if item[0] <= offset)
    return path, max(0, offset - section_start)


def _diff_inventory(text: str, paths: list[str] | tuple[str, ...]) -> str:
    """Complete changed-path inventory with each file's recoverable diff byte count."""
    raw = text.encode("utf-8")
    starts = _diff_starts(raw, paths)
    rows = []
    for index, (offset, path) in enumerate(starts):
        stop = starts[index + 1][0] if index + 1 < len(starts) else len(raw)
        rows.append(f"{json.dumps(path, ensure_ascii=True)}: {stop - offset} diff bytes")
    return "\n".join(rows) if rows else "(none)"


def _bounded_tracked_diff(text: str, paths: list[str] | tuple[str, ...], cap: int,
                          exclusion_note: str) -> str:
    """UTF-8-safe target-first cap with a truthful recovery marker."""
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    # A fixed reserve makes the reported boundary a function of the FINAL emitted prefix.
    # Filling the slack after measuring the marker can cross a file header and make the
    # marker describe the earlier prefix instead — a truthful-but-wrong recovery pointer.
    available = cap - TRACKED_MARKER_RESERVE
    if available <= 0:
        raise RuntimeError("complete tracked-diff truncation marker cannot fit review cap")
    prefix = raw[:available].decode("utf-8", "ignore").encode("utf-8")
    path, path_offset = _diff_path_at(raw, paths, len(prefix))
    omitted = len(raw) - len(prefix)
    marker = (
        f"\n{TRACKED_TRUNCATION_MARKER} {omitted} bytes omitted starting in "
        f"{json.dumps(path, ensure_ascii=True)} at diff byte +{path_offset}; inspect "
        "the complete filtered tracked diff in the working tree (agy uses its mirror); "
        f"{exclusion_note}]\n"
    ).encode("utf-8")
    if len(marker) > TRACKED_MARKER_RESERVE:
        raise RuntimeError("complete tracked-diff truncation marker exceeds its reserve")
    return (prefix + marker).decode("utf-8")


def _current_run_decisions(repo: Path, target: str) -> str:
    """Compact, deterministic latest-decision ledger scoped to the active run."""
    entries = log_entries(repo, target)
    try:
        start = _current_run_start(entries)
    except ValueError as e:
        raise RuntimeError(
            f"review prompt has no usable {RUN_START!r} for log target {target!r}: {e}") from e
    current = entries[start + 1:]
    if any(_is_run_end(entry) for entry in current):
        raise RuntimeError(
            f"review prompt cannot use a run already closed by {RUN_END!r}; "
            f"append the next {RUN_START!r} first")
    structural = {RUN_START, RUN_END, CYCLE_END, RUN_GAP_RESOLUTION}
    latest = {}
    for entry in current:
        finding_id = entry.get("finding_id")
        decision = entry.get("decision")
        if not isinstance(finding_id, str) or not finding_id:
            raise RuntimeError("current run contains a record without a finding_id")
        if finding_id not in structural:
            if decision not in DECISIONS:
                raise RuntimeError(
                    f"current-run finding {finding_id!r} has invalid decision {decision!r}")
            latest[finding_id] = decision
    if not latest:
        return "(none)"
    return "\n".join(
        f"{json.dumps(finding_id, ensure_ascii=False)}={latest[finding_id]}"
        for finding_id in sorted(latest))


def _committed_regular_blob(repo: Path, relpath: str) -> tuple[bytes, str]:
    """Read one exact regular blob from HEAD without consulting worktree bytes."""
    tree = _git_authority().run(
        ["ls-tree", "-z", "HEAD", "--", relpath], repo=repo,
        capture_output=True, check=False)
    if tree.returncode != 0:
        raise RuntimeError(
            f"cannot resolve committed council wrapper HEAD:{relpath}: "
            f"{tree.stderr.decode('utf-8', 'replace').strip()}")
    records = [record for record in tree.stdout.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise RuntimeError(
            f"committed council wrapper HEAD:{relpath} is missing or ambiguous")
    metadata, recorded_path = records[0].split(b"\t", 1)
    fields = metadata.split()
    if (len(fields) != 3 or fields[0] not in (b"100644", b"100755")
            or fields[1] != b"blob" or recorded_path != relpath.encode()):
        raise RuntimeError(
            f"committed council wrapper HEAD:{relpath} is not a regular blob")
    blob = _git_authority().run(
        ["cat-file", "blob", fields[2]], repo=repo,
        capture_output=True, check=False)
    if blob.returncode != 0:
        raise RuntimeError(
            f"cannot read committed council wrapper {fields[2].decode()}: "
            f"{blob.stderr.decode('utf-8', 'replace').strip()}")
    return blob.stdout, fields[2].decode("ascii")


def _compile_council_engine(source: bytes, origin: str, *, cache: bool = False):
    """Compile one already-captured engine blob; never re-read it during this run."""
    global _COUNCIL_ENGINE
    digest = hashlib.sha256(source).hexdigest()
    if (cache and _COUNCIL_ENGINE is not None
            and getattr(_COUNCIL_ENGINE, "_skill_tuneup_origin", None) == origin
            and getattr(_COUNCIL_ENGINE, "_skill_tuneup_digest", None) == digest):
        return _COUNCIL_ENGINE
    name = f"_skill_tuneup_council_{digest[:12]}_{uuid.uuid4().hex[:8]}"
    module = types.ModuleType(name)
    module.__file__ = origin
    module._skill_tuneup_origin = origin
    module._skill_tuneup_digest = digest
    module._skill_tuneup_source = source
    sys.modules[name] = module
    try:
        exec(compile(source, origin, "exec"), module.__dict__)  # noqa: S102
    except Exception as e:
        sys.modules.pop(name, None)
        raise RuntimeError(f"cannot execute council engine from {origin}: {e}") from e
    if cache:
        _COUNCIL_ENGINE = module
    return module


def _council_engine_module(repo: Path, *, committed: bool = False,
                           fresh: bool = False):
    """Compatibility loader; final reviews use :func:`_capture_council_reviewer`."""
    registry = registry_repo(repo)
    if committed:
        source, _blob_id = _committed_regular_blob(registry, REVIEWER_ENGINE_RELPATH)
        return _compile_council_engine(
            source, f"{registry}@HEAD:{REVIEWER_ENGINE_RELPATH}")

    path = registry / REVIEWER_ENGINE_RELPATH
    link = _symlink_component(registry, path)
    if link is not None:
        raise RuntimeError(
            f"council wrapper source {path} is symlink-backed at {link}; "
            "refusing to import code outside the repository-owned engine")
    if not path.is_file():
        raise RuntimeError(
            f"cannot locate {REVIEWER_ENGINE_RELPATH} to prove the final argv bound; "
            "run tuneup.py from the khenrix-utils checkout")
    try:
        source = path.read_bytes()
    except OSError as e:
        raise RuntimeError(f"cannot read council engine from {path}: {e}") from e
    return _compile_council_engine(source, str(path), cache=not fresh)


def _reviewer_dirty_paths(registry: Path) -> tuple[str, ...]:
    """Tracked, staged, untracked, or index-hidden reviewer inputs under both trees."""
    changed = _git_authority().run(
        ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only",
         "-z", "HEAD", "--", *REVIEWER_WATCH_PATHS],
        repo=registry, capture_output=True, check=False)
    if changed.returncode != 0:
        raise RuntimeError(
            "cannot determine council reviewer tracked state: "
            + changed.stderr.decode("utf-8", "replace").strip())
    untracked = _git_authority().run(
        ["ls-files", "--others", "--exclude-standard", "-z", "--",
         *REVIEWER_WATCH_PATHS],
        repo=registry, capture_output=True, check=False)
    if untracked.returncode != 0:
        raise RuntimeError(
            "cannot determine council reviewer untracked state: "
            + untracked.stderr.decode("utf-8", "replace").strip())
    flags = _git_authority().run(
        ["ls-files", "-v", "-z", "--", *REVIEWER_WATCH_PATHS],
        repo=registry, capture_output=True, check=False)
    if flags.returncode != 0:
        raise RuntimeError(
            "cannot determine council reviewer index flags: "
            + flags.stderr.decode("utf-8", "replace").strip())
    paths = {
        os.fsdecode(raw) for payload in (changed.stdout, untracked.stdout)
        for raw in payload.split(b"\0") if raw
    }
    for record in flags.stdout.split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            raise RuntimeError("malformed git ls-files -v reviewer record")
        # H is the ordinary cached-file marker. Lowercase means assume-unchanged and S
        # means skip-worktree; either can hide a worktree edit from `git diff HEAD`.
        if record[:1] != b"H":
            paths.add(f"{os.fsdecode(record[2:])} [index-flag {os.fsdecode(record[:1])}]")
    return tuple(sorted(paths))


def _capture_council_reviewer(repo: Path) -> CouncilReviewer:
    """Freeze the exact reviewer once, selecting HEAD if either council tree is dirty.

    The dirty decision is repository-global, not target-specific: an ordinary target must
    not be reviewed by under-test council machinery merely because its own skill name is
    different. A second state sample closes the ordinary read-between-checks race. If Git
    reports clean but the engine bytes differ from HEAD (for example an index bit hid the
    edit), HEAD wins and the discrepancy remains visible in provenance.
    """
    registry = registry_repo(repo).resolve()
    # Hermetic self-test repositories intentionally contain only the source relevant to
    # each case. When a fixture has no reviewer tree of its own, bind it to the dedicated
    # clean registry fixture instead of reaching into the developer's real checkout.
    if (not os.path.lexists(registry / REVIEWER_ENGINE_RELPATH)
            and _SELF_TEST_REGISTRY_ROOT is not None
            and registry != _SELF_TEST_REGISTRY_ROOT.resolve()):
        registry = _SELF_TEST_REGISTRY_ROOT.resolve()
    head = _git_authority().run(
        ["rev-parse", "--verify", "HEAD^{commit}"], repo=registry,
        capture_output=True, text=True, check=False)
    if head.returncode != 0 or not head.stdout.strip():
        raise RuntimeError(
            "cannot resolve the committed council reviewer HEAD: " + head.stderr.strip())
    head_commit = head.stdout.strip()
    dirty = _reviewer_dirty_paths(registry)
    head_source = None
    if dirty:
        head_source, _blob_id = _committed_regular_blob(
            registry, REVIEWER_ENGINE_RELPATH)
        module = _compile_council_engine(
            head_source, f"{registry}@{head_commit}:{REVIEWER_ENGINE_RELPATH}")
        selection = "committed-head"
    else:
        module = _council_engine_module(registry, fresh=True)
        dirty = _reviewer_dirty_paths(registry)
        if dirty:
            head_source, _blob_id = _committed_regular_blob(
                registry, REVIEWER_ENGINE_RELPATH)
            module = _compile_council_engine(
                head_source, f"{registry}@{head_commit}:{REVIEWER_ENGINE_RELPATH}")
            selection = "committed-head"
        else:
            head_source, _blob_id = _committed_regular_blob(
                registry, REVIEWER_ENGINE_RELPATH)
            captured = getattr(module, "_skill_tuneup_source", b"")
            if captured != head_source:
                dirty = (f"{REVIEWER_ENGINE_RELPATH} [working bytes differ from HEAD]",)
                module = _compile_council_engine(
                    head_source, f"{registry}@{head_commit}:{REVIEWER_ENGINE_RELPATH}")
                selection = "committed-head"
            else:
                selection = "working-tree-clean"
    return CouncilReviewer(
        module=module,
        registry=registry,
        selection=selection,
        engine_sha256=module._skill_tuneup_digest,
        head_commit=head_commit,
        dirty_paths=dirty,
    )


def _assert_review_prompt_bound(repo: Path, prompt: str, *, committed: bool = False,
                                reviewer: CouncilReviewer | None = None) -> None:
    """Fail before fanout if the captured engine's exact wrappers could cross argv."""
    if "\0" in prompt:
        raise RuntimeError(
            "complete council prompt contains a NUL byte and cannot be passed as argv")
    if reviewer is not None and committed:
        raise ValueError("pass either reviewer= or committed=True, not both")
    engine = (reviewer.module if reviewer is not None
              else _council_engine_module(repo, committed=committed))
    token = f"{engine.SENTINEL_PREFIX}{'0' * 12}"
    try:
        wrapped = engine.apply_sentinel(
            engine.apply_readonly_posture(engine.apply_member_note(prompt)), token)
    except Exception as e:  # malformed reviewer API is a clean gate refusal
        raise RuntimeError(f"council wrapper cannot apply its prompt boundary: {e}") from e
    size = len(wrapped.encode("utf-8"))
    if size + REVIEW_WRAPPER_HEADROOM > REVIEW_SINGLE_ARG_MAX:
        raise RuntimeError(
            f"complete council prompt is {size} bytes after wrappers; needs "
            f"{REVIEW_WRAPPER_HEADROOM} bytes headroom below {REVIEW_SINGLE_ARG_MAX}")


def review_material(repo: Path, skill: str, target: str,
                    total_cap: int = REVIEW_TOTAL_CAP,
                    reviewer: CouncilReviewer | None = None) -> str:
    """Build the complete bounded Step-9 prompt, with mandatory untracked content first.

    This was a shell loop in SKILL.md and shell was the wrong tool — three cycles of
    review found four ways it silently mis-served the reviewer, each verified live:

    - `cat`/`wc`/`head` FOLLOW SYMLINKS, so an untracked symlink pointing outside the
      repo would send its referent to three external CLIs. That is an exfiltration path,
      not a formatting bug; symlinks are skipped outright and named in the output.
    - `head -c` cuts by BYTE, so truncating mid-codepoint yields invalid UTF-8 and
      crashes fanout's `read_text()` before any seat spawns — reintroducing the exact
      crash the cap was added to prevent. Truncation is decoded with errors='ignore'.
    - `grep -Iq .` calls a newline-only file BINARY (`.` matches no character on an
      empty line), silently dropping a legitimate text file from the review.
    - `wc -c` on a broken symlink emits nothing, so `[ "$sz" -gt 0 ]` raises.

    Binary detection streams the complete candidate at constant memory, looking for NUL or
    invalid UTF-8 anywhere while retaining text only up to the prompt cap. Every untracked
    name and all safe text are mandatory because agy's review worktree contains tracked
    changes only. If that block cannot fit, fail rather than send a partial truth.
    Only the tracked diff may be truncated, with an exact working-tree recovery marker.
    The total includes instructions and a compact current-run decision ledger. The exact
    council wrappers are then applied in memory and checked with one page of argv headroom.
    The reviewer is captured once. If either council implementation tree is dirty, this
    uses the regular engine blob at HEAD for every target; otherwise it captures the clean
    working-tree bytes. ``review-diff`` passes this same capture to the actual fanout, so
    prompt sizing and execution cannot silently use two generations of the engine.
    The ledger and ordinary working-tree wrapper paths are independently refused when any
    lexical component is a symlink, because both are consumed outside the untracked-payload
    loop. A dirty council tree always uses the regular wrapper blob at HEAD, regardless of
    which skill is being tuned.

    The two non-obvious choices, kept here so a later 'simplification' meets them at the
    code rather than only in SKILL.md:

    - `diff HEAD`, never bare `git diff`. Bare diff is blind to the INDEX, so a fully
      staged tree returns empty — and an empty result is exactly what tells Step 9 there
      is nothing to review, silently skipping a mandatory council review.
    - untracked files are transmitted explicitly. `git diff` in any form cannot see them, so
      a file created during the run (an `evals.json` scaffolded in Step 8 is the live
      case) would never reach the reviewer while `git add -A` still ships it.
    """
    def git(*a: str, binary: bool = False):
        """A FAILED git is not an empty diff.

        Capturing stderr to decide "is there anything to review" removed the only signal
        the shell version still had: git's `fatal:` reached the terminal there. Silently
        returning "" here makes a broken repo indistinguishable from a clean one, so
        Step 9 skips the mandatory review and `convergence-status` reads the resulting
        zero-finding cycle as CONVERGED — over a candidate no council ever saw. Realistic
        triggers: dubious ownership on a /mnt/c checkout, an unborn HEAD, a mistyped $REPO.
        """
        # Bytes always: tracked diffs can contain a late NUL in a file git misclassifies as
        # text, and locale decoding would either abort or pass an argv-forbidden NUL through.
        # Review bytes must be native and canonical: repository attributes, user color
        # configuration, and an inherited external diff program can otherwise change what
        # the council sees or execute code merely while assembling the prompt.
        if a and a[0] == "diff":
            a = ("diff",
                 "--no-ext-diff", "--no-textconv", "--no-color",
                 "--no-renames", "--default-prefix", *a[1:])
        # This wrapper owns the only pathspec-magic call sites in the engine: its trusted
        # :(exclude) filters. The authority still strips every ambient pathspec mode.
        p = _git_authority().run(
            a, repo=repo, literal_pathspecs=False, config=("core.quotePath=true",),
            capture_output=True, check=False)
        if p.returncode != 0:
            raise RuntimeError(f"git {' '.join(a)} failed in {repo}: "
                               f"{p.stderr.decode('utf-8', 'replace').strip()}")
        return p.stdout if binary else p.stdout.decode("utf-8", "backslashreplace").replace(
            "\0", "\\x00")

    def tracked_diff(*pathspec: str) -> tuple[str, list[str]]:
        """Canonical patch atomically bound to Git's raw, NUL-delimited path roster."""
        payload = git(
            "diff", "--raw", "-z", "-p", "HEAD", "--", *pathspec, binary=True)
        return _parse_atomic_tracked_diff(payload)

    _validate_skill_name(skill)
    reviewer = reviewer or _capture_council_reviewer(repo)
    bound_skill = _log_target_skill(repo, target)
    if bound_skill != skill:
        raise ValueError(
            f"review target {target!r} belongs to skill {bound_skill!r}, not {skill!r}")
    info, target_paths = _require_skill_paths(repo, skill)
    target_relpaths = [str(path.relative_to(repo)) for path in target_paths]

    excluded_pathspecs = []
    exclusion_labels = []
    if info["tier"] == "full-gate":
        excluded_pathspecs.append(":(exclude)marketplaces")
        exclusion_labels.append("generated marketplaces are excluded")
    registry = registry_repo(repo)
    active_log = _require_owned_log_path(repo, target)
    _require_active_log_append_only(registry, active_log)
    if registry.resolve() == repo.resolve():
        active_log_rel = str(active_log.relative_to(repo))
        excluded_pathspecs.append(f":(exclude){active_log_rel}")
        exclusion_labels.append(f"active run log {active_log_rel} is represented by the ledger")
    ledger = _current_run_decisions(repo, target)

    manifest_relpaths = []
    fact_relpaths = []
    script_relpaths = []
    for path in target_paths:
        for manifest_name in ("SKILL.md", "SKILL.md.tmpl"):
            manifest = path / manifest_name
            if manifest.is_file():
                manifest_relpaths.append(str(manifest.relative_to(repo)))
        scripts = path / "scripts"
        if scripts.is_dir():
            script_relpaths.append(str(scripts.relative_to(repo)))
    facts_source = repo / "capabilities.toml"
    if facts_source.is_file():
        current_facts = _facts_lines(facts_source.read_text(encoding="utf-8"), skill)
        committed_facts = _facts_snapshot_at(repo, "HEAD", skill)
        if current_facts or committed_facts is not None:
            fact_relpaths.append(str(facts_source.relative_to(repo)))
    priority_relpaths = manifest_relpaths + fact_relpaths + script_relpaths
    target_parts: list[tuple[str, list[str]]] = []
    if manifest_relpaths:
        target_parts.append(tracked_diff(*manifest_relpaths))
    if fact_relpaths:
        target_parts.append(tracked_diff(*fact_relpaths))
    if script_relpaths:
        target_parts.append(tracked_diff(*script_relpaths))
    target_remainder_exclusions = [
        f":(exclude){relpath}" for relpath in priority_relpaths]
    target_parts.append(tracked_diff(
        *target_relpaths, *target_remainder_exclusions))
    target_tracked = "".join(text for text, _ in target_parts)
    target_paths_inventory = [path for _, paths in target_parts for path in paths]
    other_exclusions = excluded_pathspecs + [
        f":(exclude){relpath}" for relpath in target_relpaths + fact_relpaths]
    other_tracked, other_paths_inventory = tracked_diff(".", *other_exclusions)
    tracked_sections = []
    if target_tracked:
        tracked_sections.append(
            "=== TARGET SKILL SOURCE DIFF (first; may be truncated) ===\n"
            + target_tracked)
    if other_tracked:
        tracked_sections.append(
            "\n=== OTHER TRACKED DIFF (may be truncated) ===\n" + other_tracked)
    tracked = "".join(tracked_sections)
    tracked_inventory_parts = []
    if target_tracked:
        tracked_inventory_parts.append(
            _diff_inventory(target_tracked, target_paths_inventory))
    if other_tracked:
        tracked_inventory_parts.append(
            _diff_inventory(other_tracked, other_paths_inventory))
    tracked_inventory = "\n".join(tracked_inventory_parts)
    tracked_paths_inventory = target_paths_inventory + other_paths_inventory
    raw = git("ls-files", "--others", "--exclude-standard", "-z",
              "--", ".", *excluded_pathspecs, binary=True)
    names = list(filter(None, raw.split(b"\0")))
    if not tracked and not names:
        return ""

    prelude = (REVIEW_INSTRUCTIONS.format(target=target, repo=repo)
               + "\n\n=== CURRENT RUN DECISIONS (latest finding_id=decision) ===\n"
               + ledger
               + "\n\n=== TRACKED DIFF INVENTORY (complete; content follows or is "
                 "available in the working tree) ===\n"
               + tracked_inventory)
    mandatory = [prelude]
    last_untracked = None
    for name in filter(None, raw.split(b"\0")):
        rel = os.fsdecode(name)
        shown = json.dumps(rel, ensure_ascii=True)
        last_untracked = shown
        p = repo / rel
        if p.is_symlink():          # transmit the git blob (target string), never its referent
            try:
                link_target = os.readlink(p)
            except OSError as e:
                raise RuntimeError(f"cannot read untracked symlink {shown}: {e}") from e
            mandatory.append(
                "\n\n=== NEW FILE (untracked, SYMLINK — not followed): "
                f"{shown} -> {json.dumps(link_target, ensure_ascii=True)} ===\n")
            continue
        if not p.is_file():
            raise RuntimeError(
                f"untracked path {shown} is not a regular file; cannot transmit it safely")
        try:
            size = p.stat().st_size
            chunks = []
            observed = 0
            too_large = False
            is_binary = False
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            with open(p, "rb") as fh:
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    observed += len(chunk)
                    if b"\0" in chunk:
                        is_binary = True
                        break
                    try:
                        decoder.decode(chunk, final=False)
                    except UnicodeDecodeError:
                        is_binary = True
                        break
                    if observed <= total_cap:
                        chunks.append(chunk)
                    else:
                        too_large = True
                if not is_binary:
                    try:
                        decoder.decode(b"", final=True)
                    except UnicodeDecodeError:
                        is_binary = True
        except RuntimeError:
            raise
        except OSError as e:
            raise RuntimeError(f"cannot read untracked file {shown}: {e}") from e
        if is_binary:
            mandatory.append(
                f"\n\n=== NEW FILE (untracked, {size} bytes, "
                f"BINARY — metadata only): {shown} ===\n")
            continue
        if too_large:
            raise RuntimeError(
                f"untracked text file {shown} is at least {observed} bytes and cannot fit "
                f"the {total_cap}-byte review cap")
        data = b"".join(chunks)
        mandatory.append(
            f"\n\n=== NEW FILE (untracked, {len(data)} bytes): {shown} ===\n"
            + data.decode("utf-8"))

    if tracked:
        mandatory.append("\n\n=== TRACKED DIFF (target source first) ===\n")
    prefix = "".join(mandatory)
    prefix_size = len(prefix.encode("utf-8"))
    marker_reserve = TRACKED_MARKER_RESERVE if tracked else 0
    if prefix_size + marker_reserve > total_cap:
        raise RuntimeError(
            "complete instructions, current-run ledger, and untracked material need "
            f"{prefix_size + marker_reserve} bytes, above the {total_cap}-byte review cap"
            + (f" (last untracked path: {last_untracked})" if last_untracked else ""))
    exclusion_note = "; ".join(exclusion_labels) or "no tracked paths were excluded"
    prompt = prefix + (_bounded_tracked_diff(
        tracked, tracked_paths_inventory, total_cap - prefix_size, exclusion_note)
                       if tracked else "")
    if len(prompt.encode("utf-8")) > total_cap:
        raise RuntimeError("internal review cap error: assembled prompt exceeds total cap")
    _assert_review_prompt_bound(repo, prompt, reviewer=reviewer)
    return prompt


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Publish one review artifact without exposing a partially-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path) and path.is_symlink():
        raise RuntimeError(f"refusing to replace symlink-backed review artifact {path}")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.",
                suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def run_diff_review(repo: Path, skill: str, target: str, *, mode: str = "normal",
                    retries: int = 2, timeout: int | None = None,
                    workdir: Path | None = None,
                    reviewer: CouncilReviewer | None = None) -> tuple[int, dict]:
    """Build, size, and fan out one final diff through one immutable engine capture.

    This is deliberately one operation. The old two-command recipe sized the prompt by
    importing one engine and then executed ``fanout.py`` from the filesystem again. A
    candidate or concurrent edit between those commands could therefore be sized by HEAD
    and executed by different bytes. The returned manifest records the capture digest and
    why HEAD was selected. Providers are fixed to the shipping Codex+agy panel.
    """
    if type(retries) is not int or retries < 0:
        raise ValueError("review retries must be a non-negative integer")
    if timeout is not None and (type(timeout) is not int or timeout <= 0):
        raise ValueError("review timeout must be a positive integer")
    reviewer = reviewer or _capture_council_reviewer(repo)
    modes = getattr(reviewer.module, "MODES", {})
    if mode not in modes:
        raise ValueError(
            f"review mode {mode!r} is not supported by captured council engine "
            f"({sorted(modes)})")
    prompt = review_material(repo, skill, target, reviewer=reviewer)
    if not prompt:
        return 0, {
            "status": "no-diff",
            "reviewer_source": reviewer.provenance(),
        }

    review_dir = (Path(workdir).resolve() if workdir is not None else
                  Path(tempfile.mkdtemp(prefix="skill-tuneup-review-")).resolve())
    if os.path.lexists(review_dir) and review_dir.is_symlink():
        raise RuntimeError(f"review workdir is symlink-backed: {review_dir}")
    review_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = review_dir / "review-prompt.txt"
    prompt_bytes = prompt.encode("utf-8")
    _atomic_write_bytes(prompt_path, prompt_bytes)
    engine_argv = [
        "--prompt-file", str(prompt_path),
        "--providers", ",".join(FINAL_PANEL),
        "--mode", mode,
        "--retries", str(retries),
        "--workdir", str(review_dir),
        "--out", "json",
    ]
    if timeout is not None:
        engine_argv.extend(("--timeout", str(timeout)))

    output = io.StringIO()
    previous_cwd = Path.cwd()
    try:
        os.chdir(repo)
        with contextlib.redirect_stdout(output):
            result = reviewer.module.main(engine_argv)
    finally:
        os.chdir(previous_cwd)
    if type(result) is not int:
        raise RuntimeError(
            f"captured council engine returned non-integer status {result!r}")
    manifest = _strict_json_loads(
        output.getvalue(), context="captured council engine manifest")
    if not isinstance(manifest, dict):
        raise RuntimeError("captured council engine manifest root must be an object")
    manifest["reviewer_source"] = reviewer.provenance()
    manifest["review_prompt"] = {
        "path": str(prompt_path),
        "sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "bytes": len(prompt_bytes),
    }
    _atomic_write_bytes(
        review_dir / "manifest.json",
        (_strict_json_dumps(manifest, indent=2) + "\n").encode("utf-8"),
    )
    return result, manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="skill-tuneup deterministic helpers")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("baseline", "stale-models", "triage"):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", required=True)
        if name != "triage":
            sp.add_argument("--skill", required=(name == "baseline"))
        if name == "stale-models":
            sp.add_argument("--approved", default="", help="extra approved ids, comma-separated")
        sp.add_argument("--json", action="store_true")
    kp = sub.add_parser("lock")
    kp.add_argument("action", choices=["acquire", "refresh", "release", "status"])
    kp.add_argument("--owner", default="")
    cp = sub.add_parser("convergence-status")
    cp.add_argument("--repo", required=True)
    cp.add_argument("--target", required=True)
    cp.add_argument("--json", action="store_true")
    tp = sub.add_parser("target-info")
    tp.add_argument("--repo", required=True)
    tp.add_argument("--skill", required=True)
    tp.add_argument("--json", action="store_true")
    fp = sub.add_parser("verify-final-receipt")
    fp.add_argument("--repo", required=True)
    fp.add_argument("--skill", required=True)
    fp.add_argument(
        "--panel", default=",".join(FINAL_PANEL),
        help="compatibility spelling for the fixed capabilities.toml [eval] panel")
    fp.add_argument("--json", action="store_true")
    rp = sub.add_parser("review-material")
    rp.add_argument("--repo", required=True)
    rp.add_argument("--skill", required=True)
    rp.add_argument("--target", required=True)
    rd = sub.add_parser("review-diff")
    rd.add_argument("--repo", required=True)
    rd.add_argument("--skill", required=True)
    rd.add_argument("--target", required=True)
    rd.add_argument("--mode", default="normal")
    rd.add_argument("--retries", type=int, default=2)
    rd.add_argument("--timeout", type=int)
    rd.add_argument("--workdir")
    lp = sub.add_parser("log")
    lp.add_argument("action", choices=["append", "list"])
    lp.add_argument("--repo", required=True)
    lp.add_argument("--target", required=True)
    lp.add_argument("--entry", help="JSON object for append (or pass via stdin)")
    lp.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    skill_arg = getattr(args, "skill", None)
    if skill_arg is not None:
        try:
            _validate_skill_name(skill_arg)
        except ValueError as e:
            print(f"  ✗ {e}")
            return 2

    if args.self_test:
        return _self_test()
    if not args.cmd:
        ap.print_help()
        return 2
    if args.cmd == "lock":  # the only command with no --repo
        if args.action == "status":
            st = lock_status()
            if not st["held"]:
                print("no lock held")
                return 0
            print(f"held by {st['owner'] or 'unknown'} ({st['age_min']} min old; "
                  f"the next acquire steals it above {st['stale_after_min']} min)")
            return 0
        if args.action == "acquire":
            ok, info = lock_acquire()
            print(f"OWNER={info}" if ok else f"  ✗ lock not acquired: {info}")
        elif args.action == "refresh":
            if not args.owner:
                print("  ✗ --owner is required (the token from `lock acquire`)")
                return 2
            ok, info = lock_refresh(args.owner)
            print("lock refreshed" if ok else f"  ✗ {info} — STOP, do not keep working")
        else:
            ok, info = lock_release(args.owner)
            print(f"lock {info}" if ok else f"  ✗ {info}")
        return 0 if ok else 1
    repo = Path(args.repo).resolve()
    try:
        _require_exact_git_toplevel(repo)
    except ValueError as e:
        print(f"  ✗ {e}")
        return 2

    if args.cmd == "baseline":
        try:
            b = baseline(repo, args.skill)
        except (ValueError, FileNotFoundError) as e:
            print(f"  ✗ {e}")
            return 2
        if not b:
            print(f"no commits found for {args.skill}")
            return 1
        print(json.dumps(b, indent=2) if args.json else
              f"baseline {b['sha'][:9]}  {b['date']}  {b['subject']}"
              f"  ({b['skipped_as_chore']} newer chore/docs commit(s) skipped)")
    if args.cmd == "review-diff":
        try:
            rc, manifest = run_diff_review(
                repo, args.skill, args.target, mode=args.mode,
                retries=args.retries, timeout=args.timeout,
                workdir=Path(args.workdir) if args.workdir else None)
        except (RuntimeError, ValueError, FileNotFoundError, OSError) as e:
            print(f"  ✗ {e}", file=sys.stderr)
            return 2
        print(_strict_json_dumps(manifest, indent=2))
        return rc
    if args.cmd == "review-material":
        try:
            sys.stdout.write(review_material(repo, args.skill, args.target))
        except (RuntimeError, ValueError, FileNotFoundError) as e:
            # Never let a git/log/bound failure read as "nothing to review".
            print(f"  ✗ {e}", file=sys.stderr)
            return 2
        return 0
    elif args.cmd == "convergence-status":
        try:
            st = convergence_status(log_entries(repo, args.target))
        except (OSError, ValueError) as e:
            print(f"  ✗ {e}")
            return 2
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print(f"cycles:   {st['cycles']}   serious-per-cycle: {st['counts']}")
            print(f"verdict:  {st['verdict']}")
            for w in st.get("warnings", []):
                print(f"  ⚠ {w}")
            gap = st.get("run_gap")
            if gap and not gap["resolved"]:
                print("  applied gap records: "
                      f"{json.dumps(gap['applied_records'], sort_keys=True)}")
                if gap["resolution_open"]:
                    required = {
                        "target": args.target,
                        "finding_id": RUN_GAP_RESOLUTION,
                        "decision": "applied",
                        "title": RUN_GAP_RESOLUTION_TITLE,
                        "schema_version": RUN_GAP_RESOLUTION_SCHEMA,
                        "prior_gap_indices": [],
                        "current_gap_indices": [],
                        **{key: gap[key] for key in (
                            "run_start_ts", "gap_sha256", "gap_records",
                            "applied_findings", "resolution")},
                    }
                    print("  classify EVERY emitted applied gap index exactly once as prior "
                          "or current, re-record each current ordinary occurrence, and add a "
                          "non-empty reason:")
                    print(f"  {json.dumps(required, sort_keys=True)}")
                    for record in gap["applied_records"]:
                        if _gap_record_needs_surrogate(record):
                            print(f"  if lifecycle gap index {record['gap_index']} is current, "
                                  "append this bound ordinary serious surrogate first and "
                                  "add a non-empty reason:")
                            recipe = _gap_replay_entry(
                                gap, record, current_target=args.target)
                            print(f"  {json.dumps(recipe, sort_keys=True)}")
                else:
                    print(f"  no resolution recipe: this run's {RUN_END} already closed "
                          "the append window")
            if st["verdict"] == "stalled — hand over":
                print("  the serious-finding rate stopped falling — another cycle buys "
                      "another defect, not convergence. Hand the remainder to the user.")
        return 0 if st["converged"] else 1
    elif args.cmd == "target-info":
        info = target_info(repo, args.skill)
        problem = _target_resolution_problem(repo, args.skill, info)
        if args.json:
            print(json.dumps(info, indent=2))
        elif problem:
            print(f"  ✗ {problem}")
        else:
            print(f"skill:  {args.skill}  in  {info['repo']}")
            print(f"paths:  {', '.join(info['paths'])}")
            print(f"tier:   {info['tier']}")
            print(f"gate:   {info['gate']}")
            print(f"log:    docs/tuneups/log/{info['log_target']}.jsonl (in khenrix-utils)")
            if info["missing_manifest_near_misses"]:
                print("note:   present but not a skill: "
                      + "; ".join(info["missing_manifest_near_misses"]))
        return 2 if problem else 0
    elif args.cmd == "verify-final-receipt":
        try:
            problems = verify_final_receipt(repo, args.skill, args.panel.split(","))
        except (ValueError, FileNotFoundError) as e:
            print(f"  ✗ {e}")
            return 2
        if args.json:
            print(json.dumps({"skill": args.skill, "problems": problems}, indent=2))
        elif problems:
            for p in problems:
                print(f"  ✗ {p}")
            print("FINAL GATE NOT PROVEN — do not record convergence")
        else:
            # Name both authorities. Deterministic targets retain their certifier and now
            # also prove the same canonical advisory panel as every other shipping skill.
            _rp = repo / "evals" / args.skill / "receipt.json"
            try:
                _exempt = _load_checks().is_self_test_gated(
                    args.skill, json.loads(_rp.read_text()))
            except (OSError, TypeError, ValueError):
                _exempt = False   # unreadable receipt cannot have produced `problems == []`

            _how = ("deterministic-certifier + canonical-panel and matches source"
                    if _exempt else "canonical-panel and matches source")
            print(f"final gate proven: {args.skill} receipt is {_how}")
        return 1 if problems else 0
    elif args.cmd == "stale-models":
        try:
            approved = approved_models(repo, args.approved)
            hits = scan_stale_models(repo, getattr(args, "skill", None), approved)
        except (ValueError, FileNotFoundError) as e:
            print(f"  ✗ {e}")
            return 2
        stale = [h for h in hits if h["status"] == "stale-candidate"]
        if args.json:
            print(json.dumps({"hits": hits, "approved": sorted(approved)}, indent=2))
        else:
            for h in hits:
                print(f"{h['file']}:{h['line']}:{h['id']}:{h['status']}")
            print(f"SUMMARY {len(hits)} hits, {len(stale)} stale-candidate, "
                  f"{len({h['id'] for h in hits})} distinct ids")
    elif args.cmd == "triage":
        try:
            rows = triage(repo)
        except ValueError as e:   # a refusal is a RESULT, not a crash — every other
            print(f"  \u2717 {e}")   # refusal in this file prints and returns a code
            return 2
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(f"{'score':>5}  {'skill':<16} {'receipt':<13} {'age(d)':>6} "
                  f"{'stale-ids':>9} {'md-lines':>8}")
            for r in rows:
                print(f"{r['score']:>5}  {r['skill']:<16} {r['receipt']:<13} "
                      f"{r['age_days'] if r['age_days'] is not None else '-':>6} "
                      f"{r['stale_model_hits']:>9} {r['skill_md_lines']:>8}")
            print("\n" + triage_recommendation(rows))
    elif args.cmd == "log":
        try:
            if args.action == "append":
                raw = args.entry or sys.stdin.read()
                entry = log_append(
                    repo, args.target,
                    _strict_json_loads(raw, context="log append input"))
                print(json.dumps(entry, sort_keys=True))
            else:
                entries = log_list(repo, args.target)
                if args.json:
                    print(json.dumps(entries, indent=2))
                else:
                    for e in entries:
                        stamp = e.get("ts") if isinstance(e.get("ts"), str) else "?"
                        print(f"{stamp or '?':<26} {e['decision']:<9} {e['finding_id']}"
                              f"  {e.get('title', '')}")
                    print(f"({len(entries)} finding(s) with a recorded decision)")
        except (OSError, TypeError, ValueError) as e:
            print(f"  ✗ {e}")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
