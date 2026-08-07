# khenrix-setup — flow

Rendered per CLI from ONE shared template — this chart draws that shared flow, not any
single CLI's rendering. The read-only review always runs first, even when the user tries
to waive it. Every managed surface is classified against `capabilities.toml` as EXTRA /
ADD / UPDATE / MATCH; an unreadable config file or an unpaired instruction marker refuses
rather than guesses. The user confirms before an additive apply, and drifted managed
entries are left alone unless `--update-drift` is asked for. Source:
`shared/skill-templates/khenrix-setup/SKILL.md.tmpl`; engine `scripts/lib/reconcile.py`.

```mermaid
flowchart TD
    accTitle: khenrix-setup reconcile flow
    accDescr: One shared template rendered per CLI. The read-only review always runs first even when the user waives it. Every managed surface classifies against capabilities.toml as EXTRA, ADD, UPDATE or MATCH; unreadable config and unpaired instruction markers refuse rather than guess. The user confirms before an additive apply that leaves drift alone unless update-drift is requested.

    START([user wants this CLI<br/>reconciled with capabilities.toml]) --> G_WAIVE{user waives the<br/>review pause?}
    G_WAIVE -- "even so, trust it" --> REVIEW_ANYWAY[Step 1 runs anyway: show the<br/>ADD list before or alongside apply]
    G_WAIVE -- "no, normal ask" --> REVIEW[Step 1: reconcile.py --cli CLI,<br/>read-only review]
    REVIEW_ANYWAY --> G_READABLE{every live config file<br/>parses as JSON or TOML?}
    REVIEW --> G_READABLE
    G_READABLE -- "no, unparseable" --> HALT_READ([refuse: raises rather than<br/>treating unreadable as empty])
    G_READABLE -- "yes" --> CLASSIFY

    subgraph SURFACES [classify every managed surface]
        CLASSIFY[MCP servers, settings, statusline,<br/>hooks, shell aliases] --> G_DECLARED{entry name declared<br/>in capabilities.toml?}
        G_DECLARED -- "no" --> MARK_EXTRA[EXTRA: left untouched,<br/>never removed]
        G_DECLARED -- "yes" --> G_DRIFT{live entry matches<br/>the declared spec?}
        G_DRIFT -- "absent" --> MARK_ADD[ADD]
        G_DRIFT -- "present, differs" --> MARK_UPDATE[UPDATE]
        G_DRIFT -- "present, same" --> MARK_MATCH[MATCH]

        INSTR[base instructions block] --> G_MARKERS{khenrix-managed markers<br/>well paired in the target?}
        G_MARKERS -- "no, orphaned or inverted" --> REFUSE_INSTR[REFUSED: nothing written,<br/>repair the markers by hand]
        G_MARKERS -- "yes" --> INSTR_CLASSIFY[MATCH, UPDATE or ADD<br/>the house-style block]
    end

    MARK_EXTRA --> SUMMARIZE[Step 2: summarize the<br/>full report to the user]
    MARK_ADD --> SUMMARIZE
    MARK_UPDATE --> SUMMARIZE
    MARK_MATCH --> SUMMARIZE
    REFUSE_INSTR --> SUMMARIZE
    INSTR_CLASSIFY --> SUMMARIZE

    SUMMARIZE --> G_CONFIRM{Step 3: user approves<br/>applying the additions?}
    G_CONFIRM -- "no" --> SHOW([stop: diff shown,<br/>nothing written])
    G_CONFIRM -- "yes" --> BACKUP[back up every file about to<br/>change: numbered khenrix-backup]

    BACKUP --> APPLY[Step 4: reconcile.py --cli CLI<br/>--apply, ADD-only by default]
    APPLY --> G_UPDATE_DRIFT{user explicitly wants drifted<br/>managed entries realigned?}
    G_UPDATE_DRIFT -- "no" --> LEAVE[drifted entries<br/>left as-is]
    G_UPDATE_DRIFT -- "yes" --> REALIGN[--update-drift<br/>reapplies them]

    LEAVE --> VERIFY[Step 5: verify by re-running<br/>the read-only review]
    REALIGN --> VERIFY
    VERIFY --> DONE([reconciled: additive,<br/>EXTRA untouched, backups made])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_WAIVE | agent | `evals/khenrix-setup/evals.json::Still does a read-only review first and surfaces what will be added before applying, instead of blindly applying because the user said to skip it` |
| G_READABLE | code | `scripts/lib/reconcile.py::def read_json_object` |
| G_DECLARED | code | `scripts/lib/reconcile.py::def classify_mcp` |
| G_DRIFT | code | `scripts/lib/reconcile.py::def mcp_drift` |
| G_MARKERS | code | `scripts/lib/reconcile.py::def instructions_report` |
| G_CONFIRM | agent | `evals/khenrix-setup/evals.json::Requires explicit user confirmation before applying (an --apply run); does not apply changes unprompted` |
| G_UPDATE_DRIFT | agent | no eval covers this; SKILL.md.tmpl's Notes section — default behavior leaves drifted managed entries as-is, only an explicit `--update-drift` re-applies them |
