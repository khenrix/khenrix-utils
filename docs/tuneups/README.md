# Tune-up run memory

`log/<target>.jsonl` is the append-only per-target memory of `skill-tuneup` runs: one
line per finding decision, so later runs never re-propose something already rejected
(and can surface previously deferred items as such).

Committed on purpose — decisions must survive sessions and machines. `docs/` is outside
every eval-receipt closure, so appending here never stales a receipt.

Line schema (written via `tuneup.py log append`; later lines for the same `finding_id` win):

```json
{"ts": "2026-07-05T12:00:00+00:00", "target": "markitdown", "baseline_sha": "c3ae395",
 "finding_id": "stale-uvx-flag", "title": "…", "tag": "proportionate|risky",
 "decision": "applied|rejected|deferred", "reason": "…", "sources": ["…"]}
```

Required keys: `target`, `finding_id`, `decision` (the engine enforces these; `ts` is
stamped automatically). Read it back with `tuneup.py log list --target <t>` — it reports
the latest decision per finding.
