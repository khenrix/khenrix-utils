# llm-fanout-plan — admission and compilation

One accepted input becomes one validated, skill-aware DAG. Claude, Codex, and agy are executor seats inside each work task, not extra DAG nodes. In v2, every work task has one repository target; separate targets may use the same ticket and each may have one writer. Source: `shared/skills/llm-fanout-plan/SKILL.md`; deterministic compiler: `shared/lib/fanout/compiler.py`.

```mermaid
flowchart TD
    accTitle: llm-fanout-plan admission and compilation
    accDescr: Direct questions, approved plans, and task bundles enter through distinct admission gates before strict source compilation and skill resolution.

    IN{Input kind} -->|direct question| Q[One read-only task]
    IN -->|fresh build, no accepted plan| SP[brainstorming then writing-plans]
    SP --> P
    IN -->|approved source plan| P[Partition whole Steps]
    IN -->|owned task bundle| B{Repo write or action?}
    B -->|yes| R[Separate owner review file<br/>bound to bundle digest]
    B -->|no| P
    R --> P
    Q --> T{Quality tier and class agree?}
    P --> T
    T -->|deep with write or action| STOP[Refuse without provider spend]
    T -->|normal to standard; read-only deep to deep| C[Strict source and draft compiler]
    C --> S[Resolve executor skill closure]
    S --> V[Validate exact dependencies,<br/>paths, checks, and skills]
    V --> D[Validated fanout DAG]
    D --> G_V2_START{V2 target registry?}
    G_V2_START -->|yes| DORMANT[Dormant v2 owner start;<br/>public resume refused]
    G_V2_START -->|no, v1| E{Profiles characterized<br/>and installed CLIs match?}
    E -->|yes| RUN[V1 execution preflight]
    E -->|no| BLOCK[Block before provider spend]
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_V2_START | code | `tests/test_fanout_multirepo_integration.py::test_two_writer_admission_starts_dormant_owner_run` |

## Additional evidence

| Gate | Evidence |
|---|---|
| Direct question has one read-only task | `tests/test_fanout_plan_skill.py::test_direct_question_is_one_read_only_task_with_invocation_approval` |
| Direct deep question preserves its tier | `tests/test_fanout_plan_skill.py::test_direct_question_preserves_deep_tier_for_one_read_only_task` |
| Hidden or malformed source tier is refused | `tests/test_fanout_compiler.py::test_compiler_refuses_hidden_or_malformed_source_tier_hint` |
| Read-only task may use deep under normal defaults | `tests/test_fanout_compiler.py::test_compiler_preserves_task_local_deep_read_only_in_standard_plan` |
| Uncharacterized deep profile blocks before spend | `tests/test_fanout_profiles.py::test_uncharacterized_deep_turn_blocks_without_provider_spend` |
| V2 dormant versus v1 profile admission | `tests/test_fanout_multirepo_integration.py::test_two_writer_admission_starts_dormant_owner_run` |
| Write review is separate and byte-bound | `tests/test_fanout_plan_skill.py::test_write_bundle_requires_digest_bound_owner_review`, `test_bundle_cannot_self_assert_its_owner_review` |
| Source/draft/skill drift is refused | `tests/test_fanout_plan_skill.py::test_compile_and_validate_recompute_exact_source_and_draft` |
| Source work cannot disappear or claim false write concurrency | `tests/test_fanout_compiler.py::test_parser_rejects_action_or_constraints_after_task_section`, `test_compiler_rejects_repo_write_without_owned_paths`, `test_compiler_requires_one_repo_write_owner_for_each_source_task` |
| Runtime and memory controller are delivered and receipt-bound | `tests/test_fanout_plan_skill.py::test_delivery_stages_runtime_and_memory_and_receipt_covers_both` |
| One ticket may own independent local branches in two repositories | `tests/test_fanout_multirepo_integration.py::test_two_writer_admission_starts_dormant_owner_run`, `test_two_repositories_one_run_two_ticket_branches` |
| Cross-target dependencies distinguish verified artifact from completed handover | `tests/test_fanout_multirepo_integration.py::test_cross_repository_artifact_unlocks_before_branch_handover`, `test_two_repositories_one_run_two_ticket_branches` |
| The same owner-reviewed packet reaches native fake seats, reconciliation, and both local handovers | `tests/test_fanout_multirepo_integration.py::test_admitted_packet_runs_native_fake_seats_through_both_handovers` (disposable shell transport only) |

Source and bundle tiers bind plan defaults; normal defaults may contain a read-only deep task. A top-level deep plan contains only read-only deep work. A source Task declaring `Create`/`Modify` paths has one repo-write owner; separate owners for those paths need separate approved source Tasks. Read-only source Steps may join distinct writers whose write paths come from other source Tasks. The chart's execution preflight is a separate admission gate; compilation does not characterize an installed CLI or approve provider spend. Owner review is a digest-bound attestation, not cryptographic identity proof.

Bundle write admission uses `from-bundle` with a separate `--owner-review-file`. The `compile` and `validate` commands do not accept that option. Changing declared bundle values after review requires a new review bound to the new canonical digest.

For v2, `Target` metadata and dependency modes are part of the approved source plan. An `artifact` edge may unlock after verified reconciliation; a `handover` edge waits for an authenticated local branch terminal. An orchestrator-only integration barrier may consume both targets without giving a seat access to both repositories. Public v2 provider execution remains closed until live native certification; a valid plan permits only a dormant owner run today.
