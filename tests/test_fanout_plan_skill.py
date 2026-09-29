"""The planner CLI admits bounded questions and reviewed task bundles."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "shared/skills/llm-fanout-plan/scripts/plan.py"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *arguments], cwd=ROOT,
        capture_output=True, text=True, check=False,
    )


def _source() -> str:
    return "\n".join([
        "# Change Implementation Plan", "", "**Goal:** Update the bounded example.", "",
        "**Architecture:** One owned task.", "", "**Tech Stack:** Python 3.11+ stdlib.", "",
        "**Spec:** `docs/change.md`", "", "## Global Constraints", "",
        "- Preserve the declared task boundary.", "", "### Task 1: Update module", "",
        "**Files:**", "- Modify: `src/change.py`", "",
        "- [ ] **Step 1: Implement and test**", "  **Depends on:** none", "",
        "Update the module and run its declared check.", "",
    ])


def _check() -> dict[str, object]:
    return {
        "argv": ["python3", "-m", "pytest", "-q", "tests/test_change.py"],
        "cwd": "", "env_allowlist": [], "timeout": 120,
        "accepted_exit_codes": [0], "expected_artifacts": [],
    }


def _bundle(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = _source()
    skill_root = tmp_path / "skills"
    skill = skill_root / "khenrix-quality"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Quality\n", encoding="utf-8")
    draft = {
        "schema_version": "fanout-draft-v1",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "defaults": {"executor_ids": ["claude", "codex", "agy"], "rounds": 2,
                     "timeout": 120, "retries": 0, "minimum_success": 2},
        "tasks": [{
            "id": "change", "kind": "work", "parent_id": None,
            "title": "Update module", "objective": "Implement the bounded change.",
            "source_step_ids": ["Task 1/Step 1"], "depends_on": [],
            "execution_class": "repo-write", "required_skills": ["khenrix-quality"],
            "none_reason": None, "owned_paths": ["src/change.py"],
            "acceptance": ["The module change passes its test."],
            "checks": [_check()], "provider_policy": None,
        }],
    }
    core = {
        "schema_version": "fanout-bundle-ingress-v1",
        "quality_tier": "normal",
        "source_path": "docs/change.md", "source_markdown": source,
        "draft": draft,
    }
    digest = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False).encode() + b"\n").hexdigest()
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(core), encoding="utf-8")
    review = tmp_path / "owner-review.json"
    review.write_text(json.dumps({"schema_version": "fanout-owner-review-v1",
                                  "reviewer": "repository-owner", "binding_sha256": digest}),
                      encoding="utf-8")
    return bundle, skill_root, review


def _v2_bundle(tmp_path: Path) -> tuple[Path, Path, Path]:
    bundle, skill_root, review = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    packet["schema_version"] = "fanout-bundle-ingress-v2"
    packet["source_markdown"] = packet["source_markdown"].replace(
        "### Task 1: Update module\n", "### Task 1: Update module\n\n**Target:** address\n", 1,
    )
    draft = packet["draft"]
    draft["schema_version"] = "fanout-draft-v2"
    draft["source_sha256"] = hashlib.sha256(packet["source_markdown"].encode()).hexdigest()
    draft["targets"] = [{
        "id": "address", "repository": "github.com/example/address-service",
        "ticket_key": "TASK-123", "branch_ref": "refs/heads/feat/TASK-123-address",
    }]
    draft["tasks"][0]["target_id"] = "address"
    draft["tasks"][0]["dependency_modes"] = {}
    bundle.write_text(json.dumps(packet), encoding="utf-8")
    review.write_text(json.dumps({
        "schema_version": "fanout-owner-review-v2", "reviewer": "repository-owner",
        "binding_sha256": hashlib.sha256(
            (json.dumps({"schema_version": "fanout-owner-review-binding-v2", "bundle": packet},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        ).hexdigest(),
    }), encoding="utf-8")
    return bundle, skill_root, review


def test_v2_bundle_compiles_and_admits_exact_target(tmp_path: Path):
    bundle, skill_root, review = _v2_bundle(tmp_path)
    result = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root),
                  "--owner-review-file", str(review))
    assert result.returncode == 0, result.stderr
    packet = json.loads(result.stdout)
    assert packet["schema_version"] == "fanout-plan-admission-v2"
    assert packet["compiled"]["schema_version"] == "compiled-fanout-plan-v2"
    assert packet["compiled"]["plan"]["tasks"][0]["target_id"] == "address"
    assert packet["compiled"]["plan"]["source_steps"][0]["target_id"] == "address"


def test_v2_bundle_review_revoked_by_target_branch_change(tmp_path: Path):
    bundle, skill_root, review = _v2_bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    packet["draft"]["targets"][0]["branch_ref"] = "refs/heads/feat/TASK-123-other"
    bundle.write_text(json.dumps(packet), encoding="utf-8")
    result = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root),
                  "--owner-review-file", str(review))
    assert result.returncode != 0 and not result.stdout
    assert "review" in result.stderr.lower()


def test_v1_source_cannot_gain_target_from_v2_draft(tmp_path: Path):
    bundle, skill_root, _ = _v2_bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    packet["source_markdown"] = packet["source_markdown"].replace("**Target:** address\n\n", "", 1)
    packet["draft"]["source_sha256"] = hashlib.sha256(packet["source_markdown"].encode()).hexdigest()
    source_path, draft_path = tmp_path / "source.md", tmp_path / "draft.json"
    source_path.write_text(packet["source_markdown"], encoding="utf-8")
    draft_path.write_text(json.dumps(packet["draft"]), encoding="utf-8")
    result = _run("compile", "--source", str(source_path), "--source-path", "docs/change.md",
                  "--draft", str(draft_path), "--skill-root", str(skill_root))
    assert result.returncode != 0 and not result.stdout
    assert "schema_version" in result.stderr or "target" in result.stderr.lower()


def test_direct_question_is_one_read_only_task_with_invocation_approval(tmp_path: Path):
    """A read-only question must not become a model-call DAG or authorize edits."""
    question = tmp_path / "question.txt"
    question.write_text("Should rounding happen before a discount?\n", encoding="utf-8")
    skill_root = tmp_path / "skills"
    skill_root.mkdir()

    result = _run("from-question", "--question-file", str(question),
                  "--tier", "normal", "--approval", "invocation-authorized",
                  "--none-reason", "No specialized skill applies to this bounded question.",
                  "--skill-root", str(skill_root))

    assert result.returncode == 0, result.stderr
    packet = json.loads(result.stdout)
    assert packet["admission"] == {"kind": "direct-question", "approval": "invocation-authorized",
                                    "quality_tier": "normal"}
    tasks = packet["compiled"]["plan"]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["execution_class"] == "read-only"
    assert tasks[0]["depends_on"] == []
    assert tasks[0]["owned_paths"] == []
    assert tasks[0]["checks"] == []
    assert tasks[0]["provider_policy"] is None
    assert packet["compiled"]["plan"]["defaults"]["executor_ids"] == ["claude", "codex", "agy"]
    assert packet["compiled"]["plan"]["defaults"]["quality_tier"] == "standard"
    assert "answer.md" not in packet["source_markdown"]
    assert packet["compiled"]["plan"]["source"]["sha256"] == hashlib.sha256(
        packet["source_markdown"].encode()).hexdigest()


def test_direct_question_requires_invocation_approval(tmp_path: Path):
    """A tier choice does not itself authorize model calls."""
    question = tmp_path / "question.txt"
    question.write_text("Review the invoice rule.\n", encoding="utf-8")
    skill_root = tmp_path / "skills"
    skill_root.mkdir()

    missing = _run("from-question", "--question-file", str(question),
                   "--tier", "normal", "--skill-root", str(skill_root))
    assert missing.returncode != 0 and not missing.stdout


def test_direct_question_preserves_deep_tier_for_one_read_only_task(tmp_path: Path):
    """A requested deep answer must reach execution as deep, with profile-derived timeout."""
    question = tmp_path / "question.txt"
    question.write_text("Review the invoice rule.\n", encoding="utf-8")
    skill_root = tmp_path / "skills"
    skill_root.mkdir()

    result = _run("from-question", "--question-file", str(question),
                  "--tier", "deep", "--approval", "invocation-authorized",
                  "--none-reason", "No specialized skill applies.",
                  "--skill-root", str(skill_root))

    assert result.returncode == 0, result.stderr
    packet = json.loads(result.stdout)
    assert packet["admission"]["quality_tier"] == "deep"
    assert "- Quality tier: deep." in packet["source_markdown"]
    assert packet["draft"]["defaults"]["quality_tier"] == "deep"
    assert packet["compiled"]["plan"]["defaults"]["quality_tier"] == "deep"
    assert packet["compiled"]["plan"]["defaults"]["timeout"] is None
    assert len(packet["compiled"]["plan"]["tasks"]) == 1
    assert packet["compiled"]["plan"]["tasks"][0]["execution_class"] == "read-only"


def test_direct_question_requires_reviewed_skill_choice(tmp_path: Path):
    """No skill and no explicit none reason cannot be a silent planning default."""
    question = tmp_path / "question.txt"
    question.write_text("Review the invoice rule.\n", encoding="utf-8")
    skill_root = tmp_path / "skills"
    skill_root.mkdir()

    result = _run("from-question", "--question-file", str(question),
                  "--tier", "normal", "--approval", "invocation-authorized",
                  "--skill-root", str(skill_root))

    assert result.returncode != 0 and not result.stdout
    assert "--none-reason" in result.stderr


def test_direct_question_accepts_explicit_executor_ids_but_rejects_maka(tmp_path: Path):
    """New registry IDs need no adapter change; Maka remains routing-only."""
    question = tmp_path / "question.txt"
    question.write_text("Which behavior is correct?\n", encoding="utf-8")
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    common = ("from-question", "--question-file", str(question),
              "--tier", "normal", "--approval", "invocation-authorized",
              "--none-reason", "No specialized executor skill applies.",
              "--skill-root", str(skill_root))

    expanded = _run(*common, "--executor", "claude", "--executor", "codex",
                    "--executor", "future-cli")
    maka = _run(*common, "--executor", "claude", "--executor", "maka")

    assert expanded.returncode == 0, expanded.stderr
    assert json.loads(expanded.stdout)["compiled"]["plan"]["defaults"]["executor_ids"] == [
        "claude", "codex", "future-cli"]
    assert maka.returncode != 0 and not maka.stdout
    assert "maka" in maka.stderr.lower()


def test_direct_question_quotes_source_metadata_without_turning_it_into_metadata(tmp_path: Path):
    question = tmp_path / "question.txt"
    question.write_text("Explain the literal marker **Depends on:** none.\n", encoding="utf-8")
    root = tmp_path / "skills"
    root.mkdir()
    result = _run("from-question", "--question-file", str(question), "--tier", "normal",
                  "--approval", "invocation-authorized", "--none-reason", "No skill applies.",
                  "--skill-root", str(root))
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)["compiled"]["plan"]["tasks"]) == 1


def test_ordered_skill_roots_resolve_split_installs_and_refuse_divergent_shadows(tmp_path: Path):
    question = tmp_path / "question.txt"
    question.write_text("Review the result.\n", encoding="utf-8")
    primary, secondary = tmp_path / "primary", tmp_path / "secondary"
    primary.mkdir()
    skill = secondary / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
    common = ("from-question", "--question-file", str(question), "--tier", "normal",
              "--approval", "invocation-authorized", "--skill", "review",
              "--skill-root", str(primary), "--skill-root", str(secondary))
    split = _run(*common)
    assert split.returncode == 0, split.stderr
    assert json.loads(split.stdout)["compiled"]["resolved_skill_manifests"]["answer"][0]["source"] == "root-1"

    shadow = primary / "review"
    shadow.mkdir()
    (shadow / "SKILL.md").write_text("# Divergent\n", encoding="utf-8")
    divergent = _run(*common)
    assert divergent.returncode != 0 and not divergent.stdout
    assert "divergent skill shadows" in divergent.stderr


def test_write_bundle_requires_digest_bound_owner_review(tmp_path: Path):
    """A stale or absent review cannot authorize a repository-writing packet."""
    bundle, skill_root, review = _bundle(tmp_path)
    absent = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root))
    review.write_text(json.dumps({"schema_version": "fanout-owner-review-v1",
                                  "reviewer": "repository-owner", "binding_sha256": "0" * 64}),
                      encoding="utf-8")
    stale = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root),
                 "--owner-review-file", str(review))

    assert absent.returncode != 0 and not absent.stdout
    assert stale.returncode != 0 and not stale.stdout
    assert "owner review" in absent.stderr.lower()
    assert "owner review" in stale.stderr.lower()


def test_bundle_cannot_self_assert_its_owner_review(tmp_path: Path):
    """A model-produced bundle carrying its own review must be refused."""
    bundle, skill_root, review = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    packet["owner_review"] = json.loads(review.read_text())
    bundle.write_text(json.dumps(packet), encoding="utf-8")

    result = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root))

    assert result.returncode != 0 and not result.stdout
    assert "owner review" in result.stderr.lower()


def test_reviewed_bundle_preserves_ownership_checks_and_skill_routing(tmp_path: Path):
    """Ingress may validate a bundle but must not weaken its task declaration."""
    bundle, skill_root, review = _bundle(tmp_path)

    result = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root),
                  "--owner-review-file", str(review))

    assert result.returncode == 0, result.stderr
    packet = json.loads(result.stdout)
    task = packet["compiled"]["plan"]["tasks"][0]
    assert task["owned_paths"] == ["src/change.py"]
    assert task["checks"] == [_check()]
    assert task["required_skills"] == ["khenrix-quality"]
    assert packet["compiled"]["resolved_skill_manifests"]["change"][0]["name"] == "khenrix-quality"
    assert packet["admission"]["owner_review"]["reviewer"] == "repository-owner"


def test_bundle_tamper_after_review_is_refused(tmp_path: Path):
    """Changing the owned path after sign-off invalidates the review binding."""
    bundle, skill_root, review = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    packet["draft"]["tasks"][0]["owned_paths"] = ["src/other.py"]
    bundle.write_text(json.dumps(packet), encoding="utf-8")

    result = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root),
                  "--owner-review-file", str(review))

    assert result.returncode != 0 and not result.stdout
    assert "owner review" in result.stderr.lower()


def test_bundle_requires_explicit_quality_tier_and_refuses_deep_write(tmp_path: Path):
    """A write bundle cannot acquire a read-only deep profile."""
    bundle, skill_root, review = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    packet.pop("quality_tier")
    bundle.write_text(json.dumps(packet), encoding="utf-8")
    missing = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root),
                   "--owner-review-file", str(review))
    packet["quality_tier"] = "deep"
    bundle.write_text(json.dumps(packet), encoding="utf-8")
    binding = hashlib.sha256(json.dumps(packet, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False).encode() + b"\n").hexdigest()
    review.write_text(json.dumps({"schema_version": "fanout-owner-review-v1",
                                  "reviewer": "repository-owner", "binding_sha256": binding}),
                      encoding="utf-8")
    deep = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root),
                "--owner-review-file", str(review))

    assert missing.returncode != 0 and not missing.stdout
    assert deep.returncode != 0 and not deep.stdout
    assert "quality_tier" in missing.stderr
    assert "read-only" in deep.stderr


@pytest.mark.parametrize("policy_location", ["defaults", "task"])
def test_normal_bundle_binds_defaults_but_allows_read_only_task_local_deep(
        tmp_path: Path, policy_location: str):
    bundle, skill_root, _ = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    source_text = packet["source_markdown"].replace(
        "- Modify: `src/change.py`", "- Test: `tests/test_change.py`").replace(
        "Update the module and run its declared check.", "Inspect the module and report findings.")
    packet["source_markdown"] = source_text
    draft = packet["draft"]
    draft["source_sha256"] = hashlib.sha256(source_text.encode()).hexdigest()
    task = draft["tasks"][0]
    task["execution_class"] = "read-only"
    task["owned_paths"] = []
    task["checks"] = []
    if policy_location == "defaults":
        draft["defaults"]["quality_tier"] = "deep"
    else:
        task["provider_policy"] = {**draft["defaults"], "quality_tier": "deep"}
    bundle.write_text(json.dumps(packet), encoding="utf-8")
    source = tmp_path / "source.md"
    source.write_text(source_text, encoding="utf-8")
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")

    direct = _run("compile", "--source", str(source), "--source-path", "docs/change.md",
                  "--draft", str(draft_path), "--skill-root", str(skill_root))
    wrapped = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root))

    assert direct.returncode == 0, direct.stderr
    compiled = json.loads(direct.stdout)
    policy = (compiled["plan"]["defaults"] if policy_location == "defaults"
              else compiled["plan"]["tasks"][0]["provider_policy"])
    assert policy["quality_tier"] == "deep"
    if policy_location == "defaults":
        assert wrapped.returncode != 0 and not wrapped.stdout
        assert "quality_tier" in wrapped.stderr
    else:
        assert wrapped.returncode == 0, wrapped.stderr
        admitted = json.loads(wrapped.stdout)
        assert admitted["admission"]["quality_tier"] == "normal"
        assert admitted["compiled"]["plan"]["defaults"]["quality_tier"] == "standard"
        assert admitted["compiled"]["plan"]["tasks"][0]["provider_policy"]["quality_tier"] == "deep"


def test_reviewed_mixed_bundle_keeps_read_only_deep_override(tmp_path: Path):
    """Owner review of a write task must not erase a separate read-only deep tier."""
    bundle, skill_root, review = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    source = packet["source_markdown"] + "\n".join([
        "### Task 2: Review module", "", "**Files:**", "- Test: `tests/test_change.py`", "",
        "- [ ] **Step 1: Inspect and report**", "  **Depends on:** Task 1/Step 1", "",
        "Inspect the module and report findings.", "",
    ])
    packet["source_markdown"] = source
    packet["draft"]["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    packet["draft"]["tasks"].append({
        "id": "review", "kind": "work", "parent_id": None,
        "title": "Review module", "objective": "Inspect and report findings.",
        "source_step_ids": ["Task 2/Step 1"], "depends_on": ["change"],
        "execution_class": "read-only", "required_skills": ["khenrix-quality"],
        "none_reason": None, "owned_paths": [],
        "acceptance": ["Findings are reported."], "checks": [],
        "provider_policy": {**packet["draft"]["defaults"], "quality_tier": "deep"},
    })
    bundle.write_text(json.dumps(packet), encoding="utf-8")
    binding = hashlib.sha256(json.dumps(packet, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False).encode() + b"\n").hexdigest()
    review.write_text(json.dumps({"schema_version": "fanout-owner-review-v1",
                                  "reviewer": "repository-owner", "binding_sha256": binding}),
                      encoding="utf-8")

    result = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root),
                  "--owner-review-file", str(review))

    assert result.returncode == 0, result.stderr
    admitted = json.loads(result.stdout)
    assert admitted["admission"]["owner_review"]["reviewer"] == "repository-owner"
    assert admitted["compiled"]["plan"]["defaults"]["quality_tier"] == "standard"
    assert [task["execution_class"] for task in admitted["compiled"]["plan"]["tasks"]] == [
        "repo-write", "read-only",
    ]
    assert admitted["compiled"]["plan"]["tasks"][1]["provider_policy"]["quality_tier"] == "deep"


def test_source_deep_tier_is_not_laundered_through_normal_bundle_or_compile(tmp_path: Path):
    bundle, skill_root, _ = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    packet["source_markdown"] = packet["source_markdown"].replace(
        "- Modify: `src/change.py`", "- Test: `tests/test_change.py`")
    packet["draft"]["tasks"][0].update({
        "execution_class": "read-only", "owned_paths": [], "checks": [],
    })
    packet["source_markdown"] = packet["source_markdown"].replace(
        "- Preserve the declared task boundary.",
        "- Preserve the declared task boundary.\n- Quality tier: deep.")
    packet["draft"]["source_sha256"] = hashlib.sha256(
        packet["source_markdown"].encode()).hexdigest()
    bundle.write_text(json.dumps(packet), encoding="utf-8")
    source = tmp_path / "source.md"
    source.write_text(packet["source_markdown"], encoding="utf-8")
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(packet["draft"]), encoding="utf-8")
    direct = _run("compile", "--source", str(source), "--source-path", "docs/change.md",
                  "--draft", str(draft), "--skill-root", str(skill_root))
    wrapped = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root))
    assert direct.returncode != 0 and not direct.stdout
    assert wrapped.returncode != 0 and not wrapped.stdout
    assert "deep" in direct.stderr.lower()
    assert "deep" in wrapped.stderr.lower()


def test_deep_read_only_bundle_preserves_tier_without_write_review(tmp_path: Path):
    bundle, skill_root, _ = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    packet["source_markdown"] = packet["source_markdown"].replace(
        "- Modify: `src/change.py`", "- Test: `tests/test_change.py`")
    packet["draft"]["source_sha256"] = hashlib.sha256(
        packet["source_markdown"].encode()).hexdigest()
    packet["draft"]["tasks"][0].update({
        "execution_class": "read-only", "owned_paths": [], "checks": [],
    })
    packet["draft"]["defaults"].update({"quality_tier": "deep", "timeout": None})
    packet["quality_tier"] = "deep"
    bundle.write_text(json.dumps(packet), encoding="utf-8")

    result = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root))

    assert result.returncode == 0, result.stderr
    admitted = json.loads(result.stdout)
    assert admitted["admission"]["quality_tier"] == "deep"
    assert admitted["admission"]["owner_review"] is None
    assert admitted["compiled"]["plan"]["defaults"]["quality_tier"] == "deep"
    assert admitted["compiled"]["plan"]["defaults"]["timeout"] is None


def test_bold_source_deep_tier_cannot_be_downgraded_by_draft(tmp_path: Path):
    bundle, skill_root, _ = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    source_text = packet["source_markdown"].replace(
        "- Preserve the declared task boundary.",
        "- Preserve the declared task boundary.\n- **Quality tier:** deep")
    draft = packet["draft"]
    draft["source_sha256"] = hashlib.sha256(source_text.encode()).hexdigest()
    source = tmp_path / "source.md"
    source.write_text(source_text, encoding="utf-8")
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    result = _run("compile", "--source", str(source), "--source-path", "docs/change.md",
                  "--draft", str(draft_path), "--skill-root", str(skill_root))
    assert result.returncode != 0 and not result.stdout
    assert "deep" in result.stderr.lower()


def test_backticked_source_deep_tier_cannot_be_downgraded_by_draft(tmp_path: Path):
    bundle, skill_root, _ = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    source = tmp_path / "source.md"
    draft_path = tmp_path / "draft.json"

    def compile_source(text: str):
        source.write_text(text, encoding="utf-8")
        draft = dict(packet["draft"])
        draft["source_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        draft_path.write_text(json.dumps(draft), encoding="utf-8")
        return _run("compile", "--source", str(source), "--source-path", "docs/change.md",
                    "--draft", str(draft_path), "--skill-root", str(skill_root))

    deep = compile_source(packet["source_markdown"].replace(
        "- Preserve the declared task boundary.",
        "- Preserve the declared task boundary.\n- Quality tier: `deep`."))
    assert deep.returncode != 0 and not deep.stdout
    assert "deep" in deep.stderr.lower()


def test_fenced_deep_example_does_not_override_normal_plan(tmp_path: Path):
    bundle, skill_root, _ = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    source_text = packet["source_markdown"].replace(
        "Update the module and run its declared check.",
        "Update the module and run its declared check.\n\n```md\n- Quality tier: deep\n```")
    source = tmp_path / "source.md"
    source.write_text(source_text, encoding="utf-8")
    draft = packet["draft"]
    draft["source_sha256"] = hashlib.sha256(source_text.encode()).hexdigest()
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    example = _run("compile", "--source", str(source), "--source-path", "docs/change.md",
                   "--draft", str(draft_path), "--skill-root", str(skill_root))
    assert example.returncode == 0, example.stderr


def test_source_write_intent_cannot_skip_bundle_owner_review(tmp_path: Path):
    bundle, skill_root, _ = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    packet["draft"]["tasks"][0].update({
        "execution_class": "read-only", "checks": [], "owned_paths": [],
    })
    bundle.write_text(json.dumps(packet), encoding="utf-8")
    result = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root))
    assert result.returncode != 0 and not result.stdout
    assert "source write intent" in result.stderr


def test_read_only_grouped_bundle_needs_no_write_review(tmp_path: Path):
    """Hierarchy alone must not be mistaken for a write authorization request."""
    bundle, skill_root, _ = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    packet["source_markdown"] = packet["source_markdown"].replace(
        "Update the module and run its declared check.", "Inspect the module and report its behavior.")
    packet["source_markdown"] = packet["source_markdown"].replace(
        "- Modify: `src/change.py`", "- Test: `src/change.py`")
    packet["draft"]["source_sha256"] = hashlib.sha256(
        packet["source_markdown"].encode()).hexdigest()
    packet["draft"]["tasks"][0].update({
        "parent_id": "phase", "execution_class": "read-only", "owned_paths": [],
        "required_skills": [], "none_reason": "No executor skill applies to this inspection.",
        "checks": [],
    })
    packet["draft"]["tasks"].insert(0, {
        "id": "phase", "kind": "group", "parent_id": None,
        "title": "Inspection phase", "objective": "Organize the read-only task.",
        "required_skills": [],
    })
    bundle.write_text(json.dumps(packet), encoding="utf-8")

    result = _run("from-bundle", "--bundle", str(bundle), "--skill-root", str(skill_root))

    assert result.returncode == 0, result.stderr
    tasks = json.loads(result.stdout)["compiled"]["plan"]["tasks"]
    assert [task["kind"] for task in tasks] == ["group", "work"]
    assert tasks[1]["checks"] == []


def test_compile_and_validate_recompute_exact_source_and_draft(tmp_path: Path):
    """A cached compiled packet cannot validate against revised source bytes."""
    bundle, skill_root, _ = _bundle(tmp_path)
    packet = json.loads(bundle.read_text())
    source = tmp_path / "source.md"
    source.write_text(packet["source_markdown"], encoding="utf-8")
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(packet["draft"]), encoding="utf-8")
    compiled = tmp_path / "compiled.json"

    made = _run("compile", "--source", str(source), "--source-path", "docs/change.md",
                "--draft", str(draft), "--skill-root", str(skill_root))
    assert made.returncode == 0, made.stderr
    compiled.write_text(made.stdout, encoding="utf-8")
    valid = _run("validate", "--source", str(source), "--source-path", "docs/change.md",
                 "--draft", str(draft), "--compiled", str(compiled),
                 "--skill-root", str(skill_root))
    assert valid.returncode == 0, valid.stderr

    source.write_text(packet["source_markdown"].replace("Update the module", "Change the module"),
                      encoding="utf-8")
    drift = _run("validate", "--source", str(source), "--source-path", "docs/change.md",
                 "--draft", str(draft), "--compiled", str(compiled),
                 "--skill-root", str(skill_root))
    assert drift.returncode != 0 and not drift.stdout


def test_delivery_stages_runtime_and_memory_and_receipt_covers_both(tmp_path: Path):
    """A runtime or memory-controller edit must change the delivered tree and eval closure."""
    def module(path: Path, name: str):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[name] = loaded
        spec.loader.exec_module(loaded)
        return loaded

    skillctl = module(ROOT / "components/skills/skillctl.py", "_fanout_plan_skillctl")
    checks = module(ROOT / "scripts/lib/checks.py", "_fanout_plan_checks")
    config = skillctl.load_configuration(ROOT, tmp_path / "home", None)
    question = tmp_path / "question.txt"
    question.write_text("Is this read-only?\n", encoding="utf-8")
    empty_skills = tmp_path / "skills"
    empty_skills.mkdir()

    with skillctl.staged_skill_sources(config) as staged:
        bundled = staged["llm-fanout-plan"]
        assert (bundled / "lib/fanout/__init__.py").read_bytes() == (
            ROOT / "shared/lib/fanout/__init__.py").read_bytes()
        assert (bundled / "memory/memory_exchange.py").read_bytes() == (
            ROOT / "components/memory/memory_exchange.py").read_bytes()
        result = subprocess.run(
            [sys.executable, str(bundled / "scripts/plan.py"), "from-question",
             "--question-file", str(question), "--approval", "invocation-authorized",
             "--tier", "normal", "--none-reason", "No executor skill applies.",
             "--skill-root", str(empty_skills)],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["compiled"]["plan"]["tasks"][0]["execution_class"] == "read-only"

    source_paths = {path for path, _ in checks.source_manifest(ROOT, "llm-fanout-plan")}
    assert "shared/lib/fanout/compiler.py" in source_paths
    assert "components/memory/memory_exchange.py" in source_paths
    assert "scripts/lib/portability.py" in source_paths
    assert "capabilities.toml" in source_paths


def test_bundle_mapping_edit_changes_planner_source_hash(tmp_path: Path):
    spec = importlib.util.spec_from_file_location("_fanout_plan_checks_closure",
                                                  ROOT / "scripts/lib/checks.py")
    assert spec and spec.loader
    checks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checks)
    caps = tmp_path / "capabilities.toml"
    caps.write_text('[skill_delivery]\nskills = ["llm-fanout-plan"]\n', encoding="utf-8")
    before = checks.source_hash(tmp_path, "llm-fanout-plan")
    caps.write_text('[skill_delivery]\nskills = ["llm-fanout-plan"]\n'
                    '[skill_delivery.bundles.llm-fanout-plan]\nlib = "shared/lib/fanout"\n',
                    encoding="utf-8")
    assert checks.source_hash(tmp_path, "llm-fanout-plan") != before


def test_portability_exempts_declared_native_only_scripts_not_plugin_scripts(tmp_path: Path):
    """A direct-copy script needs all native targets, not duplicate plugin copies."""
    spec = importlib.util.spec_from_file_location("_fanout_plan_portability",
                                                  ROOT / "scripts/lib/portability.py")
    assert spec and spec.loader
    portability = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = portability
    spec.loader.exec_module(portability)
    (tmp_path / "capabilities.toml").write_text(
        '[skill_delivery]\nskills = ["native"]\n', encoding="utf-8")
    for name in ("native", "plugin"):
        scripts = tmp_path / "shared/skills" / name / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "tool.py").write_text("print(1)\n", encoding="utf-8")
    for cli in ("claude", "codex", "agy"):
        scripts = tmp_path / "marketplaces" / cli / "plugins/khenrix-utils/skills/plugin/scripts"
        scripts.mkdir(parents=True)
        (scripts / "tool.py").write_text("print(1)\n", encoding="utf-8")

    assert portability.script_tree_parity(tmp_path) == []
    (tmp_path / "marketplaces/agy/plugins/khenrix-utils/skills/plugin/scripts/tool.py").unlink()
    problems = portability.script_tree_parity(tmp_path)
    assert len(problems) == 1 and "plugin/scripts/tool.py" in problems[0] and "agy" in problems[0]
