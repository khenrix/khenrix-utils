---
name: llm-council
description: Run the same prompt across all three CLIs on this machine (Claude, Codex, agy) headlessly, then synthesize the single best answer from their three independent responses — a cross-model "council" for high-stakes questions. Use this whenever the user wants a second opinion, cross-model consensus, to "ask all three", to compare what different models say, or maximum confidence on a hard, important, or ambiguous question (architecture decisions, tricky debugging, risky changes, judgment calls). Also trigger on "llm-council", "council", "ask the other CLIs", "what do codex/agy think", or any request to poll several LLMs and merge their answers. Costs roughly 3x a normal turn (three full agent runs), so prefer it when the decision justifies the spend.
allowed-tools: Bash, Read
---

# llm-council — the council

Fan one prompt out to all three agentic CLIs on this machine — `claude`, `codex`,
`agy` — running each headlessly under identical conditions, then read their three
answers and synthesize the single best response. The point is **independent
perspectives**: three different models answer the same question blind to each
other, so where they agree you can be confident, and where they disagree you have
the raw material to reason out which is right.

You are the orchestrator. A bundled engine (`fanout.py`) owns the mechanical part —
running the three CLIs in parallel, validating each result, and retrying failures.
**You own the judgment**: comparing the answers and merging them. Don't reinvent the
fan-out in bash; run the engine and synthesize from its manifest.

> **Cost & when to use.** This runs three full agent turns in parallel (including a
> fresh headless run of *this* CLI), so it costs ~3x a normal turn. Use it for
> decisions that justify the spend — high-stakes, ambiguous, or contested questions —
> not routine tasks. It bypasses all permission/sandbox prompts (same caveat as the
> `clauded`/`aggy`/`codexo` aliases); only run it in a trusted workspace.

## 1. Locate the engine

The skill body is identical in all three plugins, but each CLI exposes its plugin
root differently. Run this first to set `$FANOUT` to whichever copy exists:

```bash
FANOUT=""
for c in \
  "${CLAUDE_PLUGIN_ROOT:-}/skills/llm-council/scripts/fanout.py" \
  "${PLUGIN_ROOT:-}/skills/llm-council/scripts/fanout.py" \
  "$HOME/.gemini/config/plugins/khenrix-utils/skills/llm-council/scripts/fanout.py"; do
  [ -f "$c" ] && FANOUT="$c" && break
done
[ -z "$FANOUT" ] && echo "fanout.py not found — is khenrix-utils installed?" && exit 1
echo "engine: $FANOUT"
```

## 2. Run the fan-out

Write the **underlying question** to a file and hand it to the engine. Each provider
is a single assistant answering that question directly — so send the question itself,
not the council framing around it. If the user wrote "ask all three CLIs whether I
should use X" or "convene the council on Y", pass just "should I use X" / "Y": keep
their wording and intent, but drop the "ask the others / use the council" wrapper.
Don't summarize or editorialize the question otherwise. This keeps all three answering
the *same* thing and avoids a provider trying to convene its own council (the
`LLM_COUNCIL_DEPTH` guard blocks the recursion, but it wastes a turn).

```bash
PROMPT_FILE="$(mktemp)"
cat > "$PROMPT_FILE" <<'EOF'
<the user's task, exactly as they asked it>
EOF
python3 "$FANOUT" --prompt-file "$PROMPT_FILE" --out json
```

The engine prints a JSON **manifest** to stdout (also saved to
`<workdir>/manifest.json`). Useful flags: `--timeout SECONDS` (per-attempt, default
300 — raise it for big tasks), `--retries N` (default 2), `--providers claude,codex`
(narrow the panel), `--model-claude/-codex/-agy ID` (pin a model). Defaults are fine
for most runs.

**The engine handles the "valid result or retry" contract for you.** Each provider
is validated (exit 0, non-empty answer, no auth/rate-limit error) and retried with
backoff on failure; a missing binary fails fast without burning retries. You just
consume the manifest — never paper over a failure by re-running a provider yourself.

## 3. Read the answers

From the manifest, for every provider with `"valid": true`, **Read** its
`result_file` to get the full answer (the `result_text` in the manifest is truncated
for display — always read the file for synthesis). For a `failed` provider whose
`reason` is `timeout` or `parse_failure`, the raw output may still hold something
useful — glance at its `raw_stdout_file` before discarding it, but treat it as
low-confidence.

`codex` and `agy` print their answer as plain text that can include CLI log chrome.
The engine keeps their stdout verbatim rather than risk trimming real content — so
when you read those files, extract the substantive answer yourself and ignore
obvious log lines.

## 4. Synthesize the best answer

This is the actual skill. Produce **one** integrated answer — not three summaries
stapled together:

- **Agreement → confidence.** Where all valid providers converge, state it plainly;
  independent agreement across different models is strong signal.
- **Disagreement → reason it out.** Where they conflict, don't majority-vote blindly.
  Look at the actual arguments, check them against the code/facts, and decide which is
  correct (or flag a genuine open question). The minority answer is sometimes the right
  one — that's much of the value of asking three.
- **Unique contributions.** Surface anything only one provider raised (an edge case, a
  risk, a better approach) and fold it in if it holds up.
- **Attribute.** Briefly note who contributed what and who was right on the
  contested points, so the user can see how the council reached the answer.
- **Stay neutral.** Don't privilege this CLI's own provider just because you're it —
  all three ran under the same headless conditions; weigh them on merit.

If `summary.degraded` is true, say so: which provider failed, its `reason`, and that
the synthesis is based on the N that succeeded. If fewer than two providers are
valid, tell the user the council was inconclusive — offer to answer directly in this
session or to retry with a longer `--timeout`.

## Failure handling

| `reason` in manifest | What it means | What to tell the user |
|----------------------|---------------|-----------------------|
| `ok` | valid answer | use it |
| `not_installed` | that CLI isn't on PATH | "provider X isn't installed here"; proceed with the rest |
| `auth_or_quota` | not logged in, or a quota/usage wall — **not retried**, since it won't clear on a retry | name the provider and the cause (e.g. "agy hit its Antigravity quota"); proceed with the rest |
| `error_sentinel` | a transient error (rate-limit, overloaded) that survived retries | name the provider, quote the stderr tail; proceed with ≥2 if possible |
| `nonzero_exit` | crashed with no recognized cause | name the provider, quote the stderr tail; proceed if possible |
| `timeout` | hung past `--timeout` | offer a re-run with a larger `--timeout`; use partial output only as low-confidence |
| `empty` / `parse_failure` | no usable answer extracted | drop it from synthesis; note it failed |

Note: some CLIs report their real failure only in a log, not on stdout/stderr (agy prints
nothing on a 429 and logs `RESOURCE_EXHAUSTED … Individual quota reached`). The engine
captures agy's `--log-file` and scans it, so a bare `empty` is upgraded to the precise
`auth_or_quota` — trust the manifest's `reason`.

The engine **always** emits a manifest and you **always** synthesize from whatever is
valid — degrade to 2/3 or 1/3 and report it, rather than aborting because one provider
died. The only hard stop is zero valid providers.

## Tuning (for maintainers)

When a real headless run surfaces a new failure string or an output-parsing quirk, the
fix lives in `scripts/fanout.py`: `PERSISTENT_SENTINELS` (fatal — auth/quota, not
retried) vs `TRANSIENT_SENTINELS` (retryable — rate-limit, overloaded),
`extract_claude_json` / `extract_raw` (how each CLI's answer is pulled out), and the
`build_real_spec` argv builders (the exact headless flags — kept in sync with
`headless-invocation.md` at the plugin root; note agy's Go-style flag parser needs every
flag *before* the positional prompt, and its real error lands in `--log-file`). Add a
matching case to `tests/stub_provider.py` and confirm
`python3 scripts/fanout.py --self-test` stays green. Validate live binaries cheaply with
`--smoke` before a full council.
