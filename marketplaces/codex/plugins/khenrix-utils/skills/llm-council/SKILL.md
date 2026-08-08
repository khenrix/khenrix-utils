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
> not routine tasks.
>
> **Read-only by default.** All three members are now **mechanically constrained**:
> Claude (plan mode, plan-file writes suppressed), Codex (read-only sandbox), and agy
> (`--mode plan`, available since agy 1.1.1 — probed 2026-07-11: it mechanically blocked
> a write the model claimed to have made). agy keeps `--dangerously-skip-permissions`
> *alongside* `--mode plan`: the two are orthogonal per `agy --help` (auto-approve is a
> prompting policy, plan mode is the write barrier), and dropping it left agy unable to
> approve its own reads headlessly. Two soft layers remain on top: a read-only
> posture line prepended to every member's prompt (identical conditions preserved) and a
> throwaway git-worktree cwd for agy — both added after the 2026-07-11 breakout incident,
> kept as defense in depth. This suits the council's job (a second opinion / synthesis,
> not edits) and makes it low-risk to convene even
> mid-task. Pass `--allow-writes` only when you explicitly want the members
> to edit/execute (that bypasses permission/sandbox prompts — only in a trusted workspace).

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
`LLM_COUNCIL_DEPTH` guard blocks the recursion, but it wastes a turn). The engine also
prepends one identical member note to every seat: use whatever skills your environment
provides when they help, but never a council/fan-out skill — so members benefit from
their installed skills without re-convening the council.

```bash
PROMPT_FILE="$(mktemp)"
cat > "$PROMPT_FILE" <<'EOF'
<the user's task, exactly as they asked it>
EOF
python3 "$FANOUT" --prompt-file "$PROMPT_FILE" --out json          # normal mode (default)
# high-stakes / maximum confidence:
python3 "$FANOUT" --prompt-file "$PROMPT_FILE" --mode deep --out json
```

### Prompt shape (matters most for the codex seat)

- **Open with the request-type verb.** GPT-5.6 dispatches on it: "Review …" / "Diagnose …
  — do not modify anything" lands in a bucket that natively forbids writes and stops short
  of implementing fixes; "fix"/"implement" authorizes edits. Match the verb to intent.
- **Review asks need an explicit output contract** (5.6 dropped its built-in one):
  findings first, ordered by severity, each with file:line evidence; then open
  questions/assumptions; summary last; if clean, say so explicitly with residual risks.
- **Supply defaults for ambiguity.** Headless members never ask clarifying questions —
  they guess. "If X is ambiguous, assume Y; state assumptions inline."
- **Don't write "think harder"** — reasoning depth is the `--mode` knob, prose adds nothing.
  Do authorize length when you want depth ("a long structured answer is expected");
  the codex harness biases hard toward brevity otherwise.

The engine prints a JSON **manifest** to stdout (also saved to
`<workdir>/manifest.json`). Useful flags: `--mode normal|deep` (see below),
`--allow-writes` (drop the default read-only posture so members can edit/execute),
`--timeout SECONDS` (per-attempt; default is per-mode — 900 normal / 1200 deep — raise
it for big tasks), `--retries N` (default 2), `--providers claude,codex` (narrow the
panel), `--model-claude/-codex/-agy ID` (override a model for one run). Defaults are
fine for most runs.

### Models & thinking modes

The council is a fixed panel of three models. The two modes differ in **both** the claude
seat's reasoning tier and how hard the others think:

- **`normal`** (default) — Fable 5 at `max`, GPT-5.6 Sol at `high`, Gemini 3.6 Flash
  (High). Use for most council runs.
- **`deep`** — Fable 5 at **`ultracode`**, Sol at **`ultra`**, Flash unchanged (no tier
  above High exists) + a longer timeout. Use for genuinely high-stakes /
  maximum-confidence asks (architecture, risky changes), or when the user says "deep",
  "think hard", or "maximum confidence".

`ultracode` and `ultra` are real but **undocumented** tiers (probed 2026-08-05, each with
a garbage-value control): claude's `--help` lists only `low…max` yet accepts `ultracode`
silently — and *warn-and-ignores* an unknown value, so if a future CLI drops the tier the
seat downgrades to default effort with only a stderr line; codex accepts `ultra` and fails
**closed** with an API 400 on garbage; agy refuses anything above `high`.

**Automatic model fallback.** Fable sits behind the narrowest weekly sub-cap on this
machine and now holds the claude seat in both modes, so a wall is expected rather than
surprising: when that seat fails for a **model-attributable** reason (`auth_or_quota`, a
structured claude error) the retry runs on `claude-opus-5` instead of spending the attempt
on a model that cannot answer. It never fires on a timeout, parse failure or tool-permission
denial — the model is not the cause there, and swapping would mask the real defect. Every
swap is disclosed: `model_fallback {from,to,reason}` on the provider record, the manifest's
`model` field is the model that *actually answered*, and `summary.header` gains a
`Model fallback:` clause you must not drop from the synthesis.

The panel and tiers live in **one place** — the `MODES` table at the top of
`engine.py` (repo: `shared/lib/council/engine.py`; rendered plugin:
`<plugin>/lib/council/engine.py` — `scripts/fanout.py` is now a thin façade over it).
To change
a tier, edit one cell there. A *new* model id must also be registered in
`capabilities.toml [models]` — `make verify` fails otherwise. Since agy 1.1.1 the
engine pins agy's model per-run via `--model` (the thinking tier is encoded in the model
string — `agy models` prints them as slugs, e.g. `gemini-3.6-flash-high`; the display
label we pin resolves too, re-probed on 1.1.11), so the agy cell's MODEL is enforced like the
others; its tier tops out at "(High)" (no Flash Max tier exists), so deep mode deepens
the claude and codex seats only.
Deep-mode members need up to ~800s each at max reasoning (measured on the current
panel 2026-07-25: opus-5 565s, sol 374s; up to 796s on 2026-07-11): launch deep
fan-outs in the background, or make sure any outer command cap exceeds
`--timeout × (retries+1)` — a killed fan-out loses its results, and a SIGKILL
(unlike SIGTERM, which the engine now handles) bypasses the worktree cleanup.

**The engine handles the "valid result or retry" contract for you.** Each provider
is validated and retried with backoff on failure. What decides whether a failure is
terminal is **where the reason came from**, not the phrase:

- **Scan-derived** (a substring found in a merged stderr stream) — always RETRIED. A seat
  that merely echoed a file naming those strings must not lose its seat to a wall that
  does not exist.
- **Structured** (the provider's OWN error field — claude's `is_error`, agy's `status`/
  `error`, codex's `turn.failed`) — may be terminal, but only for a reason we RECOGNISE.
  An unrecognised structured error still retries.
- **A missing binary** is terminal either way; it cannot appear between attempts.

You just consume the manifest — never paper over a failure by re-running a provider yourself.

**Non-empty is not a pass.** A seat scores `valid` only if it cleared a length floor
*and* quoted the per-run `SENTINEL-…` token the engine injected into its prompt —
proof it actually opened the material rather than answering from the question alone.
This exists because a seat once soft-denied its own file read, replied with one
sentence, and was scored `ok`: a 3-seat verdict silently became a 2-seat one and the
synthesis reported it as three. Do **not** re-admit a seat the engine failed.

## 3. Read the answers

From the manifest, for every provider with `"valid": true`, **Read** its
`result_file` to get the full answer (the `result_text` in the manifest is truncated
for display — always read the file for synthesis). For a `failed` provider whose
`reason` is `timeout` or `parse_failure`, the raw output may still hold something
useful — glance at its `raw_stdout_file` before discarding it, but treat it as
low-confidence.

All three seats now run in a structured output mode — claude and agy return one JSON
object, codex an NDJSON event stream — so the engine extracts the answer field and the
`result_file` holds the answer, not CLI chrome. The parsers fail CLOSED on a malformed
stream (`parse_failure`) rather than silently handing you a log dump. The raw stream is
still saved alongside if you need to audit what the seat actually printed.

## 4. Synthesize the best answer

**Open with the seat count — always, verbatim, before anything else.** The engine
composes it for you at `summary.header`; emit that exact line as the first line of your
reply, then a blank line, then the answer:

```
**Council: 3 of 3 seats responded.**
```
```
**Council: 2 of 3 seats responded — DEGRADED.**  Failed: agy (tool_permission — …)
```

This is not optional and not process narration — it is the provenance of the answer.
A reader must never be able to mistake a 2-seat verdict for a 3-seat one, which is
exactly what happened when a silently-failed seat was scored `ok`. Never restate the
count as anything other than what `summary.header` says, and never round it up.

Write the single best answer to the user's question. **Below that header it should read
like one expert's answer — not a report about a council.** The three runs are your *input*;
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

If `summary.degraded` is true the header already names every failed seat and its cause;
add at most one further line if a `hint` suggests a concrete next step (e.g. a
`tool_permission` failure is *often* a fixable invocation bug — but confirm it is a real
CLI diagnostic and not a file the seat read — check `structured` in the manifest first: a
structured reason came from the provider's own error field and needs no such confirmation), then
give the answer as usual. If fewer than two providers are valid, say plainly that the
council was inconclusive and offer to answer directly or retry with a longer `--timeout`.

## Failure handling

| `reason` in manifest | What it means | What to tell the user |
|----------------------|---------------|-----------------------|
| `ok` | valid answer | use it |
| `not_installed` | that CLI isn't on PATH | "provider X isn't installed here"; proceed with the rest |
| `auth_or_quota` | not logged in, a quota/usage wall, **or (codex only) a CLI too old for the model pinned in `MODES`**. **Retried** — same reason as `tool_permission`: scan-derived, and *this file itself* classifies as `auth_or_quota` because its own table names those strings. Retrying a genuine wall costs up to `retries`+1 attempts plus backoff; a phantom cost a third of the panel, silently | name the provider and the cause (e.g. "agy hit its Antigravity quota"); proceed with the rest. If codex's stderr says the model needs a newer Codex, the fix is `codex update`, not re-auth — 0.143.0 rejected `gpt-5.6-sol` that way on 2026-07-25. Only codex's phrasing is recognised; the same wall on another CLI lands in `nonzero_exit` until its string joins `PERSISTENT_SENTINELS` |
| `tool_permission` | the seat could not get its OWN tool call approved — headless mode has no one to prompt, so it soft-denied its read and answered blind. **Retried** when SCANNED — a seat that merely READ a file containing one of these phrases lands here too (this repo's own docs do), and a phantom must cost an attempt, not a seat. `tool_permission` is not in `STRUCTURED_TERMINAL_REASONS`, so it retries on either path | confirm the match is a real CLI diagnostic before acting — see `TOOL_PERMISSION_SENTINELS` below. If real, the invocation needs its auto-approve flag and the manifest `hint` says which; report it as a bug, not a flake |
| `claude_error` / `codex_error` / `agy_error` | the provider reported a failure in its OWN structured field (claude `is_error`, codex `turn.failed`, agy `status != SUCCESS`) but the message matched no known cause. STRUCTURED, so it is trustworthy about *that a failure happened*; unrecognised, so it is **retried** | quote the provider's own wording from `result_text` — it is the CLI speaking, not a phrase scanned out of a transcript. No flag change is implied |
| `parse_failure` | the structured stream was malformed, or (in JSON mode) never started | the seat produced no usable answer; treat as a failed attempt, not as an empty one |
| `non_substantive` | exit 0 and non-empty, but shorter than the substantive floor — a stub answer, not an answer | drop it from synthesis; note the seat returned a non-answer |
| `did_not_read_input` | long enough, but never quoted the run's `SENTINEL-…` token, so it cannot be shown to have read the material | drop it from synthesis; treat its content as unfounded even though it reads confidently |
| `error_sentinel` | a transient error (rate-limit, overloaded) that survived retries | name the provider, quote the stderr tail; proceed with ≥2 if possible |
| `nonzero_exit` | crashed with no recognized cause | name the provider, quote the stderr tail; proceed if possible |
| `timeout` | hung past `--timeout` | offer a re-run with a larger `--timeout`; use partial output only as low-confidence. For **agy**: pre-1.1.1 CLIs reliably rode the whole window on substantive prompts (fixed upstream — see the HISTORY note in `build_real_spec`; 1.1.1 completed 54–97s reviews on 2026-07-11). If it recurs, retries multiply the wait — prefer `--providers claude,codex` when the third seat isn't worth the delay |
| `empty` / `parse_failure` | no usable answer extracted | drop it from synthesis; note it failed |

Note: some CLIs report their real failure only in a log, not on stdout/stderr (agy prints
nothing on a 429 and logs `RESOURCE_EXHAUSTED … Individual quota reached`). The engine
captures agy's `--log-file` and scans it, so a bare `empty` is upgraded to the precise
`auth_or_quota` — trust the manifest's `reason`.

The engine **always** emits a manifest and you **always** synthesize from whatever is
valid — degrade to 2/3 or 1/3 and report it, rather than aborting because one provider
died. The only hard stop is zero valid providers.

## Tuning (for maintainers)

To change which models sit on the council or how hard they think, edit the `MODES`
table at the top of `engine.py` (one cell per model/tier); the per-provider
flag mapping (`--effort`, `model_reasoning_effort`, agy's settings file) lives in
`build_real_spec`. When a real headless run surfaces a new failure string or an
output-parsing quirk, the fix also lives in `engine.py`: `TOOL_PERMISSION_SENTINELS`
(our invocation defect — a seat denied its own tool call; carries a `REASON_HINTS` fix)
vs `PERSISTENT_SENTINELS` (auth/quota) vs `TRANSIENT_SENTINELS` (rate-limit, overloaded),
`AGY_STRUCTURED_TOOL_PERMISSION` (agy's own soft-deny wording — STRUCTURED-ONLY, matched
against agy's `error` field and NEVER added to the scanned lists, since `permissions.allow`
is a config key and `--dangerously-skip-permissions` is a flag we pass ourselves),
`score_seat` / `MIN_SUBSTANTIVE_CHARS` (what makes a seat's answer count at all),
`extract_claude_json` / `extract_agy_json` / `extract_codex_json` / `extract_raw`
(how each CLI's answer is pulled out, and which of them carry structured provenance), and the
`build_real_spec` argv builders (the exact headless flags — kept in sync with
`headless-invocation.md` at the plugin root; note agy's Go-style flag parser needs every
flag *before* the positional prompt, and its real error lands in `--log-file`).

Those first three sentinel lists feed the SCANNED path, which is always retried; a reason taken
from a provider's own structured error field may be terminal when recognised
(`STRUCTURED_TERMINAL_REASONS`). Keep every phrase NARROW — a seat reviewing this repo
echoes these lists into its own stderr, and a match there is indistinguishable from the CLI
actually saying it. Add a matching case to `tests/stub_provider.py` and confirm
`python3 scripts/fanout.py --self-test` stays green. Validate live binaries cheaply with
`--smoke` before a full council.
