# llm-council — flow

Fan one question out to three CLIs in parallel: a cost gate, then each seat runs a
bounded attempt loop — valid, a terminal failure, or a retry — until the engine writes
a manifest. Two or more valid seats synthesize into one answer; fewer than two is
reported inconclusive. Source: `shared/skills/llm-council/SKILL.md`; engine:
`shared/lib/council/engine.py`.

```mermaid
flowchart TD
    accTitle: llm-council fan-out and synthesis flow
    accDescr: Fan one question out to three CLIs in parallel - cost-gate, locate the engine, inject one sentinel and read-only posture, then each seat runs a bounded attempt loop of valid, terminal failure, or retry until the engine writes a manifest. Two or more valid seats synthesize into one answer; fewer than two is reported inconclusive.

    START([user asks a high-stakes<br/>or contested question]) --> G_JUSTIFIES{worth ~3x<br/>a normal turn?}
    G_JUSTIFIES -- "no" --> ANSWER([answer directly, no council])
    G_JUSTIFIES -- "yes" --> LOCATE[Step 1: locate fanout.py<br/>across CLI plugin roots]
    LOCATE --> BUILD[Step 2: write the underlying<br/>question to a file]
    BUILD --> FANOUT[engine injects one sentinel<br/>+ read-only posture, launches<br/>3 seats in parallel]

    subgraph SEAT ["each seat - claude, codex, agy - bounded attempt loop"]
        ATTEMPT[run one headless attempt] --> G_VALID{cleared length floor<br/>and quoted its sentinel?}
        G_VALID -- "no" --> G_TERMINAL{terminal reason?<br/>not_installed, or a<br/>structured auth/quota error}
        G_TERMINAL -- "no" --> G_ATTEMPTS{attempts<br/>remaining?}
        G_ATTEMPTS -- "yes, backoff" --> ATTEMPT
    end

    FANOUT --> ATTEMPT
    MANIFEST[engine writes manifest.json<br/>header states seats responded vs attempted]
    G_VALID -- "yes" --> MANIFEST
    G_TERMINAL -- "yes" --> MANIFEST
    G_ATTEMPTS -- "no" --> MANIFEST
    MANIFEST --> G_TWO{at least two<br/>valid seats?}
    G_TWO -- "no" --> INCONCLUSIVE([say inconclusive; offer a retry])
    G_TWO -- "yes" --> READ[Step 3: read each valid seat's<br/>full result_file]
    READ --> SYNTH[Step 4: synthesize one answer<br/>header first, no per-provider attribution]
    SYNTH --> DONE([one expert's answer])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_JUSTIFIES | agent | no eval covers this; SKILL.md's "Cost & when to use" callout — three full agent turns in parallel, ~3x a normal turn, so the caller weighs it before convening |
| G_VALID | code | `tests/test_council_seat_validity.py::def test_substantive_response_citing_sentinel_is_ok` |
| G_TERMINAL | code | `tests/test_council_seams.py::def test_agys_structured_timeout_keeps_its_retries_but_its_quota_wall_does_not` |
| G_ATTEMPTS | code | `tests/test_council_seat_validity.py::def test_only_missing_binary_is_terminal_all_scan_derived_reasons_retry` |
| G_TWO | agent | no eval covers this; SKILL.md: fewer than two valid providers must be reported plainly as an inconclusive council rather than synthesized as if the panel were complete |
