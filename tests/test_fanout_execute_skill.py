"""The installed executor CLI admits exact plans and safe owner-only recovery."""
from __future__ import annotations

import hashlib
import dataclasses
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "shared/skills/llm-fanout-execute/scripts/execute.py"
PLANNER = ROOT / "shared/skills/llm-fanout-plan/scripts/plan.py"
sys.path.insert(0, str(ROOT / "shared/lib"))
import fanout  # noqa: E402


@pytest.fixture
def execute():
    if not CLI.is_file():
        return SimpleNamespace()
    spec = importlib.util.spec_from_file_location("fanout_execute_skill", CLI)
    assert spec is not None and spec.loader is not None, "executor skill CLI is missing"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v2_read_only_cli_dispatch_requires_native_boundary(execute):
    plan = SimpleNamespace(schema_version="v2", tasks=(
        SimpleNamespace(kind="work", execution_class="read-only"),
    ))
    with pytest.raises(ValueError, match="native seat boundary"):
        execute._require_native_seat_boundary(plan)
    execute._require_native_seat_boundary(SimpleNamespace(
        schema_version="v1", tasks=plan.tasks,
    ))


@pytest.mark.parametrize("source_cli", (PLANNER, CLI))
def test_installed_skill_entrypoint_does_not_dirty_its_bundle_with_bytecode(
    tmp_path: Path, source_cli: Path,
) -> None:
    isolated = tmp_path / source_cli.parent.parent.name
    shutil.copytree(
        ROOT / "shared/lib/fanout", isolated / "lib/fanout",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    scripts = isolated / "scripts"
    scripts.mkdir()
    cli = scripts / source_cli.name
    shutil.copy2(source_cli, cli)
    missing = tmp_path / "missing"
    argv = (
        ["compile", "--source", str(missing), "--source-path", "missing.md",
         "--draft", str(missing), "--skill-root", str(tmp_path)]
        if source_cli == PLANNER else
        ["status", "--private-root", str(missing)]
    )
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}}
    result = subprocess.run(
        [sys.executable, str(cli), *argv], capture_output=True, text=True,
        check=False, env=environment,
    )

    assert result.returncode == 2
    expected_error = (
        "input must be a regular file" if source_cli == PLANNER else
        "private descriptor directory is unavailable"
    )
    assert expected_error in result.stderr
    assert not list(isolated.rglob("__pycache__"))


def _question_packet(tmp_path: Path, *, executors=()) -> tuple[dict[str, object], fanout.SkillResolver]:
    question = tmp_path / "question.txt"
    question.write_text("Which invoice rounding order is correct?\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    result = subprocess.run(
        [sys.executable, str(PLANNER), "from-question", "--question-file", str(question),
         "--approval", "invocation-authorized", "--tier", "normal",
         "--none-reason", "No specialist skill applies.", "--skill-root", str(skills),
         *(argument for executor in executors for argument in ("--executor", executor))],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    resolver = fanout.SkillResolver((fanout.SkillRoot("root-0", skills, 0),))
    return json.loads(result.stdout), resolver


def _write_packet(tmp_path: Path, *, retries: int = 0) -> tuple[dict[str, object], fanout.SkillResolver]:
    source = "\n".join([
        "# Change Implementation Plan", "", "**Goal:** Update the bounded example.", "",
        "**Architecture:** One owned task.", "", "**Tech Stack:** Python 3.11+ stdlib.",
        "", "**Spec:** `docs/change.md`", "", "## Global Constraints", "",
        "- Preserve the declared task boundary.", "", "### Task 1: Update module", "",
        "**Files:**", "- Modify: `src/change.py`", "",
        "- [ ] **Step 1: Implement and test**", "  **Depends on:** none", "",
        "Update the module and run its declared check.", "",
    ])
    skills = tmp_path / "skills"
    skill = skills / "khenrix-quality"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Quality\n", encoding="utf-8")
    draft = {
        "schema_version": "fanout-draft-v1",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "defaults": {"executor_ids": ["claude", "codex", "agy"], "rounds": 2,
                     "timeout": 120, "retries": retries, "minimum_success": 2},
        "tasks": [{
            "id": "change", "kind": "work", "parent_id": None,
            "title": "Update module", "objective": "Implement the bounded change.",
            "source_step_ids": ["Task 1/Step 1"], "depends_on": [],
            "execution_class": "repo-write", "required_skills": ["khenrix-quality"],
            "none_reason": None, "owned_paths": ["src/change.py"],
            "acceptance": ["The module change passes its test."],
            "checks": [{"argv": ["python3", "-m", "pytest", "-q", "tests/test_change.py"],
                        "cwd": "", "env_allowlist": [], "timeout": 120,
                        "accepted_exit_codes": [0], "expected_artifacts": []}],
            "provider_policy": None,
        }],
    }
    ingress = {"schema_version": "fanout-bundle-ingress-v1", "quality_tier": "normal",
               "source_path": "docs/change.md", "source_markdown": source, "draft": draft}
    ingress_path = tmp_path / "ingress.json"
    ingress_path.write_text(json.dumps(ingress), encoding="utf-8")
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps({
        "schema_version": "fanout-owner-review-v1", "reviewer": "repository-owner",
        "binding_sha256": hashlib.sha256(fanout.canonical_json(ingress)).hexdigest(),
    }), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PLANNER), "from-bundle", "--bundle", str(ingress_path),
         "--owner-review-file", str(review_path), "--skill-root", str(skills)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    resolver = fanout.SkillResolver((fanout.SkillRoot("root-0", skills, 0),))
    return json.loads(result.stdout), resolver


def _v2_write_packet(tmp_path: Path) -> tuple[dict[str, object], fanout.SkillResolver]:
    original, resolver = _write_packet(tmp_path)
    source = original["source_markdown"].replace(
        "### Task 1: Update module\n", "### Task 1: Update module\n\n**Target:** address\n", 1,
    )
    draft = original["draft"]
    draft["schema_version"] = "fanout-draft-v2"
    draft["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    draft["targets"] = [{
        "id": "address", "repository": "github.com/example/address-service",
        "ticket_key": "TASK-123", "branch_ref": "refs/heads/feat/TASK-123-address",
    }]
    draft["tasks"][0]["target_id"] = "address"
    draft["tasks"][0]["dependency_modes"] = {}
    ingress = {
        "schema_version": "fanout-bundle-ingress-v2", "quality_tier": "normal",
        "source_path": "docs/change.md", "source_markdown": source, "draft": draft,
    }
    review = {
        "schema_version": "fanout-owner-review-v2", "reviewer": "repository-owner",
        "binding_sha256": hashlib.sha256(fanout.canonical_json({
            "schema_version": "fanout-owner-review-binding-v2", "bundle": ingress,
        })).hexdigest(),
    }
    bundle_path, review_path = tmp_path / "v2-ingress.json", tmp_path / "v2-review.json"
    bundle_path.write_bytes(fanout.canonical_json(ingress))
    review_path.write_bytes(fanout.canonical_json(review))
    result = subprocess.run(
        [sys.executable, str(PLANNER), "from-bundle", "--bundle", str(bundle_path),
         "--owner-review-file", str(review_path), "--skill-root", str(tmp_path / "skills")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout), resolver


def _v2_two_repo_packet(tmp_path: Path, *, writer_check=None) -> dict[str, object]:
    original, _ = _v2_write_packet(tmp_path)
    source = original["source_markdown"] + "\n".join([
        "### Task 2: Inspect booking", "", "**Target:** booking", "", "**Files:**", "",
        "- [ ] **Step 1: Inspect the bounded example**", "  **Depends on:** none", "",
        "Inspect booking without changing files.", "",
    ])
    draft = original["draft"]
    draft["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    if writer_check is not None:
        draft["tasks"][0]["checks"] = [writer_check]
    draft["targets"].append({
        "id": "booking", "repository": "github.com/example/booking-service",
        "ticket_key": "TASK-123", "branch_ref": "refs/heads/feat/TASK-123-booking",
    })
    draft["tasks"].append({
        "id": "inspect-booking", "kind": "work", "parent_id": None,
        "title": "Inspect booking", "objective": "Inspect the booking example.",
        "source_step_ids": ["Task 2/Step 1"], "depends_on": [],
        "execution_class": "read-only", "required_skills": [],
        "none_reason": "No specialist skill applies.", "owned_paths": [],
        "acceptance": ["Booking example inspected."], "checks": [],
        "provider_policy": None, "target_id": "booking", "dependency_modes": {},
    })
    ingress = {
        "schema_version": "fanout-bundle-ingress-v2", "quality_tier": "normal",
        "source_path": "docs/change.md", "source_markdown": source, "draft": draft,
    }
    review = {
        "schema_version": "fanout-owner-review-v2", "reviewer": "repository-owner",
        "binding_sha256": hashlib.sha256(fanout.canonical_json({
            "schema_version": "fanout-owner-review-binding-v2", "bundle": ingress,
        })).hexdigest(),
    }
    bundle, review_file = tmp_path / "two-repo-ingress.json", tmp_path / "two-repo-review.json"
    bundle.write_bytes(fanout.canonical_json(ingress))
    review_file.write_bytes(fanout.canonical_json(review))
    result = subprocess.run(
        [sys.executable, str(PLANNER), "from-bundle", "--bundle", str(bundle),
         "--owner-review-file", str(review_file), "--skill-root", str(tmp_path / "skills")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _action_gate_packet(tmp_path: Path) -> dict[str, object]:
    source = "\n".join([
        "# Change Implementation Plan", "", "**Goal:** Answer after owner approval.", "",
        "**Architecture:** One owner action followed by a read-only task.", "",
        "**Tech Stack:** Python 3.11+ stdlib.", "", "**Spec:** `docs/change.md`", "",
        "## Global Constraints", "", "- Preserve the owner action gate.", "",
        "### Task 1: Approve", "", "**Files:**", "", "- [ ] **Step 1: Owner approves**",
        "  **Depends on:** none", "", "Record the owner's decision.", "",
        "### Task 2: Analyze", "", "**Files:**", "", "- [ ] **Step 1: Analyze**",
        "  **Depends on:** Task 1/Step 1", "", "Analyze only after approval.", "",
    ])
    defaults = {"executor_ids": ["claude", "codex"], "rounds": 2,
                "timeout": 120, "retries": 0, "minimum_success": 2}
    tasks = []
    for task_id, title, step, execution_class, depends_on in (
        ("approve", "Approve", "Task 1/Step 1", "orchestrator-action", []),
        ("later", "Analyze", "Task 2/Step 1", "read-only", ["approve"]),
    ):
        tasks.append({
            "id": task_id, "kind": "work", "parent_id": None, "title": title,
            "objective": "Record approval." if task_id == "approve" else "Analyze the question.",
            "source_step_ids": [step], "depends_on": depends_on,
            "execution_class": execution_class, "required_skills": [],
            "none_reason": "No specialist skill applies.", "owned_paths": [],
            "acceptance": ["The bounded task completes."], "checks": [],
            "provider_policy": None,
        })
    draft = {"schema_version": "fanout-draft-v1",
             "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
             "defaults": defaults, "tasks": tasks}
    ingress = {"schema_version": "fanout-bundle-ingress-v1", "quality_tier": "normal",
               "source_path": "docs/change.md", "source_markdown": source, "draft": draft}
    bundle = tmp_path / "action-ingress.json"
    bundle.write_bytes(fanout.canonical_json(ingress))
    review = tmp_path / "action-review.json"
    review.write_bytes(fanout.canonical_json({
        "schema_version": "fanout-owner-review-v1", "reviewer": "repository-owner",
        "binding_sha256": hashlib.sha256(fanout.canonical_json(ingress)).hexdigest(),
    }))
    skills = tmp_path / "skills"
    skills.mkdir()
    result = subprocess.run(
        [sys.executable, str(PLANNER), "from-bundle", "--bundle", str(bundle),
         "--owner-review-file", str(review), "--skill-root", str(skills)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_delivery_stages_executor_runtime_memory_and_gate(tmp_path: Path):
    """Direct copies and certification must close over the runtime they execute."""
    skillctl_path = ROOT / "components/skills/skillctl.py"
    spec = importlib.util.spec_from_file_location("_fanout_execute_skillctl", skillctl_path)
    assert spec and spec.loader
    skillctl = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = skillctl
    spec.loader.exec_module(skillctl)
    checks_path = ROOT / "scripts/lib/checks.py"
    spec = importlib.util.spec_from_file_location("_fanout_execute_checks", checks_path)
    assert spec and spec.loader
    checks = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = checks
    spec.loader.exec_module(checks)
    harness_path = ROOT / "scripts/eval_harness.py"
    spec = importlib.util.spec_from_file_location("_fanout_execute_eval_harness", harness_path)
    assert spec and spec.loader
    harness = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = harness
    spec.loader.exec_module(harness)

    config = skillctl.load_configuration(ROOT, tmp_path / "home", None)
    with skillctl.staged_skill_sources(config) as staged:
        bundled = staged["llm-fanout-execute"]
        for relative, source in (("shared/lib/fanout/__init__.py", "shared/lib/fanout/__init__.py"),
                                 ("components/memory/memory_exchange.py", "components/memory/memory_exchange.py")):
            assert (bundled / ".runtime" / relative).read_bytes() == (ROOT / source).read_bytes()
        assert (bundled / "scripts/execute.py").is_file()
        assert {path.relative_to(bundled).as_posix() for path in bundled.rglob("SKILL.md")} == {
            "SKILL.md", ".runtime/shared/skills/llm-fanout-plan/SKILL.md",
            ".runtime/shared/skills/llm-fanout-execute/SKILL.md",
        }
    source_paths = {path for path, _ in checks.source_manifest(ROOT, "llm-fanout-execute")}
    assert "shared/lib/fanout/execute.py" in source_paths
    assert "components/memory/memory_exchange.py" in source_paths
    assert "capabilities.toml" in source_paths
    assert str(ROOT / "tests/test_fanout_execute_skill.py") in harness.DETERMINISTIC_GATED["llm-fanout-execute"]
    assert harness.DETERMINISTIC_GATE_NAMES["llm-fanout-execute"] == "fanout-execute-contracts"


@pytest.mark.parametrize("skill,entry", (("llm-fanout-plan", "plan.py"),
                                        ("llm-fanout-execute", "execute.py")))
def test_delivered_cold_helpers_attest_checkout_closure_without_source(
    tmp_path: Path, skill: str, entry: str,
):
    spec = importlib.util.spec_from_file_location("_cold_delivery", ROOT / "components/skills/skillctl.py")
    assert spec and spec.loader
    delivery = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = delivery
    spec.loader.exec_module(delivery)
    original_config = delivery.load_configuration(ROOT, tmp_path / "home", None)
    origin = tmp_path / "origin"
    origin.mkdir()
    files = {ROOT / "capabilities.toml", original_config.house_source,
             *original_config.instruction_overlays.values()}
    sources = {*original_config.sources.values(),
               *(source for bundles in original_config.skill_bundles.values() for source in bundles.values())}
    for source in files | sources:
        destination = origin / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, destination)
    config = delivery.load_configuration(origin, tmp_path / "home", None)
    delivery.apply(config, expect=delivery.plan(config)[0], as_json=True)
    from fanout.container_source import release_source_binding
    expected = release_source_binding(ROOT).to_dict()
    origin.rename(tmp_path / "hidden-origin")
    installed = config.home / ".agents/skills" / skill
    probe = '''
import importlib, importlib.util, json, pathlib, sys
entry = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("cold_helper", entry)
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
runtime = helper._runtime()
source = importlib.import_module(runtime.__name__ + ".container_source")
binding = source.release_source_binding()
certification = importlib.import_module(runtime.__name__ + ".container_certification")
coordinator = importlib.import_module(runtime.__name__ + ".container_coordinator")
probe = importlib.import_module("scripts.fanout_container_certify")
evaluations = importlib.import_module("scripts.fanout_release_evaluations")
assert probe.SourceClosureError is source.SourceClosureError
assert probe.CertificationRejected is certification.CertificationRejected
assert evaluations.ReleaseSourceBinding is source.ReleaseSourceBinding
assert coordinator.run_coordinator_turn.__globals__["ArtifactStore"] is runtime.ArtifactStore
assert pathlib.Path(probe.__file__).is_relative_to(entry.parent.parent)
assert pathlib.Path(source.__file__).is_relative_to(entry.parent.parent)
if hasattr(helper, "_memory_modules"):
    controller, exchange = helper._memory_modules()
    assert pathlib.Path(controller.__file__).is_relative_to(entry.parent.parent / ".runtime/components/memory")
    assert not controller.installed_controller_root().is_relative_to(entry.parent.parent)
print(json.dumps(binding.to_dict(), sort_keys=True))
'''
    environment = {"PATH": os.environ["PATH"], "HOME": str(config.home),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-I", "-B", "-c", probe,
                             str(installed / "scripts" / entry)],
                            cwd=config.home, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
    assert not list(installed.rglob("__pycache__"))
    foreign = '''
from types import ModuleType
foreign = ModuleType("shared")
foreign.__path__ = ["/foreign/shared"]
sys.modules["shared"] = foreign
'''
    refused = subprocess.run([sys.executable, "-I", "-B", "-c",
                              "import sys\n" + foreign + probe,
                              str(installed / "scripts" / entry)],
                             cwd=config.home, env=environment, capture_output=True, text=True)
    assert refused.returncode != 0
    assert "foreign parent package" in refused.stderr
    for relative in ("shared/lib/fanout/container_boundary.py",
                     "components/memory/memoryctl.py", "mise.lock"):
        leaf = installed / ".runtime" / relative
        original = leaf.read_bytes()
        leaf.write_bytes(original + b"\n# source changed\n")
        changed = subprocess.run([sys.executable, "-I", "-B", "-c", probe,
                                  str(installed / "scripts" / entry)],
                                 cwd=config.home, env=environment, capture_output=True, text=True)
        assert changed.returncode == 0, changed.stderr
        assert json.loads(changed.stdout) != expected
        leaf.write_bytes(original)
    leaf = installed / ".runtime/components/fanout-container/relay.py"
    original = leaf.read_bytes()
    leaf.unlink()
    for symlink in (False, True):
        if symlink:
            leaf.symlink_to(tmp_path / "hidden-origin/components/fanout-container/relay.py")
        refused = subprocess.run([sys.executable, "-I", "-B", "-c", probe,
                                  str(installed / "scripts" / entry)],
                                 cwd=config.home, env=environment, capture_output=True, text=True)
        assert refused.returncode != 0
        assert "SourceClosureError" in refused.stderr
    leaf.unlink()
    leaf.write_bytes(original)


@pytest.mark.parametrize("skill,entry", (("llm-fanout-plan", "plan.py"),
                                        ("llm-fanout-execute", "execute.py")))
@pytest.mark.parametrize("relative", ("SKILL.md", "agents/openai.yaml", "scripts/{entry}"))
@pytest.mark.parametrize("canonical", (False, True))
def test_source_binding_refuses_changed_active_installed_entry(tmp_path: Path, relative: str,
                                                              skill: str, entry: str, canonical: bool):
    spec = importlib.util.spec_from_file_location("_active_delivery", ROOT / "components/skills/skillctl.py")
    assert spec and spec.loader
    delivery = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = delivery
    spec.loader.exec_module(delivery)
    config = delivery.load_configuration(ROOT, tmp_path / "home", None)
    from fanout.container_source import SourceClosureError, release_source_binding
    with delivery.staged_skill_sources(config) as staged:
        installed = staged[skill]
        active_root = installed / ".runtime/shared/skills" / skill if canonical else installed
        active = active_root / relative.format(entry=entry)
        active.write_bytes(active.read_bytes() + b"\n# changed active entry\n")
        with pytest.raises(SourceClosureError, match="active"):
            release_source_binding(installed)


@pytest.mark.parametrize("source_cli", (PLANNER, CLI))
def test_installed_helper_does_not_fall_back_to_unrelated_parent_runtime(tmp_path, source_cli):
    installed = tmp_path / "owner/skills" / source_cli.parent.parent.name
    (installed / "shared").mkdir(parents=True)
    (installed / "scripts").mkdir()
    shutil.copy2(source_cli, installed / "scripts" / source_cli.name)
    unrelated = installed.parent.parent / "lib/fanout"
    unrelated.mkdir(parents=True)
    (unrelated / "__init__.py").write_text("raise RuntimeError('unrelated runtime executed')\n")
    command = '''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("helper", sys.argv[1])
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
helper._runtime()
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", command,
                             str(installed / "scripts" / source_cli.name)],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0
    assert "bundled fanout runtime is missing" in result.stderr
    assert "unrelated runtime executed" not in result.stderr


@pytest.mark.parametrize("source_cli", (PLANNER, CLI))
@pytest.mark.parametrize("relative", (".runtime", ".runtime/shared", ".runtime/shared/lib",
                                      ".runtime/shared/lib/fanout",
                                      ".runtime/shared/lib/fanout/__init__.py",
                                      ".runtime/shared/lib/fanout/container_source.py",
                                      ".runtime/shared/lib/fanout/compiler.py"))
def test_cold_helper_refuses_linked_runtime_before_external_code(tmp_path, source_cli, relative):
    spec = importlib.util.spec_from_file_location("_import_boundary_delivery", ROOT / "components/skills/skillctl.py")
    delivery = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = delivery
    spec.loader.exec_module(delivery)
    config = delivery.load_configuration(ROOT, tmp_path / "home", None)
    marker = tmp_path / "external-imported"
    external = tmp_path / "external"
    shutil.copytree(ROOT / "shared/lib/fanout", external / ".runtime/shared/lib/fanout",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    leaf = external / (relative if relative.endswith(".py") else ".runtime/shared/lib/fanout/__init__.py")
    code = leaf.read_text()
    mark = f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
    if "from __future__ import annotations\n" in code:
        code = code.replace("from __future__ import annotations\n", "from __future__ import annotations\n" + mark, 1)
    else:
        code = mark + code
    leaf.write_text(code)
    with delivery.staged_skill_sources(config) as staged:
        installed = staged[source_cli.parent.parent.name]
        selected = installed / relative
        if selected.is_dir():
            shutil.rmtree(selected)
        else:
            selected.unlink()
        selected.symlink_to(external / relative, target_is_directory=not relative.endswith(".py"))
        command = '''
import importlib, importlib.util, sys
spec = importlib.util.spec_from_file_location("helper", sys.argv[1])
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
runtime = helper._runtime()
importlib.import_module(runtime.__name__ + ".container_source").release_source_binding()
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", command,
                                 str(installed / "scripts" / source_cli.name)],
                                cwd=tmp_path, capture_output=True, text=True)
    assert not marker.exists(), result.stderr
    assert result.returncode != 0
    assert "unsafe" in result.stderr


@pytest.mark.parametrize("relative", (".runtime", ".runtime/components", ".runtime/components/memory"))
def test_cold_memory_loader_refuses_linked_components_before_external_code(tmp_path, relative):
    installed = tmp_path / "installed"
    (installed / ".runtime/components/memory").mkdir(parents=True)
    (installed / "scripts").mkdir()
    shutil.copy2(CLI, installed / "scripts/execute.py")
    external = tmp_path / "external"
    shutil.copytree(ROOT / "components/memory", external / ".runtime/components/memory",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    marker = tmp_path / "external-memory-imported"
    ingress = external / ".runtime/components/memory/memory_ingress.py"
    ingress.write_text(ingress.read_text().replace("from __future__ import annotations\n",
        f"from __future__ import annotations\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n", 1))
    shutil.rmtree(installed / relative)
    (installed / relative).symlink_to(external / relative, target_is_directory=True)
    command = '''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("helper", sys.argv[1])
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
helper._memory_modules()
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", command,
                             str(installed / "scripts/execute.py")],
                            cwd=tmp_path, capture_output=True, text=True)
    assert not marker.exists(), result.stderr
    assert result.returncode != 0 and "unsafe" in result.stderr


@pytest.mark.parametrize("source_cli", (PLANNER, CLI))
@pytest.mark.parametrize("malformed", ("missing", "empty", "file", "broken-symlink"))
def test_hidden_runtime_refuses_missing_or_malformed_closure(tmp_path, source_cli, malformed):
    installed = tmp_path / "installed"
    (installed / "scripts").mkdir(parents=True)
    shutil.copy2(source_cli, installed / "scripts" / source_cli.name)
    hidden = installed / ".runtime"
    if malformed == "empty":
        hidden.mkdir()
    elif malformed == "file":
        hidden.write_text("not a runtime directory\n")
    elif malformed == "broken-symlink":
        hidden.symlink_to(tmp_path / "missing-runtime", target_is_directory=True)
    if malformed != "missing":
        fallback = installed / "lib/fanout"
        fallback.mkdir(parents=True)
        (fallback / "__init__.py").write_text("raise RuntimeError('flat fallback executed')\n")
    command = '''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("helper", sys.argv[1])
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
for operation in (helper._runtime, *([helper._memory_modules] if hasattr(helper, "_memory_modules") else [])):
    try:
        operation()
    except ValueError:
        continue
    raise AssertionError("malformed hidden closure was accepted")
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", command,
                             str(installed / "scripts" / source_cli.name)],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("source_cli", (PLANNER, CLI))
def test_cold_helper_preserves_host_parent_alias(tmp_path, source_cli):
    installed = tmp_path / "owner/installed"
    (installed / "scripts").mkdir(parents=True)
    shutil.copy2(source_cli, installed / "scripts" / source_cli.name)
    shutil.copytree(ROOT / "shared/lib/fanout", installed / "shared/lib/fanout",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    alias = tmp_path / "owner-alias"
    alias.symlink_to(installed.parent, target_is_directory=True)
    command = '''
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("helper", sys.argv[1])
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
runtime = helper._runtime()
assert pathlib.Path(runtime.__file__) == pathlib.Path(sys.argv[2])
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", command,
                             str(alias / "installed/scripts" / source_cli.name),
                             str(installed / "shared/lib/fanout/__init__.py")],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("source_cli", (PLANNER, CLI))
def test_broken_canonical_layout_cannot_execute_flat_fallback(tmp_path, source_cli):
    installed = tmp_path / "installed"
    (installed / "scripts").mkdir(parents=True)
    shutil.copy2(source_cli, installed / "scripts" / source_cli.name)
    (installed / "shared").symlink_to(tmp_path / "missing-shared", target_is_directory=True)
    legacy = installed / "lib/fanout"
    legacy.mkdir(parents=True)
    marker = tmp_path / "fallback-imported"
    (legacy / "__init__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n")
    command = '''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("helper", sys.argv[1])
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
helper._runtime()
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", command,
                             str(installed / "scripts" / source_cli.name)],
                            cwd=tmp_path, capture_output=True, text=True)
    assert not marker.exists(), result.stderr
    assert result.returncode != 0


def test_execute_receipt_stales_on_runtime_memory_or_bundle_mapping(tmp_path: Path):
    spec = importlib.util.spec_from_file_location("_fanout_execute_hash_checks",
                                                  ROOT / "scripts/lib/checks.py")
    assert spec and spec.loader
    checks = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = checks
    spec.loader.exec_module(checks)
    runtime = tmp_path / "shared/lib/fanout/execute.py"
    memory = tmp_path / "components/memory/memory_exchange.py"
    caps = tmp_path / "capabilities.toml"
    for source in (runtime, memory):
        source.parent.mkdir(parents=True)
        source.write_text("initial\n", encoding="utf-8")
    caps.write_text('[skill_delivery]\nskills = ["llm-fanout-execute"]\n', encoding="utf-8")
    before = checks.source_hash(tmp_path, "llm-fanout-execute")
    runtime.write_text("runtime changed\n", encoding="utf-8")
    after_runtime = checks.source_hash(tmp_path, "llm-fanout-execute")
    assert after_runtime != before
    memory.write_text("memory changed\n", encoding="utf-8")
    after_memory = checks.source_hash(tmp_path, "llm-fanout-execute")
    assert after_memory != after_runtime
    caps.write_text('[skill_delivery]\nskills = ["llm-fanout-execute"]\n'
                    '[skill_delivery.skill_bundles.llm-fanout-execute]\n'
                    '"lib/fanout" = "shared/lib/fanout"\n', encoding="utf-8")
    assert checks.source_hash(tmp_path, "llm-fanout-execute") != after_memory


def test_memory_loader_ignores_a_foreign_preloaded_controller(execute, monkeypatch):
    foreign = SimpleNamespace(__file__="/tmp/foreign/memoryctl.py")
    monkeypatch.setitem(sys.modules, "memoryctl", foreign)
    monkeypatch.setitem(sys.modules, "memory_exchange",
                        SimpleNamespace(__file__="/tmp/foreign/memory_exchange.py"))
    foreign_ingress = SimpleNamespace(__file__="/tmp/foreign/memory_ingress.py")
    monkeypatch.setitem(sys.modules, "memory_ingress", foreign_ingress)
    memoryctl, exchange = execute._memory_modules()
    assert Path(memoryctl.__file__).resolve() == (ROOT / "components/memory/memoryctl.py").resolve()
    assert Path(exchange.__file__).resolve() == (ROOT / "components/memory/memory_exchange.py").resolve()
    assert exchange.memoryctl is memoryctl
    assert Path(memoryctl.memory_ingress.__file__).resolve() == (ROOT / "components/memory/memory_ingress.py").resolve()
    assert sys.modules["memory_ingress"] is foreign_ingress


@pytest.mark.parametrize("changed", ("missing", "symlink"))
def test_memory_loader_refuses_missing_or_symlinked_owned_ingress(execute, tmp_path, monkeypatch, changed):
    skill = tmp_path / "skills/llm-fanout-execute"
    source = skill / "memory"
    source.mkdir(parents=True)
    for name in ("memoryctl.py", "memory_exchange.py"):
        shutil.copy2(ROOT / "components/memory" / name, source / name)
    if changed == "symlink":
        (source / "memory_ingress.py").symlink_to(ROOT / "components/memory/memory_ingress.py")
    monkeypatch.setattr(execute, "__file__", str(skill / "scripts/execute.py"))
    monkeypatch.setitem(sys.modules, "memory_ingress", SimpleNamespace(__file__="/tmp/foreign/memory_ingress.py"))
    with pytest.raises(ValueError, match="bundled memory controller"):
        execute._memory_modules()


def test_question_admission_recompiles_exact_source_draft_and_skills(execute, tmp_path):
    """A stale compiled answer plan must not reach provider preflight."""
    packet, resolver = _question_packet(tmp_path)
    compiled = execute.verify_admission(packet, fanout, resolver)
    assert compiled.plan.tasks[0].execution_class == "read-only"
    packet["draft"]["tasks"][0]["objective"] = "Changed after compilation"
    with pytest.raises(ValueError, match="compiled|source|draft"):
        execute.verify_admission(packet, fanout, resolver)


def test_planner_bundle_digest_matches_native_skill_grant_encoding(execute, tmp_path):
    packet, resolver = _write_packet(tmp_path)
    compiled = execute.verify_admission(packet, fanout, resolver)
    task_id = compiled.plan.tasks[0].id
    bundle = execute._task_skill_bundle(fanout, resolver, compiled.plan, task_id)
    admission = resolver.admit_plan(
        compiled.plan, task_id, seat_id="seat-1", provider="codex",
        session_id="admission-1",
    )
    admission = resolver.stage(admission, tmp_path / "stage")
    admission = resolver.verify_engine_delivery(admission, tuple(
        fanout.SkillLoadEvidence.engine(skill, admission) for skill in admission.skills
    ))
    assignment = fanout.SeatAssignment.from_admission(
        admission, skill_bundle=bundle,
        artifacts=fanout.ArtifactStore(tmp_path / "artifacts"),
    )
    assert len(json.loads(bundle)["skills"]) == 1
    assert assignment.skill_bundle_sha256 == hashlib.sha256(bundle).hexdigest()
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    controller = tmp_path / "controller"
    controller.mkdir(mode=0o700)
    denied = tmp_path / "denied"
    denied.mkdir(mode=0o700)
    request = fanout.ProviderRequest(
        "codex", "bounded task", cwd=workspace,
        task_id=task_id, seat_id=assignment.seat_id,
        staged_skill_root=assignment.staged_root,
        staged_skill_admission=assignment.admission,
        skill_bundle_sha256=assignment.skill_bundle_sha256,
        skill_delivery_sha256=assignment.delivery_evidence_sha256,
    )

    native_boundary = importlib.import_module("fanout.native_boundary")
    grant = native_boundary._staged_skill_grant(request, (denied,), workspace, controller)
    assert grant is not None
    assert grant["skill_bundle_sha256"] == assignment.skill_bundle_sha256


def test_oversized_valid_admission_fails_before_startup_escrow(execute, tmp_path):
    packet, resolver = _question_packet(tmp_path, executors=("claude", "codex"))
    source = packet["source_markdown"].replace(
        "Which invoice rounding order is correct?", "x" * (4 * 1024 * 1024),
    )
    assert len(source.encode("utf-8")) > 4 * 1024 * 1024
    packet["source_markdown"] = source
    packet["draft"]["source_sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
    source_path = packet["compiled"]["plan"]["source"]["path"]
    packet["compiled"] = fanout.compile_superpowers_plan(
        source.encode("utf-8"), source_path=source_path,
        draft_bytes=fanout.canonical_json(packet["draft"]), resolver=resolver,
    ).to_dict()
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="source.*limit|source.*large|source.*bounded"):
        execute.prepare_run(
            packet, repo_root=repo, run_root=run, authority_root=authority,
            private_root=private, skill_roots=(tmp_path / "skills",), budget=12,
            runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
            memory_preflight=lambda: True,
            provider_runner=lambda *args, **kwargs: pytest.fail("provider launched"),
        )
    assert not run.exists() and not authority.exists()
    assert list(private.iterdir()) == []


def test_write_bundle_requires_separate_exact_owner_review(execute, tmp_path):
    """Removing or changing review must revoke repository-write admission."""
    packet, resolver = _write_packet(tmp_path)
    assert execute.verify_admission(packet, fanout, resolver).plan.tasks[0].execution_class == "repo-write"
    packet["admission"]["owner_review"]["binding_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="owner review"):
        execute.verify_admission(packet, fanout, resolver)


def test_v2_owner_review_binds_target_registry_and_branches(execute, tmp_path):
    packet, resolver = _v2_write_packet(tmp_path)
    assert execute.verify_admission(packet, fanout, resolver).plan.schema_version == "v2"
    packet["draft"]["targets"][0]["branch_ref"] = "refs/heads/feat/TASK-123-other"
    with pytest.raises(ValueError, match="review|compiled"):
        execute.verify_admission(packet, fanout, resolver)


def test_v2_owner_review_binds_source_step_target(execute, tmp_path):
    packet, resolver = _v2_write_packet(tmp_path)
    packet["source_markdown"] = packet["source_markdown"].replace(
        "**Target:** address", "**Target:** booking", 1,
    )
    packet["draft"]["source_sha256"] = hashlib.sha256(packet["source_markdown"].encode()).hexdigest()
    with pytest.raises(fanout.CompilerError, match="target"):
        execute.verify_admission(packet, fanout, resolver)


def test_v2_admission_rejects_recorded_v1_parser_provenance(execute, tmp_path):
    packet, resolver = _v2_write_packet(tmp_path)
    packet["compiled"]["parser_version"] = fanout.PARSER_VERSION
    packet["compiled"]["plan"]["source"]["parser_version"] = fanout.PARSER_VERSION
    with pytest.raises(ValueError, match="compiled"):
        execute.verify_admission(packet, fanout, resolver)


def test_v2_prepare_refuses_before_durable_run_or_external_calls(execute, tmp_path):
    packet, _ = _v2_write_packet(tmp_path)
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    external_calls = []

    def preflight():
        external_calls.append("memory preflight")
        return True

    def memory_factory(artifacts, health):
        external_calls.append("memory factory")
        return _NeverMemory(artifacts, health)

    def provider_runner(*_args, **_kwargs):
        external_calls.append("provider")
        raise AssertionError("provider launched")

    with pytest.raises(ValueError, match="v2 admission requires target-bound preparation"):
        execute.prepare_run(
            packet, repo_root=repo, run_root=run, authority_root=authority,
            private_root=private, skill_roots=(tmp_path / "skills",), budget=18,
            runtime=fanout, registry=_registry(), memory_factory=memory_factory,
            memory_preflight=preflight, provider_runner=provider_runner,
        )
    assert not run.exists() and not authority.exists()
    assert list(private.iterdir()) == []
    assert external_calls == []


def test_bare_compiled_write_plan_is_not_an_admission(execute, tmp_path):
    packet, resolver = _write_packet(tmp_path)
    with pytest.raises(ValueError, match="admission"):
        execute.verify_admission(packet["compiled"], fanout, resolver)


def test_nested_executor_cannot_orchestrate(execute):
    for environment in ({"KHENRIX_NESTED_AGENT": "1"}, {"LLM_FANOUT_DEPTH": "1"}):
        with pytest.raises(ValueError, match="nested"):
            execute.reject_nested(environment)
    execute.reject_nested({})


class _HealthResponse:
    def __init__(self, status=200, body=b'{"status":"ok","initialized":true}',
                 content_type="application/json"):
        self.status = status
        self.body = body
        self.content_type = content_type

    def getheader(self, name):
        return self.content_type if name == "Content-Type" else None

    def read(self, length):
        return self.body[:length]


class _HealthConnection:
    def __init__(self, response):
        self.response = response
        self.requested = None
        self.closed = False

    def request(self, method, path, headers):
        self.requested = method, path, headers

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def _memory_fixture(tmp_path, response):
    controller = tmp_path / "memory_exchange.py"
    controller.write_text("# installed controller\n", encoding="utf-8")
    controller.chmod(0o700)
    memoryctl = SimpleNamespace(
        install_receipt_problem=lambda: None,
        health_document=lambda *, require_running: {"ok": True, "worker": "running", "gateway": "running"},
        installed_controller_root=lambda: tmp_path,
    )
    exchange = SimpleNamespace(
        _default_endpoint=lambda: "http://127.0.0.1:48175",
        _default_token_path=lambda: tmp_path / "token",
        _read_gateway_token=lambda path: "t" * 43,
        GatewayClient=lambda endpoint, token, timeout: (endpoint, token, timeout),
    )
    connection = _HealthConnection(response)
    return memoryctl, exchange, controller, connection


def test_memory_preflight_requires_authenticated_initialized_worker(execute, tmp_path):
    """A green process check with a 502 or uninitialized worker must not spend."""
    for response in (_HealthResponse(status=502),
                     _HealthResponse(body=b'{"status":"ok","initialized":false}'),
                     _HealthResponse(content_type="text/html")):
        memoryctl, exchange, _, connection = _memory_fixture(tmp_path, response)
        with pytest.raises(ValueError, match="memory"):
            execute.authenticated_memory_preflight(
                memoryctl, exchange, connection_factory=lambda host, port, timeout: connection,
            )
        assert connection.requested[0:2] == ("GET", "/api/health")
        assert connection.requested[2]["Authorization"] == "Bearer " + "t" * 43
        assert connection.closed
    memoryctl, exchange, controller, connection = _memory_fixture(tmp_path, _HealthResponse())
    path, digest = execute.authenticated_memory_preflight(
        memoryctl, exchange, connection_factory=lambda host, port, timeout: connection,
    )
    assert path == controller and digest == hashlib.sha256(controller.read_bytes()).hexdigest()


def test_private_descriptor_refuses_broad_modes_symlinks_and_tamper(execute, tmp_path):
    """The owner token must never be read from an unsafe or rolled-back descriptor."""
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    document = {"schema_version": "fanout-execute-private-v1", "run_id": "run-1",
                "owner_token": "s" * 43, "run_root": str(tmp_path / "run")}
    execute.save_private_descriptor(private_root, document)
    assert execute.load_private_descriptor(private_root) == document
    path = private_root / "run.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        execute.load_private_descriptor(private_root)
    path.chmod(0o600)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="descriptor"):
        execute.load_private_descriptor(private_root)
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="private"):
        execute.load_private_descriptor(private_root)


def test_failed_private_descriptor_sync_leaves_no_secret_temp(execute, tmp_path,
                                                              monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    document = {"schema_version": "fanout-execute-private-v1", "owner_token": "s" * 43}

    def fail_sync(_descriptor):
        raise OSError("injected sync failure")

    monkeypatch.setattr(execute.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="injected sync"):
        execute.save_private_descriptor(private, document)
    assert list(private.iterdir()) == []


def test_status_is_read_only_and_resume_requires_explicit_owner_capability(execute, tmp_path):
    """A missing private descriptor cannot be replaced by run-root contents."""
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="descriptor"):
        execute.load_private_descriptor(private_root)
    assert list(private_root.iterdir()) == []


def _git(repository: Path, *arguments: str) -> None:
    result = subprocess.run(["git", "-C", str(repository), *arguments],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def _git_output(repository: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repository), *arguments],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir(mode=0o700)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Fanout Test")
    _git(repository, "config", "user.email", "fanout@example.invalid")
    (repository / "source.txt").write_text("HEAD content\n", encoding="utf-8")
    _git(repository, "add", "source.txt")
    _git(repository, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "baseline")
    (repository / "source.txt").write_text("dirty caller content\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("untracked caller content\n", encoding="utf-8")
    return repository


def _v2_target_root(tmp_path: Path, *, origin: str = "address-service") -> Path:
    repository = _repository(tmp_path)
    (repository / "source.txt").write_text("HEAD content\n", encoding="utf-8")
    (repository / "untracked.txt").unlink()
    _git(repository, "remote", "add", "origin", f"https://github.com/example/{origin}.git")
    return repository


def test_v2_preflight_binds_target_before_provider_spend(execute, tmp_path):
    packet, _ = _v2_write_packet(tmp_path)
    root = _v2_target_root(tmp_path)
    calls = []
    prepared = execute.prepare_v2_run(
        packet, target_roots={"address": root}, skill_roots=(tmp_path / "skills",),
        provider_runner=lambda request: calls.append(request),
    )
    assert prepared.inputs.to_dict()["schema_version"] == "fanout-run-inputs-v3"
    assert prepared.inputs.targets["address"].spec.repository == "github.com/example/address-service"
    assert prepared.baselines["address"].digest == prepared.inputs.targets["address"].baseline_sha256
    assert not calls


def test_v2_wrong_remote_blocks_all_provider_spend(execute, tmp_path):
    packet, _ = _v2_write_packet(tmp_path)
    root = _v2_target_root(tmp_path, origin="wrong-repo")
    calls = []
    with pytest.raises(ValueError, match="address.*origin"):
        execute.prepare_v2_run(
            packet, target_roots={"address": root},
            skill_roots=(tmp_path / "skills",),
            provider_runner=lambda request: calls.append(request),
        )
    assert not calls


def test_v2_one_wrong_remote_blocks_both_target_preparations(execute, tmp_path):
    packet = _v2_two_repo_packet(tmp_path)
    address_dir = tmp_path / "address-work"
    booking_dir = tmp_path / "booking-work"
    address_dir.mkdir()
    booking_dir.mkdir()
    address = _v2_target_root(address_dir)
    booking = _v2_target_root(booking_dir, origin="wrong-repo")
    _git(address, "remote", "set-url", "origin", "https://github.com/example/address-service.git")
    calls = []
    with pytest.raises(ValueError, match="booking.*origin"):
        execute.prepare_v2_run(
            packet, target_roots={"address": address, "booking": booking},
            skill_roots=(tmp_path / "skills",),
            provider_runner=lambda request: calls.append(request),
        )
    assert not calls


def test_v2_same_ticket_distinct_repositories_bind_dirty_read_only_target(execute, tmp_path):
    packet = _v2_two_repo_packet(tmp_path)
    address_dir = tmp_path / "address-work"
    booking_dir = tmp_path / "booking-work"
    address_dir.mkdir()
    booking_dir.mkdir()
    address = _v2_target_root(address_dir)
    booking = _v2_target_root(booking_dir, origin="booking-service")
    (booking / "source.txt").write_text("read-only caller edit\n")
    prepared = execute.prepare_v2_run(
        packet, target_roots={"address": address, "booking": booking},
        skill_roots=(tmp_path / "skills",), provider_runner=lambda _: pytest.fail("provider launched"),
    )
    assert set(prepared.inputs.targets) == {"address", "booking"}
    assert prepared.inputs.targets["address"].spec.ticket_key == "TASK-123"
    assert prepared.inputs.targets["booking"].spec.ticket_key == "TASK-123"
    assert prepared.baselines["booking"].repository == booking


def test_v2_nested_target_roots_fail_before_capture(execute, tmp_path):
    packet = _v2_two_repo_packet(tmp_path)
    address_dir = tmp_path / "address-work"
    address_dir.mkdir()
    address = _v2_target_root(address_dir)
    nested = address / "booking-work"
    nested.mkdir()
    booking = _v2_target_root(nested, origin="booking-service")
    with pytest.raises(ValueError, match="disjoint"):
        execute.prepare_v2_run(
            packet, target_roots={"address": address, "booking": booking},
            skill_roots=(tmp_path / "skills",), provider_runner=lambda _: None,
        )


def test_v2_dirty_write_target_is_refused(execute, tmp_path):
    packet, _ = _v2_write_packet(tmp_path)
    root = _v2_target_root(tmp_path)
    (root / "source.txt").write_text("uncommitted user edit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="clean"):
        execute.prepare_v2_run(
            packet, target_roots={"address": root}, skill_roots=(tmp_path / "skills",),
            provider_runner=lambda _: None,
        )


def test_v2_write_target_rejects_recursive_head_alias_before_start(execute, tmp_path):
    packet, _ = _v2_write_packet(tmp_path)
    root = _v2_target_root(tmp_path)
    _git(root, "branch", "feat/TASK-123-address")
    _git(root, "symbolic-ref", "refs/heads/alias", "refs/heads/feat/TASK-123-address")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/alias")
    with pytest.raises(ValueError, match="^target address: target current HEAD: branch ref is symbolic$"):
        execute.prepare_v2_run(
            packet, target_roots={"address": root}, skill_roots=(tmp_path / "skills",),
            provider_runner=lambda _: pytest.fail("provider launched"),
        )


def test_v3_baseline_artifacts_are_target_prefixed_even_for_equal_bytes(execute, tmp_path):
    root = _v2_target_root(tmp_path)
    baseline = fanout.capture_repository_baseline(root)
    with fanout.ArtifactStore(tmp_path / "artifacts") as artifacts:
        address = execute._store_baseline(fanout, artifacts, baseline, target_id="address")
        booking = execute._store_baseline(fanout, artifacts, baseline, target_id="booking")
        assert address.path == "baseline/address/manifest.json"
        assert booking.path == "baseline/booking/manifest.json"
        assert address.digest != booking.digest
        assert execute._load_baseline(
            fanout, artifacts, execute._ref_document(address), root, target_id="address",
        ).digest == baseline.digest
        with pytest.raises(ValueError, match="target"):
            execute._load_baseline(
                fanout, artifacts, execute._ref_document(address), root, target_id="booking",
            )


def test_v3_startup_and_private_baseline_records_bind_target_manifest(execute, tmp_path):
    packet, _ = _v2_write_packet(tmp_path)
    root = _v2_target_root(tmp_path)
    prepared = execute.prepare_v2_run(
        packet, target_roots={"address": root}, skill_roots=(tmp_path / "skills",),
        provider_runner=lambda _: None,
    )
    runtime = execute._runtime()
    with (runtime.ArtifactStore(tmp_path / "startup-artifacts") as startup_artifacts,
          runtime.ArtifactStore(tmp_path / "run-artifacts") as artifacts):
        startup = execute._store_target_baseline_record(
            runtime, startup_artifacts, prepared.inputs, prepared.baselines,
            kind="startup", writable_ids={"address"},
        )
        private = execute._store_target_baseline_record(
            runtime, artifacts, prepared.inputs, prepared.baselines,
            kind="private", writable_ids={"address"},
        )
        assert startup["schema_version"] == "fanout-execute-startup-v2"
        assert private["schema_version"] == "fanout-execute-private-v2"
        assert startup["baseline_manifest_refs"]["address"]["path"] == "baseline/address/manifest.json"
        assert startup["writable_target_ids"] == ["address"]
        assert "baseline_manifest_ref" not in startup
        assert execute._load_target_baseline_record(runtime, artifacts, private)["address"].digest == (
            prepared.baselines["address"].digest
        )
        altered = {**private, "baseline_manifest_refs": {
            "address": {**private["baseline_manifest_refs"]["address"],
                        "path": "baseline/manifest.json"},
        }}
        with pytest.raises(ValueError, match="target"):
            execute._load_target_baseline_record(runtime, artifacts, altered)


def test_v3_storage_roots_are_disjoint_from_every_target(execute, tmp_path):
    address = tmp_path / "address"
    booking = tmp_path / "booking"
    address.mkdir()
    booking.mkdir()
    execute._separate_target_paths(
        {"address": address, "booking": booking},
        tmp_path / "run", tmp_path / "authority", tmp_path / "private",
    )
    with pytest.raises(ValueError, match="disjoint"):
        execute._separate_target_paths(
            {"address": address, "booking": booking},
            address / "run", tmp_path / "authority", tmp_path / "private",
        )
    with pytest.raises(ValueError, match="disjoint"):
        execute._separate_target_paths(
            {"address": address, "booking": address / "nested"},
            tmp_path / "run", tmp_path / "authority", tmp_path / "private",
        )


def test_v2_cli_start_requires_exact_target_root_flags(execute, tmp_path, capsys):
    packet = _v2_two_repo_packet(tmp_path)
    admission = tmp_path / "admission.json"
    admission.write_bytes(fanout.canonical_json(packet))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    common = ["start", "--admission-file", str(admission),
              "--private-root", str(private), "--run-root", str(tmp_path / "run"),
              "--authority-root", str(tmp_path / "authority"),
              "--skill-root", str(tmp_path / "skills"), "--max-turns", "12"]
    for extra, message in (
        (["--repo-root", str(tmp_path)], "repo-root"),
        (["--target-root", f"address={tmp_path}"], "booking"),
        (["--target-root", f"address={tmp_path}",
          "--target-root", f"booking={tmp_path}",
          "--repo-root", str(tmp_path)], "repo-root"),
        (["--target-root", f"address={tmp_path}",
          "--target-root", f"address={tmp_path}"], "duplicate"),
        (["--target-root", "address=relative/path",
          "--target-root", f"booking={tmp_path}"], "absolute"),
    ):
        assert execute.main(common + extra, runtime=fanout, registry=_registry()) == 2
        assert message in capsys.readouterr().err
    assert not (private / "startup.json").exists()
    assert not (tmp_path / "run").exists()


def test_v1_cli_start_rejects_target_root_without_upgrading(execute, tmp_path, capsys):
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    admission = tmp_path / "admission.json"
    admission.write_bytes(fanout.canonical_json(packet))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    args = ["start", "--admission-file", str(admission),
            "--private-root", str(private), "--run-root", str(tmp_path / "run"),
            "--authority-root", str(tmp_path / "authority"),
            "--skill-root", str(tmp_path / "skills"), "--max-turns", "30"]
    assert execute.main(args + ["--target-root", f"address={tmp_path}"],
                        runtime=fanout, registry=_registry()) == 2
    assert "requires --repo-root and rejects --target-root" in capsys.readouterr().err
    assert not (private / "startup.json").exists()


def test_v2_public_cli_start_is_dormant_and_resume_refuses_spend(execute, tmp_path, capsys, monkeypatch):
    packet, _ = _v2_write_packet(tmp_path)
    address = _v2_target_root(tmp_path)
    admission = tmp_path / "admission.json"
    admission.write_bytes(fanout.canonical_json(packet))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    calls = []
    def no_provider(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("provider launched")

    result = execute.main(
        ["start", "--admission-file", str(admission),
         "--private-root", str(private), "--run-root", str(tmp_path / "run"),
         "--authority-root", str(tmp_path / "authority"),
         "--skill-root", str(tmp_path / "skills"), "--max-turns", "6",
         "--target-root", f"address={address}"],
        runtime=fanout, registry=_registry(), provider_runner=no_provider,
    )
    captured = capsys.readouterr()
    assert result == 0, captured.err
    assert json.loads(captured.out)["provider_turns"] == 0
    assert execute.load_private_descriptor(private)["schema_version"] == "fanout-execute-private-v2"
    assert execute.main(["resume", "--private-root", str(private)],
                        runtime=fanout, registry=_registry(), provider_runner=no_provider) == 2
    assert "native seat boundary" in capsys.readouterr().err
    monkeypatch.setattr(execute, "_real_memory", lambda *args: pytest.fail("unapproved resume probed ambient memory"))
    with pytest.raises(ValueError, match="native seat boundary"):
        execute.resume_run(
            private, runtime=fanout, registry=_registry(), provider_runner=no_provider,
        )
    assert calls == []


def test_v2_start_escrows_targets_and_dormant_backends_without_spend(execute, tmp_path):
    packet = _v2_two_repo_packet(tmp_path)
    address_dir, booking_dir = tmp_path / "address-work", tmp_path / "booking-work"
    address_dir.mkdir()
    booking_dir.mkdir()
    address = _v2_target_root(address_dir)
    booking = _v2_target_root(booking_dir, origin="booking-service")
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)

    result = execute.start_v2_run(
        packet, target_roots={"address": address, "booking": booking},
        run_root=run, authority_root=authority, private_root=private,
        skill_roots=(tmp_path / "skills",), budget=12,
        runtime=fanout, registry=_registry(),
        provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"),
    )
    assert result["provider_turns"] == 0
    assert result["run_id"].startswith("fanout-")
    startup = execute.load_private_descriptor(private, name="startup.json")
    descriptor = execute.load_private_descriptor(private)
    assert startup["schema_version"] == "fanout-execute-startup-v2"
    assert descriptor["schema_version"] == "fanout-execute-private-v2"
    assert startup["run_id"] == descriptor["run_id"] == result["run_id"]
    assert set(startup["baseline_manifest_refs"]) == {"address", "booking"}
    assert set(descriptor["baseline_manifest_refs"]) == {"address", "booking"}
    assert startup["writable_target_ids"] == ["address"]
    assert descriptor["writable_target_ids"] == ["address"]
    owner = fanout.OwnerCapability.from_token(descriptor["owner_token"])
    with (fanout.FileSchedulerBackend.resume(run, run_id=result["run_id"], owner=owner) as scheduler,
          fanout.FileExecutionBackend.resume(run, run_id=result["run_id"], owner=owner) as execution):
        assert scheduler.read() is None
        assert execution.read() is None
    assert not (run / "slots").exists()


def test_v2_repeated_plan_uses_distinct_guest_sockets_and_cold_reopens_exact_seats(
    execute, tmp_path,
):
    from fanout.container_runtime import seat_socket_path

    packet, _ = _v2_write_packet(tmp_path)
    address = _v2_target_root(tmp_path)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: False,
                  provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"))
    sockets = []
    for index in range(2):
        storage = tmp_path / f"execution-{index}"
        storage.mkdir()
        private = storage / "private"
        private.mkdir(mode=0o700)
        execute.start_v2_run(
            packet, target_roots={"address": address}, run_root=storage / "run",
            authority_root=storage / "authority", private_root=private,
            skill_roots=(tmp_path / "skills",), budget=6,
            runtime=fanout, registry=_registry(),
        )
        with execute.open_run(private, **common) as opened:
            seats = tuple(seat.seat_id for seat in opened.service.preparations["change"].seats)
        with execute.open_run(private, **common) as reopened:
            assert tuple(seat.seat_id for seat in reopened.service.preparations["change"].seats) == seats
        assert len(set(seats)) == 3
        sockets.append({seat_socket_path(seat) for seat in seats})
    assert sockets[0].isdisjoint(sockets[1])


def _v2_cold_status_fixture(execute, tmp_path, *, writer_check=None):
    packet = _v2_two_repo_packet(tmp_path, writer_check=writer_check)
    address_dir, booking_dir = tmp_path / "address-work", tmp_path / "booking-work"
    address_dir.mkdir()
    booking_dir.mkdir()
    address = _v2_target_root(address_dir)
    booking = _v2_target_root(booking_dir, origin="booking-service")
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    execute.start_v2_run(
        packet, target_roots={"address": address, "booking": booking},
        run_root=run, authority_root=authority, private_root=private,
        skill_roots=(tmp_path / "skills",), budget=12,
        runtime=fanout, registry=_registry(),
        provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"),
    )
    return private, run, authority, address, booking


def test_v2_cli_cold_status_reads_two_dormant_targets_without_repair(execute, tmp_path, capsys):
    private, run, authority, _address, _booking = _v2_cold_status_fixture(execute, tmp_path)
    before = tuple(sorted(
        (str(path.relative_to(tmp_path)), path.read_bytes())
        for root in (private, run, authority) for path in root.rglob("*") if path.is_file()
    ))
    result = execute.main(
        ["status", "--private-root", str(private)], runtime=fanout,
        registry=_registry(), provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"),
    )
    captured = capsys.readouterr()
    assert result == 0, captured.err
    assert json.loads(captured.out)["target_states"] == {
        "address": "pending", "booking": "pending",
    }
    after = tuple(sorted(
        (str(path.relative_to(tmp_path)), path.read_bytes())
        for root in (private, run, authority) for path in root.rglob("*") if path.is_file()
    ))
    assert after == before


def test_v2_cli_cold_status_refuses_tampered_target_manifest(execute, tmp_path, capsys):
    private, run, _authority, _address, _booking = _v2_cold_status_fixture(execute, tmp_path)
    manifest = run / "artifacts" / "baseline" / "address" / "manifest.json"
    manifest.write_bytes(b"tampered")
    assert execute.main(
        ["status", "--private-root", str(private)], runtime=fanout, registry=_registry(),
    ) == 2
    assert "artifact digest mismatch" in capsys.readouterr().err


def test_v2_cli_cold_status_refuses_resigned_target_registry(execute, tmp_path, capsys):
    private, _run, _authority, _address, _booking = _v2_cold_status_fixture(execute, tmp_path)
    descriptor = execute.load_private_descriptor(private)
    descriptor["writable_target_ids"] = []
    envelope = {"schema_version": "fanout-private-envelope-v1",
                "sha256": execute._sha256(execute._canonical(descriptor)),
                "document": descriptor}
    (private / "run.json").write_bytes(execute._canonical(envelope))
    assert execute.main(["status", "--private-root", str(private)], runtime=fanout,
                        registry=_registry()) == 2
    assert "exact startup escrow" in capsys.readouterr().err


def test_v2_cli_cold_status_checks_journal_before_live_targets(execute, tmp_path, capsys):
    private, run, _authority, _address, booking = _v2_cold_status_fixture(execute, tmp_path)
    _git(booking, "switch", "-q", "-c", "other", _git_output(booking, "rev-parse", "HEAD"))
    (run / "inputs.json").write_bytes(b"{}\n")
    assert execute.main(["status", "--private-root", str(private)], runtime=fanout,
                        registry=_registry()) == 2
    error = capsys.readouterr().err.lower()
    assert "inputs" in error and "original baseline changed" not in error


def test_v2_cli_cold_status_survives_runtime_and_profile_drift_only(execute, tmp_path,
                                                                     monkeypatch, capsys):
    private, _run, _authority, _address, _booking = _v2_cold_status_fixture(execute, tmp_path)
    monkeypatch.setattr(execute, "_runtime_digest", lambda _runtime: "0" * 64)
    drifted = fanout.ProviderRegistry.default(version_probe=lambda _executor: "0.0.0")
    assert execute.main(
        ["status", "--private-root", str(private)], runtime=fanout, registry=drifted,
    ) == 0, capsys.readouterr().err
    assert execute.main(
        ["resume", "--private-root", str(private)], runtime=fanout, registry=drifted,
    ) == 2
    assert "native seat boundary" in capsys.readouterr().err


def _v2_cold_delivered_fixture(execute, tmp_path):
    check = {"argv": ["/bin/test", "-e", "source.txt"], "cwd": "",
             "env_allowlist": [], "timeout": 10, "accepted_exit_codes": [0],
             "expected_artifacts": []}
    private, run, authority, address, booking = _v2_cold_status_fixture(
        execute, tmp_path, writer_check=check,
    )
    descriptor = execute.load_private_descriptor(private)
    inputs = fanout.RunInputs.from_dict(descriptor["inputs"])
    plan = fanout.plan.FanoutPlanV2.from_dict(descriptor["admission_packet"]["compiled"]["plan"])
    owner = fanout.OwnerCapability.from_token(descriptor["owner_token"])
    anchor = fanout.LocalAnchorAuthority(
        authority, run_root=run, repo_root=address, target_bindings=inputs.targets,
    )
    journal = fanout.RunJournal.resume(run, inputs, owner, anchor_store=anchor)
    controller = fanout.resume_lifecycle_controller(
        run / "lifecycle", fanout.LifecycleCapability(descriptor["controller_token"]),
    )
    handover = importlib.import_module("fanout.branch_handover")
    try:
        with (fanout.ArtifactStore.open_existing(run / "artifacts") as artifacts,
              fanout.FileSchedulerBackend.resume(run, run_id=inputs.run_id, owner=owner) as backend):
            baseline = execute._load_baseline(
                fanout, artifacts, descriptor["baseline_manifest_refs"]["address"],
                address, target_id="address",
            )
            scheduler = fanout.Scheduler.create(
                plan, inputs, backend, artifacts, owner=owner, anchor_store=anchor,
                journal=journal, lifecycle_controller=controller,
                repository_baselines={"address": baseline},
            )
            scheduler.schedule_ready(owner=owner)
            scheduler.mark_active("change", owner=owner)
            scheduler.begin_reconciliation("change", owner=owner)
            candidate = fanout.CandidateBundle(baseline.digest, (), ())
            issued = fanout.issue_target_candidate(
                candidate, task_id="change", plan=plan, inputs=inputs,
                store=artifacts, controller=controller,
            )
            verified, verification = fanout.verify_target_candidate(
                issued, baseline=baseline, plan=plan, inputs=inputs,
                store=artifacts, controller=controller,
            )
            assert verification.valid
            result = artifacts.write_bytes("results/change/candidate.json", candidate.manifest_bytes)
            scheduler.complete_reconciliation(
                "change", scheduler.result_receipt("change", result), owner=owner,
            )
            prepared = handover.prepare_branch_handover(
                inputs.targets["address"], issued, verified,
                plan=plan, inputs=inputs, artifacts=artifacts, scheduler=scheduler,
                controller=controller, journal=journal, owner=owner, task_id="change",
            )
            terminal = handover.deliver_branch_candidate(
                prepared, plan=plan, inputs=inputs, artifacts=artifacts,
                scheduler=scheduler, controller=controller, journal=journal, owner=owner,
            )
            assert isinstance(terminal, fanout.HandoverTerminalV2)
    finally:
        journal.close()
    return private, run, authority, address, booking


def test_v2_cli_cold_status_authenticates_delivered_and_original_targets(execute, tmp_path,
                                                                          capsys):
    private, run, authority, address, booking = _v2_cold_delivered_fixture(execute, tmp_path)
    original_booking = _git_output(booking, "rev-parse", "HEAD")
    delivered_address = _git_output(address, "rev-parse", "HEAD")
    assert _git_output(address, "symbolic-ref", "--no-recurse", "HEAD") == (
        "refs/heads/feat/TASK-123-address"
    )
    before = tuple(sorted(
        (str(path.relative_to(tmp_path)), path.read_bytes())
        for root in (private, run, authority) for path in root.rglob("*") if path.is_file()
    ))
    result = execute.main(
        ["status", "--private-root", str(private)], runtime=fanout,
        registry=_registry(), provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"),
    )
    captured = capsys.readouterr()
    assert result == 0, captured.err
    assert json.loads(captured.out)["target_states"] == {
        "address": "delivered", "booking": "pending",
    }
    assert _git_output(address, "rev-parse", "HEAD") == delivered_address
    assert _git_output(booking, "rev-parse", "HEAD") == original_booking
    after = tuple(sorted(
        (str(path.relative_to(tmp_path)), path.read_bytes())
        for root in (private, run, authority) for path in root.rglob("*") if path.is_file()
    ))
    assert after == before


def test_v2_cli_cold_status_refuses_changed_delivered_ref(execute, tmp_path, capsys):
    private, _run, _authority, address, _booking = _v2_cold_delivered_fixture(execute, tmp_path)
    _git(address, "update-ref", "refs/heads/feat/TASK-123-address",
         _git_output(address, "rev-parse", "main"))
    assert execute.main(["status", "--private-root", str(private)], runtime=fanout,
                        registry=_registry()) == 2
    assert "handover" in capsys.readouterr().err.lower()


def test_v2_cli_cold_status_refuses_changed_undelivered_baseline(execute, tmp_path,
                                                                   capsys):
    private, _run, _authority, _address, booking = _v2_cold_delivered_fixture(execute, tmp_path)
    _git(booking, "switch", "-q", "-c", "other", _git_output(booking, "rev-parse", "HEAD"))
    assert execute.main(["status", "--private-root", str(private)], runtime=fanout,
                        registry=_registry()) == 2
    assert "target booking: original baseline changed" in capsys.readouterr().err


def _v2_cold_task_state_fixture(execute, tmp_path, *, task_id: str, complete: bool):
    private, run, authority, address, booking = _v2_cold_status_fixture(execute, tmp_path)
    descriptor = execute.load_private_descriptor(private)
    inputs = fanout.RunInputs.from_dict(descriptor["inputs"])
    plan = fanout.plan.FanoutPlanV2.from_dict(descriptor["admission_packet"]["compiled"]["plan"])
    owner = fanout.OwnerCapability.from_token(descriptor["owner_token"])
    anchor = fanout.LocalAnchorAuthority(
        authority, run_root=run, repo_root=address, target_bindings=inputs.targets,
    )
    journal = fanout.RunJournal.resume(run, inputs, owner, anchor_store=anchor)
    controller = fanout.resume_lifecycle_controller(
        run / "lifecycle", fanout.LifecycleCapability(descriptor["controller_token"]),
    )
    try:
        with (fanout.ArtifactStore.open_existing(run / "artifacts") as artifacts,
              fanout.FileSchedulerBackend.resume(run, run_id=inputs.run_id, owner=owner) as backend):
            scheduler = fanout.Scheduler.create(
                plan, inputs, backend, artifacts, owner=owner, anchor_store=anchor,
                journal=journal, lifecycle_controller=controller,
            )
            scheduler.schedule_ready(owner=owner)
            scheduler.mark_active(task_id, owner=owner)
            if complete:
                scheduler.begin_reconciliation(task_id, owner=owner)
                result = artifacts.write_bytes(f"results/{task_id}/cold-status.json", b"{}\n")
                scheduler.complete_reconciliation(
                    task_id, scheduler.result_receipt(task_id, result), owner=owner,
                )
            else:
                scheduler.fail_task(task_id, "test failure", owner=owner)
    finally:
        journal.close()
    return private, address, booking


def test_v2_cli_cold_status_checks_blocked_undelivered_target_baseline(execute, tmp_path,
                                                                         capsys):
    private, address, _booking = _v2_cold_task_state_fixture(
        execute, tmp_path, task_id="change", complete=False,
    )
    (address / "source.txt").write_text("changed after failure\n", encoding="utf-8")
    _git(address, "add", "source.txt")
    _git(address, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "changed baseline")
    assert execute.main(["status", "--private-root", str(private)], runtime=fanout,
                        registry=_registry()) == 2
    assert "target address: original baseline changed" in capsys.readouterr().err


def test_v2_cli_cold_status_checks_read_only_complete_target_baseline(execute, tmp_path,
                                                                        capsys):
    private, _address, booking = _v2_cold_task_state_fixture(
        execute, tmp_path, task_id="inspect-booking", complete=True,
    )
    (booking / "source.txt").write_text("changed after read-only work\n", encoding="utf-8")
    _git(booking, "add", "source.txt")
    _git(booking, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "changed baseline")
    assert execute.main(["status", "--private-root", str(private)], runtime=fanout,
                        registry=_registry()) == 2
    assert "target booking: original baseline changed" in capsys.readouterr().err


def _v2_cli_handover_fixture(execute, tmp_path, *, new_parent=False):
    check = {"argv": ["/bin/test", "-e", "source.txt"], "cwd": "",
             "env_allowlist": [], "timeout": 10, "accepted_exit_codes": [0],
             "expected_artifacts": []}
    private, run, authority, address, _booking = _v2_cold_status_fixture(
        execute, tmp_path, writer_check=check,
    )
    descriptor = execute.load_private_descriptor(private)
    inputs = fanout.RunInputs.from_dict(descriptor["inputs"])
    plan = fanout.plan.FanoutPlanV2.from_dict(descriptor["admission_packet"]["compiled"]["plan"])
    owner = fanout.OwnerCapability.from_token(descriptor["owner_token"])
    anchor = fanout.LocalAnchorAuthority(
        authority, run_root=run, repo_root=address, target_bindings=inputs.targets,
    )
    journal = fanout.RunJournal.resume(run, inputs, owner, anchor_store=anchor)
    controller = fanout.resume_lifecycle_controller(
        run / "lifecycle", fanout.LifecycleCapability(descriptor["controller_token"]),
    )
    try:
        with (fanout.ArtifactStore.open_existing(run / "artifacts") as artifacts,
              fanout.FileSchedulerBackend.resume(run, run_id=inputs.run_id, owner=owner) as backend):
            scheduler = fanout.Scheduler.create(
                plan, inputs, backend, artifacts, owner=owner, anchor_store=anchor,
                journal=journal, lifecycle_controller=controller,
            )
            scheduler.schedule_ready(owner=owner)
            scheduler.mark_active("change", owner=owner)
            scheduler.begin_reconciliation("change", owner=owner)
            baseline = execute._load_baseline(
                fanout, artifacts, descriptor["baseline_manifest_refs"]["address"],
                address, target_id="address",
            )
            entries = (fanout.CandidateEntry("src", "directory", 0o755, b""),
                       fanout.CandidateEntry("src/change.py", "file", 0o644, b"owned change\n")) if new_parent else ()
            candidate = fanout.CandidateBundle(baseline.digest, entries, ())
            issued = fanout.issue_target_candidate(
                candidate, task_id="change", plan=plan, inputs=inputs,
                store=artifacts, controller=controller, baseline=baseline,
            )
            verified, receipt = fanout.verify_target_candidate(
                issued, baseline=baseline, plan=plan, inputs=inputs,
                store=artifacts, controller=controller,
            )
            assert receipt.valid
            result = artifacts.write_bytes("results/change/owner-candidate.json",
                                           candidate.manifest_bytes)
            scheduler.complete_reconciliation(
                "change", scheduler.result_receipt("change", result), owner=owner,
            )
            source = scheduler.handover_source_for("change")
    finally:
        journal.close()
    candidate_ref = tmp_path / "candidate-ref.json"
    verification_ref = tmp_path / "verification-ref.json"
    for path, ref in ((candidate_ref, issued.payload), (verification_ref, verified.payload)):
        path.write_bytes(fanout.canonical_json({"path": ref.path, "digest": ref.digest,
                                                "size": ref.size}))
    evidence = {
        "target_id": "address",
        "candidate": {"path": issued.payload.path, "digest": issued.payload.digest,
                      "size": issued.payload.size},
        "verification": {"path": verified.payload.path, "digest": verified.payload.digest,
                         "size": verified.payload.size},
        "target_binding": inputs.targets["address"].to_dict(),
        "settled_source": {
            "run_id": source.run_id, "task_id": source.task_id,
            "plan_revision": source.plan_revision, "plan_sha256": source.plan_sha256,
            "inputs_digest": source.inputs_digest,
            "artifact": {"path": source.artifact.path, "digest": source.artifact.digest,
                         "size": source.artifact.size},
        },
    }
    return private, run, authority, address, candidate_ref, verification_ref, evidence


def _v2_owner_approval(tmp_path, *, action, run_id, task_id, evidence):
    approval = tmp_path / f"{action}-approval.json"
    approval.write_bytes(fanout.canonical_json({
        "schema_version": "fanout-owner-action-v1", "action": action,
        "run_id": run_id, "task_id": task_id, "reviewer": "repository-owner",
        "evidence_sha256": hashlib.sha256(fanout.canonical_json(evidence)).hexdigest(),
    }))
    return approval


@pytest.mark.parametrize("new_parent", (False, True))
def test_v2_cli_handover_delivers_with_exact_same_process_owner_approval(execute, tmp_path,
                                                                       capsys, new_parent):
    private, _run, _authority, address, candidate_ref, verification_ref, evidence = (
        _v2_cli_handover_fixture(execute, tmp_path, new_parent=new_parent)
    )
    run_id = execute.load_private_descriptor(private)["run_id"]
    approval = _v2_owner_approval(
        tmp_path, action="handover", run_id=run_id, task_id="change", evidence=evidence,
    )
    result = execute.main([
        "handover", "--private-root", str(private), "--task-id", "change",
        "--candidate-ref-file", str(candidate_ref),
        "--verification-ref-file", str(verification_ref),
        "--owner-approval-file", str(approval),
    ], runtime=fanout, registry=_registry(),
       provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"))
    output = capsys.readouterr()
    assert result == 0, output.err
    assert json.loads(output.out)["status"] == "committed"
    assert _git_output(address, "symbolic-ref", "--no-recurse", "HEAD") == (
        "refs/heads/feat/TASK-123-address"
    )
    if new_parent:
        assert (address / "src/change.py").read_bytes() == b"owned change\n"
    assert execute.main(["status", "--private-root", str(private)], runtime=fanout,
                        registry=_registry()) == 0
    assert json.loads(capsys.readouterr().out)["target_states"]["address"] == "delivered"


def test_v2_cli_handover_rejects_changed_approval_before_intent(execute, tmp_path, capsys):
    private, run, authority, address, candidate_ref, verification_ref, evidence = (
        _v2_cli_handover_fixture(execute, tmp_path)
    )
    run_id = execute.load_private_descriptor(private)["run_id"]
    changed_evidence = (
        {**evidence, "target_id": "booking"},
        {**evidence, "candidate": {**evidence["candidate"], "digest": "0" * 64}},
        {**evidence, "verification": {**evidence["verification"], "digest": "0" * 64}},
        {**evidence, "target_binding": {**evidence["target_binding"], "base_oid": "0" * 40}},
        {**evidence, "settled_source": {**evidence["settled_source"],
                                         "inputs_digest": "0" * 64}},
    )
    for changed in changed_evidence:
        approval = _v2_owner_approval(
            tmp_path, action="handover", run_id=run_id, task_id="change", evidence=changed,
        )
        assert execute.main([
            "handover", "--private-root", str(private), "--task-id", "change",
            "--candidate-ref-file", str(candidate_ref),
            "--verification-ref-file", str(verification_ref),
            "--owner-approval-file", str(approval),
        ], runtime=fanout, registry=_registry()) == 2
        assert "owner action approval differs" in capsys.readouterr().err
    descriptor = execute.load_private_descriptor(private)
    inputs = fanout.RunInputs.from_dict(descriptor["inputs"])
    owner = fanout.OwnerCapability.from_token(descriptor["owner_token"])
    anchor = fanout.LocalAnchorAuthority(
        authority, run_root=run, repo_root=address, target_bindings=inputs.targets,
    )
    inspection = fanout.RunJournal.inspect(run, inputs, owner, anchor_store=anchor)
    assert not inspection.state.branch_handovers
    assert _git_output(address, "symbolic-ref", "--no-recurse", "HEAD") == "refs/heads/main"


@pytest.mark.parametrize("checkpoint", ("after-ref-cas", "after-journal-terminal"))
@pytest.mark.parametrize("new_parent", (False, True))
def test_v2_cli_recover_handover_uses_only_authenticated_intent(execute, tmp_path,
                                                                  monkeypatch, capsys,
                                                                  checkpoint, new_parent):
    private, run, authority, address, candidate_ref, verification_ref, evidence = (
        _v2_cli_handover_fixture(execute, tmp_path, new_parent=new_parent)
    )
    descriptor = execute.load_private_descriptor(private)
    approval = _v2_owner_approval(
        tmp_path, action="handover", run_id=descriptor["run_id"],
        task_id="change", evidence=evidence,
    )
    handover = importlib.import_module("fanout.branch_handover")

    def interrupt(phase):
        if phase == checkpoint:
            raise RuntimeError("interrupted handover")

    monkeypatch.setattr(handover, "_checkpoint", interrupt)
    assert execute.main([
        "handover", "--private-root", str(private), "--task-id", "change",
        "--candidate-ref-file", str(candidate_ref),
        "--verification-ref-file", str(verification_ref),
        "--owner-approval-file", str(approval),
    ], runtime=fanout, registry=_registry()) == 2
    assert "exact recovery" in capsys.readouterr().err
    if checkpoint == "after-ref-cas":
        assert execute.main(["status", "--private-root", str(private)],
                            runtime=fanout, registry=_registry()) == 2
        assert "original baseline changed" in capsys.readouterr().err
    monkeypatch.setattr(handover, "_checkpoint", lambda _phase: None)
    inputs = fanout.RunInputs.from_dict(descriptor["inputs"])
    owner = fanout.OwnerCapability.from_token(descriptor["owner_token"])
    anchor = fanout.LocalAnchorAuthority.inspect(
        authority, run_root=run, repo_root=address, target_bindings=inputs.targets,
    )
    inspected = fanout.RunJournal.inspect(run, inputs, owner, anchor_store=anchor)
    intent, terminal = inspected.state.branch_handovers["change"]
    assert (terminal is not None) == (checkpoint == "after-journal-terminal")
    association = inspected.state.branch_records["change"]["association"]
    recovery_evidence = {
        "intent": intent.to_dict(),
        "association": {"name": association[0], "digest": association[1]},
    }
    recovery_approval = _v2_owner_approval(
        tmp_path, action="recover-handover", run_id=inputs.run_id,
        task_id="change", evidence=recovery_evidence,
    )
    result = execute.main([
        "recover-handover", "--private-root", str(private),
        "--task-id", "change", "--owner-approval-file", str(recovery_approval),
    ], runtime=fanout, registry=_registry(),
       provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"))
    output = capsys.readouterr()
    assert result == 0, output.err
    assert json.loads(output.out)["status"] == "committed"
    assert _git_output(address, "symbolic-ref", "--no-recurse", "HEAD") == (
        "refs/heads/feat/TASK-123-address"
    )
    if new_parent:
        assert (address / "src/change.py").read_bytes() == b"owned change\n"
    assert execute.main([
        "recover-handover", "--private-root", str(private), "--task-id", "change",
        "--candidate-ref-file", str(candidate_ref),
        "--transaction-root", str(run / "caller-transaction"),
        "--owner-approval-file", str(recovery_approval),
    ], runtime=fanout, registry=_registry()) == 2
    assert "forbid" in capsys.readouterr().err.lower()


def test_v2_cli_recover_handover_refuses_pending_scheduler_without_repair(
    execute, tmp_path, monkeypatch, capsys,
):
    private, run, authority, address, candidate_ref, verification_ref, evidence = (
        _v2_cli_handover_fixture(execute, tmp_path)
    )
    descriptor = execute.load_private_descriptor(private)
    approval = _v2_owner_approval(
        tmp_path, action="handover", run_id=descriptor["run_id"],
        task_id="change", evidence=evidence,
    )
    handover = importlib.import_module("fanout.branch_handover")
    with monkeypatch.context() as injected:
        def interrupt(phase):
            if phase == "after-association":
                raise RuntimeError("interrupted after handover association")
        injected.setattr(handover, "_checkpoint", interrupt)
        assert execute.main([
            "handover", "--private-root", str(private), "--task-id", "change",
            "--candidate-ref-file", str(candidate_ref),
            "--verification-ref-file", str(verification_ref),
            "--owner-approval-file", str(approval),
        ], runtime=fanout, registry=_registry()) == 2
        assert "interrupted after handover association" in capsys.readouterr().err
    inputs = fanout.RunInputs.from_dict(descriptor["inputs"])
    plan = fanout.plan.FanoutPlanV2.from_dict(descriptor["admission_packet"]["compiled"]["plan"])
    owner = fanout.OwnerCapability.from_token(descriptor["owner_token"])
    anchor = fanout.LocalAnchorAuthority(
        authority, run_root=run, repo_root=address, target_bindings=inputs.targets,
    )
    controller = fanout.resume_lifecycle_controller(
        run / "lifecycle", fanout.LifecycleCapability(descriptor["controller_token"]),
    )
    scheduler_module = importlib.import_module("fanout.scheduler_authority")
    with (fanout.RunJournal.resume(run, inputs, owner, anchor_store=anchor) as journal,
          fanout.ArtifactStore.open_existing(run / "artifacts") as artifacts,
          fanout.FileSchedulerBackend.resume(run, run_id=inputs.run_id, owner=owner) as backend):
        scheduler = fanout.Scheduler.resume(
            plan, inputs, backend, artifacts, owner=owner, anchor_store=anchor,
            journal=journal, lifecycle_controller=controller,
        )
        with monkeypatch.context() as injected:
            def interrupt(*_args, **_kwargs):
                raise RuntimeError("interrupted after pending scheduler authority")
            injected.setattr(scheduler_module, "_commit_backend", interrupt)
            with pytest.raises(RuntimeError, match="pending scheduler authority"):
                scheduler.mark_active("inspect-booking", owner=owner)
    before = tuple(sorted(
        (str(path.relative_to(tmp_path)), path.read_bytes())
        for root in (run, authority) for path in root.rglob("*") if path.is_file()
    ))
    approval = _v2_owner_approval(
        tmp_path, action="recover-handover", run_id=inputs.run_id,
        task_id="change", evidence={},
    )
    assert execute.main([
        "recover-handover", "--private-root", str(private),
        "--task-id", "change", "--owner-approval-file", str(approval),
    ], runtime=fanout, registry=_registry()) == 2
    assert "committed authority binding" in capsys.readouterr().err.lower()
    after = tuple(sorted(
        (str(path.relative_to(tmp_path)), path.read_bytes())
        for root in (run, authority) for path in root.rglob("*") if path.is_file()
    ))
    assert after == before
    with (fanout.RunJournal.resume(run, inputs, owner, anchor_store=anchor) as journal,
          fanout.ArtifactStore.open_existing(run / "artifacts") as artifacts,
          fanout.FileSchedulerBackend.resume(run, run_id=inputs.run_id, owner=owner) as backend):
        with pytest.raises(fanout.SchedulerStateError, match="pending"):
            fanout.Scheduler.resume_exact_v2(
                plan, inputs, backend, artifacts, owner=owner, anchor_store=anchor,
                journal=journal, lifecycle_controller=controller,
            )


def test_v2_recover_start_authenticates_exact_escrow_without_restarting(execute, tmp_path,
                                                                         monkeypatch):
    packet, _ = _v2_write_packet(tmp_path)
    address = _v2_target_root(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    save = execute.save_private_descriptor

    def interrupt_final(root, document, *, name="run.json"):
        if name == "run.json":
            raise RuntimeError("interrupted before final descriptor")
        return save(root, document, name=name)

    monkeypatch.setattr(execute, "save_private_descriptor", interrupt_final)
    with pytest.raises(RuntimeError, match="interrupted"):
        execute.start_v2_run(
            packet, target_roots={"address": address}, run_root=run,
            authority_root=authority, private_root=private,
            skill_roots=(tmp_path / "skills",), budget=6,
            runtime=fanout, registry=_registry(),
            provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"),
        )
    monkeypatch.setattr(execute, "save_private_descriptor", save)
    assert (private / "startup.json").is_file()
    assert not (private / "run.json").exists()
    recovered = execute.recover_start(private, runtime=fanout, registry=_registry())
    assert recovered["provider_turns"] == 0
    descriptor = execute.load_private_descriptor(private)
    assert descriptor["run_id"] == recovered["run_id"]
    assert descriptor["budget"] == 6
    with pytest.raises(ValueError, match="published"):
        execute.recover_start(private, runtime=fanout, registry=_registry())


def test_v2_recover_start_rejects_resigned_budget_increase(execute, tmp_path, monkeypatch):
    packet, _ = _v2_write_packet(tmp_path)
    address = _v2_target_root(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    save = execute.save_private_descriptor

    def interrupt_final(root, document, *, name="run.json"):
        if name == "run.json":
            raise RuntimeError("interrupted before final descriptor")
        return save(root, document, name=name)

    monkeypatch.setattr(execute, "save_private_descriptor", interrupt_final)
    with pytest.raises(RuntimeError, match="interrupted"):
        execute.start_v2_run(
            packet, target_roots={"address": address}, run_root=run,
            authority_root=authority, private_root=private,
            skill_roots=(tmp_path / "skills",), budget=6,
            runtime=fanout, registry=_registry(),
        )
    monkeypatch.setattr(execute, "save_private_descriptor", save)
    startup = execute.load_private_descriptor(private, name="startup.json")
    startup["budget"] = 100
    envelope = {"schema_version": "fanout-private-envelope-v1",
                "sha256": execute._sha256(execute._canonical(startup)),
                "document": startup}
    (private / "startup.json").write_bytes(execute._canonical(envelope))
    with pytest.raises(ValueError, match="startup budget differs from authority"):
        execute.recover_start(private, runtime=fanout, registry=_registry())
    assert not (private / "run.json").exists()


def test_v2_recover_start_does_not_retry_missing_budget_anchor(execute, tmp_path,
                                                                  monkeypatch):
    packet, _ = _v2_write_packet(tmp_path)
    address = _v2_target_root(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    create = fanout.LocalAnchorAuthority.create

    def interrupt_budget_anchor(self, key, value):
        if key.startswith("startup-budget/"):
            raise RuntimeError("budget anchor creation interrupted")
        return create(self, key, value)

    monkeypatch.setattr(fanout.LocalAnchorAuthority, "create", interrupt_budget_anchor)
    with pytest.raises(RuntimeError, match="budget anchor creation interrupted"):
        execute.start_v2_run(
            packet, target_roots={"address": address}, run_root=run,
            authority_root=authority, private_root=private,
            skill_roots=(tmp_path / "skills",), budget=6,
            runtime=fanout, registry=_registry(),
        )
    assert (private / "startup.json").exists()
    assert not run.exists()
    with pytest.raises(fanout.RunStateError, match="missing or unsafe"):
        execute.recover_start(private, runtime=fanout, registry=_registry())
    assert not (private / "run.json").exists()


def test_v2_recover_start_refuses_incomplete_controller_without_repeating_create(
    execute, tmp_path, monkeypatch,
):
    packet, _ = _v2_write_packet(tmp_path)
    address = _v2_target_root(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    creations = []

    def interrupted_create(*args, **kwargs):
        creations.append((args, kwargs))
        raise RuntimeError("controller creation interrupted")

    monkeypatch.setattr(fanout, "create_lifecycle_controller", interrupted_create)
    with pytest.raises(RuntimeError, match="controller creation interrupted"):
        execute.start_v2_run(
            packet, target_roots={"address": address}, run_root=run,
            authority_root=authority, private_root=private,
            skill_roots=(tmp_path / "skills",), budget=6,
            runtime=fanout, registry=_registry(),
        )
    with pytest.raises(fanout.LifecycleError, match="controller root cannot be opened safely"):
        execute.recover_start(private, runtime=fanout, registry=_registry())
    assert len(creations) == 1
    assert not (private / "run.json").exists()


def test_v2_recover_start_rejects_resigned_baseline_ref_tamper(execute, tmp_path,
                                                                 monkeypatch):
    packet, _ = _v2_write_packet(tmp_path)
    address = _v2_target_root(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    save = execute.save_private_descriptor

    def interrupt_final(root, document, *, name="run.json"):
        if name == "run.json":
            raise RuntimeError("interrupted before final descriptor")
        return save(root, document, name=name)

    monkeypatch.setattr(execute, "save_private_descriptor", interrupt_final)
    with pytest.raises(RuntimeError, match="interrupted"):
        execute.start_v2_run(
            packet, target_roots={"address": address}, run_root=run,
            authority_root=authority, private_root=private,
            skill_roots=(tmp_path / "skills",), budget=6,
            runtime=fanout, registry=_registry(),
        )
    startup = execute.load_private_descriptor(private, name="startup.json")
    startup["baseline_manifest_refs"]["address"]["digest"] = "0" * 64
    envelope = {"schema_version": "fanout-private-envelope-v1",
                "sha256": execute._sha256(execute._canonical(startup)), "document": startup}
    (private / "startup.json").write_bytes(execute._canonical(envelope))
    with pytest.raises(fanout.ArtifactIntegrityError, match="artifact digest mismatch"):
        execute.recover_start(private, runtime=fanout, registry=_registry())
    assert not (private / "run.json").exists()


def test_v2_recover_start_rejects_resigned_partial_final_descriptor(execute, tmp_path):
    packet, _ = _v2_write_packet(tmp_path)
    address = _v2_target_root(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    execute.start_v2_run(
        packet, target_roots={"address": address}, run_root=run,
        authority_root=authority, private_root=private,
        skill_roots=(tmp_path / "skills",), budget=6,
        runtime=fanout, registry=_registry(),
    )
    published = execute.load_private_descriptor(private)
    authentic = dict(published)
    published["budget"] += 1
    envelope = {"schema_version": "fanout-private-envelope-v1",
                "sha256": execute._sha256(execute._canonical(published)),
                "document": published}
    descriptor = private / "run.json"
    descriptor.write_bytes(execute._canonical(envelope))
    temporary = private / (".run.json." + "a" * 32 + ".tmp")
    os.link(descriptor, temporary)
    with pytest.raises(ValueError, match="published v2 descriptor differs"):
        execute.recover_start(private, runtime=fanout, registry=_registry())
    assert not temporary.exists()
    authentic_envelope = {"schema_version": "fanout-private-envelope-v1",
                          "sha256": execute._sha256(execute._canonical(authentic)),
                          "document": authentic}
    descriptor.write_bytes(execute._canonical(authentic_envelope))
    os.link(descriptor, temporary)
    repaired = execute.recover_start(private, runtime=fanout, registry=_registry())
    assert repaired["descriptor_link_repaired"] is True
    assert repaired["provider_turns"] == 0


class _NeverMemory:
    def __init__(self, artifacts, health):
        self.artifacts = artifacts
        self.health = health

    def preflight(self):
        return self.health()

    def publish(self, *_args, **_kwargs):
        raise AssertionError("start must not publish")

    def recover(self, *_args, **_kwargs):
        raise AssertionError("start must not recover")

    def verify_existing(self, *_args, **_kwargs):
        raise AssertionError("start must not verify an absent checkpoint")

    def fetch_verified(self, *_args, **_kwargs):
        raise AssertionError("start must not fetch an absent checkpoint")


def _registry(*, write_characterized: bool = False):
    """Synthetic admission for fake transports; not live 6.1 proof."""
    pins = {"claude": "2.1.284", "codex": "0.160.0", "agy": "1.2.13"}
    registry = fanout.ProviderRegistry.default(version_probe=lambda executor: pins[executor])
    return fanout.ProviderRegistry((
        dataclasses.replace(profile, characterized=True, profile_sha256=None)
        if (profile.executor_id == "codex" and profile.execution_class == "read-only"
            or write_characterized and profile.execution_class == "repo-write") else profile
        for profile in registry._profiles.values()
    ), version_probe=lambda executor: pins[executor])


def test_start_and_cold_status_use_real_durable_stores_without_spend(execute, tmp_path):
    """A dirty caller stays byte-identical and owner capability cold-opens privately."""
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    before = fanout.capture_repository_baseline(repo).digest
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    launches = []

    def no_provider(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("start and status must not spend")

    status = execute.prepare_run(
        packet, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=12,
        runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True, provider_runner=no_provider,
    )
    assert status["tasks"][0]["phase"] == "scheduled"
    assert not launches
    assert fanout.capture_repository_baseline(repo).digest == before
    assert not (run / "run.json").exists()
    assert "owner_token" not in status
    with execute.open_run(
        private, runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True, provider_runner=no_provider,
    ) as reopened:
        assert reopened.service.status().to_dict() == status
    assert not launches


def test_fresh_readonly_start_records_installed_profiles_and_cold_dispatch_refuses_upgrade(
    execute, tmp_path, monkeypatch,
):
    versions = {"claude": "2.2.0", "codex": "0.160.0"}
    probes = []

    def probe(executor):
        probes.append(executor)
        return versions[executor]

    monkeypatch.setattr(fanout.providers, "_installed_cli_version", probe)
    spec = importlib.util.spec_from_file_location("installed_profile_smoke", ROOT / "scripts/fanout_smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = smoke
    spec.loader.exec_module(smoke)
    smoke.fanout = fanout
    sent = []

    for index, version in enumerate(("0.160.0", "0.161.0")):
        versions["codex"] = version
        case = tmp_path / str(index)
        case.mkdir()
        packet, _ = _question_packet(case, executors=("claude", "codex"))
        repo = _repository(case)
        private, run, authority = (case / name for name in ("private", "run", "authority"))
        private.mkdir(mode=0o700)
        fake = smoke._FakeProvider(case, poison_peer=False)

        def sender(request, *, registry):
            assert request.profile.cli_version == versions[request.executor_id]
            assert registry.select(*request.profile.key).digest == request.profile.digest
            sent.append((request.executor_id, request.profile.cli_version))
            return fake(request, registry=registry)

        common = dict(runtime=fanout,
                      memory_factory=lambda artifacts, health: smoke._FakeMemory(case, artifacts),
                      memory_preflight=lambda: True, provider_runner=sender)
        probes.clear()
        execute.prepare_run(
            packet, repo_root=repo, run_root=run, authority_root=authority,
            private_root=private, skill_roots=(case / "skills",), budget=12, **common,
        )
        assert probes[:2] == ["claude", "codex"]
        assert set(probes) == {"claude", "codex"}
        descriptor = execute.load_private_descriptor(private)
        stored = next(profile for profile in descriptor["provider_profile_descriptors"]
                      if (profile["executor_id"], profile["execution_class"], profile["quality_tier"])
                      == ("codex", "read-only", "standard"))
        assert stored["cli_version"] == version
        assert stored["characterized"] is False
        with execute.open_run(private, **common) as opened:
            assert opened.service.coordinator.registry.select(
                "codex", "read-only", "standard",
            ).cli_version == version
            opened.service.resume(owner=opened.owner)
        assert ("codex", version) in sent
        if index == 0:
            recorded = {path: path.read_bytes() for path in
                        (private / "run.json", private / "startup.json", run / "inputs.json")}
            sent_before = len(sent)
            versions["codex"] = "0.161.0"
            with execute.open_run(private, **common) as opened:
                with pytest.raises(fanout.ExecutionPreflightError, match="execution preflight"):
                    opened.service.resume(owner=opened.owner)
            assert len(sent) == sent_before
            assert all(path.read_bytes() == raw for path, raw in recorded.items())


def test_startup_recovery_rehydrates_installed_profiles_without_rebinding(
    execute, tmp_path, monkeypatch,
):
    versions = {"claude": "2.2.0", "codex": "0.160.0"}
    monkeypatch.setattr(fanout.providers, "_installed_cli_version", lambda executor: versions[executor])
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    original_save = execute.save_private_descriptor

    def crash_final(root, document, **kwargs):
        if kwargs.get("name", "run.json") == "run.json":
            raise OSError("injected descriptor crash")
        return original_save(root, document, **kwargs)

    monkeypatch.setattr(execute, "save_private_descriptor", crash_final)
    with pytest.raises(OSError, match="descriptor crash"):
        execute.prepare_run(
            packet, repo_root=repo, run_root=run, authority_root=authority,
            private_root=private, skill_roots=(tmp_path / "skills",), budget=12,
            runtime=fanout, memory_factory=_NeverMemory, memory_preflight=lambda: True,
            provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"),
        )
    escrow = execute.load_private_descriptor(private, name="startup.json")
    versions["codex"] = "0.161.0"
    monkeypatch.setattr(execute, "save_private_descriptor", original_save)
    execute.recover_start(private, runtime=fanout)
    descriptor = execute.load_private_descriptor(private)
    assert descriptor["inputs"] == escrow["inputs"]
    assert descriptor["provider_profile_descriptors"] == escrow["provider_profile_descriptors"]
    with execute.open_run(
        private, runtime=fanout, memory_factory=_NeverMemory, memory_preflight=lambda: True,
        provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"),
    ) as opened:
        assert opened.service.coordinator.registry.select("codex", "read-only", "deep").cli_version == "0.160.0"


def test_changed_runtime_v1_status_reads_live_authenticated_stores(execute, tmp_path):
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    execute.prepare_run(
        packet, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=12,
        runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True,
        provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"),
    )
    descriptor = execute.load_private_descriptor(private)
    descriptor["runtime_sha256"] = "a" * 64
    envelope = {
        "schema_version": "fanout-private-envelope-v1",
        "sha256": hashlib.sha256(execute._canonical(descriptor)).hexdigest(),
        "document": descriptor,
    }
    (private / "run.json").write_bytes(execute._canonical(envelope))
    status = execute.inspect_historical_run(private, runtime=fanout)
    assert status.run_id == descriptor["run_id"]
    assert status.scheduler_revision >= 1
    assert status.journal_seq == 0


def _private_inventory(*roots):
    return {
        str(path): (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
                    info.st_dev, info.st_ino,
                    path.read_bytes() if path.is_file() else None)
        for root in roots for path in (root, *root.rglob("*"))
        for info in (path.lstat(),)
    }


def test_current_v1_status_does_not_settle_complete_pending_journal(execute, tmp_path,
                                                                  monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("status_pending_smoke", ROOT / "scripts/fanout_smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = smoke
    spec.loader.exec_module(smoke)
    smoke.fanout = fanout
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)

    def composition():
        return dict(runtime=fanout, registry=_registry(),
                    memory_factory=lambda artifacts, health: smoke._FakeMemory(tmp_path, artifacts),
                    memory_preflight=lambda: True,
                    provider_runner=smoke._FakeProvider(tmp_path, poison_peer=False))

    execute.prepare_run(packet, repo_root=repo, run_root=run, authority_root=authority,
                        private_root=private, skill_roots=(tmp_path / "skills",),
                        budget=12, **composition())
    with execute.open_run(private, **composition()) as opened:
        assert opened.service.resume(owner=opened.owner).tasks[0].completed_rounds == 2
        verified = opened.service.verify_read_only_synthesis(
            "answer", source_seat_ids=[execute._task_seat_id("answer", executor)
                                     for executor in ("claude", "codex")],
            synthesizer_id="owner", answer=b"A final answer from both final seats.\n",
            owner=opened.owner,
        )

        def crash_commit(record, offset):
            assert record["type"] == "reconciliation-verifying"
            raise OSError("complete event before authority CAS")

        monkeypatch.setattr(opened.journal, "_anchor_commit", crash_commit)
        with pytest.raises(OSError, match="before authority CAS"):
            opened.service.submit("answer", verified, owner=opened.owner)
    before = _private_inventory(private, run, authority, repo)
    calls = _private_inventory(tmp_path / "calls")
    result = execute.main(["status", "--private-root", str(private)], **composition())
    assert _private_inventory(private, run, authority, repo) == before
    assert _private_inventory(tmp_path / "calls") == calls
    assert result == 2
    assert "recover-journal" in capsys.readouterr().err

    def forbidden(*_args, **_kwargs):
        pytest.fail("inspection/recovery invoked provider, memory, version, Git or scheduler repair")

    with monkeypatch.context() as blocked:
        blocked.setattr(fanout.repo, "_git", forbidden)
        blocked.setattr(fanout.providers, "run_command", forbidden)
        blocked.setattr(fanout.Scheduler, "resume", forbidden)
        assert execute.main(["recover-journal", "--private-root", str(private)],
                            runtime=fanout) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["recovered"] is True
    assert _private_inventory(tmp_path / "calls") == calls
    with execute.open_run(private, **composition()) as reopened:
        assert reopened.journal.state.task_phases["answer"] == "reconciliation-verifying"
        assert reopened.service.resume(owner=reopened.owner).tasks[0].completed_rounds == 2
        reopened.service.submit("answer", verified, owner=reopened.owner)
    assert _private_inventory(tmp_path / "calls") == calls


def test_fresh_process_v1_status_authenticates_accepted_amendment_and_runtime_fence(
    execute, tmp_path,
):
    runtime = execute._runtime()
    packet = _action_gate_packet(tmp_path)
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    defaults = runtime.ProviderRegistry.default(version_probe=lambda executor: {
        "claude": "2.1.284", "codex": "0.160.0", "agy": "1.2.13",
    }[executor])
    registry = runtime.ProviderRegistry((
        dataclasses.replace(profile, characterized=True, profile_sha256=None)
        if profile.executor_id == "codex" and profile.execution_class == "read-only" else profile
        for profile in defaults._profiles.values()
    ), version_probe=defaults._version_probe)
    common = dict(runtime=runtime, registry=registry, memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *_args, **_kwargs: pytest.fail("fixture dispatched"))
    execute.prepare_run(packet, repo_root=repo, run_root=run, authority_root=authority,
                        private_root=private, skill_roots=(tmp_path / "skills",),
                        budget=12, **common)
    with execute.open_run(private, **common) as opened:
        service = opened.service
        task = dataclasses.replace(next(task for task in service.plan.tasks if task.id == "later"),
                                   objective="Analyze the amended question.")
        plan = dataclasses.replace(service.plan, tasks=tuple(
            task if old.id == "later" else old for old in service.plan.tasks))
        plan_bytes = runtime.canonical_json(plan.to_dict())
        inputs = dataclasses.replace(service.inputs, compiled_plan_sha256=hashlib.sha256(plan_bytes).hexdigest())
        old = service.preparations["later"]
        task_bytes = runtime.canonical_json(task.to_dict())
        preparation = dataclasses.replace(old, inputs=inputs, plan_revision=2,
            packet=dataclasses.replace(old.packet, compiled_plan=plan_bytes,
                compiled_plan_sha256=inputs.compiled_plan_sha256, task=task_bytes,
                task_sha256=hashlib.sha256(task_bytes).hexdigest()))
        assert service.submit_amendment(plan, inputs, {"later": preparation},
            inputs.provider_profiles, expected_plan_revision=1, owner=opened.owner).plan_revision == 2

    isolated = tmp_path / "owned-helper"
    shutil.copytree(ROOT / "shared/lib/fanout", isolated / "shared/lib/fanout",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (isolated / "scripts").mkdir()
    cli = isolated / "scripts/execute.py"
    shutil.copy2(CLI, cli)
    compiler = isolated / "shared/lib/fanout/compiler.py"
    original_compiler = compiler.read_bytes()
    compiler.write_bytes(original_compiler + b"\n# Owned current-runtime change.\n")
    before = _private_inventory(private, run, authority, repo)
    activity = tmp_path / "unexpected-activity"
    sentinels = tmp_path / "activity-sentinels"
    sentinels.mkdir()
    (sentinels / "sitecustomize.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "def audit(event, args):\n"
        "    if event in {'subprocess.Popen', 'socket.connect'}:\n"
        f"        Path({str(activity)!r}).write_text(event)\n"
        "        raise AssertionError('unexpected process/network activity')\n"
        "sys.addaudithook(audit)\n",
    )

    def invoke(command):
        return subprocess.run([sys.executable, str(cli), command, "--private-root", str(private)],
                              capture_output=True, text=True, check=False,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                   "PYTHONPATH": str(sentinels)})

    historical = invoke("status")
    assert historical.returncode == 0, historical.stderr
    status = json.loads(historical.stdout)
    assert status["run_id"] == inputs.run_id
    assert status["plan_revision"] == 2
    assert status["inputs_digest"] == inputs.digest
    assert status["provider_profiles"] == dict(inputs.provider_profiles)
    assert status["read_only"] is True
    assert _private_inventory(private, run, authority, repo) == before
    assert not activity.exists()

    compiler.write_bytes(original_compiler)
    current = invoke("status")
    assert current.returncode == 0, current.stderr
    assert json.loads(current.stdout)["plan_revision"] == 2
    assert _private_inventory(private, run, authority, repo) == before
    assert not activity.exists()

    for module, function in (("repo.py", "_git"), ("providers.py", "run_command")):
        path = isolated / "shared/lib/fanout" / module
        path.write_bytes(path.read_bytes() + (
            f"\ndef {function}(*args, **kwargs):\n"
            f"    Path({str(activity)!r}).write_text('unexpected activity')\n"
            "    raise AssertionError('unexpected activity')\n"
        ).encode())
    refused = invoke("resume")
    assert refused.returncode == 2
    assert "restore the original runtime to resume this run" in refused.stderr
    assert not activity.exists()
    assert _private_inventory(private, run, authority, repo) == before

    document = run / "artifacts/amendments/inputs" / (inputs.digest + ".json")
    original = document.read_bytes()
    document.write_bytes(b"{}\n")
    tampered = _private_inventory(private, run, authority, repo)
    assert invoke("status").returncode == 2
    assert _private_inventory(private, run, authority, repo) == tampered
    document.write_bytes(original)
    document.rename(document.with_suffix(".missing"))
    missing = _private_inventory(private, run, authority, repo)
    assert invoke("status").returncode == 2
    assert _private_inventory(private, run, authority, repo) == missing
    document.with_suffix(".missing").rename(document)
    scheduler_path = run / ".fanout-scheduler.record.json"
    scheduler = json.loads(scheduler_path.read_bytes())
    scheduler["revision"] += 1
    scheduler["snapshot"]["backend_revision"] += 1
    scheduler["snapshot_sha256"] = hashlib.sha256(
        runtime.canonical_json(scheduler["snapshot"]),
    ).hexdigest()
    scheduler_path.write_bytes(runtime.canonical_json(scheduler))
    mismatched = _private_inventory(private, run, authority, repo)
    external_refusal = invoke("status")
    assert external_refusal.returncode == 2
    assert not activity.exists()
    assert _private_inventory(private, run, authority, repo) == mismatched


def test_cold_status_survives_agy_binary_drift_but_dispatch_still_refuses(
    execute, tmp_path, monkeypatch,
):
    """A changed installed CLI must not hide saved state or authorize another turn."""
    packet, _ = _question_packet(tmp_path, executors=("claude", "agy"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *args, **kwargs: pytest.fail("provider launched"))
    monkeypatch.setattr(fanout, "issue_agy_readonly_guard", lambda *_args: None)
    started = execute.prepare_run(
        packet, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=12, **common,
    )

    def drifted_binary(*_args):
        raise fanout.ProviderRequestError("direct agy binary version differs from profile")

    monkeypatch.setattr(fanout, "issue_agy_readonly_guard", drifted_binary)
    with execute.open_run(private, **common) as reopened:
        assert reopened.service.status().to_dict() == started
    with pytest.raises(fanout.ProviderRequestError, match="binary version differs"):
        execute.open_run(private, for_dispatch=True, **common)


def test_cold_status_preserves_issued_agy_guard_binding(
    execute, tmp_path, monkeypatch,
):
    """An issued guard must remain bound without a live binary probe on status."""
    packet, _ = _question_packet(tmp_path, executors=("claude", "agy"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    guard_digest = "a" * 64

    def issue_guard(controller, verification, profile):
        return fanout.AgyReadOnlyGuard(
            controller, verification, profile.digest,
            tmp_path / "guard", tmp_path / "agy", tmp_path / "adc",
            "guard.json", guard_digest,
        )

    monkeypatch.setattr(fanout, "issue_agy_readonly_guard", issue_guard)
    monkeypatch.setattr(
        fanout, "agy_readonly_guard_receipt_sha256", lambda *_args: guard_digest,
        raising=False,
    )
    monkeypatch.setattr(fanout.execute, "validate_agy_readonly_guard", lambda *_args: None)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *args, **kwargs: pytest.fail("provider launched"))
    started = execute.prepare_run(
        packet, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=12, **common,
    )

    with execute.open_run(private, **common) as reopened:
        assert reopened.service.status().to_dict() == started


def test_admitted_source_reaches_cold_reopened_read_only_seats(execute, tmp_path):
    """A synthetic question path must not leave restricted seats with only source hashes."""
    admission, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *args, **kwargs: pytest.fail("provider launched"))
    execute.prepare_run(
        admission, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=12, **common,
    )
    with execute.open_run(private, **common) as reopened:
        preparation = reopened.service.preparations["answer"]
        assert preparation.packet.context_document()["source_markdown"] == admission["source_markdown"]
        for seat in preparation.seats:
            workspace = seat.workspace_verification.workspace.root
            assert not (workspace / admission["compiled"]["plan"]["source"]["path"]).exists()


@pytest.mark.parametrize("injected_runner", (False, True))
def test_public_start_rejects_repo_write_before_git_version_or_roots(
    execute, tmp_path, monkeypatch, capsys, injected_runner,
):
    packet, _ = _write_packet(tmp_path)
    packet_file = tmp_path / "admission.json"
    packet_file.write_bytes(fanout.canonical_json(packet))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    called = []

    def forbidden(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("FORBIDDEN_PROBE")

    monkeypatch.setattr(sys.modules["fanout.repo"], "_git", forbidden)
    monkeypatch.setattr(sys.modules["fanout.providers"], "run_command", forbidden)
    registry = fanout.ProviderRegistry.default(version_probe=forbidden)

    result = execute.main([
        "start", "--private-root", str(private), "--admission-file", str(packet_file),
        "--repo-root", str(repo), "--run-root", str(run),
        "--authority-root", str(authority), "--skill-root", str(tmp_path / "skills"),
        "--max-turns", "18",
    ], runtime=fanout, registry=registry, memory_factory=_NeverMemory,
        memory_preflight=lambda: True,
        **({"provider_runner": forbidden} if injected_runner else {}))

    assert result == 2
    assert "native seat boundary" in capsys.readouterr().err
    assert called == []
    assert not run.exists() and not authority.exists()
    assert list(private.iterdir()) == []


@pytest.mark.parametrize("injected_runner", (False, True))
def test_public_cold_resume_rejects_repo_write_before_workspace_git_or_version(
    execute, tmp_path, monkeypatch, capsys, injected_runner,
):
    packet, _ = _write_packet(tmp_path)
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(write_characterized=True), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *_args, **_kwargs: pytest.fail("fixture spent a provider turn"))
    execute.prepare_run(packet, repo_root=repo, run_root=run,
                        authority_root=authority, private_root=private,
                        skill_roots=(tmp_path / "skills",), budget=18, **common)
    assert execute.main(["status", "--private-root", str(private)],
                        runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                        memory_preflight=lambda: True) == 0
    capsys.readouterr()
    called = []

    def forbidden(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("FORBIDDEN_PROBE")

    monkeypatch.setattr(sys.modules["fanout.repo"], "_git", forbidden)
    monkeypatch.setattr(sys.modules["fanout.providers"], "run_command", forbidden)
    monkeypatch.setattr(fanout.RunJournal, "resume", forbidden)
    registry = fanout.ProviderRegistry.default(version_probe=forbidden)

    result = execute.main(["resume", "--private-root", str(private)],
                          runtime=fanout, registry=registry, memory_factory=_NeverMemory,
                          memory_preflight=lambda: True,
                          **({"provider_runner": forbidden} if injected_runner else {}))

    assert result == 2
    assert "native seat boundary" in capsys.readouterr().err
    assert called == []


@pytest.mark.parametrize("reason", ("pending-authority", "amended-repo-write"))
def test_public_resume_inspects_journal_before_mutating_recovery(
    execute, tmp_path, monkeypatch, capsys, reason,
):
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *_args, **_kwargs: pytest.fail("provider launched"))
    execute.prepare_run(packet, repo_root=repo, run_root=run,
                        authority_root=authority, private_root=private,
                        skill_roots=(tmp_path / "skills",), budget=12, **common)
    with execute.open_run(private, **common) as opened:
        initial_inputs = opened.service.inputs
    if reason == "pending-authority":
        inspection = SimpleNamespace(inputs=initial_inputs,
                                     state=SimpleNamespace(amendments={}),
                                     pending_authority=True)
    else:
        write_root = tmp_path / "write"
        write_root.mkdir()
        write_packet, write_resolver = _write_packet(write_root)
        write_plan = execute.verify_admission(write_packet, fanout, write_resolver).plan
        inspection = SimpleNamespace(inputs=initial_inputs,
                                     state=SimpleNamespace(amendments={2: object()}),
                                     pending_authority=False)
        monkeypatch.setattr(fanout.ExecutionService, "recovery_documents",
                            staticmethod(lambda *_args, **_kwargs: SimpleNamespace(
                                plan=write_plan)))
    monkeypatch.setattr(fanout.RunJournal, "inspect",
                        lambda *_args, **_kwargs: inspection)
    resumed = []

    def forbidden_resume(*_args, **_kwargs):
        resumed.append(True)
        raise AssertionError("journal resume mutated authority before refusal")

    monkeypatch.setattr(fanout.RunJournal, "resume", forbidden_resume)
    result = execute.main(["resume", "--private-root", str(private)], **common)

    assert result == 2
    assert not resumed
    message = capsys.readouterr().err
    assert ("pending journal authority" if reason == "pending-authority"
            else "native seat boundary") in message


def test_failed_memory_preflight_creates_no_run_or_provider_turn(execute, tmp_path):
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="memory"):
        execute.prepare_run(
            packet, repo_root=repo, run_root=run, authority_root=authority,
            private_root=private, skill_roots=(tmp_path / "skills",), budget=12,
            runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
            memory_preflight=lambda: False, provider_runner=lambda *args, **kwargs: None,
        )
    assert not run.exists() and not authority.exists()
    assert list(private.iterdir()) == []


@pytest.mark.parametrize("max_turns", [0, 11])
def test_cli_rejects_invalid_or_insufficient_turn_budget_before_startup_escrow(
    execute, tmp_path, capsys, max_turns,
):
    """A three-seat, two-round, one-retry plan needs 12 turns before roots exist."""
    packet, _ = _write_packet(tmp_path, retries=1)
    packet_file = tmp_path / "admission.json"
    packet_file.write_bytes(fanout.canonical_json(packet))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    result = execute.main([
        "start", "--private-root", str(private), "--admission-file", str(packet_file),
        "--repo-root", str(repo), "--run-root", str(run),
        "--authority-root", str(authority), "--skill-root", str(tmp_path / "skills"),
        "--max-turns", str(max_turns),
    ], runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True,
        provider_runner=lambda *_args, **_kwargs: pytest.fail("budget preflight spent"))
    assert result == 2
    assert "provider turn budget" in capsys.readouterr().err.lower()
    assert not run.exists() and not authority.exists()
    assert list(private.iterdir()) == []


def test_pre_descriptor_crash_reopens_same_run_without_provider_spend(execute, tmp_path,
                                                                      monkeypatch):
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    launches = []
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *args, **kwargs: launches.append((args, kwargs)))
    original_save = execute.save_private_descriptor

    def crash_final_descriptor(root, document, **kwargs):
        if kwargs.get("name", "run.json") == "run.json":
            raise OSError("injected final descriptor write crash")
        return original_save(root, document, **kwargs)

    monkeypatch.setattr(execute, "save_private_descriptor", crash_final_descriptor)
    with pytest.raises(OSError, match="injected final descriptor"):
        execute.prepare_run(packet, repo_root=repo, run_root=run,
                            authority_root=authority, private_root=private,
                            skill_roots=(tmp_path / "skills",), budget=12, **common)
    assert not launches
    assert not (private / "run.json").exists()
    monkeypatch.setattr(execute, "save_private_descriptor", original_save)
    recovered = execute.recover_start(private, runtime=fanout, registry=_registry())
    assert recovered["run_id"]
    assert recovered["recovered"] is True
    with execute.open_run(private, **common) as recovered:
        assert recovered.service.inputs.run_id
        assert recovered.service.status().execution_revision == 0
        assert recovered.service.start(owner=recovered.owner).tasks[0].phase == "scheduled"
    with execute.open_run(private, **common) as reopened:
        assert reopened.service.status().tasks[0].phase == "scheduled"
    assert not launches


def test_recover_start_repairs_only_its_exact_atomic_publication_link(execute, tmp_path):
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *args, **kwargs: pytest.fail("provider launched"))
    execute.prepare_run(packet, repo_root=repo, run_root=run,
                        authority_root=authority, private_root=private,
                        skill_roots=(tmp_path / "skills",), budget=12, **common)
    descriptor = private / "run.json"
    temporary = private / (".run.json." + "a" * 32 + ".tmp")
    os.link(descriptor, temporary)
    with pytest.raises(ValueError, match="unsafe"):
        execute.load_private_descriptor(private)
    result = execute.recover_start(private, runtime=fanout, registry=_registry())
    assert result["recovered"] is True
    assert not temporary.exists()
    assert stat.S_IMODE(descriptor.stat().st_mode) == 0o600
    with execute.open_run(private, **common) as reopened:
        assert reopened.service.status().tasks[0].phase == "scheduled"
        assert (reopened.service.preparations["answer"].packet.context_document()["source_markdown"]
                == packet["source_markdown"])


def test_failed_start_preflight_is_retryable_without_provider_spend(execute, tmp_path):
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    probes = [True, False]
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: probes.pop(0) if probes else True,
                  provider_runner=lambda *args, **kwargs: pytest.fail("provider spent during start"))
    with pytest.raises(fanout.ExecutionPreflightError):
        execute.prepare_run(packet, repo_root=repo, run_root=run,
                            authority_root=authority, private_root=private,
                            skill_roots=(tmp_path / "skills",), budget=12, **common)
    assert (private / "run.json").is_file()
    with execute.open_run(private, **common) as recovered:
        assert recovered.service.status().execution_revision == 0
    assert execute.main(["resume", "--private-root", str(private)], **common) == 0
    with execute.open_run(private, **common) as reopened:
        assert reopened.service.status().tasks[0].phase == "scheduled"


def test_accepted_amendment_cold_reopens_replacement_before_resume(execute, tmp_path):
    packet = _action_gate_packet(tmp_path)
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *args, **kwargs: pytest.fail("blocked task launched"))
    execute.prepare_run(packet, repo_root=repo, run_root=run,
                        authority_root=authority, private_root=private,
                        skill_roots=(tmp_path / "skills",), budget=12, **common)
    with execute.open_run(private, **common) as opened:
        service = opened.service
        old_task = next(task for task in service.plan.tasks if task.id == "later")
        replacement_task = dataclasses.replace(old_task, objective="Analyze the amended question.")
        replacement = dataclasses.replace(
            service.plan,
            tasks=tuple(replacement_task if task.id == "later" else task
                        for task in service.plan.tasks),
        )
        replacement_bytes = fanout.canonical_json(replacement.to_dict())
        replacement_digest = hashlib.sha256(replacement_bytes).hexdigest()
        new_inputs = dataclasses.replace(service.inputs, compiled_plan_sha256=replacement_digest)
        old_preparation = service.preparations["later"]
        task_bytes = fanout.canonical_json(replacement_task.to_dict())
        packet_bytes = dataclasses.replace(
            old_preparation.packet, compiled_plan=replacement_bytes,
            compiled_plan_sha256=replacement_digest, task=task_bytes,
            task_sha256=hashlib.sha256(task_bytes).hexdigest(),
        )
        preparation = dataclasses.replace(
            old_preparation, packet=packet_bytes, inputs=new_inputs, plan_revision=2,
        )
        accepted = service.submit_amendment(
            replacement, new_inputs, {"later": preparation},
            new_inputs.provider_profiles, expected_plan_revision=1, owner=opened.owner,
        )
        assert accepted.plan_revision == 2
    with execute.open_run(private, **common) as reopened:
        assert reopened.service.plan == replacement
        assert reopened.service.inputs.digest == new_inputs.digest
        assert reopened.service.status().plan_revision == 2
        assert reopened.service.resume(owner=reopened.owner).plan_revision == 2


def test_source_changing_amendment_cannot_reuse_initial_source_bytes(execute, tmp_path):
    packet = _action_gate_packet(tmp_path)
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    launches = []

    def no_provider(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("provider must not launch")

    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True, provider_runner=no_provider)
    execute.prepare_run(packet, repo_root=repo, run_root=run,
                        authority_root=authority, private_root=private,
                        skill_roots=(tmp_path / "skills",), budget=12, **common)
    replacement_source = b"# Different admitted source\n"
    with execute.open_run(private, **common) as opened:
        service = opened.service
        replacement = dataclasses.replace(
            service.plan,
            source=dataclasses.replace(
                service.plan.source,
                sha256=hashlib.sha256(replacement_source).hexdigest(),
            ),
        )
        prior = service.preparations["later"]
        with pytest.raises(fanout.CollaborationValidationError, match="admitted source"):
            dataclasses.replace(
                prior.packet,
                compiled_plan=fanout.canonical_json(replacement.to_dict()),
                compiled_plan_sha256=hashlib.sha256(
                    fanout.canonical_json(replacement.to_dict()),
                ).hexdigest(),
            )
    assert launches == []


def test_accepted_profile_transition_cold_reopens_pinned_registry(execute, tmp_path):
    packet = _action_gate_packet(tmp_path)
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    old_registry = _registry()
    common = dict(runtime=fanout, registry=old_registry, memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *args, **kwargs: pytest.fail("blocked task launched"))
    execute.prepare_run(packet, repo_root=repo, run_root=run,
                        authority_root=authority, private_root=private,
                        skill_roots=(tmp_path / "skills",), budget=12, **common)
    profiles = tuple(
        dataclasses.replace(profile, cli_version="2.1.282", profile_sha256=None)
        if profile.key == ("claude", "read-only", "standard") else profile
        for profile in old_registry._profiles.values()
    )
    pins = {"claude": "2.1.282", "codex": "0.160.0", "agy": "1.2.13"}
    updated_registry = fanout.ProviderRegistry(
        profiles, version_probe=lambda executor: pins[executor],
    )
    with execute.open_run(private, **common) as opened:
        service = opened.service
        new_inputs = dataclasses.replace(
            service.inputs, provider_profiles=dict(updated_registry.profile_digests),
        )
        preparation = dataclasses.replace(
            service.preparations["later"], inputs=new_inputs,
            registry=updated_registry, plan_revision=2,
        )
        transition = service.prepare_provider_profile_transition(
            service.plan, new_inputs, updated_registry, new_inputs.provider_profiles,
            owner=opened.owner,
        )
        service.submit_amendment(
            service.plan, new_inputs, {"later": preparation},
            new_inputs.provider_profiles, expected_plan_revision=1,
            provider_transition=transition, owner=opened.owner,
        )
    with execute.open_run(private, **dict(common, registry=updated_registry)) as reopened:
        assert reopened.service.inputs.provider_profiles == new_inputs.provider_profiles
        assert reopened.service.coordinator.registry.profile_digests == new_inputs.provider_profiles
        assert reopened.service.resume(owner=reopened.owner).plan_revision == 2


def test_cli_recovers_scheduler_advanced_pending_amendment_after_cold_open(
    execute, tmp_path, monkeypatch, capsys,
):
    packet = _action_gate_packet(tmp_path)
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    launches = []
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *args, **kwargs: launches.append((args, kwargs)))
    execute.prepare_run(packet, repo_root=repo, run_root=run,
                        authority_root=authority, private_root=private,
                        skill_roots=(tmp_path / "skills",), budget=12, **common)
    with execute.open_run(private, **common) as opened:
        service = opened.service
        old_task = next(task for task in service.plan.tasks if task.id == "later")
        changed_task = dataclasses.replace(old_task, objective="Analyze after the amendment.")
        replacement = dataclasses.replace(
            service.plan,
            tasks=tuple(changed_task if task.id == "later" else task
                        for task in service.plan.tasks),
        )
        plan_bytes = fanout.canonical_json(replacement.to_dict())
        plan_digest = hashlib.sha256(plan_bytes).hexdigest()
        new_inputs = dataclasses.replace(service.inputs, compiled_plan_sha256=plan_digest)
        task_bytes = fanout.canonical_json(changed_task.to_dict())
        old_preparation = service.preparations["later"]
        new_packet = dataclasses.replace(
            old_preparation.packet, compiled_plan=plan_bytes,
            compiled_plan_sha256=plan_digest, task=task_bytes,
            task_sha256=hashlib.sha256(task_bytes).hexdigest(),
        )
        preparation = dataclasses.replace(
            old_preparation, packet=new_packet, inputs=new_inputs, plan_revision=2,
        )
        original_accept = service.scheduler.accept_amendment

        def crash_after_scheduler_cas(*args, **kwargs):
            original_accept(*args, **kwargs)
            raise SystemExit("crash after scheduler CAS")

        monkeypatch.setattr(service.scheduler, "accept_amendment", crash_after_scheduler_cas)
        with pytest.raises(SystemExit, match="scheduler CAS"):
            service.submit_amendment(
                replacement, new_inputs, {"later": preparation},
                new_inputs.provider_profiles, expected_plan_revision=1,
                owner=opened.owner,
            )
    monkeypatch.undo()
    assert not launches
    with execute.open_run(private, **common) as reopened:
        assert reopened.service.plan != replacement
        assert reopened.service.status().plan_revision == 2
    assert execute.main(["abandon-amendment", "--private-root", str(private)], **common) != 0
    capsys.readouterr()
    assert execute.main(["recover-amendment", "--private-root", str(private)], **common) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["plan_revision"] == 2
    with execute.open_run(private, **common) as recovered:
        assert recovered.service.plan == replacement
        assert recovered.service.inputs.digest == new_inputs.digest
    assert not launches


@pytest.mark.parametrize("crash_boundary", ["scheduler", "journal"])
def test_cli_recovers_pending_profile_transition_from_durable_descriptors(
    execute, tmp_path, monkeypatch, capsys, crash_boundary,
):
    packet = _action_gate_packet(tmp_path)
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    original_registry = _registry()
    common = dict(runtime=fanout, registry=original_registry, memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *args, **kwargs: pytest.fail("provider launched"))
    execute.prepare_run(packet, repo_root=repo, run_root=run,
                        authority_root=authority, private_root=private,
                        skill_roots=(tmp_path / "skills",), budget=12, **common)
    profiles = tuple(
        dataclasses.replace(profile, cli_version="2.1.282", profile_sha256=None)
        if profile.key == ("claude", "read-only", "standard") else profile
        for profile in original_registry._profiles.values()
    )
    versions = {"claude": "2.1.282", "codex": "0.160.0", "agy": "1.2.13"}
    replacement_registry = fanout.ProviderRegistry(
        profiles, version_probe=lambda executor: versions[executor],
    )
    with execute.open_run(private, **common) as opened:
        service = opened.service
        new_inputs = dataclasses.replace(
            service.inputs, provider_profiles=dict(replacement_registry.profile_digests),
        )
        preparation = dataclasses.replace(
            service.preparations["later"], inputs=new_inputs,
            registry=replacement_registry, plan_revision=2,
        )
        transition = service.prepare_provider_profile_transition(
            service.plan, new_inputs, replacement_registry,
            new_inputs.provider_profiles, owner=opened.owner,
        )
        if crash_boundary == "scheduler":
            original_accept = service.scheduler.accept_amendment

            def crash_after_scheduler_cas(*args, **kwargs):
                original_accept(*args, **kwargs)
                raise SystemExit("profile scheduler CAS")

            monkeypatch.setattr(service.scheduler, "accept_amendment", crash_after_scheduler_cas)
        else:
            original_append = opened.journal.append_amendment

            def crash_after_journal_acceptance(phase, **kwargs):
                original_append(phase, **kwargs)
                if phase == "plan-amendment-accepted":
                    raise SystemExit("profile journal acceptance")

            monkeypatch.setattr(opened.journal, "append_amendment", crash_after_journal_acceptance)
        with pytest.raises(SystemExit, match="profile (scheduler CAS|journal acceptance)"):
            service.submit_amendment(
                service.plan, new_inputs, {"later": preparation},
                new_inputs.provider_profiles, expected_plan_revision=1,
                provider_transition=transition, owner=opened.owner,
            )
    monkeypatch.undo()
    assert execute.main(["recover-amendment", "--private-root", str(private)],
                        **common) != 0
    capsys.readouterr()
    with execute.open_run(private, **common) as pending:
        expected_phase = (
            "plan-amendment-accepted" if crash_boundary == "journal"
            else "plan-amendment-intent"
        )
        assert pending.journal.state.amendments[2].phase == expected_phase
    assert execute.main(["recover-amendment", "--private-root", str(private)],
                        **dict(common, registry=replacement_registry)) == 0
    assert json.loads(capsys.readouterr().out)["plan_revision"] == 2
    with execute.open_run(private, **dict(common, registry=replacement_registry)) as reopened:
        assert reopened.service.coordinator.registry.profile_digests == new_inputs.provider_profiles


@pytest.mark.parametrize("tamper_predecessor", [False, True])
def test_cli_recovers_accepted_journal_before_execution_cas_after_cold_open(
    execute, tmp_path, monkeypatch, capsys, tamper_predecessor,
):
    packet = _action_gate_packet(tmp_path)
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    launches = []
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *args, **kwargs: launches.append((args, kwargs)))
    execute.prepare_run(packet, repo_root=repo, run_root=run,
                        authority_root=authority, private_root=private,
                        skill_roots=(tmp_path / "skills",), budget=12, **common)
    with execute.open_run(private, **common) as opened:
        service = opened.service
        old_task = next(task for task in service.plan.tasks if task.id == "later")
        changed_task = dataclasses.replace(old_task, objective="Analyze after acceptance.")
        replacement = dataclasses.replace(
            service.plan,
            tasks=tuple(changed_task if task.id == "later" else task
                        for task in service.plan.tasks),
        )
        plan_bytes = fanout.canonical_json(replacement.to_dict())
        plan_digest = hashlib.sha256(plan_bytes).hexdigest()
        new_inputs = dataclasses.replace(service.inputs, compiled_plan_sha256=plan_digest)
        task_bytes = fanout.canonical_json(changed_task.to_dict())
        old_preparation = service.preparations["later"]
        new_packet = dataclasses.replace(
            old_preparation.packet, compiled_plan=plan_bytes,
            compiled_plan_sha256=plan_digest, task=task_bytes,
            task_sha256=hashlib.sha256(task_bytes).hexdigest(),
        )
        preparation = dataclasses.replace(
            old_preparation, packet=new_packet, inputs=new_inputs, plan_revision=2,
        )
        original_append = opened.journal.append_amendment

        def crash_after_journal_acceptance(phase, **kwargs):
            original_append(phase, **kwargs)
            if phase == "plan-amendment-accepted":
                raise SystemExit("crash after journal acceptance")

        monkeypatch.setattr(opened.journal, "append_amendment", crash_after_journal_acceptance)
        with pytest.raises(SystemExit, match="journal acceptance"):
            service.submit_amendment(
                replacement, new_inputs, {"later": preparation},
                new_inputs.provider_profiles, expected_plan_revision=1,
                owner=opened.owner,
            )
    monkeypatch.undo()
    assert not launches
    with execute.open_run(private, **common) as stalled:
        assert stalled.service.plan != replacement
        assert stalled.service.status().plan_revision == 2
        if tamper_predecessor:
            record = stalled.execution_backend.read()
            corrupt = dataclasses.replace(
                record.snapshot, inputs_digest="0" * 64,
                backend_revision=record.revision + 1,
            )
            stalled.execution_backend.compare_and_set(
                record.revision, corrupt, owner=stalled.owner,
            )
    if tamper_predecessor:
        assert execute.main(["recover-amendment", "--private-root", str(private)],
                            **common) != 0
        assert not launches
        return
    assert execute.main(["recover-amendment", "--private-root", str(private)], **common) == 0
    assert json.loads(capsys.readouterr().out)["plan_revision"] == 2
    with execute.open_run(private, **common) as recovered:
        assert recovered.service.plan == replacement
        assert recovered.service.inputs.digest == new_inputs.digest
        assert recovered.service.resume(owner=recovered.owner).plan_revision == 2
    assert not launches


def test_cli_start_status_and_memory_block_use_same_private_run(execute, tmp_path, capsys):
    """The public command path must not bypass the tested service or leak owner token."""
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    packet_file = tmp_path / "admission.json"
    packet_file.write_bytes(fanout.canonical_json(packet))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    launched = []

    def no_provider(*args, **kwargs):
        launched.append((args, kwargs))
        raise AssertionError("provider must not launch")

    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True, provider_runner=no_provider)
    assert execute.main([
        "start", "--private-root", str(private), "--admission-file", str(packet_file),
        "--repo-root", str(repo), "--run-root", str(run),
        "--authority-root", str(authority), "--skill-root", str(tmp_path / "skills"),
        "--max-turns", "12",
    ], **common) == 0
    started = json.loads(capsys.readouterr().out)
    assert started["tasks"][0]["phase"] == "scheduled"
    assert "owner_token" not in json.dumps(started)
    assert execute.main(["status", "--private-root", str(private)], **common) == 0
    observed = json.loads(capsys.readouterr().out)
    assert {key: value for key, value in observed.items()
            if not key.startswith("unresolved_uncertain")} == started
    assert observed["unresolved_uncertain"] == []
    assert observed["unresolved_uncertain_total"] == 0
    assert observed["unresolved_uncertain_remaining"] == 0
    assert not launched
    blocked = dict(common, memory_preflight=lambda: False)
    assert execute.main(["resume", "--private-root", str(private)], **blocked) != 0
    output = capsys.readouterr()
    assert not output.out and "memory" in output.err.lower()
    assert not launched


def test_cold_status_lists_exact_unresolved_uncertain_turns_without_source_bytes(
    execute, tmp_path, capsys,
):
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *_args, **_kwargs: pytest.fail("status spent"))
    execute.prepare_run(
        packet, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=12,
        **common,
    )
    with execute.open_run(private, **common) as opened:
        task_id = opened.service.plan.tasks[0].id
        first, second = (seat.seat_id for seat in opened.service.preparations[task_id].seats)
        for seat_id in (first, second):
            opened.journal.append("dispatch-intent", task_id=task_id, seat_id=seat_id,
                                  attempt=1, round=1, evidence_sha256="a" * 64)
            opened.journal.append("process-started", task_id=task_id, seat_id=seat_id,
                                  attempt=1, round=1)
        assert opened.journal.recover_uncertain(opened.owner) == 2
        opened.journal.resolve_uncertain(opened.owner, task_id=task_id,
                                        seat_id=second, attempt=1, round=1)

    assert execute.main(["status", "--private-root", str(private)], **common) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["unresolved_uncertain"] == [{
        "task_id": task_id, "seat_id": first, "attempt": 1, "round": 1,
    }]
    assert status["unresolved_uncertain_total"] == 1
    assert status["unresolved_uncertain_remaining"] == 0
    assert "invoice rounding" not in json.dumps(status).lower()


def test_cold_status_caps_uncertain_identities_and_reports_remaining(execute, tmp_path, capsys):
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True,
                  provider_runner=lambda *_args, **_kwargs: pytest.fail("status spent"))
    execute.prepare_run(
        packet, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=12,
        **common,
    )
    with execute.open_run(private, **common) as opened:
        task_id = opened.service.plan.tasks[0].id
        for index in reversed(range(65)):
            seat_id = f"seat-{index:03d}"
            opened.journal.append("dispatch-intent", task_id=task_id, seat_id=seat_id,
                                  attempt=1, round=1, evidence_sha256="a" * 64)
            opened.journal.append("uncertain-attempt", task_id=task_id, seat_id=seat_id,
                                  attempt=1, round=1, owner=opened.owner)

    assert execute.main(["status", "--private-root", str(private)], **common) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["unresolved_uncertain_total"] == 65
    assert status["unresolved_uncertain_remaining"] == 1
    assert status["unresolved_uncertain"] == [
        {"task_id": task_id, "seat_id": f"seat-{index:03d}", "attempt": 1, "round": 1}
        for index in range(64)
    ]


def test_cli_rejects_nested_invocation_before_private_descriptor_read(execute, tmp_path,
                                                                      monkeypatch, capsys):
    monkeypatch.setenv("KHENRIX_NESTED_AGENT", "1")
    assert execute.main(["status", "--private-root", str(tmp_path / "missing")]) != 0
    assert "nested" in capsys.readouterr().err.lower()
    assert not (tmp_path / "missing").exists()


def test_cold_status_reopens_original_baseline_after_caller_changes(execute, tmp_path):
    """A handover or later human edit cannot erase the immutable run baseline."""
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    execute.prepare_run(
        packet, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=12,
        runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True, provider_runner=lambda *args, **kwargs: None,
    )
    original = json.loads((private / "run.json").read_text())["document"]["inputs"]["repo_baseline_sha256"]
    (repo / "source.txt").write_text("later caller edit\n", encoding="utf-8")
    with execute.open_run(
        private, runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True, provider_runner=lambda *args, **kwargs: None,
    ) as reopened:
        assert reopened.service.baseline.digest == original
        assert reopened.service.status().tasks[0].phase == "scheduled"


def test_cold_open_refuses_tampered_baseline_artifact_before_provider(execute, tmp_path):
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    launches = []

    def no_provider(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("tampered baseline must block before spend")

    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
                  memory_preflight=lambda: True, provider_runner=no_provider)
    execute.prepare_run(
        packet, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=12, **common,
    )
    manifest = run / "artifacts/baseline/manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    with pytest.raises(fanout.ArtifactError):
        execute.open_run(private, **common)
    assert not launches


def test_resume_fuses_two_hermetic_seats_and_cold_opens_completed_result(execute, tmp_path,
                                                                        capsys):
    """The public composition must actually drive rounds and submit verified synthesis."""
    spec = importlib.util.spec_from_file_location("fanout_smoke_fixture", ROOT / "scripts/fanout_smoke.py")
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = smoke
    spec.loader.exec_module(smoke)
    smoke.fanout = fanout
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repo = _repository(tmp_path)
    before = fanout.capture_repository_baseline(repo).digest
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    provider = smoke._FakeProvider(tmp_path, poison_peer=False)
    memory_factory = lambda artifacts, health: smoke._FakeMemory(tmp_path, artifacts)
    common = dict(runtime=fanout, registry=_registry(), memory_factory=memory_factory,
                  memory_preflight=lambda: True, provider_runner=provider)
    execute.prepare_run(
        packet, repo_root=repo, run_root=run, authority_root=authority,
        private_root=private, skill_roots=(tmp_path / "skills",), budget=12, **common,
    )
    with execute.open_run(private, **common) as opened:
        advanced = opened.service.resume(owner=opened.owner)
        assert advanced.tasks[0].phase == "reconciliation-pending"
    assert execute.main(["inspect-answers", "--private-root", str(private),
                         "--task-id", "answer"], **common) == 0
    inspected = json.loads(capsys.readouterr().out)
    private_answers = Path(inspected["private_answers_file"])
    assert private_answers.parent == private
    assert stat.S_IMODE(private_answers.stat().st_mode) == 0o600
    answers = json.loads(private_answers.read_text())
    assert len(answers["seats"]) == 2
    assert {seat["executor_id"] for seat in answers["seats"]} == {"claude", "codex"}
    assert all("answer" in seat for seat in answers["seats"])
    with execute.open_run(private, **common) as opened:
        sources = [execute._task_seat_id("answer", executor) for executor in ("claude", "codex")]
        verified = opened.service.verify_read_only_synthesis(
            "answer", source_seat_ids=sources, synthesizer_id="owner",
            answer=b"A final answer that considered both seats.\n", owner=opened.owner,
        )
        assert verified.valid
        result = opened.service.submit("answer", verified, owner=opened.owner)
        assert result.artifact == verified.answer_ref
    with execute.open_run(private, **common) as reopened:
        assert reopened.service.status().tasks[0].phase == "completed"
        assert reopened.artifacts.read_bytes(result.artifact) == b"A final answer that considered both seats.\n"
    assert fanout.capture_repository_baseline(repo).digest == before
    assert len(list((tmp_path / "calls").iterdir())) == 4


def _repo_write_fixture(execute, tmp_path: Path, *, complete: bool,
                        large_seat: bool = False):
    spec = importlib.util.spec_from_file_location(
        "fanout_smoke_candidate_fixture", ROOT / "scripts/fanout_smoke.py",
    )
    assert spec and spec.loader
    smoke = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = smoke
    spec.loader.exec_module(smoke)
    smoke.fanout = fanout
    packet, _ = _write_packet(tmp_path)
    repository = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    fake = smoke._FakeProvider(tmp_path, poison_peer=False)

    def provider(request, *, registry):
        target = Path(request.cwd) / "src/change.py"
        target.parent.mkdir(exist_ok=True)
        if large_seat and request.executor_id == "claude":
            target.write_bytes(b"L" * (24 * 1024 * 1024) + b"\n")
        else:
            target.write_text(f"candidate from {request.executor_id}\n", encoding="utf-8")
        return fake(request, registry=registry)

    common = dict(runtime=fanout, registry=_registry(write_characterized=True),
                  memory_factory=lambda artifacts, health: smoke._FakeMemory(tmp_path, artifacts),
                  memory_preflight=lambda: True, provider_runner=provider)
    execute.prepare_run(packet, repo_root=repository, run_root=run,
                        authority_root=authority, private_root=private,
                        skill_roots=(tmp_path / "skills",), budget=18, **common)
    if complete:
        with execute.open_run(private, **common) as opened:
            assert opened.service.resume(owner=opened.owner).tasks[0].phase == "reconciliation-pending"
    return private, run, common


def test_inspect_candidates_exports_exact_final_seats_privately(execute, tmp_path, capsys):
    private, _, common = _repo_write_fixture(execute, tmp_path, complete=True)
    caller = tmp_path / "repo"
    before = fanout.capture_repository_baseline(caller).digest
    inspection = {key: value for key, value in common.items() if key != "provider_runner"}
    for _ in range(2):
        assert execute.main(["inspect-candidates", "--private-root", str(private),
                             "--task-id", "change"], **inspection) == 0
        stdout = capsys.readouterr().out
        result = json.loads(stdout)
        assert set(result) == {"private_candidates_file", "sha256", "candidate_count"}
        assert result["candidate_count"] == 3
        assert "candidate from" not in stdout
        index = Path(result["private_candidates_file"])
        assert index.parent == private
        assert stat.S_IMODE(index.stat().st_mode) == 0o600
        raw = index.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == result["sha256"]
        document = json.loads(raw)
        assert document["schema_version"] == "fanout-private-final-candidates-v1"
        assert document["task_id"] == "change"
        assert len(document["candidates"]) == 3
        with execute.open_run(private, **common) as opened:
            state = opened.service._execution_state("change")
            assert document["run_id"] == opened.service.inputs.run_id
            assert document["barrier_sha256"] == state.barriers[-1].digest
            barrier = opened.service._restore_barrier(
                state.barriers[-1], opened.service._packet_for(opened.service._task("change")),
            )
            expected = dict(barrier.candidate_sources)
        for item in document["candidates"]:
            ref = item["candidate_ref"]
            assert item["seat_id"] in expected
            assert ref == execute._ref_document(expected[item["seat_id"]])
            source = Path(item["private_manifest_file"])
            assert source.parent == private
            assert stat.S_IMODE(source.stat().st_mode) == 0o600
            source_bytes = source.read_bytes()
            assert hashlib.sha256(source_bytes).hexdigest() == item["private_manifest_sha256"]
            assert item["candidate_sha256"] == ref["digest"]
            assert fanout.CandidateBundle.from_manifest(source_bytes).digest == ref["digest"]
            assert b"candidate from" not in stdout.encode()
    assert fanout.capture_repository_baseline(caller).digest == before


def test_inspect_candidates_refuses_not_terminal_and_wrong_class(execute, tmp_path, capsys):
    private, _, common = _repo_write_fixture(execute, tmp_path, complete=False)
    assert execute.main(["inspect-candidates", "--private-root", str(private),
                         "--task-id", "change"], **common) != 0
    assert not capsys.readouterr().out
    assert not list(private.glob("candidates-*.json"))

    question_root = tmp_path / "question"
    question_root.mkdir()
    packet, _ = _question_packet(question_root, executors=("claude", "codex"))
    repository = _repository(question_root)
    other_private, run, authority = (
        question_root / name for name in ("private", "run", "authority")
    )
    other_private.mkdir(mode=0o700)
    execute.prepare_run(
        packet, repo_root=repository, run_root=run, authority_root=authority,
        private_root=other_private, skill_roots=(question_root / "skills",), budget=12,
        runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True, provider_runner=lambda *args, **kwargs: None,
    )
    assert execute.main(["inspect-candidates", "--private-root", str(other_private),
                         "--task-id", "answer"], runtime=fanout, registry=_registry(),
                        memory_factory=_NeverMemory, memory_preflight=lambda: True,
                        provider_runner=lambda *args, **kwargs: None) != 0
    assert not capsys.readouterr().out
    assert not list(other_private.glob("candidates-*.json"))


def test_inspect_candidates_refuses_tampered_final_artifact(execute, tmp_path, capsys):
    private, run, common = _repo_write_fixture(execute, tmp_path, complete=True)
    with execute.open_run(private, **common) as opened:
        task = opened.service._task("change")
        state = opened.service._execution_state("change")
        barrier = opened.service._restore_barrier(state.barriers[-1],
                                                  opened.service._packet_for(task))
        ref = barrier.candidate_sources[0][1]
    (run / "artifacts" / ref.path).write_bytes(b"{}\n")
    assert execute.main(["inspect-candidates", "--private-root", str(private),
                         "--task-id", "change"], **common) != 0
    assert not capsys.readouterr().out
    assert not list(private.glob("candidates-*.json"))


def test_inspect_candidates_preserves_manifest_above_32_mib(execute, tmp_path, capsys):
    private, _, common = _repo_write_fixture(execute, tmp_path, complete=True,
                                             large_seat=True)
    assert execute.main(["inspect-candidates", "--private-root", str(private),
                         "--task-id", "change"], **common) == 0
    result = json.loads(capsys.readouterr().out)
    document = json.loads(Path(result["private_candidates_file"]).read_bytes())
    large = next(item for item in document["candidates"]
                 if item["seat_id"] == execute._task_seat_id("change", "claude"))
    source = Path(large["private_manifest_file"])
    assert 32 * 1024 * 1024 < source.stat().st_size <= 64 * 1024 * 1024
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert fanout.CandidateBundle.from_manifest(source.read_bytes()).digest == large["candidate_sha256"]


def test_synthesize_repository_reads_candidate_manifest_above_32_mib(execute, tmp_path,
                                                                      capsys):
    private, _, common = _repo_write_fixture(execute, tmp_path, complete=False)
    large = tmp_path / "large-candidate.json"
    large.write_bytes(b" " * (32 * 1024 * 1024 + 1))
    assert execute.main(["synthesize-repository", "--private-root", str(private),
                         "--task-id", "change", "--candidate-file", str(large)],
                        **common) != 0
    captured = capsys.readouterr()
    assert not captured.out
    assert "candidate manifest is not valid UTF-8 JSON" in captured.err
    assert "bounded regular file" not in captured.err


def test_unchecked_selection_ref_must_belong_to_named_seat(execute, tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("fanout_smoke_selection_fixture",
                                                  ROOT / "scripts/fanout_smoke.py")
    assert spec and spec.loader
    smoke = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = smoke
    spec.loader.exec_module(smoke)
    smoke.fanout = fanout
    packet, _ = _question_packet(tmp_path, executors=("claude", "codex"))
    repository = _repository(tmp_path)
    private, run, authority = (tmp_path / name for name in ("private", "run", "authority"))
    private.mkdir(mode=0o700)
    common = dict(runtime=fanout, registry=_registry(),
                  memory_factory=lambda artifacts, health: smoke._FakeMemory(tmp_path, artifacts),
                  memory_preflight=lambda: True,
                  provider_runner=smoke._FakeProvider(tmp_path, poison_peer=False))
    execute.prepare_run(packet, repo_root=repository, run_root=run,
                        authority_root=authority, private_root=private,
                        skill_roots=(tmp_path / "skills",), budget=12, **common)
    with execute.open_run(private, **common) as opened:
        assert opened.service.resume(owner=opened.owner).tasks[0].phase == "reconciliation-pending"
        answers = execute._inspect_answers(opened, "answer", private)
    seats = json.loads(Path(answers["private_answers_file"]).read_text())["seats"]
    first, second = seats
    wrong_ref = tmp_path / "wrong-answer-ref.json"
    wrong_ref.write_bytes(fanout.canonical_json(second["answer_ref"]))
    rc = execute.main([
        "select-answer", "--private-root", str(private), "--task-id", "answer",
        "--seat-id", first["seat_id"], "--answer-ref-file", str(wrong_ref),
    ], **common)
    assert rc != 0
    assert not capsys.readouterr().out
    with execute.open_run(private, **common) as opened:
        assert opened.service.status().tasks[0].phase == "reconciliation-pending"
