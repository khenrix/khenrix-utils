# Remediation ladder — cheapest available rung first

| Rung | Action | Available when | Gate |
|------|--------|----------------|------|
| 1 | Narrow OUR skill's description (frontmatter only) | subject skill lives in khenrix-utils shared/ or skill-templates/ | arena eval over the edited skill + every skill it was nominated against, accuracy ≥ 0.8, then `make eval SKILL=<name>` receipt |
| 2 | Disable the offending plugin | any plugin | list EVERYTHING the plugin provides first (skills, MCP, hooks, agents); user confirms the full loss |
| 3 | Vendor the one wanted skill into khenrix-utils | plugin skill worth keeping | record `vendored_from` in the copy's frontmatter meta; staleness tracking belongs to skill-tuneup; license/provenance reviewed |
| — | Anything else | — | advisory only, report text |

## Rung-1 gate detail
Description edits reroute prompts BETWEEN skills — a single-skill receipt cannot
see that. Run: `python3 scripts/eval_trigger.py --arena <edited>,<neighbor1>,...`
with prompts in `evals/<edited>/arena.json` covering both sides' trigger phrases.
Body-level changes are skill-tuneup's job — hand off, do not apply here.
