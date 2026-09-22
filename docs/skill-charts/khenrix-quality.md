# khenrix-quality - flow

An explicit request selects one focused mode. ADHD may shape the surrounding response,
but only one prose mode owns an artifact; specialist workflows and repository safeguards
keep precedence. Source: `shared/skills/khenrix-quality/SKILL.md`.

```mermaid
flowchart TD
    accTitle: khenrix-quality flow
    accDescr: Route explicit ADHD, prose edit or detection, code minimality, and focused code-inspection requests to one mode. Deep humanization goes to khenrix-writing. Preserve facts, repository conventions, safety controls, and useful tests.

    START([user request]) --> G_EXPLICIT{named quality intent or<br/>former natural-language name?}
    G_EXPLICIT -- no --> DEFAULT[apply compact shared<br/>house style only] --> DONE_D([continue ordinary task])
    G_EXPLICIT -- yes --> G_WRITING{deep humanization or<br/>voice matching?}
    G_WRITING -- yes --> WRITING[route artifact to<br/>khenrix-writing] --> DONE_W([one rewrite owner])
    G_WRITING -- no --> ROUTE[select one primary mode]

    ROUTE --> ADHD[ADHD: action first,<br/>bounded steps, visible state]
    ROUTE --> PROSE[prose edit or detect:<br/>preserve voice and facts]
    ROUTE --> CODE[code: lite, full, or ultra<br/>simplification ladder]
    ROUTE --> INSPECT[review, audit, debt,<br/>benchmark, or help]

    ADHD --> G_OWNER{another mode targets<br/>the same artifact?}
    PROSE --> G_OWNER
    CODE --> G_OWNER
    INSPECT --> G_OWNER
    G_OWNER -- yes --> SINGLE[keep one artifact owner;<br/>other mode may shape surrounding reply]
    G_OWNER -- no --> BOUNDS[apply requested scope +<br/>repository and safety boundaries]
    SINGLE --> BOUNDS
    BOUNDS --> G_WRITE{inspection workflow<br/>explicitly asks for edits?}
    G_WRITE -- no --> READONLY[report findings only]
    G_WRITE -- yes --> APPLY[apply only requested fixes]
    READONLY --> DONE([return mode-specific result])
    APPLY --> DONE
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_EXPLICIT | agent | `evals/khenrix-quality/triggers.json::use the former ponytail-audit workflow on this repository` |
| G_WRITING | agent | `evals/khenrix-quality/arena.json::humanize this cover letter and match the casual voice in my sample` |
| G_OWNER | agent | `evals/khenrix-quality/evals.json::Does not provide a rewritten paragraph` |
| G_WRITE | agent | `evals/khenrix-quality/evals.json::REVIEW reports an actionable path-and-line-style yagni or shrink finding for SenderFactory, protects the validator and useful tests, and applies no fix` |
