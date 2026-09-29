"""Hermetic admission tests for the fanout skill boundary."""
from __future__ import annotations

import dataclasses
import hashlib
import os
import sys
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components" / "skills"))
from skillctl import tree_hash as skillctl_tree_hash
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_skill_contracts", FANOUT_ROOT / "__init__.py", submodule_search_locations=[str(FANOUT_ROOT)]
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)
skills_module = sys.modules["_fanout_skill_contracts.skills"]

from _fanout_skill_contracts import (
    FanoutPlanV1,
    PlanTaskV1,
    SkillAdmissionError,
    SkillLimits,
    SkillLoadEvidence,
    SkillResolver,
    SkillRoot,
    SourceInfoV1,
    SourceStepV1,
)


def _write_skill(root: Path, name: str, text: str = "# Skill\n", *, references: dict[str, str] | None = None) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(text, encoding="utf-8")
    for relative, contents in (references or {}).items():
        destination = skill / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents, encoding="utf-8")
    return skill


def _root(identity: str, path: Path, precedence: int) -> SkillRoot:
    return SkillRoot(identity=identity, path=path, precedence=precedence, provider="codex", target="seat")


def _resolver(*roots: SkillRoot, **limits: int) -> SkillResolver:
    return SkillResolver(roots, limits=SkillLimits(**limits))


def test_target_skill_evidence_loader_binds_receipt_without_granting_delivery(tmp_path):
    source = b"# Plan\n"
    spec = fanout.TargetSpec(
        "booking", "github.com/example/booking", "TASK-123",
        "refs/heads/feat/TASK-123-booking",
    )
    plan = fanout.plan.FanoutPlanV2.from_dict({
        "schema_version": "v2",
        "source": {"path": "plan.md", "sha256": hashlib.sha256(source).hexdigest(),
                   "parser_version": "v1"},
        "defaults": {"executor_ids": ["claude", "codex"], "rounds": 1,
                     "timeout": 120, "retries": 0, "minimum_success": 2},
        "targets": [spec.to_dict()],
        "source_steps": [{"id": "Task 1/Step 1", "sha256": "a" * 64,
                          "target_id": "booking"}],
        "tasks": [{"id": "work", "kind": "work", "parent_id": None,
                   "title": "Work", "objective": "Finish.",
                   "source_step_ids": ["Task 1/Step 1"], "depends_on": [],
                   "execution_class": "read-only", "required_skills": [],
                   "none_reason": "No specialist skill is needed.", "owned_paths": [],
                   "acceptance": ["Finished."], "checks": [], "provider_policy": None,
                   "target_id": "booking", "dependency_modes": {}}],
    })
    binding = fanout.TargetBinding(spec, tmp_path, tmp_path, 1, 1, "a" * 40, None, "b" * 64,
                                   "refs/heads/main")
    inputs = fanout.RunInputs(
        "run-target", hashlib.sha256(fanout.canonical_json(plan.to_dict())).hexdigest(),
        plan.source.sha256, "c" * 64, "d" * 64, "e" * 64,
        {"claude/read-only/standard": "f" * 64,
         "codex/read-only/standard": "f" * 64},
        {"work": "8" * 64}, profile_shape="class-tier", targets={"booking": binding},
    )
    document = {
        "schema_version": "fanout-target-skill-load-v1",
        "target": {
            "run_id": inputs.run_id, "task_id": "work", "target_id": "booking",
            "repository": spec.repository, "branch_ref": spec.branch_ref,
            "base_oid": binding.base_oid, "baseline_sha256": binding.baseline_sha256,
        },
        "events": [{
            "kind": "model", "skill": "", "tree_hash": "", "source": "",
            "provider": "", "session_id": "", "seat_id": "", "truncated": False,
            "text": "I loaded it",
        }],
    }
    with fanout.ArtifactStore(tmp_path / "artifacts") as store:
        ref = store.write_bytes(
            "target-evidence/booking/skill-load/receipt.json",
            fanout.canonical_json(document),
        )
        envelope = fanout.TargetEvidenceEnvelope(
            inputs.run_id, "work", "booking", spec.repository, spec.branch_ref,
            binding.base_oid, binding.baseline_sha256, "skill-load", ref,
        )
        events = fanout.load_target_skill_evidence(
            envelope, plan=plan, inputs=inputs, store=store,
        )
        assert events == (SkillLoadEvidence.model_acknowledgement("I loaded it"),)
        wrong = fanout.TargetEvidenceEnvelope(
            inputs.run_id, "work", "address", spec.repository, spec.branch_ref,
            binding.base_oid, binding.baseline_sha256, "skill-load", ref,
        )
        with pytest.raises(fanout.CandidateValidationError, match="target"):
            fanout.load_target_skill_evidence(
                wrong, plan=plan, inputs=inputs, store=store,
            )
        address_spec = fanout.TargetSpec(
            "address", "github.com/example/address", "TASK-123",
            "refs/heads/feat/TASK-123-address",
        )
        plan_document = plan.to_dict()
        plan_document["targets"].append(address_spec.to_dict())
        plan_document["source_steps"].append({
            "id": "Task 2/Step 1", "sha256": "a" * 64, "target_id": "address",
        })
        plan_document["tasks"].append({
            **plan_document["tasks"][0], "id": "address-work", "target_id": "address",
            "source_step_ids": ["Task 2/Step 1"],
        })
        multi_plan = fanout.plan.FanoutPlanV2.from_dict(plan_document)
        address_binding = fanout.TargetBinding(
            address_spec, tmp_path, tmp_path, 2, 2, "c" * 40, None,
            binding.baseline_sha256, "refs/heads/main",
        )
        multi_inputs = dataclasses.replace(
            inputs,
            compiled_plan_sha256=hashlib.sha256(
                fanout.canonical_json(multi_plan.to_dict())
            ).hexdigest(),
            targets={"booking": binding, "address": address_binding},
            skill_manifests={"work": "8" * 64, "address-work": "8" * 64},
        )
        copied_ref = store.write_bytes(
            "target-evidence/address/skill-load/copied.json", store.read_bytes(ref),
        )
        copied_envelope = fanout.TargetEvidenceEnvelope(
            multi_inputs.run_id, "address-work", "address", address_spec.repository,
            address_spec.branch_ref, address_binding.base_oid,
            address_binding.baseline_sha256, "skill-load", copied_ref,
        )
        with pytest.raises(SkillAdmissionError, match="target"):
            fanout.load_target_skill_evidence(
                copied_envelope, plan=multi_plan, inputs=multi_inputs, store=store,
            )
        invalid_ref = store.write_bytes(
            "target-evidence/booking/skill-load/invalid.json",
            fanout.canonical_json(document).replace(b'"text":"I loaded it"', b'"text":NaN'),
        )
        invalid = fanout.TargetEvidenceEnvelope(
            inputs.run_id, "work", "booking", spec.repository, spec.branch_ref,
            binding.base_oid, binding.baseline_sha256, "skill-load", invalid_ref,
        )
        with pytest.raises(SkillAdmissionError, match="schema"):
            fanout.load_target_skill_evidence(
                invalid, plan=plan, inputs=inputs, store=store,
            )


def test_resolve_uses_explicit_precedence_and_records_identical_shadows(tmp_path: Path):
    """Changing root order must not silently change which identical source is attributed."""
    first, second = tmp_path / "first", tmp_path / "second"
    _write_skill(first, "review")
    _write_skill(second, "review")

    resolved = _resolver(_root("later", second, 20), _root("first", first, 10)).resolve(("review",))

    assert [skill.name for skill in resolved] == ["review"]
    assert resolved[0].source.identity == "first"
    assert [origin.identity for origin in resolved[0].origins] == ["first", "later"]
    assert resolved[0].tree_hash == skillctl_tree_hash(first / "review")


def test_tree_hash_matches_skillctl_and_is_independent_of_host_root(tmp_path: Path):
    """Changing the host absolute path must not change portable skill provenance."""
    left, right = tmp_path / "left" / "skills", tmp_path / "right" / "skills"
    left_skill = _write_skill(left, "review", "[checklist](references/checklist.md)\n", references={"references/checklist.md": "Do the check.\n"})
    right_skill = _write_skill(right, "review", "[checklist](references/checklist.md)\n", references={"references/checklist.md": "Do the check.\n"})
    (left_skill / "bin").mkdir()
    (right_skill / "bin").mkdir()
    for path in (left_skill / "bin" / "run", right_skill / "bin" / "run"):
        path.write_bytes(b"#!/bin/sh\n")
        path.chmod(0o755)

    left_resolved = _resolver(_root("left", left, 0)).resolve(("review",))[0]
    right_resolved = _resolver(_root("right", right, 0)).resolve(("review",))[0]

    assert left_resolved.tree_hash == skillctl_tree_hash(left_skill)
    assert left_resolved.tree_hash == right_resolved.tree_hash
    assert left_resolved.manifest == right_resolved.manifest
    assert left_resolved.references == (("references/checklist.md", left_resolved.file_hashes["references/checklist.md"]),)


def test_divergent_shadow_fails_before_any_staging_or_runner(tmp_path: Path):
    """A root-specific variant would make provider behavior ambiguous."""
    first, second = tmp_path / "first", tmp_path / "second"
    _write_skill(first, "review", "# Skill\nfirst\n")
    _write_skill(second, "review", "# Skill\nsecond\n")

    with pytest.raises(SkillAdmissionError, match="divergent"):
        _resolver(_root("first", first, 0), _root("second", second, 1)).resolve(("review",))


@pytest.mark.parametrize("name", ("../escape", ".", "", "nested/name", "review/.."))
def test_skill_names_are_safe_single_path_components(tmp_path: Path, name: str):
    """A plan-supplied name must never select a file outside its root."""
    root = tmp_path / "skills"
    _write_skill(root, "review")

    with pytest.raises(SkillAdmissionError):
        _resolver(_root("root", root, 0)).resolve((name,))


def test_rejects_symlinks_nonregular_entries_and_hardlinked_files(tmp_path: Path):
    """Following an alias could replace reviewed bytes after discovery."""
    root = tmp_path / "skills"
    skill = _write_skill(root, "review")
    (skill / "linked.md").symlink_to(tmp_path / "outside")
    with pytest.raises(SkillAdmissionError, match="symlink"):
        _resolver(_root("root", root, 0)).resolve(("review",))

    (skill / "linked.md").unlink()
    target = skill / "real.md"
    target.write_text("real", encoding="utf-8")
    os.link(target, skill / "hard.md")
    with pytest.raises(SkillAdmissionError, match="hard"):
        _resolver(_root("root", root, 0)).resolve(("review",))


def test_rejects_oversized_files_trees_counts_and_depth(tmp_path: Path):
    """Unbounded trees could turn a skill preflight into a provider denial of service."""
    root = tmp_path / "skills"
    skill = _write_skill(root, "review")
    (skill / "big").write_bytes(b"x" * 5)
    with pytest.raises(SkillAdmissionError, match="file size"):
        _resolver(_root("root", root, 0), max_file_bytes=4).resolve(("review",))
    with pytest.raises(SkillAdmissionError, match="tree size"):
        _resolver(_root("root", root, 0), max_tree_bytes=6).resolve(("review",))
    with pytest.raises(SkillAdmissionError, match="file count"):
        _resolver(_root("root", root, 0), max_files=1).resolve(("review",))
    nested = skill / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "file").write_text("x", encoding="utf-8")
    with pytest.raises(SkillAdmissionError, match="depth"):
        _resolver(_root("root", root, 0), max_depth=2).resolve(("review",))


def test_rejects_empty_directory_flood_before_descending(tmp_path: Path):
    """Directory-only input must consume an explicit finite discovery budget."""
    root = tmp_path / "skills"
    skill = _write_skill(root, "review")
    for number in range(4):
        (skill / f"empty-{number}").mkdir()

    with pytest.raises(SkillAdmissionError, match="entry count"):
        _resolver(_root("root", root, 0), max_entries=4).resolve(("review",))
    with pytest.raises(SkillAdmissionError, match="directory count"):
        _resolver(_root("root", root, 0), max_directories=3).resolve(("review",))


def test_requires_utf8_skill_and_complete_one_level_local_references(tmp_path: Path):
    """A prompt cannot safely contain an undecodable or silently missing instruction resource."""
    root = tmp_path / "skills"
    skill = _write_skill(root, "review", "[guide](references/guide.md)\n")
    with pytest.raises(SkillAdmissionError, match="missing referenced"):
        _resolver(_root("root", root, 0)).resolve(("review",))
    (skill / "references").mkdir()
    (skill / "references" / "guide.md").write_text("Guide.\n", encoding="utf-8")
    resolved = _resolver(_root("root", root, 0)).resolve(("review",))[0]
    assert resolved.references == (("references/guide.md", resolved.file_hashes["references/guide.md"]),)
    (skill / "references" / "guide.md").write_text("[again](other.md)\n", encoding="utf-8")
    with pytest.raises(SkillAdmissionError, match="one-level"):
        _resolver(_root("root", root, 0)).resolve(("review",))
    (skill / "SKILL.md").write_bytes(b"\xff")
    with pytest.raises(SkillAdmissionError, match="UTF-8"):
        _resolver(_root("root", root, 0)).resolve(("review",))


@pytest.mark.parametrize(("link", "label"), (
    ("[guide][details]", "details"),
    ("[guide][DETAILS]", "details"),
    ("[guide][]", "guide"),
    ("[guide]", "guide"),
))
def test_reference_style_local_resource_must_exist_and_is_indexed(
        tmp_path: Path, link: str, label: str):
    """A defined local Markdown link cannot silently omit its instruction resource."""
    root = tmp_path / "skills"
    skill = _write_skill(root, "review", f"Read {link}.\n\n[{label}]: references/guide.md\n")
    resolver = _resolver(_root("root", root, 0))

    with pytest.raises(SkillAdmissionError, match="missing referenced"):
        resolver.resolve(("review",))

    guide = skill / "references" / "guide.md"
    guide.parent.mkdir()
    guide.write_text("Guide.\n", encoding="utf-8")
    resolved = resolver.resolve(("review",))[0]
    assert resolved.references == (("references/guide.md", hashlib.sha256(b"Guide.\n").hexdigest()),)


def test_reference_style_destination_after_one_line_ending_is_admitted(tmp_path: Path):
    """A CommonMark reference destination may follow the label on the next line."""
    root = tmp_path / "skills"
    skill = _write_skill(root, "review", "Read [guide].\n\n[guide]:\n  references/guide.md\n")
    resolver = _resolver(_root("root", root, 0))

    with pytest.raises(SkillAdmissionError, match="missing referenced"):
        resolver.resolve(("review",))

    guide = skill / "references" / "guide.md"
    guide.parent.mkdir()
    guide.write_text("Guide.\n", encoding="utf-8")
    assert resolver.resolve(("review",))[0].references == (
        ("references/guide.md", hashlib.sha256(b"Guide.\n").hexdigest()),
    )


def test_reference_style_nested_local_resource_exceeds_one_level(tmp_path: Path):
    """A linked resource cannot hide a second local reference behind a definition."""
    root = tmp_path / "skills"
    _write_skill(root, "review", "Read [guide][details].\n\n[details]: references/guide.md\n",
                 references={"references/guide.md": "Read [next][more].\n\n[more]: next.md\n"})

    with pytest.raises(SkillAdmissionError, match="one-level"):
        _resolver(_root("root", root, 0)).resolve(("review",))


def test_reference_style_external_uris_are_not_local_resources(tmp_path: Path):
    """External and fragment destinations do not need staged skill files."""
    root = tmp_path / "skills"
    _write_skill(root, "review", "[web][w] [mail][m] [heading][h]\n\n"
                 "[w]: https://example.test/guide\n[m]: mailto:ops@example.test\n[h]: #part\n")

    assert _resolver(_root("root", root, 0)).resolve(("review",))[0].references == ()


def test_unused_reference_definition_is_not_a_resource(tmp_path: Path):
    """A Markdown definition without a corresponding link is not a dependency."""
    root = tmp_path / "skills"
    _write_skill(root, "review", "# Review\n\n[unused]: references/unused.md\n")

    assert _resolver(_root("root", root, 0)).resolve(("review",))[0].references == ()


@pytest.mark.parametrize(("target", "message"), (
    ("file:///etc/passwd", "file URI"),
    ("../outside.md", "escapes"),
    ("C%3A%5CWindows%5CSystem32", "escapes|encoded"),
))
def test_reference_style_dangerous_destinations_fail_closed(tmp_path: Path, target: str, message: str):
    """Reference definitions use the same path safety rules as inline links."""
    root = tmp_path / "skills"
    _write_skill(root, "review", f"Read [guide][details].\n\n[details]: {target}\n")

    with pytest.raises(SkillAdmissionError, match=message):
        _resolver(_root("root", root, 0)).resolve(("review",))


def test_reference_index_skips_external_uris_but_rejects_unsafe_file_uris(tmp_path: Path):
    """Only direct local Markdown targets belong in the bounded reference graph."""
    root = tmp_path / "skills"
    _write_skill(
        root,
        "review",
        "[web](https://example.test) [mail](mailto:ops@example.test) [phone](tel:+123) [here](#part)\n"
        "[guide](references/guide.md#start)\n",
        references={"references/guide.md": "[web](https://example.test) [mail](mailto:ops@example.test)\n"},
    )
    resolved = _resolver(_root("root", root, 0)).resolve(("review",))[0]
    assert [path for path, _digest in resolved.references] == ["references/guide.md"]

    skill = root / "review"
    (skill / "SKILL.md").write_text("[bad](file:///etc/passwd)\n", encoding="utf-8")
    with pytest.raises(SkillAdmissionError, match="file URI"):
        _resolver(_root("root", root, 0)).resolve(("review",))
    (skill / "SKILL.md").write_text("[bad](../outside.md)\n", encoding="utf-8")
    with pytest.raises(SkillAdmissionError, match="escapes"):
        _resolver(_root("root", root, 0)).resolve(("review",))
    (skill / "SKILL.md").write_text("[guide](references/guide.md)\n", encoding="utf-8")
    (skill / "references" / "guide.md").write_text("[bad](file:guide.md)\n", encoding="utf-8")
    with pytest.raises(SkillAdmissionError, match="file URI"):
        _resolver(_root("root", root, 0)).resolve(("review",))


@pytest.mark.parametrize("target", (
    "C%3A%5CWindows%5CSystem32",
    "%5C%5Cserver%5Cshare%5Cguide.md",
    "..%5Cescape.md",
    "%2Fetc%2Fpasswd",
    "references%2Fguide.md",
    "references%5Cguide.md",
    "%2e%2e/%2e%2e/escape.md",
    "%252e%252e%252fescape.md",
))
def test_reference_paths_revalidate_percent_decoded_portable_grammar(tmp_path: Path, target: str):
    """URI decoding must not turn a harmless-looking target into a platform escape."""
    root = tmp_path / "skills"
    _write_skill(root, "review", f"[bad]({target})\n")

    with pytest.raises(SkillAdmissionError, match="escapes|encoded"):
        _resolver(_root("root", root, 0)).resolve(("review",))


@pytest.mark.parametrize("target", (
    "C:%5CWindows%5CSystem32",
    "C:relative.md",
    "c:relative.md",
    "C%3Arelative.md",
    "c%3A%5CWindows%5CSystem32",
))
def test_reference_paths_reject_drive_syntax_before_uri_scheme_classification(tmp_path: Path, target: str):
    """A drive prefix is a local-path hazard, never an external URI scheme."""
    root = tmp_path / "skills"
    _write_skill(root, "review", f"[bad]({target})\n")

    with pytest.raises(SkillAdmissionError, match="escapes|encoded"):
        _resolver(_root("root", root, 0)).resolve(("review",))


def test_direct_resources_apply_the_same_drive_syntax_classifier(tmp_path: Path):
    """Second-level resource links cannot bypass the SKILL.md drive-prefix check."""
    root = tmp_path / "skills"
    _write_skill(
        root,
        "review",
        "[guide](references/guide.md#safe)\n",
        references={"references/guide.md": "[bad](c:%5CWindows%5CSystem32)\n"},
    )

    with pytest.raises(SkillAdmissionError, match="escapes|encoded"):
        _resolver(_root("root", root, 0)).resolve(("review",))


def test_stage_is_private_read_only_atomic_and_detects_source_drift(tmp_path: Path):
    """A post-resolution edit must be refused instead of staged into a seat bundle."""
    root, bundles = tmp_path / "skills", tmp_path / "bundles"
    bundles.mkdir()
    skill = _write_skill(root, "review", "# Skill\n[guide](references/guide.md)\n", references={"references/guide.md": "Guide.\n"})
    resolver = _resolver(_root("root", root, 0))
    admission = resolver.admit(("review",), task_id="task", seat_id="codex-1", provider="codex", session_id="session")
    staged = resolver.stage(admission, bundles / "seat")

    assert staged.staged_root == bundles / "seat" / "ready"
    assert (staged.staged_root / "manifest.json").is_file()
    assert (staged.staged_root / "review" / "SKILL.md").read_text(encoding="utf-8") == "# Skill\n[guide](references/guide.md)\n"
    assert (staged.staged_root / "review" / "SKILL.md").stat().st_mode & 0o222 == 0
    assert (staged.staged_root / "review").stat().st_mode & 0o077 == 0

    skill.joinpath("SKILL.md").write_text("# changed\n", encoding="utf-8")
    with pytest.raises(SkillAdmissionError, match="drift"):
        resolver.stage(admission, bundles / "changed")
    assert not (bundles / "changed").exists()


def test_stage_reservation_never_overwrites_a_racing_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A kernel-exclusive reservation must preserve a rival bundle created after preflight."""
    root, bundles = tmp_path / "skills", tmp_path / "bundles"
    bundles.mkdir()
    _write_skill(root, "review")
    resolver = _resolver(_root("root", root, 0))
    admission = resolver.admit(("review",), task_id="task", seat_id="seat", provider="codex", session_id="session")
    destination = bundles / "seat"
    original_mkdir = skills_module.os.mkdir

    def rival_first(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if Path(path) == destination:
            original_mkdir(path, mode, dir_fd=dir_fd)
            (destination / "rival").write_text("preserve", encoding="utf-8")
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(skills_module.os, "mkdir", rival_first)
    with pytest.raises(SkillAdmissionError, match="destination"):
        resolver.stage(admission, destination)
    assert (destination / "rival").read_text(encoding="utf-8") == "preserve"


def test_failed_reservation_cleans_its_partial_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A crash-like failure after reservation leaves no ready or reusable partial bundle."""
    root, bundles = tmp_path / "skills", tmp_path / "bundles"
    bundles.mkdir()
    _write_skill(root, "review")
    resolver = _resolver(_root("root", root, 0))
    admission = resolver.admit(("review",), task_id="task", seat_id="seat", provider="codex", session_id="session")

    def crash(*_args: object) -> None:
        raise SkillAdmissionError("simulated crash")

    monkeypatch.setattr(resolver, "_copy_skill", crash)
    with pytest.raises(SkillAdmissionError, match="simulated crash"):
        resolver.stage(admission, bundles / "seat")
    assert not (bundles / "seat").exists()


def test_stage_rechecks_every_shadow_before_copying(tmp_path: Path):
    """A lower-precedence source changing after admission must not bypass shadow comparison."""
    first, second, bundles = tmp_path / "first", tmp_path / "second", tmp_path / "bundles"
    bundles.mkdir()
    _write_skill(first, "review")
    shadow = _write_skill(second, "review")
    resolver = _resolver(_root("first", first, 0), _root("second", second, 1))
    admission = resolver.admit(("review",), task_id="task", seat_id="seat", provider="codex", session_id="session")
    (shadow / "SKILL.md").write_text("# changed\n", encoding="utf-8")

    with pytest.raises(SkillAdmissionError, match="divergent"):
        resolver.stage(admission, bundles / "seat")


def test_stage_detects_drift_while_copying(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A source edit during staging must discard the temporary bundle before delivery."""
    root, bundles = tmp_path / "skills", tmp_path / "bundles"
    bundles.mkdir()
    skill = _write_skill(root, "review")
    resolver = _resolver(_root("root", root, 0))
    admission = resolver.admit(("review",), task_id="task", seat_id="seat", provider="codex", session_id="session")
    original = resolver._copy_skill

    def copy_then_mutate(*args: object) -> None:
        original(*args)  # type: ignore[arg-type]
        (skill / "SKILL.md").write_text("# changed during copy\n", encoding="utf-8")

    monkeypatch.setattr(resolver, "_copy_skill", copy_then_mutate)
    with pytest.raises(SkillAdmissionError, match="drift"):
        resolver.stage(admission, bundles / "seat")
    assert not (bundles / "seat").exists()


def test_plan_effective_skills_only_admit_executor_inheritance_and_none_is_empty(tmp_path: Path):
    """A seat inherits Task 5 executor skills only, never orchestrator controls or defaults."""
    root = tmp_path / "skills"
    _write_skill(root, "review")
    for name in ("brainstorming", "finishing-a-development-branch", "requesting-code-review",
                 "llm-fanout-plan", "llm-fanout-execute"):
        _write_skill(root, name)
    marketplace = tmp_path / "marketplace"
    for name in ("llm-council", "llm-forge"):
        _write_skill(marketplace, name)
    plan = FanoutPlanV1(
        source=SourceInfoV1("plan.md", "0" * 64, "test"),
        source_steps=(SourceStepV1("Task 1/Step 1", "1" * 64),),
        tasks=(
            PlanTaskV1("group", "group", "Group", "Group.",
                       required_skills=("brainstorming", "finishing-a-development-branch",
                                        "llm-council", "llm-fanout-plan", "review")),
            PlanTaskV1("work", "work", "Work", "Work.", parent_id="group", source_step_ids=("Task 1/Step 1",),
                       execution_class="read-only", required_skills=("requesting-code-review", "llm-forge",
                                                                   "llm-fanout-execute"),
                       acceptance=("Done.",)),
        ),
    )
    resolver = _resolver(_root("root", root, 0), _root("marketplace", marketplace, 1))
    assert [skill.name for skill in resolver.admit_plan(plan, "work", seat_id="s", provider="codex", session_id="x").skills] == ["review"]

    none_plan = FanoutPlanV1(
        source=SourceInfoV1("plan.md", "0" * 64, "test"),
        source_steps=(SourceStepV1("Task 1/Step 1", "1" * 64),),
        tasks=(PlanTaskV1("work", "work", "Work", "Work.", source_step_ids=("Task 1/Step 1",),
                            execution_class="read-only", none_reason="Reviewed: no skill needed.", acceptance=("Done.",)),),
    )
    assert resolver.admit_plan(none_plan, "work", seat_id="s", provider="codex", session_id="x").skills == ()


def test_engine_evidence_is_authoritative_native_is_additional_and_model_ack_is_telemetry(tmp_path: Path):
    """A model sentence cannot substitute for exact engine delivery of reviewed bytes."""
    root, bundles = tmp_path / "skills", tmp_path / "bundles"
    bundles.mkdir()
    _write_skill(root, "review")
    resolver = _resolver(_root("root", root, 0))
    admission = resolver.admit(("review",), task_id="task", seat_id="seat", provider="codex", session_id="session")
    staged = resolver.stage(admission, bundles / "seat")
    skill = staged.skills[0]
    engine = SkillLoadEvidence.engine(skill, staged)
    native = SkillLoadEvidence.native(skill, staged)
    model = SkillLoadEvidence.model_acknowledgement("I loaded review")

    assert resolver.verify_engine_delivery(staged, (engine,)).engine_delivered
    assert resolver.verify_native_events(staged, (native,)) == (native,)
    assert resolver.verify_model_acknowledgements(staged, (model,)) == (model,)
    with pytest.raises(SkillAdmissionError, match="engine"):
        resolver.verify_engine_delivery(staged, (model,))
    duplicate = (engine, engine)
    with pytest.raises(SkillAdmissionError, match="duplicate"):
        resolver.verify_engine_delivery(staged, duplicate)
    wrong_session = SkillLoadEvidence.engine(skill, staged, session_id="other")
    with pytest.raises(SkillAdmissionError, match="session"):
        resolver.verify_engine_delivery(staged, (wrong_session,))
    (staged.staged_root / "review" / "SKILL.md").chmod(0o600)
    with pytest.raises(SkillAdmissionError, match="staged"):
        resolver.verify_engine_delivery(staged, (engine,))


def test_engine_delivery_rejects_extra_or_missing_staged_skills(tmp_path: Path):
    """A seat bundle must contain exactly the admitted skills, not a permissive root."""
    root, bundles = tmp_path / "skills", tmp_path / "bundles"
    bundles.mkdir()
    _write_skill(root, "review")
    resolver = _resolver(_root("root", root, 0))
    staged = resolver.stage(
        resolver.admit(("review",), task_id="task", seat_id="seat", provider="codex", session_id="session"),
        bundles / "seat",
    )
    (staged.staged_root / "unrelated").mkdir()
    with pytest.raises(SkillAdmissionError, match="unrelated"):
        resolver.verify_engine_delivery(staged, (SkillLoadEvidence.engine(staged.skills[0], staged),))
