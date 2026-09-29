"""Strict, immutable execution-plan contracts for the fanout scheduler."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import signal
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

ROOT = Path(__file__).resolve().parents[1]
FANOUT_ROOT = ROOT / "shared" / "lib" / "fanout"
SPEC = importlib.util.spec_from_file_location(
    "_fanout_plan_contracts",
    FANOUT_ROOT / "__init__.py",
    submodule_search_locations=[str(FANOUT_ROOT)],
)
assert SPEC and SPEC.loader
fanout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fanout
SPEC.loader.exec_module(fanout)

FanoutPlanV1 = fanout.FanoutPlanV1
PlanValidationError = fanout.PlanValidationError
effective_skills = fanout.effective_skills
load_plan = fanout.load_plan
topological_order = fanout.topological_order
validate_plan = fanout.validate_plan
write_plan = fanout.write_plan
plan_module = sys.modules[f"{SPEC.name}.plan"]


def _v2_data() -> dict[str, object]:
    data = _data()
    data["schema_version"] = "v2"
    data["targets"] = [
        {"id": target, "repository": f"github.com/example/{target}-service",
         "ticket_key": "TASK-123", "branch_ref": f"refs/heads/feat/TASK-123-{target}"}
        for target in ("address", "booking")
    ]
    for step, target in zip(data["source_steps"], ("address", "booking")):
        step["target_id"] = target
    data["source_steps"][1]["id"] = "Task 2/Step 1"
    data["tasks"][1]["source_step_ids"] = ["Task 2/Step 1"]
    for task, target in zip(data["tasks"], ("address", "booking")):
        task["target_id"] = target
        task["dependency_modes"] = {dependency: "artifact" for dependency in task["depends_on"]}
    return data


def test_v2_same_relative_path_in_different_targets_does_not_overlap():
    data = _v2_data()
    data["tasks"][0]["owned_paths"] = ["src/shared.py"]
    data["tasks"][1]["owned_paths"] = ["src/shared.py"]

    plan = plan_module.load_fanout_plan(data)

    assert plan.schema_version == "v2"
    assert plan.tasks[0].target_id == "address"
    assert plan.tasks[1].target_id == "booking"


def test_v2_writer_cannot_mix_source_steps_from_two_targets():
    data = _v2_data()
    data["tasks"] = [data["tasks"][1]]
    data["tasks"][0]["source_step_ids"] = ["Task 1/Step 1", "Task 2/Step 1"]
    data["tasks"][0]["depends_on"] = []
    data["tasks"][0]["dependency_modes"] = {}

    with pytest.raises(PlanValidationError, match="target"):
        plan_module.load_fanout_plan(data)


def test_v2_check_cwd_cannot_escape_target():
    data = _v2_data()
    data["tasks"][0]["checks"][0]["cwd"] = "../booking"

    with pytest.raises(PlanValidationError, match="cwd|working directory"):
        plan_module.load_fanout_plan(data)


def test_v2_control_barrier_consumes_two_targets_without_executor_workspace():
    data = _v2_data()
    data["source_steps"].append({"id": "Task 3/Step 1", "sha256": _hash("integrate"),
                                 "target_id": "address"})
    data["tasks"].append({
        "id": "integration", "kind": "work", "parent_id": None,
        "title": "Integration", "objective": "Review both results.",
        "source_step_ids": ["Task 3/Step 1"], "depends_on": ["prepare", "implement"],
        "dependency_modes": {"prepare": "artifact", "implement": "artifact"},
        "target_id": "address", "execution_class": "orchestrator-action",
        "required_skills": [], "none_reason": "Owner reviews both results.",
        "owned_paths": [], "acceptance": ["Both results are reviewed."],
        "checks": [], "provider_policy": None,
    })

    plan = plan_module.load_fanout_plan(data)

    barrier = next(task for task in plan.tasks if task.id == "integration")
    assert barrier.execution_class == "orchestrator-action"
    assert barrier.owned_paths == () and barrier.checks == ()
    assert plan_module.topological_order(plan) == ("prepare", "implement", "integration")


def test_v2_one_target_cannot_have_two_repository_writers():
    data = _v2_data()
    data["source_steps"][0]["target_id"] = "booking"
    data["tasks"][0]["target_id"] = "booking"
    data["tasks"][0]["execution_class"] = "repo-write"

    with pytest.raises(PlanValidationError, match="one.*writer"):
        plan_module.load_fanout_plan(data)


def test_v2_dependency_modes_require_handover_from_a_writer():
    data = _v2_data()
    data["tasks"][1]["dependency_modes"] = {"prepare": "handover"}

    with pytest.raises(PlanValidationError, match="handover"):
        plan_module.load_fanout_plan(data)


def test_v2_plan_round_trip_preserves_registry_and_source_targets(tmp_path):
    plan = plan_module.load_fanout_plan(_v2_data())
    destination = tmp_path / "plan.json"

    write_plan(destination, plan)

    loaded = load_plan(destination)
    assert loaded == plan
    assert [target.id for target in loaded.targets] == ["address", "booking"]
    assert [step.target_id for step in loaded.source_steps] == ["address", "booking"]


def test_v2_loader_accepts_mapping_input():
    plan = plan_module.load_fanout_plan(MappingProxyType(_v2_data()))

    assert plan.schema_version == "v2"


def test_v2_source_task_cannot_declare_two_targets():
    data = _v2_data()
    data["source_steps"][1]["id"] = "Task 1/Step 2"
    data["tasks"][1]["source_step_ids"] = ["Task 1/Step 2"]

    with pytest.raises(PlanValidationError, match="source Task.*target"):
        plan_module.load_fanout_plan(data)


@pytest.mark.parametrize("child_kind", ["source_step", "task"])
def test_typed_v1_plan_rejects_v2_children_before_serialization(child_kind, tmp_path):
    plan = _plan()
    if child_kind == "source_step":
        step = plan.source_steps[0]
        children = {"source_steps": (plan_module.SourceStepV2(step.id, step.sha256, "address"),
                                     *plan.source_steps[1:])}
    else:
        task = plan.tasks[0]
        v2_task = plan_module.PlanTaskV2.from_dict({
            **task.to_dict(), "target_id": "address", "dependency_modes": {},
        }, where="task")
        children = {"tasks": (v2_task, *plan.tasks[1:])}

    with pytest.raises(PlanValidationError, match="v1|V1"):
        mixed = replace(plan, **children)
        write_plan(tmp_path / "mixed.json", mixed)
    assert not (tmp_path / "mixed.json").exists()


@pytest.mark.parametrize("mode", [[], {}])
def test_v2_dependency_mode_value_must_be_a_string(mode):
    data = _v2_data()
    data["tasks"][1]["dependency_modes"] = {"prepare": mode}

    with pytest.raises(PlanValidationError, match="dependency_modes"):
        plan_module.load_fanout_plan(data)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _data() -> dict[str, object]:
    return {
        "schema_version": "v1",
        "source": {
            "path": "docs/superpowers/plans/example.md",
            "sha256": _hash("source"),
            "parser_version": "parser-v1",
        },
        "defaults": {
            "executor_ids": ["claude", "codex", "agy"],
            "rounds": 2,
            "timeout": 120,
            "retries": 1,
            "minimum_success": 2,
        },
        "source_steps": [
            {"id": "Task 1/Step 1", "sha256": _hash("step one")},
            {"id": "Task 1/Step 2", "sha256": _hash("step two")},
        ],
        "tasks": [
            {
                "id": "prepare",
                "kind": "work",
                "parent_id": None,
                "title": "Prepare",
                "objective": "Inspect the implementation surface.",
                "source_step_ids": ["Task 1/Step 1"],
                "depends_on": [],
                "execution_class": "read-only",
                "required_skills": ["code-search"],
                "none_reason": None,
                "owned_paths": ["shared/lib/fanout"],
                "acceptance": ["The surface is inspected."],
                "checks": [{
                    "argv": ["python", "-m", "pytest", "-q"],
                    "cwd": "",
                    "env_allowlist": ["PATH", "LANG"],
                    "timeout": 30,
                    "accepted_exit_codes": [0],
                    "expected_artifacts": ["reports/prepare.json"],
                }],
                "provider_policy": None,
            },
            {
                "id": "implement",
                "kind": "work",
                "parent_id": None,
                "title": "Implement",
                "objective": "Add the bounded implementation.",
                "source_step_ids": ["Task 1/Step 2"],
                "depends_on": ["prepare"],
                "execution_class": "repo-write",
                "required_skills": ["test-driven-development"],
                "none_reason": None,
                "owned_paths": ["shared/lib/fanout/plan.py"],
                "acceptance": ["The implementation has focused tests."],
                "checks": [],
                "provider_policy": {
                    "executor_ids": ["claude", "codex"],
                    "rounds": 3,
                    "timeout": 300,
                    "retries": 2,
                    "minimum_success": 2,
                },
            },
        ],
    }


def _plan(data: dict[str, object] | None = None) -> FanoutPlanV1:
    return FanoutPlanV1.from_dict(_data() if data is None else data)


@contextmanager
def _rejects_hang_after_one_second():
    """Turn a malformed-plan loop into an ordinary failed regression assertion."""
    def expire(_signal, _frame):
        raise TimeoutError("schema validation did not terminate")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, 1)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_canonical_round_trip_retains_source_defaults_and_is_immutable(tmp_path):
    """A mutable or lossy plan could change what a later scheduler executes."""
    source = _data()
    plan = _plan(source)
    source["defaults"]["rounds"] = 3  # type: ignore[index]

    destination = tmp_path / "plan.json"
    write_plan(destination, plan)

    assert destination.read_bytes() == (
        json.dumps(plan.to_dict(), allow_nan=False, ensure_ascii=False,
                   separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    )
    restored = load_plan(destination)
    assert restored == plan
    assert restored.source.path == "docs/superpowers/plans/example.md"
    assert restored.defaults.rounds == 2
    with pytest.raises((AttributeError, TypeError)):
        restored.tasks[0].required_skills += ("other",)


@pytest.mark.parametrize("mutate", [
    lambda value: value.__setitem__("extra", True),
    lambda value: value["tasks"][0].__setitem__("extra", True),  # type: ignore[index]
    lambda value: value["defaults"].__setitem__("unknown", 1),  # type: ignore[index]
    lambda value: value.__setitem__("schema_version", "v2"),
])
def test_schema_rejects_unknown_fields_and_other_versions(mutate):
    """Permissive parsing would let a misspelled safety control disappear silently."""
    data = _data()
    mutate(data)

    with pytest.raises(PlanValidationError):
        _plan(data)


def test_source_registry_is_hashed_and_work_tasks_partition_it_exactly_once():
    """Duplicating, omitting, or splitting source evidence changes the execution scope."""
    data = _data()
    duplicate = copy.deepcopy(data)
    duplicate["tasks"][1]["source_step_ids"] = ["Task 1/Step 1"]  # type: ignore[index]
    missing = copy.deepcopy(data)
    missing["tasks"][1]["source_step_ids"] = []  # type: ignore[index]
    unregistered = copy.deepcopy(data)
    unregistered["tasks"][1]["source_step_ids"] = ["Task 9/Step 9"]  # type: ignore[index]
    malformed_hash = copy.deepcopy(data)
    malformed_hash["source_steps"][0]["sha256"] = "bad"  # type: ignore[index]

    for candidate in (duplicate, missing, unregistered, malformed_hash):
        with pytest.raises(PlanValidationError):
            _plan(candidate)


def test_groups_form_a_real_acyclic_hierarchy_and_never_become_dependencies():
    """Scheduling a structural group or a hierarchy loop would deadlock the DAG."""
    data = _data()
    group = {
        "id": "phase",
        "kind": "group",
        "parent_id": None,
        "title": "Phase",
        "objective": "Organize child work.",
        "required_skills": ["skill-a"],
    }
    data["tasks"].insert(0, group)  # type: ignore[index]
    data["tasks"][1]["parent_id"] = "phase"  # type: ignore[index]
    data["tasks"][2]["parent_id"] = "phase"  # type: ignore[index]
    assert _plan(data).tasks[0].kind == "group"

    group_dependency = copy.deepcopy(data)
    group_dependency["tasks"][1]["depends_on"] = ["phase"]  # type: ignore[index]
    hierarchy_cycle = copy.deepcopy(data)
    hierarchy_cycle["tasks"][0]["parent_id"] = "prepare"  # type: ignore[index]
    for candidate in (group_dependency, hierarchy_cycle):
        with pytest.raises(PlanValidationError):
            _plan(candidate)


def test_cyclic_group_lineage_with_descendant_work_fails_without_hanging():
    """Inherited skills must not traverse a cyclic group lineage forever."""
    data = _data()
    data["tasks"][0]["parent_id"] = "a"  # type: ignore[index]
    data["tasks"].extend([  # type: ignore[index]
        {
            "id": "a", "kind": "group", "parent_id": "b", "title": "A",
            "objective": "First cyclic group.", "required_skills": ["skill-a"],
        },
        {
            "id": "b", "kind": "group", "parent_id": "a", "title": "B",
            "objective": "Second cyclic group.", "required_skills": ["skill-b"],
        },
    ])

    with _rejects_hang_after_one_second(), pytest.raises(PlanValidationError):
        _plan(data)


def test_effective_skill_helper_has_its_own_cycle_backstop():
    """Direct helper use must fail closed even before a caller validates the whole plan."""
    group_a = fanout.PlanTaskV1("a", "group", "A", "First cyclic group.", parent_id="b")
    group_b = fanout.PlanTaskV1("b", "group", "B", "Second cyclic group.", parent_id="a")
    work = fanout.PlanTaskV1(
        "work", "work", "Work", "Descends from the groups.", parent_id="a",
        source_step_ids=("Task 1/Step 1",), execution_class="read-only",
        required_skills=("skill-a",), acceptance=("It is bounded.",),
    )
    malformed = object.__new__(FanoutPlanV1)
    object.__setattr__(malformed, "tasks", (group_a, group_b, work))

    with _rejects_hang_after_one_second(), pytest.raises(PlanValidationError):
        plan_module._effective_skills(malformed, work)


def test_dependencies_are_work_only_acyclic_and_have_stable_topological_order():
    """A dependency cycle or unstable sort would make scheduling non-repeatable."""
    plan = _plan()
    assert topological_order(plan) == ("prepare", "implement")
    cyclic = _data()
    cyclic["tasks"][0]["depends_on"] = ["implement"]  # type: ignore[index]
    self_dependency = _data()
    self_dependency["tasks"][0]["depends_on"] = ["prepare"]  # type: ignore[index]

    for candidate in (cyclic, self_dependency):
        with pytest.raises(PlanValidationError):
            _plan(candidate)


def test_effective_executor_skills_inherit_groups_and_exclude_orchestrator_controls():
    """A seat must receive inherited executor skills but never orchestration controls."""
    data = _data()
    data["tasks"].insert(0, {
        "id": "phase", "kind": "group", "parent_id": None,
        "title": "Phase", "objective": "Organize.",
        "required_skills": ["brainstorming", "dispatching-parallel-agents",
                            "finishing-a-development-branch", "llm-council",
                            "llm-fanout-plan", "skill-a"],
    })  # type: ignore[index]
    data["tasks"][1]["parent_id"] = "phase"  # type: ignore[index]
    data["tasks"][1]["required_skills"] = ["requesting-code-review", "llm-forge",
                                            "llm-fanout-execute"]  # type: ignore[index]
    data["tasks"][1]["none_reason"] = None  # type: ignore[index]

    plan = _plan(data)
    assert effective_skills(plan, "prepare") == ("skill-a",)

    no_reason = copy.deepcopy(data)
    no_reason["tasks"][0]["required_skills"] = ["brainstorming"]  # type: ignore[index]
    with pytest.raises(PlanValidationError):
        _plan(no_reason)


def test_executor_skill_declarations_reject_duplicate_or_conflicting_values():
    """Ambiguous skill selection could load the wrong behavior into a provider seat."""
    duplicate = _data()
    duplicate["tasks"][0]["required_skills"] = ["code-search", "code-search"]  # type: ignore[index]
    conflicting = _data()
    conflicting["tasks"][0]["none_reason"] = "No skills needed"  # type: ignore[index]

    for candidate in (duplicate, conflicting):
        with pytest.raises(PlanValidationError):
            _plan(candidate)


def test_orchestrator_actions_are_barriers_without_provider_work():
    """An external action must remain an owner-controlled scheduler barrier."""
    data = _data()
    action = copy.deepcopy(data["tasks"][0])  # type: ignore[index]
    action.update({
        "id": "approve", "execution_class": "orchestrator-action",
        "source_step_ids": ["Task 1/Step 2"], "depends_on": ["prepare"],
        "required_skills": [], "none_reason": "Owner approval is required.",
        "provider_policy": None, "checks": [], "owned_paths": [],
    })
    data["tasks"] = [data["tasks"][0], action]  # type: ignore[index]
    assert _plan(data).tasks[1].execution_class == "orchestrator-action"

    action["required_skills"] = ["code-search"]
    with pytest.raises(PlanValidationError):
        _plan(data)


@pytest.mark.parametrize("mutate", [
    lambda value: value["defaults"].__setitem__("executor_ids", ["maka"]),  # type: ignore[index]
    lambda value: value["defaults"].__setitem__("rounds", 4),  # type: ignore[index]
    lambda value: value["defaults"].__setitem__("minimum_success", 4),  # type: ignore[index]
    lambda value: value["tasks"][0]["provider_policy"].__setitem__("timeout", 0),  # type: ignore[index]
    lambda value: value["tasks"][0]["provider_policy"].__setitem__("retries", 4),  # type: ignore[index]
])
def test_provider_policy_has_bounded_admitted_executor_settings(mutate):
    """Invalid provider policy could spend on an unsupported or unbounded execution."""
    data = _data()
    data["tasks"][0]["provider_policy"] = copy.deepcopy(data["defaults"])  # type: ignore[index]
    mutate(data)
    with pytest.raises(PlanValidationError):
        _plan(data)


@pytest.mark.parametrize("override", [
    {"executor_ids": ["claude"], "minimum_success": 1},
    {"minimum_success": 1},
])
def test_default_policy_requires_two_distinct_executors_and_two_successes(override):
    """A default single seat violates the plan's mandatory redundant execution contract."""
    data = _data()
    data["defaults"].update(override)  # type: ignore[index]

    with pytest.raises(PlanValidationError):
        _plan(data)


@pytest.mark.parametrize("override", [
    {"executor_ids": ["claude"], "minimum_success": 1},
    {"minimum_success": 1},
])
def test_task_policy_requires_two_distinct_executors_and_two_successes(override):
    """A task override must not weaken the default multi-seat safety floor."""
    data = _data()
    data["tasks"][0]["provider_policy"] = copy.deepcopy(data["defaults"])  # type: ignore[index]
    data["tasks"][0]["provider_policy"].update(override)  # type: ignore[index]

    with pytest.raises(PlanValidationError):
        _plan(data)


def test_owned_paths_are_normalized_and_only_overlap_when_dependency_orders_them():
    """Concurrent ownership overlap permits racing writes to the same repository surface."""
    unordered = _data()
    unordered["tasks"][1]["depends_on"] = []  # type: ignore[index]
    unordered["tasks"][1]["owned_paths"] = ["shared/lib/fanout/plan.py"]  # type: ignore[index]
    invalid = _data()
    invalid["tasks"][0]["owned_paths"] = ["../outside"]  # type: ignore[index]

    for candidate in (unordered, invalid):
        with pytest.raises(PlanValidationError):
            _plan(candidate)

    assert _plan().tasks[1].owned_paths == ("shared/lib/fanout/plan.py",)


def test_root_path_aliases_cannot_evade_owned_path_or_artifact_containment():
    """A repository-root alias would overlap every concurrently owned subpath."""
    root_and_subpath = _data()
    root_and_subpath["tasks"][0]["owned_paths"] = ["."]  # type: ignore[index]
    root_and_subpath["tasks"][1]["depends_on"] = []  # type: ignore[index]
    root_and_subpath["tasks"][1]["owned_paths"] = ["shared/lib"]  # type: ignore[index]
    artifact_root = _data()
    artifact_root["tasks"][0]["checks"][0]["expected_artifacts"] = ["."]  # type: ignore[index]
    cwd_root = _data()
    cwd_root["tasks"][0]["checks"][0]["cwd"] = "."  # type: ignore[index]

    for candidate in (root_and_subpath, artifact_root, cwd_root):
        with pytest.raises(PlanValidationError):
            _plan(candidate)

    assert _plan().tasks[0].checks[0].cwd == ""


@pytest.mark.parametrize("field,value", [
    ("argv", "python -m pytest"),
    ("argv", ["python", ""]),
    ("cwd", "../outside"),
    ("env_allowlist", {"API_TOKEN": "literal-secret"}),
    ("timeout", 0),
    ("accepted_exit_codes", [0, 0]),
    ("expected_artifacts", ["reports/../escape.json"]),
])
def test_checks_require_structured_contained_secret_free_values(field, value):
    """Shell strings or a literal secret would escape the bounded verifier contract."""
    data = _data()
    data["tasks"][0]["checks"][0][field] = value  # type: ignore[index]
    with pytest.raises(PlanValidationError):
        _plan(data)


@pytest.mark.parametrize("argv", [
    ["sh", "-c", "echo unsafe"],
    ["/bin/BASH", "-lc", "echo unsafe"],
    ["zsh", "-cl", "echo unsafe"],
    ["cmd.exe", "/C", "echo unsafe"],
    ["C:/Windows/System32/WindowsPowerShell/v1.0/PowerShell.EXE", "-Command", "echo unsafe"],
    ["pwsh", "-c", "echo unsafe"],
    ["env", "bash", "-c", "echo unsafe"],
    ["/usr/bin/env", "-i", "CHECK_MODE=1", "sh", "-c", "echo unsafe"],
    ["env", "--", "pwsh", "-Command", "echo unsafe"],
    ["env", "-S", "bash -c 'echo unsafe'"],
    ["timeout", "5", "bash", "-c", "echo unsafe"],
    ["mise", "exec", "--", "bash", "-c", "echo unsafe"],
    ["mise", "exec", "python@3.12.14", "--", "bash", "-c", "echo unsafe"],
])
def test_checks_reject_shell_command_strings_even_as_argv(argv):
    """An argv-wrapped command string still invokes a shell parser and is unsafe."""
    data = _data()
    data["tasks"][0]["checks"][0]["argv"] = argv  # type: ignore[index]

    with pytest.raises(PlanValidationError):
        _plan(data)


def test_checks_allow_env_wrapped_direct_program():
    data = _data()
    data["tasks"][0]["checks"][0]["argv"] = ["env", "CHECK_MODE=1", "pytest", "-q"]  # type: ignore[index]

    assert _plan(data).tasks[0].checks[0].argv == ("env", "CHECK_MODE=1", "pytest", "-q")


def test_checks_allow_mise_tool_selectors_for_direct_program():
    data = _data()
    command = ("mise", "exec", "python@3.12.14", "--", "python", "-m", "pytest", "-q")
    data["tasks"][0]["checks"][0]["argv"] = list(command)  # type: ignore[index]

    assert _plan(data).tasks[0].checks[0].argv == command


def test_load_rejects_duplicate_keys_noncanonical_numbers_and_trailing_data(tmp_path):
    """A noncanonical read could make one persisted revision mean different things."""
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"v1","schema_version":"v1"}\n', encoding="utf-8")
    trailing = tmp_path / "trailing.json"
    trailing.write_bytes(json.dumps(_data()).encode() + b"\ntrailing")
    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(json.dumps({"x": math.nan}), encoding="utf-8")

    for candidate in (duplicate, trailing, noncanonical):
        with pytest.raises(PlanValidationError):
            load_plan(candidate)


def test_write_is_exclusive_until_an_explicit_atomic_replacement(tmp_path):
    """Accidental revision replacement would invalidate a settled execution packet."""
    destination = tmp_path / "plan.json"
    first = _plan()
    second_data = _data()
    second_data["source"]["parser_version"] = "parser-v2"  # type: ignore[index]
    second = _plan(second_data)

    write_plan(destination, first)
    with pytest.raises(PlanValidationError):
        write_plan(destination, second)
    write_plan(destination, second, replace=True)

    assert load_plan(destination) == second
    assert validate_plan(second) is second
