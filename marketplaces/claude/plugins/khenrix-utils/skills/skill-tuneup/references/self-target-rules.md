# Self-target rules

Special handling when the tune-up target IS part of this skill's own machinery. The
danger: a tool reviewing its own under-test diff, or unbounded recursion.

## Target = llm-council

The council reviews (findings + diff) run through `fanout.py`. If the working tree has
modified `shared/skills/llm-council/**`, the under-test engine must NOT be its own reviewer:

1. Extract the last committed engine and run that instead:
   ```bash
   GOOD=$(mktemp -d)/fanout.py
   git -C <repo> show HEAD:shared/skills/llm-council/scripts/fanout.py > "$GOOD"
   python3 "$GOOD" --prompt-file <diff-prompt> --out json
   ```
2. If that is unusable too (e.g. the fix targets a bug in the committed engine), fall
   back to a single-provider review: run one other CLI headlessly against the diff
   (see `headless-invocation.md` at the plugin root) and treat it as a 1-member panel.
3. Either way, **tell the user the reviewer was substituted and why.**

The eval gate for llm-council is also special: `fanout.py --self-test` + a live
`--smoke` (its receipt is self-test-gated — see `references/eval-rules.md`), never the
with-skill/baseline judge harness.

Note `LLM_COUNCIL_DEPTH` already blocks a council spawning inside a council member —
do not "fix" that guard away.

## Target = skill-tuneup (this skill)

- Audit, edits, and `tuneup.py --self-test` run normally — but follow the instructions
  as committed at HEAD, not the under-test working-tree copy you are editing.
- The tuneup lock (`skill-tuneup.lock.d`, Step 1 of SKILL.md) makes a literal nested
  self-run refuse to start; don't work around it.
- For the final review: council-review the diff as usual (fanout.py is not under test),
  but the ultimate reviewer is the **user reading the diff** — say so explicitly.

## Target = khenrix-setup / khenrix-upgrade

Normal rules, plus: edits go to `shared/skill-templates/<t>/SKILL.md.tmpl` and the
`[skill_facts.<t>.<cli>]` tables — and `capabilities.toml` is in BOTH templated skills'
receipt closures, so a facts edit for one stales the other's receipt too. Budget for
re-evaling (or re-seeding, with the user's sign-off) both.
