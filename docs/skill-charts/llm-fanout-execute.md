# llm-fanout-execute — durable owner execution

An admitted plan enters one owner-controlled run. `start` schedules only; `resume` crosses the provider-spend boundary after exact preflight. Source: `shared/skills/llm-fanout-execute/SKILL.md`; owner CLI: `shared/skills/llm-fanout-execute/scripts/execute.py`.

```mermaid
flowchart TD
    accTitle: Durable llm-fanout execution
    accDescr: Exact admission, memory and authority preflight precede provider rounds; only verified final results unlock dependencies and handover.

    P[Complete fanout-plan-admission-v1 packet] --> G_ADMISSION{Recompile source, draft,<br/>skill closure, and owner review}
    G_ADMISSION -->|invalid| STOP[Stop without provider spend]
    G_ADMISSION -->|valid| G_START_BOUNDARY{Start: any repository-write task<br/>without native seat boundary?}
    G_START_BOUNDARY -->|yes| STOP
    G_START_BOUNDARY -->|no| START[Capture dirty baseline,<br/>isolate seats, store private owner capability]
    START --> G_DESCRIPTOR{Final descriptor durable?}
    G_DESCRIPTOR -->|crash before publish| REC[Recover-start from private escrow<br/>and authenticated complete stores]
    G_DESCRIPTOR -->|yes| G_RESUME_BOUNDARY{Cold resume: authenticate journal and amendments<br/>without mutation; native seat boundary present?}
    REC --> G_RESUME_BOUNDARY
    G_RESUME_BOUNDARY -->|yes| STOP
    G_RESUME_BOUNDARY -->|no| G_PREFLIGHT{Authenticated memory<br/>and local authority?}
    G_PREFLIGHT -->|unhealthy or unresolved| BLOCK[Durable block; inspect status<br/>and use explicit owner recovery]
    G_PREFLIGHT -->|healthy| ROUND[Round one blind; round two receives<br/>exact-verified untrusted peer evidence]
    ROUND --> G_FINAL{Final barrier and<br/>minimum success?}
    G_FINAL -->|no| BLOCK
    G_FINAL -->|yes, read-only| ANS[Inspect final seat answers<br/>as untrusted evidence]
    G_FINAL -->|yes, repository| CAND[Inspect exact final candidates:<br/>private full-byte manifests and index]
    ANS --> SYN[Owner selects or synthesizes,<br/>then verifies fresh result]
    CAND --> SYN
    SYN -->|failed check| PENDING[Reconciliation pending;<br/>dependants remain locked]
    SYN -->|valid| DONE[Submit durable result;<br/>unlock dependants]
    DONE --> G_ACTION{Caller write or external action?}
    G_ACTION -->|yes| APPROVAL[Separate owner approval<br/>and transactional handover or action]
    G_ACTION -->|no| RETURN[Return final answer artifact]
```

| Gate | Kind | Evidence |
|---|---|---|
| G_ADMISSION | code | `tests/test_fanout_execute_skill.py::test_bare_compiled_write_plan_is_not_an_admission`, `tests/test_fanout_execute_skill.py::test_write_bundle_requires_separate_exact_owner_review` |
| G_DESCRIPTOR | code | `tests/test_fanout_execute_skill.py::test_pre_descriptor_crash_reopens_same_run_without_provider_spend` |
| G_PREFLIGHT | code | `tests/test_fanout_execute_skill.py::test_memory_preflight_requires_authenticated_initialized_worker` |
| G_START_BOUNDARY | code | `tests/test_fanout_execute_skill.py::test_public_start_rejects_repo_write_before_git_version_or_roots`, `tests/test_fanout_execute.py::test_mixed_plan_rejects_repo_write_before_first_read_only_profile_probe` |
| G_RESUME_BOUNDARY | code | `tests/test_fanout_execute_skill.py::test_public_cold_resume_rejects_repo_write_before_workspace_git_or_version`, `tests/test_fanout_execute_skill.py::test_public_resume_inspects_journal_before_mutating_recovery`, `tests/test_fanout_runstate.py::test_inspect_exposes_fsynced_pending_state_without_authority_commit` |
| G_FINAL | code | `tests/test_fanout_execute_skill.py::test_resume_fuses_two_hermetic_seats_and_cold_opens_completed_result`, `tests/test_fanout_execute_skill.py::test_inspect_candidates_exports_exact_final_seats_privately` |
| G_ACTION | code | `tests/test_fanout_execute.py::test_orchestrator_actions_remain_owner_barriers_and_never_reach_maka_or_a_provider` |

Additional evidence:

| Gate | Evidence |
|---|---|
| Bare compiled write plans and stale bundle review are refused | `tests/test_fanout_execute_skill.py::test_bare_compiled_write_plan_is_not_an_admission`, `test_write_bundle_requires_separate_exact_owner_review` |
| Authenticated, initialized claude-mem worker is required | `tests/test_fanout_execute_skill.py::test_memory_preflight_requires_authenticated_initialized_worker` |
| Owner capability is outside the run and privately reopened | `tests/test_fanout_execute_skill.py::test_private_descriptor_refuses_broad_modes_symlinks_and_tamper`, `test_start_and_cold_status_use_real_durable_stores_without_spend` |
| Dirty baseline is immutable across caller changes and rejects tamper | `tests/test_fanout_execute_skill.py::test_cold_status_reopens_original_baseline_after_caller_changes`, `test_cold_open_refuses_tampered_baseline_artifact_before_provider` |
| Pre-descriptor crash and failed preflight recover without a provider turn | `tests/test_fanout_execute_skill.py::test_pre_descriptor_crash_reopens_same_run_without_provider_spend`, `test_failed_start_preflight_is_retryable_without_provider_spend`, `test_recover_start_repairs_only_its_exact_atomic_publication_link` |
| Accepted amendment cold-open restores replacement bindings | `tests/test_fanout_execute_skill.py::test_accepted_amendment_cold_reopens_replacement_before_resume`, `test_accepted_profile_transition_cold_reopens_pinned_registry` |
| Scheduler-advanced amendment intent has authenticated cold recovery and rejects a stale replacement CLI | `tests/test_fanout_execute_skill.py::test_cli_recovers_scheduler_advanced_pending_amendment_after_cold_open`, `test_cli_recovers_pending_profile_transition_from_durable_descriptors` |
| Accepted journal with predecessor execution CAS reopens the predecessor; tampered bindings fail closed | `tests/test_fanout_execute_skill.py::test_cli_recovers_accepted_journal_before_execution_cas_after_cold_open` |
| Repository-write spend refuses when the native seat boundary is unproved | `tests/test_fanout_providers.py` repository-write initial/resume and mixed-batch refusal cases |
| Unchecked answer selection binds the named seat to the exact final answer | `tests/test_fanout_execute_skill.py::test_unchecked_selection_ref_must_belong_to_named_seat` |
| Owner candidate inspection exports only exact final-seat full manifests to private files and refuses tamper | `tests/test_fanout_execute_skill.py::test_inspect_candidates_exports_exact_final_seats_privately`, `test_inspect_candidates_refuses_tampered_final_artifact` |
| Real durable rounds can be reconciled after cold reopen | `tests/test_fanout_execute_skill.py::test_resume_fuses_two_hermetic_seats_and_cold_opens_completed_result` |
| Nested invocation is refused | `tests/test_fanout_execute_skill.py::test_nested_executor_cannot_orchestrate` |

The chart describes a control boundary, not a model judge. Same-UID private paths and separate Git workspaces are not a native seat boundary. Read-only runs remain available when repository-write isolation is not yet certified. Live pressure cases and installation checks remain separate release gates. An action approval is a digest-bound attestation, not cryptographic proof of who reviewed it.
