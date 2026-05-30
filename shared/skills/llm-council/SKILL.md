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

Write the single best answer to the user's question. **It should read like one
expert's answer — not a report about a council.** The three runs are your *input*;
the user wants the conclusion, not a tour of how three models voted. A leaner,
decisive answer beats a longer one that shows its work — so use the council to make
your answer more *correct and confident*, not longer.

The discipline that makes this good:

- **No process narration.** Do **not** add a "how the council reached this" section,
  and do **not** do per-point bookkeeping ("Claude said X, agy said Y, Codex added
  Z"). That is padding — it restates the answer as meeting minutes and lowers
  signal-to-noise. Just give the answer.
- **Fold unique points in silently.** If only one provider caught a correct edge
  case, risk, or better approach, incorporate it as part of the answer. Don't credit
  it — the user cares that it's there, not who said it.
- **Surface genuine disagreement — this is the one thing worth the words.** When the
  providers actually conflict on something that matters, present the conflict and
  resolve it: weigh the arguments against the facts/code and say which is right (or
  flag it as a real open question). Don't majority-vote blindly; the minority answer
  is sometimes correct. This — plus catching a wrong answer — is the council's real
  payoff over asking one model, so spend words here, not on attribution.
- **Confidence, at most one line.** If all valid providers independently converged,
  you may note it in a single clause ("all three independently agree, so this is
  high-confidence") — only if it helps. Skip it otherwise.
- **Answer only what was asked.** No tangents on things the user didn't raise.
- **Length target:** about as tight as a strong single-expert answer to the same
  question. If the council mostly agreed, your answer should be roughly that length —
  the council de-risked it; it didn't earn extra paragraphs.
- **Stay neutral.** Don't privilege this CLI's own provider; all three ran under the
  same headless conditions — weigh them on merit.

If `summary.degraded` is true, add a brief one-line note of which provider failed and
its `reason`, and that the answer rests on the N that succeeded — then give the answer
as usual. If fewer than two providers are valid, tell the user the council was
inconclusive and offer to answer directly or retry with a longer `--timeout`.

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
