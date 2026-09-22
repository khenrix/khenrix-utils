# khenrix-upgrade — flow

Rendered per CLI from ONE shared template — this chart draws that shared flow; only the
research sources and native review tooling differ per rendered CLI, the steps themselves
do not. Inventory, then research that must be sourced this run rather than recalled from
memory, then a review of the khenrix skills. Findings only proceed once research shows an
actual gap, and then split into a repo-edit bucket (applied with confirmation, gated on
`mise run verify`) versus a live-config bucket that is only ever recommended. Repo edits
then take the direct-copy path, the optional-plugin path, or both according to the files
changed. Every run ends with the dated report, even a no-change one. Source:
`shared/skill-templates/khenrix-upgrade/SKILL.md.tmpl`.

```mermaid
flowchart TD
    accTitle: khenrix-upgrade flow
    accDescr: One shared template rendered per CLI with different research and review tooling but the same steps. Inventory, research sourced this run, and review lead to repo edits or live recommendations. Approved repo edits pass mise verify and then use direct skill delivery, plugin refresh, or both according to the changed surface. Every run ends with the dated report.

    START([user wants this CLI's<br/>setup modernized]) --> LOCATE[locate the khenrix-utils repo:<br/>edit here, never the installed copy]

    subgraph PERCLI [rendered per CLI: same steps, CLI-native tools]
        INVENTORY[Step 1: snapshot current<br/>version, model, installed skills]
        RESEARCH[Step 2: deep multi-source research,<br/>CLI changes, models, best practices]
        REVIEW[Step 3: review the khenrix skills<br/>with this CLI's own tooling]
    end

    LOCATE --> INVENTORY
    INVENTORY --> RESEARCH
    RESEARCH --> G_CITED{every model or CLI fact traced to<br/>this run's research or a live probe?}
    G_CITED -- "no, would be from memory" --> RESEARCH
    G_CITED -- "yes" --> REVIEW

    REVIEW --> G_CHANGED{research surfaces an<br/>actual gap vs current setup?}
    G_CHANGED -- "no" --> REPORT_NC[still write the dated report;<br/>states no change is warranted]
    G_CHANGED -- "yes" --> SYNTH[Step 4: synthesize<br/>findings into two buckets]

    SYNTH --> G_BUCKET{repo edit or<br/>live-config tuning?}
    G_BUCKET -- "repo edit" --> G_APPROVE{user approves<br/>the repo diff?}
    G_APPROVE -- "no" --> DEFER_EDIT[drop or defer<br/>that finding]
    G_APPROVE -- "yes" --> APPLY[Step 5: edit the repo,<br/>SKILL.md wording, capabilities.toml]

    APPLY --> G_GATED{mise run verify<br/>still passes?}
    G_GATED -- "no" --> APPLY
    G_GATED -- "yes" --> G_DELIVERY{which managed surface<br/>changed?}
    G_DELIVERY -- "direct skills / provenance /<br/>selective settings / house style" --> DIRECT[skills:test + upstream-status;<br/>reviewed plan/apply;<br/>doctor + Maka smoke]
    G_DELIVERY -- "optional plugin or<br/>broader rendered content" --> REFRESH[make khenrix-refresh<br/>from the repo root]
    G_DELIVERY -- "both" --> BOTH[run the direct path<br/>and plugin refresh]

    DIRECT --> G_BROADCAP{did broader reconcile<br/>capability content change?}
    REFRESH --> G_BROADCAP
    BOTH --> G_BROADCAP
    G_BROADCAP -- "yes" --> REMIND[remind: run khenrix-setup<br/>to review and apply it]
    G_BROADCAP -- "no" --> REPORT

    G_BUCKET -- "live-config" --> RECOMMEND[write the exact command to the<br/>report; never run it here]

    DEFER_EDIT --> REPORT[Step 6: write the dated report<br/>docs/upgrades]
    REMIND --> REPORT
    RECOMMEND --> REPORT

    REPORT --> DONE([repo half committed if any;<br/>live half only recommended])
    REPORT_NC --> DONE
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_CITED | agent | ``evals/khenrix-upgrade/evals.json::Uses the supplied dated research evidence to recommend `claude-opus-5` and does not replace it with an unverified model guess or claim to have performed new live research`` |
| G_CHANGED | agent | no eval covers this; SKILL.md.tmpl's Ground rules — repo edits follow research finding a genuine gap, never a scheduled churn |
| G_BUCKET | agent | `evals/khenrix-upgrade/evals.json::Separates changes into two buckets: repo edits applied with confirmation, vs live-config tuning that is only recommended (never auto-applied)` |
| G_APPROVE | agent | no eval covers this; SKILL.md.tmpl's Step 5 — show each change as a diff and get approval before editing the repo |
| G_GATED | code | `scripts/render.py::def check` |
| G_DELIVERY | agent | `evals/khenrix-upgrade/evals.json::Chooses delivery by changed surface` |
| G_BROADCAP | agent | no eval covers this; SKILL.md.tmpl's Step 5 — if broader reconcile capabilities changed, remind the user to run khenrix-setup to review and apply them |
