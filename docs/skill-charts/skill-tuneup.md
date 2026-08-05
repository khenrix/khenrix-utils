# skill-tuneup — flow

```mermaid
flowchart TD
    accTitle: skill-tuneup flow
    START([a skill to review]) --> READ[read the skill and its evals]
    READ --> G_DEFECT{is there a real defect?}
    G_DEFECT -- no --> SAY([say it is clean; change nothing])
    G_DEFECT -- yes --> EDIT[propose the smallest change]
    EDIT --> G_EVAL{does the eval delta stay non-negative?}
    G_EVAL -- no --> EDIT
    G_EVAL -- yes --> COMMIT([commit with the receipt])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_DEFECT | agent | SKILL.md: a clean pass stated plainly beats a manufactured caveat |
| G_EVAL | code | `scripts/eval_harness.py::def main` |
