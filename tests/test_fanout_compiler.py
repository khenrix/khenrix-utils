"""Strict compilation tests for approved Superpowers implementation plans."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
REAL_PLAN = ROOT / "docs" / "superpowers" / "plans" / "2026-07-30-per-provider-eval-gating.md"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_compiler_contracts", FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)


CompilerError = fanout.CompilerError
SkillResolver = fanout.SkillResolver
SkillRoot = fanout.SkillRoot
compile_superpowers_plan = fanout.compile_superpowers_plan
parse_superpowers_plan = fanout.parse_superpowers_plan
compiler_module = sys.modules[f"{SPEC.name}.compiler"]


def _v2_source() -> bytes:
    return _source().replace(
        b"### Task 1: Inspect input\n", b"### Task 1: Inspect input\n\n**Target:** address\n", 1,
    ).replace(
        b"### Task 2: Implement output\n", b"### Task 2: Implement output\n\n**Target:** booking\n", 1,
    )


def test_v2_source_requires_exactly_one_target_on_every_task():
    source = _v2_source().replace(b"**Target:** address\n", b"", 1)

    with pytest.raises(CompilerError, match="Target"):
        compiler_module.parse_superpowers_plan_v2(source, source_path="docs/plan.md")


def test_v2_source_binds_each_step_to_its_declared_target():
    parsed = compiler_module.parse_superpowers_plan_v2(_v2_source(), source_path="docs/plan.md")

    assert dict(parsed.task_targets) == {"Task 1": "address", "Task 2": "booking"}
    assert dict(parsed.step_targets) == {
        "Task 1/Step 1": "address", "Task 1/Step 2": "address",
        "Task 2/Step 1": "booking",
    }
    assert parsed.steps[-1].target_id == "booking"


def test_v1_parser_refuses_v2_target_metadata():
    with pytest.raises(CompilerError, match="Target"):
        parse_superpowers_plan(_v2_source(), source_path="docs/plan.md")


def test_v2_cross_task_dependency_requires_exact_mode():
    source = _v2_source().replace(
        b"- [ ] **Step 1: Add bounded implementation**\n  **Depends on:** none\n",
        b"- [ ] **Step 1: Add bounded implementation**\n"
        b"  **Depends on:** Task 1/Step 2\n"
        b"  **Dependency modes:** Task 1/Step 2=artifact\n", 1,
    )
    parsed = compiler_module.parse_superpowers_plan_v2(source, source_path="docs/plan.md")
    assert parsed.steps[-1].dependency_modes == (("Task 1/Step 2", "artifact"),)

    for malformed in (
        source.replace(b"  **Dependency modes:** Task 1/Step 2=artifact\n", b"", 1),
        source.replace(b"=artifact\n", b"=unknown\n", 1),
        source.replace(b"=artifact\n", b"=artifact, Task 1/Step 2=artifact\n", 1),
    ):
        with pytest.raises(CompilerError, match="Dependency modes"):
            compiler_module.parse_superpowers_plan_v2(malformed, source_path="docs/plan.md")


def test_v1_parser_refuses_v2_dependency_modes_metadata():
    source = _source().replace(
        b"  **Depends on:** none\n", b"  **Depends on:** none\n  **Dependency modes:** Task 1/Step 1=artifact\n", 1,
    )
    with pytest.raises(CompilerError, match="Dependency modes"):
        parse_superpowers_plan(source, source_path="docs/plan.md")


def test_v1_parser_refuses_target_metadata_before_task_sections():
    source = _source().replace(b"## Global Constraints\n", b"**Target:** address\n\n## Global Constraints\n", 1)

    with pytest.raises(CompilerError, match="Target"):
        parse_superpowers_plan(source, source_path="docs/plan.md")


@pytest.mark.parametrize("metadata", [
    "**Target:** address",
    "  **Dependency modes:** Task 1/Step 1=artifact",
])
def test_v1_parser_refuses_v2_metadata_in_document_notes(metadata):
    source = _source() + f"## Notes\n\nOrdinary review note.\n\n{metadata}\n".encode()

    with pytest.raises(CompilerError, match="Target|Dependency modes"):
        parse_superpowers_plan(source, source_path="docs/plan.md")


def test_v1_parser_keeps_ordinary_notes_and_fenced_metadata_examples():
    source = (_source() + b"## Notes\n\nOrdinary review note.\n\n```markdown\n"
              b"**Target:** address\n  **Dependency modes:** Task 1/Step 1=artifact\n```\n")

    parsed = parse_superpowers_plan(source, source_path="docs/plan.md")

    assert len(parsed.steps) == 3


def test_v2_source_file_path_cannot_escape_its_target():
    source = _v2_source().replace(
        b"- Create: `shared/example.py`", b"- Create: `../booking/shared/example.py`", 1,
    )

    with pytest.raises(CompilerError, match="unsafe path"):
        compiler_module.parse_superpowers_plan_v2(source, source_path="docs/plan.md")


def _source(*, newline: str = "\n") -> bytes:
    return newline.join([
        "# Example Implementation Plan",
        "",
        "> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.",
        "",
        "**Goal:** Build the bounded example.",
        "",
        "**Architecture:** Two independent source tasks.",
        "",
        "**Tech Stack:** Python 3.11+ stdlib only.",
        "",
        "**Spec:** `docs/spec.md`",
        "",
        "## Global Constraints",
        "",
        "- Preserve byte-bound source evidence.",
        "- Do not invent work.",
        "",
        "## Scope",
        "",
        "This scope prose is a named document section, not task work.",
        "",
        "### Task 1: Inspect input",
        "",
        "**Files:**",
        "- Test: `tests/test_example.py`",
        "",
        "- [ ] **Step 1: Read fixture**",
        "  **Depends on:** none",
        "",
        "Read the checked-in fixture without modifying it.",
        "",
        "- [ ] **Step 2: Record finding**",
        "  **Depends on:** none",
        "",
        "Record the exact finding in the report.",
        "",
        "### Task 2: Implement output",
        "",
        "**Files:**",
        "- Create: `shared/example.py`",
        "",
        "- [ ] **Step 1: Add bounded implementation**",
        "  **Depends on:** none",
        "",
        "Implement only the checked behavior.",
        "",
    ]).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _draft(source: bytes, tasks: list[dict[str, object]] | None = None) -> bytes:
    if tasks is None:
        tasks = [
            {
                "id": "inspect", "kind": "work", "parent_id": None,
                "title": "Inspect", "objective": "Read and record the fixture.",
                "source_step_ids": ["Task 1/Step 1", "Task 1/Step 2"],
                "depends_on": [], "execution_class": "read-only",
                "required_skills": ["review"], "none_reason": None,
                "owned_paths": ["reports/fixture.md"],
                "acceptance": ["The fixture finding is recorded."],
                "checks": [{"argv": ["python", "-m", "pytest", "-q"], "cwd": "",
                            "env_allowlist": [], "timeout": 30,
                            "accepted_exit_codes": [0], "expected_artifacts": []}],
                "provider_policy": None,
            },
            {
                "id": "implement", "kind": "work", "parent_id": None,
                "title": "Implement", "objective": "Make the bounded change.",
                "source_step_ids": ["Task 2/Step 1"], "depends_on": [],
                "execution_class": "repo-write", "required_skills": [],
                "none_reason": "No executor skill is required for this bounded task.",
                "owned_paths": ["shared/example.py"],
                "acceptance": ["The checked behavior is implemented."],
                "checks": [{"argv": ["python", "-m", "pytest", "-q", "tests/test_example.py"],
                            "cwd": "", "env_allowlist": [], "timeout": 30,
                            "accepted_exit_codes": [0], "expected_artifacts": []}],
                "provider_policy": None,
            },
        ]
    return json.dumps({
        "schema_version": "fanout-draft-v1", "source_sha256": _sha(source),
        "defaults": {"executor_ids": ["claude", "codex", "agy"], "rounds": 2,
                     "timeout": 120, "retries": 0, "minimum_success": 2},
        "tasks": tasks,
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _resolver(tmp_path: Path) -> SkillResolver:
    root = tmp_path / "skills"
    skill = root / "review"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
    return SkillResolver((SkillRoot("test", root, 0, provider="codex", target="test"),))


def _v2_draft(source: bytes) -> bytes:
    draft = json.loads(_draft(source))
    draft["schema_version"] = "fanout-draft-v2"
    draft["targets"] = [
        {"id": "address", "repository": "github.com/example/address-service",
         "ticket_key": "TASK-123", "branch_ref": "refs/heads/feat/TASK-123-address"},
        {"id": "booking", "repository": "github.com/example/booking-service",
         "ticket_key": "TASK-123", "branch_ref": "refs/heads/feat/TASK-123-booking"},
    ]
    for task in draft["tasks"]:
        task["target_id"] = "address" if task["id"] == "inspect" else "booking"
        task["dependency_modes"] = {dependency: "artifact" for dependency in task["depends_on"]}
    return fanout.canonical_json(draft)


def test_v2_draft_cannot_retarget_approved_source(tmp_path: Path):
    source = _v2_source()
    draft = json.loads(_v2_draft(source))
    draft["tasks"][0]["target_id"] = "booking"
    with pytest.raises(CompilerError, match="target"):
        compiler_module.compile_superpowers_plan_v2(
            source, source_path="docs/plan.md", draft_bytes=fanout.canonical_json(draft),
            resolver=_resolver(tmp_path),
        )


def test_v2_compiled_bytes_bind_targets_and_source_steps(tmp_path: Path):
    source = _v2_source()
    resolver = _resolver(tmp_path)
    compiled = compiler_module.compile_superpowers_plan_v2(
        source, source_path="docs/plan.md", draft_bytes=_v2_draft(source), resolver=resolver,
    )
    document = json.loads(compiled.to_bytes())
    assert document["schema_version"] == "compiled-fanout-plan-v2"
    assert document["parser_version"] == compiler_module.PARSER_VERSION_V2
    assert document["plan"]["source"]["parser_version"] == compiler_module.PARSER_VERSION_V2
    assert document["plan"]["targets"][0]["repository"] == "github.com/example/address-service"
    assert document["plan"]["source_steps"][0]["target_id"] == "address"
    assert document["plan"]["tasks"][1]["target_id"] == "booking"
    assert document["resolved_skill_manifests"]["inspect"][0]["name"] == "review"
    changed = json.loads(_v2_draft(source))
    changed["targets"][0]["branch_ref"] = "refs/heads/feat/TASK-123-other"
    assert compiler_module.compile_superpowers_plan_v2(
        source, source_path="docs/plan.md", draft_bytes=fanout.canonical_json(changed),
        resolver=resolver,
    ).to_bytes() != compiled.to_bytes()


def test_v2_subtask_skill_closure_stays_on_declared_target(tmp_path: Path):
    source = _v2_source()
    draft = json.loads(_v2_draft(source))
    skill = tmp_path / "skills" / "test-driven-development"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Test-driven development\n", encoding="utf-8")
    draft["tasks"].insert(0, {
        "id": "booking-parent", "kind": "group", "parent_id": None,
        "title": "Booking", "objective": "Coordinate booking tasks.",
        "required_skills": ["test-driven-development"],
    })
    draft["tasks"][2]["parent_id"] = "booking-parent"
    draft["tasks"][2]["none_reason"] = None
    compiled = compiler_module.compile_superpowers_plan_v2(
        source, source_path="docs/plan.md", draft_bytes=fanout.canonical_json(draft),
        resolver=_resolver(tmp_path),
    )
    assert "test-driven-development" in fanout.effective_skills(compiled.plan, "implement")
    assert next(task for task in compiled.plan.tasks if task.id == "implement").target_id == "booking"


def test_v2_draft_dependency_mode_must_match_source(tmp_path: Path):
    source = _v2_source().replace(
        b"- [ ] **Step 1: Add bounded implementation**\n  **Depends on:** none\n",
        b"- [ ] **Step 1: Add bounded implementation**\n"
        b"  **Depends on:** Task 1/Step 2\n"
        b"  **Dependency modes:** Task 1/Step 2=handover\n", 1,
    )
    draft = json.loads(_v2_draft(source))
    draft["tasks"][1]["depends_on"] = ["inspect"]
    draft["tasks"][1]["dependency_modes"] = {"inspect": "artifact"}
    with pytest.raises(CompilerError, match="dependency mode"):
        compiler_module.compile_superpowers_plan_v2(
            source, source_path="docs/plan.md", draft_bytes=fanout.canonical_json(draft),
            resolver=_resolver(tmp_path),
        )


def _compile(tmp_path: Path, source: bytes | None = None, draft: bytes | None = None):
    source = _source() if source is None else source
    return compile_superpowers_plan(
        source, source_path="docs/superpowers/plans/example.md",
        draft_bytes=_draft(source) if draft is None else draft, resolver=_resolver(tmp_path),
    )


def _read_only_source(*, quality_tier: str | None = None) -> bytes:
    source = _source().split(b"### Task 2:", 1)[0]
    if quality_tier is not None:
        source = source.replace(b"- Do not invent work.",
                                f"- Do not invent work.\n- Quality tier: {quality_tier}.".encode())
    return source


def _read_only_draft(source: bytes, *, quality_tier: str = "standard") -> bytes:
    draft = json.loads(_draft(source))
    draft["tasks"] = draft["tasks"][:1]
    draft["defaults"]["quality_tier"] = quality_tier
    draft["defaults"]["timeout"] = None
    return json.dumps(draft).encode()


def test_compiler_accepts_source_declared_deep_read_only_policy(tmp_path: Path):
    """Rejecting deep at the compiler would discard the profile-backed tier."""
    source = _read_only_source(quality_tier="deep")

    compiled = _compile(tmp_path, source, _read_only_draft(source, quality_tier="deep"))

    assert compiled.plan.defaults.quality_tier == "deep"
    assert compiled.plan.defaults.timeout is None
    assert [task.execution_class for task in compiled.plan.tasks] == ["read-only"]


def test_compiler_refuses_source_deep_downgraded_to_standard(tmp_path: Path):
    """The approved source tier must survive the deterministic draft mapping."""
    source = _read_only_source(quality_tier="deep")

    with pytest.raises(CompilerError, match="quality_tier"):
        _compile(tmp_path, source, _read_only_draft(source))


def test_compiler_refuses_source_normal_upgraded_to_deep(tmp_path: Path):
    """A draft may not silently raise the global tier above the approved source."""
    source = _read_only_source(quality_tier="normal")

    with pytest.raises(CompilerError, match="quality_tier"):
        _compile(tmp_path, source, _read_only_draft(source, quality_tier="deep"))


@pytest.mark.parametrize("constraint", [
    b"- Provider policy:\n  Quality tier: deep.",
    b"- Provider policy: quality_tier=deep.",
    b"- Quality tier deep.",
    b"- Quality-tier: deep.",
])
def test_compiler_refuses_hidden_or_malformed_source_tier_hint(
        tmp_path: Path, constraint: bytes):
    """A noncanonical tier hint must not compile through the standard default."""
    source = _read_only_source().replace(b"- Do not invent work.", constraint)

    with pytest.raises(CompilerError, match="quality_tier"):
        _compile(tmp_path, source, _read_only_draft(source))


def test_compiler_refuses_mixed_classes_under_source_deep(tmp_path: Path):
    """A standard write override cannot hide inside a top-level deep request."""
    source = _source().replace(b"- Do not invent work.",
                               b"- Do not invent work.\n- Quality tier: deep.")
    draft = json.loads(_draft(source))
    draft["defaults"]["quality_tier"] = "deep"
    draft["defaults"]["timeout"] = None
    draft["tasks"][1]["provider_policy"] = {
        **draft["defaults"], "quality_tier": "standard", "timeout": 120,
    }

    with pytest.raises(CompilerError, match="read-only"):
        _compile(tmp_path, source, json.dumps(draft).encode())


def test_compiler_preserves_task_local_deep_read_only_in_standard_plan(tmp_path: Path):
    """A source normal tier binds defaults while read-only tasks may opt into deep."""
    source = _source().replace(b"- Do not invent work.",
                               b"- Do not invent work.\n- Quality tier: normal.")
    draft = json.loads(_draft(source))
    draft["tasks"][0]["provider_policy"] = {
        **draft["defaults"], "quality_tier": "deep", "timeout": None,
    }

    compiled = _compile(tmp_path, source, json.dumps(draft).encode())

    assert compiled.plan.defaults.quality_tier == "standard"
    assert compiled.plan.tasks[0].provider_policy.quality_tier == "deep"
    assert compiled.plan.tasks[1].provider_policy is None


def test_parser_normalizes_crlf_step_digests_but_binds_the_exact_source_bytes():
    """Changing transport newlines must not hide a source-byte revision."""
    lf, crlf = _source(), _source(newline="\r\n")

    parsed_lf = parse_superpowers_plan(lf, source_path="docs/superpowers/plans/example.md")
    parsed_crlf = parse_superpowers_plan(crlf, source_path="docs/superpowers/plans/example.md")

    assert [step.id for step in parsed_lf.steps] == ["Task 1/Step 1", "Task 1/Step 2", "Task 2/Step 1"]
    assert [step.sha256 for step in parsed_lf.steps] == [step.sha256 for step in parsed_crlf.steps]
    assert parsed_lf.source_sha256 == _sha(lf)
    assert parsed_crlf.source_sha256 == _sha(crlf)
    assert parsed_lf.global_constraints_sha256 == parsed_crlf.global_constraints_sha256
    assert parsed_lf.required_order == ()


def test_parser_accepts_markdown_line_break_spaces_after_step_heading():
    source = _source().replace(
        b"- [ ] **Step 1: Read fixture**\n",
        b"- [ ] **Step 1: Read fixture**  \n",
        1,
    )

    parsed = parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")

    assert parsed.steps[0].id == "Task 1/Step 1"
    assert parsed.steps[0].depends_on == ()


@pytest.mark.parametrize("replace", [
    ("### Task 2:", "### Task 3:"),
    ("- [ ] **Step 2:", "- [ ] **Step 3:"),
    ("- [ ] **Step 1: Read fixture**", "- [x] **Step 1: Read fixture**"),
    ("Read the checked-in fixture without modifying it.", ""),
    ("## Scope", "Free-floating prose"),
])
def test_parser_rejects_ambiguous_or_noncanonical_source_constructs(replace):
    """Guessing IDs, checked steps, empty work, or loose prose loses auditability."""
    before, after = replace
    malformed = _source().decode("utf-8").replace(before, after, 1).encode("utf-8")

    with pytest.raises(CompilerError):
        parse_superpowers_plan(malformed, source_path="docs/superpowers/plans/example.md")


def test_parser_requires_the_evidenced_task_owned_files_section():
    """A loose Task body could make an otherwise unowned source sentence executable."""
    malformed = _source().replace(b"**Files:**\n- Test: `tests/test_example.py`\n\n", b"Task notes remain.\n\n")

    with pytest.raises(CompilerError, match="Files"):
        parse_superpowers_plan(malformed, source_path="docs/superpowers/plans/example.md")


def test_parser_rejects_unowned_plain_action_list_inside_files_region():
    """A Files section may enumerate paths, not smuggle an executable action before Step 1."""
    malformed = _source().replace(b"- Test: `tests/test_example.py`", b"- Perform untracked action")

    with pytest.raises(CompilerError, match="actionable"):
        parse_superpowers_plan(malformed, source_path="docs/superpowers/plans/example.md")


def test_compiler_partitions_whole_steps_preserves_hashes_and_resolves_effective_skills(tmp_path: Path):
    """A compiled packet must bind its exact input and admitted executor skill tree."""
    source = _source()
    compiled = _compile(tmp_path, source)

    assert compiled.plan.source.sha256 == _sha(source)
    assert compiled.plan.source.path == "docs/superpowers/plans/example.md"
    assert [step.id for step in compiled.plan.source_steps] == ["Task 1/Step 1", "Task 1/Step 2", "Task 2/Step 1"]
    assert compiled.parser_version
    assert compiled.compiler_version
    assert compiled.global_constraints == ("Preserve byte-bound source evidence.", "Do not invent work.")
    assert compiled.global_constraints_sha256
    assert compiled.draft_sha256 == _sha(_draft(source))
    assert compiled.resolved_skills_sha256
    assert compiled.resolved_skill_manifests["inspect"][0]["name"] == "review"
    assert compiled.resolved_skill_manifests["implement"] == ()
    assert compiled.plan.tasks[1].depends_on == ()


def test_source_write_intent_must_match_repo_write_class_and_owned_paths(tmp_path: Path):
    """A source-approved edit cannot be laundered into an unreviewed read-only draft."""
    source = _source()
    downgraded = json.loads(_draft(source))
    downgraded["tasks"][1]["execution_class"] = "read-only"
    with pytest.raises(CompilerError, match="source write intent"):
        _compile(tmp_path, source, json.dumps(downgraded).encode())

    mismatched = json.loads(_draft(source))
    mismatched["tasks"][1]["owned_paths"] = ["shared/other.py"]
    with pytest.raises(CompilerError, match="source write intent"):
        _compile(tmp_path, source, json.dumps(mismatched).encode())

    extra = json.loads(_draft(source))
    extra["tasks"][1]["owned_paths"].append("shared/extra.py")
    with pytest.raises(CompilerError, match="source write intent"):
        _compile(tmp_path, source, json.dumps(extra).encode())


def test_inspection_step_joined_to_another_writer_does_not_split_source_write_ownership(tmp_path: Path):
    source = _source().replace(
        b"- Test: `tests/test_example.py`",
        b"- Create: `src/first.py`\n- Test: `tests/test_example.py`",
        1,
    )
    draft = json.loads(_draft(source))
    draft["tasks"][0]["source_step_ids"] = ["Task 1/Step 2"]
    draft["tasks"][0]["execution_class"] = "repo-write"
    draft["tasks"][0]["owned_paths"] = ["src/first.py"]
    draft["tasks"][1]["source_step_ids"] = ["Task 1/Step 1", "Task 2/Step 1"]

    compiled = _compile(tmp_path, source, json.dumps(draft).encode())

    assert compiled.plan.tasks[0].owned_paths == ("src/first.py",)
    assert compiled.plan.tasks[1].owned_paths == ("shared/example.py",)


def test_modify_line_range_maps_to_file_and_test_file_is_optional_ownership(tmp_path: Path):
    source = _source().replace(
        b"- Create: `shared/example.py`",
        b"- Modify: `shared/example.py:123-145`\n- Test: `tests/test_example.py`",
    )
    draft = json.loads(_draft(source))
    assert _compile(tmp_path, source, json.dumps(draft).encode()).plan.tasks[1].owned_paths == (
        "shared/example.py",)

    draft["tasks"][1]["owned_paths"].append("tests/test_example.py")
    assert _compile(tmp_path, source, json.dumps(draft).encode()).plan.tasks[1].owned_paths == (
        "shared/example.py", "tests/test_example.py")

    draft["tasks"][1]["owned_paths"] = ["shared/example.py:123-145"]
    with pytest.raises(CompilerError, match="source write intent"):
        _compile(tmp_path, source, json.dumps(draft).encode())


def test_modify_strips_only_numeric_line_ranges_not_other_colons(tmp_path: Path):
    source = _source().replace(b"- Create: `shared/example.py`",
                               b"- Modify: `shared/example:v2.py`")
    draft = json.loads(_draft(source))
    draft["tasks"][1]["owned_paths"] = ["shared/example:v2.py"]
    assert _compile(tmp_path, source, json.dumps(draft).encode()).plan.tasks[1].owned_paths == (
        "shared/example:v2.py",)
    draft["tasks"][1]["owned_paths"] = ["shared/example"]
    with pytest.raises(CompilerError, match="source write intent"):
        _compile(tmp_path, source, json.dumps(draft).encode())


def test_compilation_is_canonical_and_source_or_draft_change_is_not_reusable(tmp_path: Path):
    """A cached draft cannot silently target a revised source document."""
    source = _source()
    one, two = _compile(tmp_path, source), _compile(tmp_path, source)
    assert one.to_bytes() == two.to_bytes()

    revised = source.replace(b"Do not invent work.", b"Do not infer work.")
    with pytest.raises(CompilerError, match="source_sha256"):
        _compile(tmp_path, revised, _draft(source))

    changed = json.loads(_draft(source))
    changed["tasks"][0]["title"] = "A distinct mapping"
    three = _compile(tmp_path, source, json.dumps(changed, sort_keys=True).encode())
    assert one.draft_mapping_sha256 != three.draft_mapping_sha256


def test_draft_is_strict_and_requires_an_exact_atomic_partition(tmp_path: Path):
    """Unknown JSON and split, omitted, or duplicate source evidence cannot schedule work."""
    source = _source()
    unknown = json.loads(_draft(source))
    unknown["tasks"][0]["unknown"] = True
    duplicate = json.loads(_draft(source))
    duplicate["tasks"][1]["source_step_ids"] = ["Task 1/Step 2", "Task 2/Step 1"]
    omitted = json.loads(_draft(source))
    omitted["tasks"][0]["source_step_ids"] = ["Task 1/Step 1"]
    duplicate_key = b'{"schema_version":"fanout-draft-v1","schema_version":"fanout-draft-v1"}'

    for candidate in (unknown, duplicate, omitted):
        with pytest.raises(CompilerError):
            _compile(tmp_path, source, json.dumps(candidate).encode())
    with pytest.raises(CompilerError, match="duplicate JSON key"):
        _compile(tmp_path, source, duplicate_key)


def test_grouping_is_allowed_for_independent_source_steps(tmp_path: Path):
    """Structural grouping must not manufacture an edge between independent source work."""
    source = _source()
    grouped = json.loads(_draft(source))
    grouped["tasks"].insert(0, {
        "id": "phase", "kind": "group", "parent_id": None, "title": "Phase",
        "objective": "Organize source work.", "required_skills": [],
    })
    grouped["tasks"][1]["parent_id"] = "phase"
    grouped["tasks"][2]["parent_id"] = "phase"
    assert _compile(tmp_path, source, json.dumps(grouped).encode()).plan.tasks[0].kind == "group"

    assert _compile(tmp_path, source).plan.tasks[1].depends_on == ()


def test_explicit_source_edge_requires_its_exact_quotient_in_the_draft(tmp_path: Path):
    """Only an authored Depends on declaration can make independently listed work ordered."""
    source = _source().replace(
        b"- [ ] **Step 1: Add bounded implementation**\n  **Depends on:** none",
        b"- [ ] **Step 1: Add bounded implementation**\n  **Depends on:** Task 1/Step 2",
    )
    draft = json.loads(_draft(source))
    draft["tasks"][1]["depends_on"] = ["inspect"]
    expected = _compile(tmp_path, source, json.dumps(draft).encode())
    assert expected.plan.tasks[1].depends_on == ("inspect",)
    assert parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md").required_order == (
        ("Task 1/Step 2", "Task 2/Step 1"),
    )

    removed = json.loads(json.dumps(draft))
    removed["tasks"][1]["depends_on"] = []
    invented = json.loads(_draft(_source()))
    invented["tasks"][0]["depends_on"] = ["implement"]
    with pytest.raises(CompilerError, match="source dependency"):
        _compile(tmp_path, source, json.dumps(removed).encode())
    with pytest.raises(CompilerError, match="source dependency|cycle"):
        _compile(tmp_path, _source(), json.dumps(invented).encode())


def test_compilation_refuses_legacy_steps_without_source_dependency_metadata(tmp_path: Path):
    """Omitting Depends on leaves concurrency ambiguous, so compiler input must fail closed."""
    source = _source().replace(b"  **Depends on:** none\n", b"", 1)

    parsed = parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")
    assert parsed.steps[0].depends_on is None
    with pytest.raises(CompilerError, match="Task 1/Step 1.*Depends on"):
        _compile(tmp_path, source)


@pytest.mark.parametrize("source", [
    _source().replace(
        b"  **Depends on:** none\n\nRead the checked-in fixture",
        b"  **Depends on:** none\n  **Depends on:** Task 1/Step 1\n\nRead the checked-in fixture",
    ),
    _source().replace(b"  **Depends on:** none", b" **Depends on:** none", 1),
    _source().replace(
        b"- [ ] **Step 1: Read fixture**",
        b"  **Depends on:** none\n- [ ] **Step 1: Read fixture**",
        1,
    ),
    _source() + b"  **Depends on:** none\n",
])
def test_parser_rejects_every_visible_noncanonical_dependency_declaration(source: bytes):
    """Only one immediate, correctly indented Depends on line may govern a Step."""
    with pytest.raises(CompilerError, match="Depends on"):
        parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")


def test_parser_keeps_fenced_dependency_example_inert_but_hash_bound():
    """A fenced dependency spelling is Step evidence, never metadata, and changes its digest."""
    source = _source().replace(
        b"Read the checked-in fixture without modifying it.",
        b"Read the checked-in fixture without modifying it.\n\n```md\n  **Depends on:** Task 1/Step 1\n```",
    )
    baseline = parse_superpowers_plan(_source(), source_path="docs/superpowers/plans/example.md")
    parsed = parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")

    assert parsed.steps[0].depends_on == ()
    assert parsed.steps[0].sha256 != baseline.steps[0].sha256


@pytest.mark.parametrize("opener, closer", [
    (b"```md", b"```"),
    (b"  ~~~markdown", b"  ~~~"),
])
def test_fenced_task_and_document_headings_remain_step_bytes_not_boundaries(opener: bytes, closer: bytes):
    """Fence state governs global Task discovery and document-section splitting alike."""
    source = _source().replace(
        b"Read the checked-in fixture without modifying it.",
        b"Read the checked-in fixture without modifying it.\n\n" + opener
        + b"\n### Task 99: Example only\n## Example document heading\n" + closer,
    ) + b"## Final verification\nDocument review notes only.\n"
    baseline = parse_superpowers_plan(_source(), source_path="docs/superpowers/plans/example.md")
    parsed = parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")

    assert [step.id for step in parsed.steps] == ["Task 1/Step 1", "Task 1/Step 2", "Task 2/Step 1"]
    assert parsed.steps[0].sha256 != baseline.steps[0].sha256


@pytest.mark.parametrize("opener, shorter, opposite, decorated, closer", [
    (b"````markdown", b"```", b"~~~", b"```` not a closer", b"`````   "),
    (b"~~~~markdown", b"~~~", b"```", b"~~~~ not a closer", b"~~~~~\t"),
])
def test_fences_close_only_with_a_matching_delimiter_at_least_as_long_as_the_opener(
        opener: bytes, shorter: bytes, opposite: bytes, decorated: bytes, closer: bytes):
    """Short, opposite, and decorated runs remain hash-bound fence content."""
    source = _source().replace(
        b"Read the checked-in fixture without modifying it.",
        b"Read the checked-in fixture without modifying it.\n\n" + opener
        + b"\n" + shorter
        + b"\n### Task 99: Example only\n## Example document heading\n" + opposite
        + b"\n" + decorated
        + b"\n" + closer,
    )
    baseline = parse_superpowers_plan(_source(), source_path="docs/superpowers/plans/example.md")
    parsed = parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")

    assert [step.id for step in parsed.steps] == ["Task 1/Step 1", "Task 1/Step 2", "Task 2/Step 1"]
    assert parsed.steps[0].sha256 != baseline.steps[0].sha256


def test_legacy_inspection_mode_is_explicit_and_inert_dependency_cues_cannot_select_it():
    """A fenced cue never weakens strict parsing or promotes a legacy source to compilation."""
    assert hasattr(fanout, "PlanParseMode")
    parse_mode = fanout.PlanParseMode
    legacy = REAL_PLAN.read_bytes()
    with_cue = legacy.replace(
        b"    # error attribution: the judge is a SHARED instrument (Task 1)",
        b"    # error attribution: the judge is a SHARED instrument (Task 1)\n  **Depends on:** none",
        1,
    )
    source_path = str(REAL_PLAN.relative_to(ROOT))

    for source in (legacy, with_cue):
        with pytest.raises(CompilerError, match="malformed or skipped Step 7|actionable content outside"):
            parse_superpowers_plan(source, source_path=source_path)
        with pytest.raises(CompilerError, match="malformed or skipped Step 7|actionable content outside"):
            parse_superpowers_plan(source, source_path=source_path,
                                   mode=parse_mode.FANOUT_STRICT)

    inspected = [parse_superpowers_plan(source, source_path=source_path,
                                        mode=parse_mode.LEGACY_INSPECTION)
                 for source in (legacy, with_cue)]
    assert all(parsed.mode is parse_mode.LEGACY_INSPECTION for parsed in inspected)
    assert all(len(parsed.steps) == 67 for parsed in inspected)
    assert [[step.id for step in parsed.steps] for parsed in inspected] == [
        [step.id for step in inspected[0].steps],
        [step.id for step in inspected[0].steps],
    ]
    assert all(step.depends_on is None for step in inspected[0].steps)
    assert inspected[0].parser_version == inspected[1].parser_version
    assert inspected[0].source_sha256 != inspected[1].source_sha256
    assert inspected[0].source_binding_sha256 != inspected[1].source_binding_sha256

    strict = parse_superpowers_plan(_source(), source_path="docs/superpowers/plans/example.md")
    legacy_mode = parse_superpowers_plan(_source(), source_path="docs/superpowers/plans/example.md",
                                         mode=parse_mode.LEGACY_INSPECTION)
    assert strict.mode is parse_mode.FANOUT_STRICT
    assert strict.parser_version != legacy_mode.parser_version
    assert strict.source_binding_sha256 != legacy_mode.source_binding_sha256

    class NoProviderSpend:
        def resolve(self, *args, **kwargs):
            raise AssertionError("legacy inspection must fail before skill resolution")

    with pytest.raises(CompilerError, match="legacy inspection"):
        compile_superpowers_plan(inspected[1], source_path=source_path, draft_bytes=b"{}",
                                 resolver=NoProviderSpend())


@pytest.mark.parametrize("dependency", [b"Task 9/Step 9", b"Task 1/Step 2", b"Task 2/Step 1"])
def test_parser_rejects_unknown_forward_and_self_declared_source_dependencies(dependency: bytes):
    """Declared edges must name prior stable steps, making cycles impossible by construction."""
    source = _source().replace(b"  **Depends on:** none", b"  **Depends on:** " + dependency, 1)

    with pytest.raises(CompilerError, match="Depends on"):
        parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")


@pytest.mark.parametrize("insertion", [
    b"- [ ] Untracked pre-step action\n\n",
    b"- [x] Checked action between steps\n\n",
])
def test_parser_rejects_noncanonical_task_checkboxes_outside_step_declarations(insertion: bytes):
    """Accepted task-owned checkboxes must always become a stable source Step."""
    anchor = b"- [ ] **Step 1: Read fixture**" if b"pre-step" in insertion else b"- [ ] **Step 2: Record finding**"
    source = _source().replace(anchor, insertion + anchor)

    with pytest.raises(CompilerError, match="checkbox|actionable"):
        parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")


def test_parser_allows_checkbox_examples_only_inside_fenced_step_content():
    """A fenced Markdown example is evidence, not a silently executable task."""
    source = _source().replace(
        b"Read the checked-in fixture without modifying it.",
        b"Read the checked-in fixture without modifying it.\n\n```md\n- [ ] Example only\n```",
    )

    assert [step.id for step in parse_superpowers_plan(
        source, source_path="docs/superpowers/plans/example.md").steps] == [
            "Task 1/Step 1", "Task 1/Step 2", "Task 2/Step 1",
        ]


def test_parser_rejects_hidden_actionable_list_after_the_last_step():
    """Task-owned action prose after a Step cannot evade the atomic partition."""
    source = _source() + b"- Perform untracked action\n"

    with pytest.raises(CompilerError, match="actionable"):
        parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")


@pytest.mark.parametrize("insertion", [
    b"## Follow-up\n- [ ] **Step 2: Hidden task work**\n  **Depends on:** none\n",
    b"## Follow-up\n- Perform untracked action\n",
    b"## Global Constraints\n- Quality tier: deep.\n",
])
def test_parser_rejects_action_or_constraints_after_task_section(insertion: bytes):
    source = _source().replace(
        b"### Task 2: Implement output", insertion + b"\n### Task 2: Implement output",
    )

    with pytest.raises(CompilerError, match="outside|actionable|Global Constraints"):
        parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")


def test_parser_rejects_unowned_preamble_checklist():
    source = _source().replace(
        b"This scope prose is a named document section, not task work.",
        b"This scope prose is a named document section, not task work.\n"
        b"- [ ] Hidden pre-task action",
    )

    with pytest.raises(CompilerError, match="outside|actionable|checkbox"):
        parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")


def test_parser_accepts_writing_plans_review_focus_bullets():
    source = _source().replace(
        b"## Scope",
        b"## Review Focus\n\n- Empty input must fail cleanly.\n"
        b"- A repeated request should keep its task context.\n\n## Scope",
        1,
    )

    parsed = parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")

    assert [step.id for step in parsed.steps] == [
        "Task 1/Step 1", "Task 1/Step 2", "Task 2/Step 1",
    ]


def test_parser_accepts_writing_plans_file_structure_bullets():
    source = _source().replace(
        b"## Scope",
        b"## File Structure\n\n- `src/example.py`: Owns the output.\n"
        b"- `tests/test_example.py`: Owns the behavioral test.\n\n## Scope",
        1,
    )

    parsed = parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")

    assert [step.id for step in parsed.steps] == [
        "Task 1/Step 1", "Task 1/Step 2", "Task 2/Step 1",
    ]


def test_parser_rejects_review_focus_checklist_as_unowned_work():
    source = _source().replace(
        b"## Scope",
        b"## Review Focus\n\n- [ ] **Step 1: Hidden work**\n\n## Scope",
        1,
    )

    with pytest.raises(CompilerError, match="outside|actionable|checkbox"):
        parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")


def test_parser_requires_metadata_outside_fenced_examples():
    metadata = (
        b"**Goal:** Build the bounded example.\n"
        b"**Architecture:** Two independent source tasks.\n"
        b"**Tech Stack:** Python 3.11+ stdlib only.\n"
        b"**Spec:** `docs/spec.md`\n"
    )
    source = _source()
    for line in metadata.splitlines(keepends=True):
        source = source.replace(line, b"", 1)
    source = source.replace(b"## Global Constraints", b"```md\n" + metadata + b"```\n\n## Global Constraints", 1)

    with pytest.raises(CompilerError, match="missing required metadata"):
        parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")


def test_parser_uses_visible_global_constraints_heading():
    source = _source().replace(
        b"## Global Constraints",
        b"```md\n## Global Constraints\n- Example only.\n```\n\n## Global Constraints",
        1,
    )

    parsed = parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")
    assert parsed.global_constraints == (
        "Preserve byte-bound source evidence.", "Do not invent work.",
    )


def test_parser_rejects_step_checkbox_inside_global_constraints():
    source = _source().replace(
        b"- Do not invent work.",
        b"- Do not invent work.\n- [ ] **Step 9: Hidden work**",
    )

    with pytest.raises(CompilerError, match="Global Constraints|checkbox"):
        parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")


def test_compiler_rejects_repo_write_without_owned_paths(tmp_path: Path):
    source = _source().replace(b"- Create: `shared/example.py`", b"- Test: `tests/test_example.py`")
    draft = json.loads(_draft(source))
    draft["tasks"][1]["owned_paths"] = []

    with pytest.raises(CompilerError, match="owned_paths"):
        _compile(tmp_path, source, json.dumps(draft).encode())


def test_compiler_requires_one_repo_write_owner_for_each_source_task(tmp_path: Path):
    source = _source().replace(
        b"- Create: `shared/example.py`",
        b"- Create: `shared/example.py`\n- Create: `shared/other.py`",
    ).replace(
        b"Implement only the checked behavior.",
        b"Implement only the checked behavior.\n\n"
        b"- [ ] **Step 2: Add independent change**\n  **Depends on:** none\n\n"
        b"Implement the other checked behavior.",
    )
    draft = json.loads(_draft(source))
    draft["tasks"][1]["owned_paths"] = ["shared/example.py"]
    other = json.loads(json.dumps(draft["tasks"][1]))
    other.update(id="other", title="Other", objective="Make the other bounded change.",
                 source_step_ids=["Task 2/Step 2"], owned_paths=["shared/other.py"])
    draft["tasks"].append(other)

    with pytest.raises(CompilerError, match="source Task|ownership"):
        _compile(tmp_path, source, json.dumps(draft).encode())


def test_compiler_allows_read_only_source_steps_to_join_independent_writers(tmp_path: Path):
    """Inspection Steps do not confer write ownership over unrelated source Tasks."""
    source = _source() + (
        b"\n### Task 3: Implement another output\n\n**Files:**\n"
        b"- Create: `shared/other.py`\n\n"
        b"- [ ] **Step 1: Add another bounded implementation**\n"
        b"  **Depends on:** none\n\nImplement the other checked behavior.\n"
    )
    draft = json.loads(_draft(source))
    first = draft["tasks"][1]
    first["source_step_ids"] = ["Task 1/Step 1", "Task 2/Step 1"]
    other = json.loads(json.dumps(first))
    other.update(id="other", title="Other", objective="Make the other bounded change.",
                 source_step_ids=["Task 1/Step 2", "Task 3/Step 1"],
                 owned_paths=["shared/other.py"])
    draft["tasks"] = [first, other]

    compiled = _compile(tmp_path, source, json.dumps(draft).encode())

    assert [task.owned_paths for task in compiled.plan.tasks] == [
        ("shared/example.py",), ("shared/other.py",),
    ]


def test_parser_preserves_multiline_global_constraint_text_and_hashes_it():
    """Accepted continuation text is source evidence and cannot disappear from the packet."""
    source = _source().replace(
        b"- Preserve byte-bound source evidence.",
        b"- Preserve byte-bound source evidence.\n  Also preserve its continuation.",
    )

    parsed = parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")
    assert parsed.global_constraints[0] == "Preserve byte-bound source evidence.\nAlso preserve its continuation."
    assert parsed.global_constraints_sha256 != parse_superpowers_plan(
        _source(), source_path="docs/superpowers/plans/example.md").global_constraints_sha256


def test_parser_rejects_fenced_global_constraints_before_section_boundary_scanning():
    """A heading-shaped example must not terminate the constraints section by accident."""
    source = _source().replace(
        b"- Do not invent work.",
        b"- Do not invent work.\n```md\n## Example heading\n```",
    )

    with pytest.raises(CompilerError, match="fenced"):
        parse_superpowers_plan(source, source_path="docs/superpowers/plans/example.md")


@pytest.mark.parametrize("field, value", [
    ("acceptance", []),
    ("checks", [{"argv": ["sh", "-c", "pytest"], "cwd": "", "env_allowlist": [], "timeout": 30,
                  "accepted_exit_codes": [0], "expected_artifacts": []}]),
])
def test_executable_work_requires_acceptance_and_safe_argv_when_present(tmp_path: Path, field, value):
    """Missing acceptance or a shell-form verifier would evade review."""
    source = _source()
    draft = json.loads(_draft(source))
    draft["tasks"][0][field] = value
    with pytest.raises(CompilerError):
        _compile(tmp_path, source, json.dumps(draft).encode())


def test_only_repo_write_requires_argv_checks(tmp_path: Path):
    """Read-only answers are artifacts, not invented files in a verifier workspace."""
    source = _source()
    draft = json.loads(_draft(source))
    draft["tasks"][0]["checks"] = []
    assert _compile(tmp_path, source, json.dumps(draft).encode()).plan.tasks[0].checks == ()

    draft["tasks"][1]["checks"] = []
    with pytest.raises(CompilerError, match="requires at least one argv check"):
        _compile(tmp_path, source, json.dumps(draft).encode())


def test_unresolved_or_divergent_effective_skills_fail_before_compilation_returns(tmp_path: Path):
    """No provider-facing packet is created with an unresolved or shadowed executor skill."""
    source = _source()
    missing = json.loads(_draft(source))
    missing["tasks"][0]["required_skills"] = ["missing"]
    with pytest.raises(CompilerError, match="inspect|required skill"):
        _compile(tmp_path, source, json.dumps(missing).encode())

    root_one, root_two = tmp_path / "one", tmp_path / "two"
    for root, body in ((root_one, "# One\n"), (root_two, "# Two\n")):
        (root / "review").mkdir(parents=True)
        (root / "review" / "SKILL.md").write_text(body, encoding="utf-8")
    resolver = SkillResolver((SkillRoot("one", root_one, 0), SkillRoot("two", root_two, 1)))
    with pytest.raises(CompilerError, match="inspect|divergent"):
        compile_superpowers_plan(source, source_path="docs/superpowers/plans/example.md",
                                 draft_bytes=_draft(source), resolver=resolver)
