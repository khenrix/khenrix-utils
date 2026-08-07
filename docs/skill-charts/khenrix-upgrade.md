# khenrix-upgrade — flow

Rendered per CLI from ONE shared template — this chart draws that shared flow; only the
research sources and native review tooling differ per rendered CLI, the steps themselves
do not. Inventory, then research that must be sourced this run rather than recalled from
memory, then a review of the khenrix skills. Findings only proceed once research shows an
actual gap, and then split into a repo-edit bucket (applied with confirmation, gated on
`make verify`) versus a live-config bucket that is only ever recommended. Every run ends
with the dated report, even a no-change one. Source:
`shared/skill-templates/khenrix-upgrade/SKILL.md.tmpl`.

```mermaid
flowchart TD
    accTitle: khenrix-upgrade flow
    accDescr: One shared template rendered per CLI with different research and review tooling but the same steps. Inventory, then research that must be sourced this run rather than recalled from memory, then a review of the khenrix skills. A changed gate decides whether to synthesize findings into a repo-edit bucket, applied with confirmation and gated on make verify, versus a live-config bucket that is only ever recommended. Every run ends with the dated report.

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

    APPLY --> G_GATED{make verify<br/>still passes?}
    G_GATED -- "no" --> APPLY
    G_GATED -- "yes" --> REFRESH[make khenrix-refresh<br/>from the repo root]

    REFRESH --> G_CAPCHANGED{did capabilities.toml<br/>change?}
    G_CAPCHANGED -- "yes" --> REMIND[remind: run khenrix-setup<br/>to push it to the live config]
    G_CAPCHANGED -- "no" --> REPORT

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
| G_CITED | agent | `evals/khenrix-upgrade/evals.json::Researches the current recommended model before answering rather than guessing` |
| G_CHANGED | agent | no eval covers this; SKILL.md.tmpl's Ground rules — repo edits follow research finding a genuine gap, never a scheduled churn |
| G_BUCKET | agent | `evals/khenrix-upgrade/evals.json::Separates changes into two buckets: repo edits applied with confirmation, vs live-config tuning that is only recommended (never auto-applied)` |
| G_APPROVE | agent | no eval covers this; SKILL.md.tmpl's Step 5 — show each change as a diff and get approval before editing the repo |
| G_GATED | code | `scripts/render.py::def check` |
| G_CAPCHANGED | agent | no eval covers this; SKILL.md.tmpl's Step 5 — if capabilities.toml changed, remind the user to run khenrix-setup to apply it to the live config |
