# llm-fanout-execute — native seats and owner reconciliation

The invoking CLI is both orchestrator and merger. Normal Claude, Codex and agy sessions execute admitted work with their installed skills and authorized read tools. Evaluators have the same read capabilities. Shared repository and service writes belong to the owner, not the seats.

```mermaid
flowchart TD
    accTitle: Native fanout execution and reconciliation
    accDescr: Exact native sessions exchange attributed complete evidence until their individual caps or stability, then the owner reconciles all configured final identities.
    PLAN[Validated admission packet] --> G_START{Exact source, skills, targets and owner authority?}
    G_START -->|invalid| STOP[Refuse before model spend]
    G_START -->|valid| START[Capture immutable baselines and private durable stores]
    START --> G_PREFLIGHT{Recorded CLI contracts and authenticated memory healthy?}
    G_PREFLIGHT -->|no| BLOCK[Expose durable blocked or uncertain status; no automatic replay]
    G_PREFLIGHT -->|yes| BLIND[Round one: same admitted context, independent answers]
    BLIND --> CLAUDE[Claude native session: medium, cap 2 example]
    BLIND --> CODEX[Codex native session: medium, cap 3 example]
    BLIND --> AGY[agy native session: high, cap 4 example]
    CLAUDE --> SHARE[Publish exact attributed answers to shared memory]
    CODEX --> SHARE
    AGY --> SHARE
    SHARE --> NEXT[Each active session reads full verified peer context]
    NEXT --> G_SETTLED{Every configured identity settled within its cap?}
    G_SETTLED -->|no| CONTINUE[Resume only active exact sessions; retain settled peers]
    CONTINUE --> SHARE
    G_SETTLED -->|yes| MERGE[Invoking owner reconciles all three final results at xhigh]
    MERGE --> CHECK[Apply scoped proposal in verifier workspace and run declared checks]
    CHECK --> G_CHECK{Exact verified result and owner approval?}
    G_CHECK -->|no| PENDING[Keep dependent tasks locked]
    G_CHECK -->|yes| RESULT[Publish result; owner-only application or ticket-linked handover]
```

| Gate | Kind | Evidence |
|---|---|---|
| G_START | code | `tests/test_fanout_execute_skill.py::test_bare_compiled_write_plan_is_not_an_admission`, `tests/test_fanout_execute_skill.py::test_write_bundle_requires_separate_exact_owner_review` |
| G_PREFLIGHT | code | `tests/test_fanout_execute_skill.py::test_memory_preflight_requires_authenticated_initialized_worker`, `tests/test_fanout_recorded_cli.py::test_recorded_dispatch_refuses_cli_drift_before_intent` |
| G_SETTLED | code | `tests/test_fanout_collaboration.py::test_active_frontiers_keep_settled_peer_revisions_without_relaunch`, `tests/test_fanout_collaboration.py::test_equal_answer_bytes_complete_all_configured_identities_in_cap_one_mode` |
| G_CHECK | code | `tests/test_fanout_owner_actions.py::test_public_owner_preview_completion_and_ticket_handover`, `tests/test_fanout_owner_actions.py::test_public_v1_owner_application_restores_authenticated_verifier_on_cold_reopen` |

Caps range from 1 to 10 per identity; the default is 2. Unequal caps do not manufacture missing turns. Equal answer bytes do not replace missing authors. Observed stability may stop a seat early, but every configured identity remains represented in the final reconciliation. Uncertain or failed attempts consume their recorded budget and are not automatically rerun.

Peer context is complete, attributed, lineage-checked untrusted evidence. Earlier settled revisions remain available to later active seats. Cold recovery authenticates source, baselines, sessions, journals and memory receipts; read-only status does not repair stores or start models. A memory failure is reported as blocked-memory.

The trusted native path is not OS confinement and does not promise credential blindness. Known write tools are denied where the native CLI supports this, with managed policy still applying. Owner application is scoped and drift-checked; sending messages requires separate approval of exact content and destination. Compilation, deterministic tests, live qualification and installation are distinct evidence.
