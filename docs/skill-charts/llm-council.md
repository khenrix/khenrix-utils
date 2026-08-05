# llm-council — flow

```mermaid
flowchart TD
    accTitle: llm-council flow
    START([a high-stakes or contested question]) --> G_JUSTIFIES{worth ~3x one turn?}
    G_JUSTIFIES -- no --> ANSWER([answer directly])
    G_JUSTIFIES -- yes --> FAN[fanout.py runs three CLIs in parallel]
    FAN --> G_VALID{each seat cleared the floor and quoted its sentinel?}
    G_VALID -- no --> DROP[seat scored invalid and dropped]
    G_VALID -- yes --> KEEP[seat's answer read from its result file]
    DROP --> G_TWO{at least two valid seats?}
    KEEP --> G_TWO
    G_TWO -- no --> INCONCLUSIVE([say so; offer a retry])
    G_TWO -- yes --> SYNTH[synthesize one answer, header first]
    SYNTH --> DONE([one expert's answer])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_JUSTIFIES | agent | SKILL.md's cost note; the caller decides before convening |
| G_VALID | code | `tests/test_council_seat_validity.py::def test` |
| G_TWO | agent | SKILL.md: "if fewer than two providers are valid, say plainly that the council was inconclusive" |
