# khenrix-quality - flow

An explicit request selects one focused mode. ADHD may shape the surrounding response,
but only one prose mode owns an artifact; specialist workflows and repository safeguards
keep precedence. Source: `shared/skills/khenrix-quality/SKILL.md`.

```mermaid
flowchart TD
    accTitle: khenrix-quality flow
    accDescr: Route explicit quality requests to ADHD, minimal prose edit or evidence-only detection, code, or read-only inspection. Explicit review and audit cover engineering risks unless narrowed. Preserve technical spans, useful tests, shared behavior, dynamic callers, and benchmark provenance.

    START([user request]) --> G_EXPLICIT{named quality intent or<br/>former natural-language name?}
    G_EXPLICIT -- no --> DEFAULT[apply shared house style;<br/>keep specialist workflow] --> DONE_D([continue ordinary task])
    G_EXPLICIT -- yes --> G_WRITING{deep humanization or<br/>voice matching?}
    G_WRITING -- yes --> WRITING[route artifact to<br/>khenrix-writing] --> DONE_W([one rewrite owner])
    G_WRITING -- no --> ROUTE[select one primary mode;<br/>ADHD may shape surrounding reply]

    ROUTE --> ADHD[ADHD: action first,<br/>bounded steps, visible state]
    ROUTE --> PROSE[edit: smallest needed change;<br/>detect: evidence without rewrite]
    PROSE --> PRESERVE[preserve facts, voice, technical spans,<br/>quotes, URLs and uncertainty;<br/>leave clean prose unchanged]
    ROUTE --> CODE[code: lite, full, or ultra;<br/>reuse shared owner, test callers]
    ROUTE --> INSPECT[review, audit, debt,<br/>benchmark, or help]
    INSPECT --> G_SCOPE{review or audit?}
    G_SCOPE -- yes --> G_NARROW{explicitly limited<br/>to simplification?}
    G_NARROW -- yes --> NARROW[inspect only safe<br/>simplification and YAGNI]
    G_NARROW -- no --> BROAD[inspect correctness, security,<br/>expected load, tests, performance,<br/>and safe simplification]
    NARROW --> FINDINGS[rank numbered findings:<br/>trigger, impact, smallest fix,<br/>consequence if left]
    BROAD --> FINDINGS
    FINDINGS --> DYNAMIC[preserve validation, useful tests,<br/>and configured or dynamic uses]
    G_SCOPE -- no --> OTHER[debt: tracked source comments only;<br/>benchmark: upstream method and limits;<br/>help: modes and handoffs]
    DYNAMIC --> G_WRITE{fixes explicitly<br/>requested?}
    OTHER --> READONLY[report mode-specific result]
    G_WRITE -- no --> READONLY
    G_WRITE -- yes --> APPLY[apply only requested fixes]
    ADHD --> DONE([return scoped result])
    PRESERVE --> DONE
    CODE --> DONE
    READONLY --> DONE
    APPLY --> DONE
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_EXPLICIT | agent | `evals/khenrix-quality/triggers.json::use the former ponytail-audit workflow on this repository` |
| G_WRITING | agent | `evals/khenrix-quality/arena.json::humanize this cover letter and match the casual voice in my sample` |
| G_SCOPE | agent | `evals/khenrix-quality/evals.json::focused-code-workflows` |
| G_NARROW | agent | `evals/khenrix-quality/evals.json::NARROW respects the explicit simplification-only scope` |
| G_WRITE | agent | `evals/khenrix-quality/evals.json::Each review/audit finding states a concrete trigger, impact, minimal fix, and consequence of leaving it; prioritizes correctness/security/load risks and applies no changes` |
