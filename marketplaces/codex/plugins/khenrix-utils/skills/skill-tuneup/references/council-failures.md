# Council failure modes — diagnosing a `tool_permission` seat

llm-council marks a seat `tool_permission` when a tool-denial phrase appears in its stderr
(or, for agy, its log tail). **That reason is retryable** — see below for why — so a false
hit no longer costs the seat its attempt. It can still send you chasing a defect that does
not exist.

## The phantom

Sentinels are plain lowercased substrings matched against a MERGED stderr stream, and a
council seat reviewing khenrix-utils greps this repo — so `fanout.py`'s own sentinel lists
come back as stderr content and match themselves. Observed 2026-07-27: a codex seat
returned empty stdout, its 243 KB stderr contained `fanout.py:1299` (a line in fanout's own
self-test), it was classified `tool_permission`, and — because that reason was then
non-retryable — `attempts: 1`. The panel degraded 2/3 for nothing.

**Three text heuristics were tried to separate "the CLI said it" from "the CLI printed a
file that says it". Each had a real counterexample:**

| attempt | broke |
|---|---|
| strip any `file:line:` prefix | silenced agy's genuine `tool_confirmation_manager.go:183: permission denied` |
| require a `/` in the path | silenced every genuine denial reported with an ABSOLUTE path, which is what a real install emits |
| treat quoted text as source | silenced codex's version gate, which arrives as a JSON payload (`{"message":"…"}`) |

The conclusion is structural: **whether a phrase is the CLI speaking or the CLI quoting is
not recoverable from a merged stream.** What *is* reliable is reproduction — a genuine
denial recurs on retry, a phantom does not. So the heuristic was removed and
`tool_permission` was made retryable instead. A phantom now costs one extra attempt, not a
seat.

## Deciding whether a `tool_permission` is real

The reason alone cannot tell you — a phantom and a genuine denial produce the same token.
Look at the seat's own inputs:

```bash
KU="$HOME/git/khenrix-utils"          # export it: the child process reads the ENVIRONMENT
W=<workdir-from-the-manifest>
for F in "$W"/<seat>.stderr.attempt-*.txt "$W"/agy.cli.log; do   # agy's LOG can override
  [ -f "$F" ] || continue
  echo "== $F"
  KU="$KU" python3 - "$F" <<'EOF'
import os, sys
sys.path.insert(0, os.environ.get("KU", "") + "/shared/skills/llm-council/scripts")
import fanout
t = open(sys.argv[1], encoding="utf-8", errors="replace").read()
print("  verdict:", fanout.classify_sentinel(t))
for ln in t.lower().splitlines():
    if any(s in ln for s in fanout.TOOL_PERMISSION_SENTINELS):
        print("  MATCHED:", ln.strip()[:150])
EOF
done
```

`run_provider` classifies agy's **log tail** and OVERWRITES the stderr-derived reason, so
reading stderr alone can print `None` and look like an acquittal. Check both.

**Decision rule.** If every `MATCHED` line is the seat echoing a file it read — a list
entry, a numbered gutter, a quoted argument — it is the phantom. **This does not mean the
seat is fine:** it failed for some other reason (in the observed case, empty stdout), so
read the attempt files for the real cause. If instead a `MATCHED` line is the CLI's own
diagnostic, it is real: fix the invocation (agy needs `--dangerously-skip-permissions`
alongside `--mode plan`) and never accept it as ambient degradation.
