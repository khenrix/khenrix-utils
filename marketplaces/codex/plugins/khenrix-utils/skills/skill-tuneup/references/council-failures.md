# Council failure modes — diagnosing a `tool_permission` seat

llm-council classifies a failed seat through **two** channels, and which one produced the
reason is the first thing to establish:

- **Structured** — the provider's own error field (claude `is_error`, agy `status`/`error`,
  codex `turn.failed`). The manifest carries `structured` for this. It is the CLI speaking
  about itself, so there is no phantom to rule out, and a RECOGNISED structured reason may
  be terminal (`STRUCTURED_TERMINAL_REASONS`).
- **Scanned** — a substring found in a merged stderr stream or agy's log tail. Always
  retried, because a seat that merely READ a file naming the phrase lands here too.

`tool_permission` is not in the terminal set, so it retries on either path — a false hit
costs an attempt, not a seat. It can still send you chasing a defect that does not exist,
and everything below is about the scanned channel only.

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

The conclusion was structural: **whether a phrase is the CLI speaking or the CLI quoting is
not recoverable from a merged stream.** That is still true *of a merged stream* — so the
heuristic was removed and the reason made retryable, and a phantom costs one attempt rather
than a seat.

**The engine then stopped relying on a merged stream.** All three seats now run a structured
output mode and classify from the provider's own error field, which carries the provenance
no amount of text matching could recover. Scanning survives only as the fallback when there
is no structured error — so the window this page covers has shrunk to that fallback, and
the first question about any `tool_permission` is no longer "phantom or real" but "was it
structured".

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

`run_provider` classifies agy's **log tail** and can overwrite a stderr-derived reason, so
reading stderr alone can print `None` and look like an acquittal — check both. It is
skipped entirely when a STRUCTURED reason already exists (`not structured` guards it): the
provider's own error field outranks a scan of its log, or the structured path would stop
being authoritative exactly when agy also logged a matching phrase.

**Decision rule.** If every `MATCHED` line is the seat echoing a file it read — a list
entry, a numbered gutter, a quoted argument — it is the phantom. **This does not mean the
seat is fine:** it failed for some other reason (in the observed case, empty stdout), so
read the attempt files for the real cause. If instead a `MATCHED` line is the CLI's own
diagnostic, it is real: fix the invocation (agy needs `--dangerously-skip-permissions`
alongside `--mode plan`) and never accept it as ambient degradation.
