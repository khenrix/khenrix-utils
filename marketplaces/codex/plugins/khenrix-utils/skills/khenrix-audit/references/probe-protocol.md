# Trigger evidence protocol

## Arena (tier 1 — any CLI, cheap, also the rung-1 gate)
1. Write evals/<skillA>/arena.json: {"prompts": [{"prompt": "...", "expected": "<name-or-none>"}]}
   — 4+ prompts per side, including near-misses that share keywords but belong elsewhere.
2. Run: python3 scripts/eval_trigger.py --arena skillA,skillB
3. Read the confusion matrix. Misrouted prompts = the collision is real.

## Live probes (tier 2 — capabilities.can_probe only, top AMBIGUOUS pairs)
Validated mechanism (2026-07-30): the stream-json transcript exposes the fired skill.
1. Isolation is mandatory: probes both perturb and are perturbed by invocation
   history (descriptions drop least-invoked-first). Create a throwaway
   CLAUDE_CONFIG_DIR containing ONLY the pair under test + two fixed decoy skills.
2. Per prompt, k >= 3 runs of:
   claude -p "<probe>" --output-format stream-json --max-turns 1 --permission-mode plan
3. Parse tool_use events for `"name": "Skill"`; record `input.skill` per run.
4. Report fire-RATES per skill. Unstable (no skill >= 2/3) → INCONCLUSIVE.
5. A model answering inline without invoking any skill is a valid outcome —
   record as "no-skill", not as a miss for either side.
6. State the probe cost up front (k × prompts × pairs turns) and get confirmation.
