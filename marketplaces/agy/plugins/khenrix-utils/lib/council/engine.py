#!/usr/bin/env python3
"""llm-council engine — fan one prompt out to all three CLIs headlessly.

Runs the same prompt on claude / codex / agy in parallel (non-interactive, full
permissions), validates each result, retries failures with backoff, and writes a
JSON manifest the orchestrating CLI reads to synthesize a best answer. The flaky,
parallel, retry mechanics live here (deterministic, tested); the synthesis lives
in SKILL.md (LLM judgment).

Stdlib only — runs on any Python 3.11+ with no install step.

Usage:
  fanout.py --prompt-file PROMPT.txt [--providers claude,codex,agy] [--out json]
  fanout.py --self-test     # deterministic engine tests (no token cost)
  fanout.py --smoke         # one real provider, 'pong' check (costs tokens, needs auth)

Headless recipes encoded below come from headless-invocation.md. Tuning knobs the
"iterate until solid" loop touches: ERROR_SENTINELS, the extract_* functions, and
the per-provider argv builders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

MANIFEST_SCHEMA = 1
DEFAULT_PROVIDERS = ["claude", "codex", "agy"]
RESULT_TRUNCATE = 4000  # chars kept in the stdout manifest; full text is on disk

# --------------------------------------------------------------------------- #
# Council models + thinking modes — the runtime source for who sits on the council and
# how hard they think. A tier change is this cell alone; a NEW model id must also be
# registered in capabilities.toml [models], which scripts/lib/checks.py enforces.
#   normal — the default; all members at high thinking.
#   deep   — same models, maximum reasoning (and a longer default timeout) for
#            high-stakes / maximum-confidence questions.
# The claude seat is claude-opus-5 (2026-07-25). The plain id is deliberate: typical
# council prompts (a diff, a findings list) sit far inside the standard window, so the
# claude-opus-5[1m] long-context variant would price headroom these runs don't use.
# Prompt size is caller-controlled, though — for an unusually large one, override per run
# with `--model-claude "claude-opus-5[1m]"` rather than repinning the seat.
# `thinking` is an ABSTRACT tier (high|max); build_real_spec maps it to each
# CLI's own flag. agy (since 1.1.1) accepts a per-run `--model`; its thinking tier is
# encoded in the model string itself (e.g. "(High)"), so the agy cell's model IS
# applied at run time. `agy models` prints SLUGS (gemini-3.6-flash-high) since 1.1.5;
# the display label we pin is equally valid — agy's own model-resolution error lists
# the labels, and both forms were verified live on 1.1.8 and RE-PROBED 2026-08-08 on
# 1.1.11 — `agy models` lists slug and label side by side, and pinning the LABEL
# resolved at invocation.
# agy 1.1.5 also added a
# separate `--effort` flag; the engine deliberately does NOT pass it, because
# build_real_spec derives the recorded tier by regexing the label — a second knob
# could disagree with the provenance it reports.
# --------------------------------------------------------------------------- #
# FALLBACK (2026-08-12, owner's call): the claude seat is pinned to claude-opus-5 because
# Fable 5 is CREDIT-WALLED on this account — a fable-5 seat fails with "You're out of usage
# credits" before it reasons at all, which reads as a dead seat rather than a quota wall.
# Restore "claude-fable-5" here (both modes) when credits return.
MODES = {
    "normal": {
        "claude": {"model": "claude-opus-5",           "thinking": "max"},
        "codex":  {"model": "gpt-5.6-sol",            "thinking": "high"},
        "agy":    {"model": "Gemini 3.6 Flash (High)", "thinking": "high"},
    },
    "deep": {
        # `ultracode` and `ultra` are REAL BUT UNDOCUMENTED tiers, probed 2026-08-05 with
        # a garbage control on each: claude's help enumerates only low..max and
        # warn-and-IGNORES an unknown value (so a dropped tier would silently downgrade
        # this seat — the smoke asserts the warning's absence); codex accepts `ultra` and
        # fails CLOSED on garbage with an API 400. agy refuses any tier above high.
        "claude": {"model": "claude-opus-5",           "thinking": "ultracode"},
        "codex":  {"model": "gpt-5.6-sol",            "thinking": "ultra"},
        # Flash tops out at "(High)": no Max tier exists in any form (no `-max` slug,
        # and `--effort` caps at high). RE-PROBED 2026-08-08 ON agy 1.1.11 — both
        # `--effort ultra` and `--effort max` are refused with
        # `invalid --effort ... (valid: low, medium, high)`, and `--mode plan` (the
        # mechanical read-only barrier make_readonly uses) is still present. Earlier
        # confirmations on 1.1.7 and 1.1.8 agreed. agy's deep seat therefore runs
        # identically to normal; "high" keeps provenance truthful.
        "agy":    {"model": "Gemini 3.6 Flash (High)", "thinking": "high"},
    },
}
DEFAULT_MODE = "normal"
# Deep raised 600->1200 (2026-07-11): fable-5@max measured 649s and sol@max 796s on a
# substantive review — 600 killed both. Re-measured on the current panel (2026-07-25,
# one real diff review each): opus-5@max 565s, opus-4-8@max 529-623s, sol@max 374s — so
# 1200 still clears the slowest seat by a wide margin. For big deep prompts prefer
# --retries 0/1: a member that rode the window once will ride it again, and retries
# multiply the wait.
# `forge` is a BUILD window, not a review window: a forge seat does the whole task in an
# isolated clone — read, edit, test, commit — where `deep`'s 1200s sizes a single review turn.
# The inputs that exist are all review-shaped and already sit ~3x apart (claude 533s and codex
# 876s on one review prompt during this design's own review; agy re-probed at 608s on a SIMPLE
# prompt, recorded in build_real_spec), so an hour is chosen to clear a build strictly larger
# than any of them rather than fitted to a measurement of one.
# UNMEASURED, AND THIS IS THE ENTRY'S OPEN DEPENDENCY: no seat of ANY adapter has been run for
# an hour. What an hour-long three-adapter probe would settle is whether claude and codex stay
# HEALTHY that long — a CLI that idles out, drops its session or truncates before the engine's
# window closes makes 3600 a number no seat can reach, and the seat then dies carrying the
# provider's own error rather than `timeout`. If such a probe ever finds either of them
# unhealthy at this duration, THIS ENTRY IS WRONG; the fix is this number, never a second
# timeout mechanism elsewhere. agy is the only seat with evidence at length, and it is also
# the one whose --print-timeout tracks this value directly.
# `--mode` offers only the keys of MODES, so this entry is not reachable from this CLI: the
# forge front end reads it by name, and that is the only reader.
# NORMAL RAISED 300 -> 900 (2026-08-05) BECAUSE THE PANEL MOVED, NOT BECAUSE RUNS GOT
# SLOWER. Normal mode now carries Fable 5 at `max`, whose only substantive measurement is
# 649s — more than double the old window. Leaving 300 would have manufactured timeouts on
# routine councils and failed the gate CLOSED, which is the agy print-timeout lesson
# exactly: a fixed sub-engine cap turns slow success into reported failure.
MODE_TIMEOUT = {"normal": 900, "deep": 1200, "forge": 3600}  # per-attempt seconds

# Map the abstract thinking tier to each provider's own flag value.
# Unknown tiers pass through VERBATIM (`.get(t, t)`), which is how `ultracode`/`ultra`
# reach the CLIs at all — both are real but absent from `--help`. Probed 2026-08-05:
# claude accepts `ultracode` silently and warn-and-IGNORES garbage (a dropped tier would
# downgrade a seat with only a stderr line); codex accepts `ultra` and fails closed with
# an API 400 on garbage; agy refuses anything above `high`. RE-PROBED 2026-08-08 on
# claude 2.1.220 / codex 0.147.0 / agy 1.1.11: unchanged on all three — codex's exec
# header still prints `reasoning effort: ultra`, and claude's --help still omits
# `ultracode` while accepting it. The versions are stamped because a claim carrying a
# version it was NOT checked against is the provenance defect this repo keeps finding.
CLAUDE_EFFORT = {"high": "high", "max": "max"}   # claude --effort: low,medium,high,xhigh,max
# gpt-5.6-sol accepts low/medium/high/xhigh/max/ultra (probed 2026-07-11); "ultra" is
# deliberately unused — it spawns internal sub-agents (a council inside a council member)
# and is Pro-plan-gated, so deep mode maps to "max".
CODEX_EFFORT = {"high": "high", "max": "max"}

# A seat whose MODEL is the plausible cause of its failure retries on this model instead
# of burning the attempt on one that cannot answer. Fable sits behind the narrowest weekly
# sub-cap on this machine, and it now holds the claude seat in BOTH modes, so the wall is
# an expected event rather than a surprise.
FALLBACK_MODELS = {"claude": "claude-opus-5"}
# DELIBERATELY NARROW. `auth_or_quota` is the wall (and the codex version-gate wording);
# a structured unknown-model rejection lands in the *_error family. Everything else —
# timeout, parse_failure, tool_permission, non_substantive, did_not_read_input — has a
# cause the model is not, and swapping there would MASK the real defect behind a silent
# panel change: a window that needs resizing, a parser that needs fixing, an invocation
# flag that is ours to correct.
FALLBACK_REASONS = {"auth_or_quota", "claude_error"}

# Substrings that mark a provider's output as a failure rather than an answer.
# Scanned in stderr and the provider's log file always, and in the result text ONLY
# when the exit code is nonzero (so an exit-0 answer that legitimately discusses
# "rate limits" isn't rejected). Split by whether a retry could plausibly help:
#   PERSISTENT — auth missing or a quota wall; retrying only burns the budget, so
#                these are CLASSIFICATION, not retry policy — they still consume retries,
#                because the classification is scan-derived and can be a phantom.
#                (e.g. agy emits nothing to stdout on a 429 and logs
#                "RESOURCE_EXHAUSTED ... Individual quota reached" to its --log-file.)
#   TRANSIENT  — momentary; worth a bounded retry.
PERSISTENT_SENTINELS = [
    "not logged in",
    "please run `claude login`",
    "please run 'claude login'",
    "resource_exhausted",
    "individual quota",
    "quota reached",
    "quota exceeded",
    "authentication failed",
    "invalid api key",
    "no credentials",
    "unauthorized",
]
TRANSIENT_SENTINELS = [
    "rate limit",
    "usage limit",
    "overloaded",
    "try again later",
    "temporarily unavailable",
]
# Real-world failure strings observed across the three CLIs (extend in place so the
# additions read as list growth, not string concatenation). All lowercase — input is lowered.
PERSISTENT_SENTINELS.extend(["unauthenticated"])
# NOT "permission denied" — that lives in TOOL_PERMISSION_SENTINELS below, which
# classify_sentinel checks first. A seat denied its own tool call is our invocation
# defect, not the provider's auth wall, and the two want different remedies.
# A server-side model/CLI version gate: codex 0.143.0 rejected `-m gpt-5.6-sol` with an
# HTTP 400 telling us to upgrade (observed 2026-07-25 — on stderr, stdout was empty, so
# `evaluate`'s stderr scan does reach it) and the seat burned both retries on it. No retry
# clears a version gate; only upgrading the CLI does. Kept deliberately NARROW: sentinels
# are plain substrings, and a member reviewing this repo echoes these very files into
# stderr — the wider the phrase, the likelier an unrelated transient failure gets
# misclassified as permanent and loses its retry. Name the CLI to keep that window small.
PERSISTENT_SENTINELS.append("requires a newer version of codex")
TRANSIENT_SENTINELS.extend(["heap out of memory", "econnreset", "503"])
# A seat that could not get its OWN tool call approved. Distinct from auth_or_quota
# on purpose: an outage is the provider's problem and will recur, but this is OUR
# invocation defect — the seat authenticated fine and simply could not be granted
# permission to read the thing it was asked to review. Observed on the agy seat
# (tool_confirmation_manager.go:183) when it ran with `--mode plan` but WITHOUT
# `--dangerously-skip-permissions`: headless mode has no one to prompt, so agy
# soft-denied its own ReadFile and answered from an empty context.
TOOL_PERMISSION_SENTINELS = [
    "tool_confirmation_manager",
    "permission denied",
    "tool permission",
    "permission request",
    "requires approval",
    "user did not approve",
]
# `tool_permission` is deliberately NOT here. It is derived by substring-scanning a MERGED
# stderr stream, and a seat reviewing this repo echoes our own sentinel lists into that
# stream — observed 2026-07-27: a codex seat matched fanout.py's own self-test line, was
# classified non-retryable, and lost its retry to a defect that did not exist.
# Three heuristics were tried and each had a real counterexample: matching `file:line:`
# silenced agy's genuine bare-filename denial; requiring a `/` silenced every genuine
# denial reported with an absolute path; and treating quoted text as source silenced
# codex's version gate, which arrives as a JSON payload. Whether a phrase is the CLI
# speaking or the CLI quoting is not recoverable from a merged stream.
# What IS reliable is reproduction: a genuine denial recurs on retry, a phantom does not.
# So keep the actionable reason, but let the seat have its attempt.
#
# `auth_or_quota` is out for the SAME reason, verified 2026-07-28: this skill's own
# SKILL.md documents the strings `not logged in`, `resource_exhausted`, `individual quota`
# and `quota reached` in its failure table, so `classify_sentinel(<that file>)` returns
# `auth_or_quota` — and it carries NO tool-permission phrase, so nothing rescues it onto
# the retryable branch. A seat that echoed the doc it was reviewing then lost its seat to
# a wall that did not exist. Retrying a genuine quota wall costs up to `retries` extra
# attempts (3 total at the default) plus backoff — bounded, but not "one"; losing a seat
# to a phantom costs a third of the panel, silently. The trade still favours the retry. Only `not_installed` is terminal — a binary absent from PATH cannot appear
# between attempts.
NONRETRYABLE_REASONS = {"not_installed"}

# Reasons that ARE terminal when they came from a provider's own structured error field
# rather than a stderr scan. `run_provider` consults this only on the structured path, so
# the same string arriving via classify_sentinel(stderr) stays retryable — the whole point
# is that provenance, not the phrase, decides.
# `agy_error` is deliberately ABSENT: it is the catch-all for a structured error whose
# text matched no sentinel, and an UNRECOGNISED failure is precisely the one worth
# retrying. Only reasons we actually recognise — and therefore know will not clear —
# belong here.
STRUCTURED_TERMINAL_REASONS = {"auth_or_quota"}

# agy's own words for "I soft-denied a tool that needs permission" (agy >= 1.1.3, per
# yuting0624/antigravity-for-claude-code, which wraps the same headless mode). These are
# STRUCTURED-ONLY on purpose and must never join TOOL_PERMISSION_SENTINELS: that list is
# scanned against a MERGED stderr stream, and `permissions.allow` is a literal config key
# while `--dangerously-skip-permissions` is a flag WE pass in argv — either would match a
# seat that merely read a config file or an echoed command line, which is the phantom this
# engine already lost a seat to. Read out of agy's own `error` field they cannot be
# anything but the CLI speaking about itself.
AGY_STRUCTURED_TOOL_PERMISSION = [
    "auto-denied",
    "permissions.allow",
    "permission that headless",
]

# agy's own words for "I gave up waiting", read out of its structured `error` field and
# NOWHERE ELSE. STRUCTURED-ONLY on the same argument as the tool-permission list above: these
# are ordinary English phrases, and a seat reviewing this repository echoes this very list into
# its merged stderr — which is how this engine lost a seat to a phantom, twice.
# The first phrase is agy's, verbatim: on 1.1.8 every agy attempt in a 10-skill eval sweep died
# at ~124s reporting `timeout waiting for response`, and every one of them was classified
# `agy_error` — an unrecognised failure whose hint says "read result_text", when the actionable
# truth was that the window was too small. A wrong reason is not a smaller failure than a wrong
# outcome: it is what made a panel that had silently degraded to two seats look healthy.
# That death is usually OUR OWN MARGIN firing: --print-timeout is set 5s inside the engine
# window, so on a seat that runs long agy stops itself first and reports it here, while the
# engine's `subprocess.TimeoutExpired` path would have called the same event `timeout`. Before
# this mapping the two spellings of one event carried two different reasons.
# The second is NOT observed from agy here. It is the gRPC status name (DEADLINE_EXCEEDED)
# belonging to the same family as the RESOURCE_EXHAUSTED text agy does emit, so it is carried
# as a forward guess; if it never fires, it costs nothing, because an unmatched error already
# falls through to the retryable catch-all.
AGY_STRUCTURED_TIMEOUT = [
    "timeout waiting for response",
    "deadline exceeded",
]

# Actionable next step per failure cause, carried into the manifest so the
# synthesizer can tell the user something better than "the seat failed".
REASON_HINTS = {
    # NOT unconditional: this reason comes from scanning a merged stderr stream, so a seat
    # that merely READ a file containing a sentinel classifies here too. The seat is now
    # retried, so a phantom costs nothing — but confirm the match is a real CLI diagnostic
    # before changing any flags.
    "tool_permission": ("headless mode cannot prompt for tool approval — IF this is a real "
                        "denial, pass the seat's auto-approve flag (agy: "
                        "--dangerously-skip-permissions, kept alongside --mode plan). First "
                        "confirm: a match that is just source the seat READ is a false hit"),
    # Same caveat as tool_permission: scan-derived, so a seat that merely READ a file
    # naming these strings lands here. Retried now, so a phantom costs an attempt.
    "auth_or_quota": ("log in or wait out the quota window — but first confirm the match "
                      "is a real CLI diagnostic, not a file the seat read"),
    # The three structured catch-alls: the provider reported an error in its OWN field but
    # the text matched no sentinel. Unrecognised, therefore RETRYABLE — and the hint has to
    # say so, or an operator reads a bare token with no next step (which is what shipped).
    "claude_error": ("claude reported an error in its own JSON (`is_error`) that matched no "
                     "known cause — read `result_text` for the provider's wording; retried"),
    "codex_error": ("codex reported turn.failed / an error event that matched no known "
                    "cause — read `result_text` for the provider's wording; retried"),
    "agy_error": ("agy returned status != SUCCESS with an error that matched no known cause "
                  "— read `result_text` for the provider's wording; retried"),
    # Reached two ways that mean the same thing: this engine's own window closed on a live
    # seat (scanned provenance), or agy reported its own wait in its structured `error` field.
    # Retried on both paths.
    "timeout": ("the seat did not answer inside the per-attempt window — retried; if it "
                "keeps timing out, widen the window (--timeout, or the mode's MODE_TIMEOUT "
                "entry) rather than adding a second, tighter one somewhere else"),
    "did_not_read_input": ("the seat answered without opening its input — check that its "
                           "read tools are approved and the prompt fits its context"),
    "non_substantive": "the seat returned a stub answer rather than a real one",
}


def classify_sentinel(text: str) -> Optional[str]:
    """Map error text to a reason: tool-permission denial, persistent auth/quota,
    transient, or None. Tool-permission is checked FIRST — it is the most specific
    and the only one of the three we can actually fix on our side."""
    low = (text or "").lower()
    if any(s in low for s in TOOL_PERMISSION_SENTINELS):
        return "tool_permission"
    if any(s in low for s in PERSISTENT_SENTINELS):
        return "auth_or_quota"
    if any(s in low for s in TRANSIENT_SENTINELS):
        return "error_sentinel"
    return None


# --------------------------------------------------------------------------- #
# Seat validity — non-empty is NOT a pass.
# --------------------------------------------------------------------------- #
# A seat's answer must clear a length floor AND quote the per-run sentinel that was
# injected into its prompt. 400 chars is deliberately low: it rejects "I was unable to
# read the document." and other one-line non-answers without touching a terse-but-real
# reply. Raise it only with evidence — a floor that rejects real answers is worse than
# the bug it guards.
MIN_SUBSTANTIVE_CHARS = 400

SENTINEL_PREFIX = "SENTINEL-"
SENTINEL_NOTE = (
    "PROOF OF READING: quote the token `{token}` verbatim, on its own line, somewhere in "
    "your final response — this confirms you actually opened and read the material below. "
    "An answer that omits the token is discarded as unread and the seat is scored failed."
)


def make_sentinel() -> str:
    """A fresh per-run token. Unique per run so a seat cannot satisfy the check by
    echoing a token it saw in an earlier transcript."""
    return SENTINEL_PREFIX + uuid.uuid4().hex[:12]


def apply_sentinel(prompt: str, token: str) -> str:
    """One identical proof-of-reading instruction for all members, prompt preserved."""
    return f"{SENTINEL_NOTE.format(token=token)}\n\n{prompt}"


def _cites_sentinel(text: str, token: Optional[str]) -> bool:
    """No token configured (smoke, eval harness) → nothing to prove. Case-insensitive
    so a model that reflows or backticks the token still counts as having read."""
    return not token or token.lower() in (text or "").lower()


def score_seat(output: str, prompt_token: Optional[str] = None,
               min_chars: int = MIN_SUBSTANTIVE_CHARS) -> dict:
    """Score one seat's answer: {"status": "ok"|"failed", "cause": str, ...}.

    A seat is `ok` only if it produced substantive output AND demonstrably read its
    input. Non-empty is NOT sufficient: during the bootstrap-hardening review the agy
    seat soft-denied its own ReadFile, returned one sentence, and scored ok — silently
    turning a 3-seat verdict into a 2-seat one that the synthesis reported as three.

    Sentinel keywords are consulted ONLY after the answer has already failed the
    substance test, which preserves the invariant the noisy-ok regression bought:
    sentinels refine the *reason* of a failing attempt, they never veto a real answer.
    A 2000-char reply that discusses rate limits is an answer, not a rate-limit error.
    """
    text = (output or "").strip()
    if not text:
        return {"status": "failed", "cause": "empty"}

    substantive = len(text) >= min_chars
    if substantive and _cites_sentinel(text, prompt_token):
        return {"status": "ok", "cause": "ok"}

    refined = classify_sentinel(text)
    if refined:
        rec = {"status": "failed", "cause": refined}
        if REASON_HINTS.get(refined):
            rec["hint"] = REASON_HINTS[refined]
        return rec
    if not substantive:
        return {"status": "failed", "cause": "non_substantive",
                "detail": f"{len(text)} chars < {min_chars}",
                "hint": REASON_HINTS["non_substantive"]}
    return {"status": "failed", "cause": "did_not_read_input",
            "detail": f"response never cites sentinel {prompt_token!r}",
            "hint": REASON_HINTS["did_not_read_input"]}


def sum_usage(entries) -> Optional[dict]:
    """Total a seat's accounting across ALL attempts. None if nothing was ever measured.

    Absent stays absent: summing a reported 12k with an unreported attempt yields 12k and
    an `attempts_measured` count, never a silent claim that the rest were free.
    """
    entries = [e for e in entries if e]
    if not entries:
        return None
    out: dict = {}
    for e in entries:
        for k, v in e.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = out.get(k, 0) + v
    if out and len(entries) > 1:
        out["attempts_measured"] = len(entries)
    return out or None


def usage_tag(u: Optional[dict]) -> str:
    """Compact per-seat accounting for the summary line, or "" when nothing was reported.

    Deliberately silent rather than zero-filled: a seat whose CLI reports no usage must not
    render as "0 tok", which reads as a measurement. Cost is shown ONLY when the CLI priced
    the turn itself (claude's total_cost_usd) — deriving one here from pricing.toml would
    put an estimate next to two measurements in the same column, and nothing on the line
    would say which was which.
    """
    if not u:
        return ""
    parts = []
    tot = u.get("total")
    if tot is None:
        known = [u.get(k) for k in ("input", "output") if u.get(k) is not None]
        tot = sum(known) if known else None
    if tot is not None:
        parts.append(f"{tot / 1000:.1f}k tok" if tot >= 1000 else f"{tot} tok")
    if u.get("cache_read"):
        parts.append(f"{u['cache_read'] / 1000:.0f}k cached")
    if u.get("cost_usd") is not None:
        parts.append(f"${u['cost_usd']:.4f}")
    return "  " + " · ".join(parts) if parts else ""


def council_header(manifest: dict) -> str:
    """The one line the synthesis MUST open with, so a reduced panel can never be
    mistaken for a full one. States seats responded / attempted and names every failed
    seat with its cause — the failure the incident exposed was not the missing seat but
    the missing *disclosure* of the missing seat."""
    s = manifest["summary"]
    ok, total = s["valid"], s["requested"]
    head = f"**Council: {ok} of {total} seats responded"
    # A SUBSTITUTED SEAT IS NOT THE PANEL THE READER THINKS THEY GOT. A fallback keeps the
    # seat count whole, so without this clause a 3-of-3 header would describe a panel that
    # silently ran a different model — the same class of concealment the seat count exists
    # to prevent, one level down.
    swapped = [f"{p['name']} {p['model_fallback']['from']}→{p['model_fallback']['to']} "
               f"({p['model_fallback']['reason']})"
               for p in manifest["providers"] if p.get("model_fallback")]
    tail = ("  Model fallback: " + "; ".join(swapped) + ".") if swapped else ""
    if ok == total:
        return head + ".**" + tail
    lost = []
    for p in manifest["providers"]:
        if p.get("valid"):
            continue
        detail = f"{p['name']} ({p.get('reason') or 'unknown'}"
        if p.get("hint"):
            detail += f" — {p['hint']}"
        lost.append(detail + ")")
    return head + " — DEGRADED.**  Failed: " + "; ".join(lost) + tail


# --------------------------------------------------------------------------- #
# Result extraction — turn raw stdout into the substantive answer text.
# Each returns (text, extract_err) where extract_err is None on success or one of
# {"parse_failure", "claude_error"}.
# --------------------------------------------------------------------------- #
def extract_claude_json(stdout: str) -> tuple[str, Optional[str]]:
    """claude -p --output-format json → a single JSON object with a `result` field."""
    s = stdout.strip()
    if not s:
        return "", None
    obj = None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        # Tolerate a stray leading/trailing log line by grabbing the outer {...}.
        lo, hi = s.find("{"), s.rfind("}")
        if lo != -1 and hi > lo:
            try:
                obj = json.loads(s[lo:hi + 1])
            except json.JSONDecodeError:
                return "", "parse_failure"
        else:
            return "", "parse_failure"
    if not isinstance(obj, dict):
        return "", "parse_failure"
    if obj.get("is_error") or obj.get("subtype") not in (None, "success"):
        txt = obj.get("result") or obj.get("error") or ""
        return (txt if isinstance(txt, str) else json.dumps(txt)), "claude_error"
    res = obj.get("result")
    if isinstance(res, str):
        return res, None
    if res is None:
        return "", None
    return json.dumps(res), None


def extract_agy_json(stdout: str) -> tuple[str, Optional[str]]:
    """agy -p --output-format json -> {status, response, error, usage} (agy >= 1.1.8).

    Why this matters beyond parsing: `status`/`error` are **the CLI speaking**, not the CLI
    quoting a file it read. Every other reason this engine derives comes from
    substring-scanning a MERGED stderr stream, which cannot tell those apart — the root of
    both 2026-07 phantom incidents. A structured error is authoritative, so a reason taken
    from here may be terminal where a scanned one may not.

    `status` is the discriminator rather than the exit code — as DEFENCE IN DEPTH, not
    because the exit code lies. A previous version of this docstring claimed agy exits 0 on
    a hard model-resolution failure "verified 2026-07-28"; that was WRONG. It came from
    reading `$?` after a pipeline (`... | head`), which reports head's status. Measured off
    the process, agy and codex both exit 1. Retracted 2026-07-28.

    The real reason to run `--output-format json` is that it EVACUATES the session
    transcript from stderr — measured on codex, 311 bytes of transcript to 0. That
    transcript is the phantom generator: it echoes whatever files the seat read, including
    this repo's own sentinel lists. Reading `status` is then simply the correct way to
    consume the mode, and it costs nothing if the exit code agrees.
    """
    s = (stdout or "").strip()
    if not s:
        return "", None
    obj = None
    # strict=False: Python REJECTS a raw control character inside a JSON string, and agy
    # has been observed emitting a literal newline inside "response" (antigravity-for-
    # claude-code, 0.21.0, reported upstream). Our own 1.1.8 escaped correctly in two
    # probes, so this is defence rather than a reproduction — but a strict parse turns one
    # stray byte into `parse_failure`, i.e. a whole seat lost for a payload we can read.
    # Verified free: a well-formed envelope parses identically under both settings.
    try:
        obj = json.loads(s, strict=False)
    except json.JSONDecodeError:
        lo, hi = s.find("{"), s.rfind("}")   # tolerate a stray log line either side
        if lo != -1 and hi > lo:
            try:
                obj = json.loads(s[lo:hi + 1], strict=False)
            except json.JSONDecodeError:
                return "", "parse_failure"
        else:
            return "", "parse_failure"
    if not isinstance(obj, dict):
        return "", "parse_failure"
    status = obj.get("status")
    if status != "SUCCESS":
        # Fail CLOSED on anything that is not an explicit SUCCESS — including a MISSING or
        # null status. Two earlier versions leaked here: `== "ERROR"` let CANCELLED/TIMEOUT
        # through, and `is not None and != "SUCCESS"` then let a payload with no status at
        # all through, which is the shape a schema change would actually produce. Anything
        # that is not positively a success must degrade to "something went wrong".
        err = obj.get("error") or f"agy status={status}"
        return (err if isinstance(err, str) else json.dumps(err)), "agy_structured_error"
    res = obj.get("response")
    if isinstance(res, str):
        return res, None
    return ("" if res is None else json.dumps(res)), None


def extract_usage(name: str, stdout: str) -> Optional[dict]:
    """Per-seat token accounting taken from the CLI's OWN envelope, never estimated.

    Field names are read off upstream source, not guessed:
      * codex - `TurnCompletedEvent { usage: Usage }` in codex-rs/exec/src/exec_events.rs
        (input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens).
      * agy   - the `usage` object in its --output-format json envelope
        (input_tokens, output_tokens, thinking_tokens, cache_read_tokens, total_tokens),
        confirmed by running agy 1.1.8 rather than by reading a doc.

      * claude - the `usage` object in its --output-format json result, declared in the
        LICENSED package's own `sdk-tools.d.ts` (input_tokens, output_tokens,
        cache_creation_input_tokens, cache_read_input_tokens) and confirmed by probing
        2.1.220. It also reports `total_cost_usd`, which the CLI computes itself — so for
        this seat the cost is measured upstream rather than derived from pricing.toml.

    Returns None when a CLI reports nothing usable. Absent beats invented — a wrong cost is
    worse than no cost, and every field above was read off source or a live probe, never a
    doc or an inference.
    """
    def norm(u: dict) -> Optional[dict]:
        if not isinstance(u, dict):
            return None
        out = {}
        for key, srcs in (("input", ("input_tokens",)),
                          ("output", ("output_tokens",)),
                          ("thinking", ("thinking_tokens",)),
                          ("cache_read", ("cache_read_tokens", "cached_input_tokens",
                                          "cache_read_input_tokens")),
                          ("cache_write", ("cache_write_input_tokens",
                                           "cache_creation_input_tokens")),
                          ("total", ("total_tokens",))):
            for sk in srcs:
                v = u.get(sk)
                # bool is an int in Python; a JSON `true` here is malformed, not a count.
                if isinstance(v, int) and not isinstance(v, bool):
                    out[key] = v
                    break
        return out or None

    s = (stdout or "").strip()
    if not s:
        return None
    if name in ("agy", "claude"):
        try:
            obj = json.loads(s, strict=False)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        u = norm(obj.get("usage"))
        # claude prices its own turn; prefer that over anything we could derive.
        cost = obj.get("total_cost_usd")
        if u is not None and isinstance(cost, (int, float)) and not isinstance(cost, bool):
            u["cost_usd"] = float(cost)
        return u
    if name == "codex":
        # LAST turn.completed wins: a resumed or multi-turn stream carries several, and the
        # final one is the accounting for the run we actually made.
        found = None
        for line in s.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line, strict=False)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "turn.completed":
                u = norm(ev.get("usage"))
                if u:
                    found = u
        return found
    return None


def extract_codex_json(stdout: str) -> tuple[str, Optional[str]]:
    """codex exec --json -> NDJSON lifecycle events (codex-cli >= 0.145).

    Deliberately a SMALL state machine over documented event kinds:
      turn.completed              -> the turn succeeded
      turn.failed / top-level     -> structured failure, carrying the provider's own message
        {"type":"error"}
      item.completed              -> IGNORED for outcomes. It also carries non-fatal
                                     warnings (e.g. "Defaulting to fallback metadata"), and
                                     enumerating which item errors are fatal is the
                                     fail-open shape this engine has already been bitten by.
                                     Only its agent_message text is collected.

    Unknown well-formed events are ignored, so a schema ADDITION is harmless. Failing
    closed is scoped narrowly on purpose: only when there is neither answer text NOR a
    terminal event. If text was extracted and nothing declared failure, the answer goes to
    score_seat exactly as before — so a RENAMED terminal event degrades to today's
    behaviour rather than killing every seat.

    Why bother at all: `--json` evacuates codex's session transcript from stderr (measured
    2026-07-28: 311 bytes to 0). That transcript is what echoes back the files a seat read —
    including this repo's own sentinel lists — and is the phantom generator. NOT because
    the exit code lies; it does not (both CLIs exit 1 on a hard error).
    """
    text_parts: list = []
    err_msg = None
    saw_terminal = False
    saw_any_json = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue          # stdin/banner chrome is on stderr, but never trust that
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return "", "parse_failure"
        if not isinstance(ev, dict):
            return "", "parse_failure"
        saw_any_json = True
        kind = ev.get("type")
        if kind == "item.completed":
            item = ev.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                txt = item.get("text")
                if isinstance(txt, str):
                    text_parts.append(txt)
        elif kind == "turn.completed":
            saw_terminal = True
        elif kind in ("turn.failed", "error"):
            saw_terminal = True
            raw = ev.get("error") if kind == "turn.failed" else ev
            if isinstance(raw, dict):
                msg = raw.get("message")
            else:
                msg = raw
            err_msg = msg if isinstance(msg, str) else json.dumps(raw)
    if err_msg is not None:
        return err_msg, "codex_structured_error"
    joined = "\n".join(text_parts)
    if not saw_any_json:
        # We asked for --json. Non-empty output that contains no events means the stream
        # never started (a crash, a usage error), so accepting it as an ANSWER would let
        # arbitrary text clear score_seat and bypass every error signal. Empty output falls
        # through to the normal `empty` path instead.
        return ("", "parse_failure") if (stdout or "").strip() else ("", None)
    if not joined and not saw_terminal:
        return "", "parse_failure"         # no text AND no outcome: fail closed
    return joined, None


def extract_raw(stdout: str) -> tuple[str, Optional[str]]:
    """Keep stdout verbatim: the codex seat has no structured extractor wired.

    NOT "no such mode exists" — probed 2026-07-28: `codex exec` offers `--json`,
    `-o/--output-last-message` and `--ephemeral`. agy's equivalent (1.1.8
    `--output-format json`) IS now wired, in `extract_agy_json`, because its `status`/
    `error` fields give error PROVENANCE the stderr scan cannot. codex's `--json` earns
    its wiring only if it offers the same; parsing it purely for extraction would add an
    event schema to track for no classification gain. The synthesizer receives the raw file as
    ground truth and strips any CLI log chrome itself, so a weak extractor degrades
    synthesis but never loses data."""
    return stdout.strip(), None


# --------------------------------------------------------------------------- #
# Provider spec + the real per-CLI invocations.
# --------------------------------------------------------------------------- #
@dataclass
class ProviderSpec:
    name: str
    argv: list                       # full command; argv[0] is the binary
    stdin: Optional[str]             # text piped to stdin, or None
    extract: Callable[[str], tuple[str, Optional[str]]]
    model: Optional[str] = None
    thinking: Optional[str] = None   # abstract tier (high|max) recorded for provenance
    log_file: Optional[str] = None   # if set, scanned for sentinels on failure
    cwd: Optional[str] = None        # if set, the provider runs from this directory
    sentinel: Optional[str] = None   # per-run proof-of-reading token this seat must quote
    # Length floor for a substantive answer. Council seats use the default; callers whose
    # correct answer is legitimately tiny (--smoke expects "pong") set it to 0.
    min_chars: int = MIN_SUBSTANTIVE_CHARS
    # When set, run_provider calls this INSTEAD of evaluate() — council seat policy
    # (length floor, sentinel) is not a property of running a provider (spec §8.1).
    # Signature: (exit_code, stdout, stderr, spec) -> (valid, reason, result_text, structured)
    validator: Optional[Callable] = None


def agy_configured_model() -> Optional[str]:
    """Provenance FALLBACK only (since 1.1.1 the engine pins agy's model via --model):
    read the settings file so the manifest can still report agy's model when no MODES
    cell/override supplied one; return None if the file/key is absent."""
    try:
        p = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        return json.loads(p.read_text()).get("model")
    except Exception:  # noqa: BLE001 — best-effort; never fail a run over this
        return None


def swap_model(argv: list, name: str, old: str, new: str):
    """Return a copy of argv with the model flag repointed at `new`, or None if the flag
    is not there to swap.

    Returning None rather than appending a flag is the point: a seat whose argv carries no
    model flag is running the CLI's own default, and ADDING one would change what the run
    is — a silent widening of the fallback's blast radius past the one thing it is for.
    The flag name differs per provider (`--model` for claude/agy, `-m` for codex), so the
    value is located by its FLAG, never by scanning argv for a string that equals `old`.
    """
    flags = {"claude": "--model", "codex": "-m", "agy": "--model"}
    flag = flags.get(name)
    if not flag or flag not in argv:
        return None
    out = list(argv)
    i = out.index(flag)
    if i + 1 >= len(out):
        return None
    out[i + 1] = new
    return out


def build_real_spec(name: str, prompt: str, timeout: int,
                    cfg: dict, workdir: Path) -> ProviderSpec:
    """cfg maps provider -> {"model": str, "thinking": "high"|"max"} (from MODES,
    with per-run --model-* overrides already merged in by resolve_mode_config)."""
    pc = cfg.get(name, {})
    model, thinking = pc.get("model"), pc.get("thinking")
    if name == "claude":
        argv = ["claude", "-p", prompt, "--output-format", "json",
                "--dangerously-skip-permissions"]
        if model:
            argv += ["--model", model]
        if thinking:
            argv += ["--effort", CLAUDE_EFFORT.get(thinking, thinking)]
        return ProviderSpec("claude", argv, None, extract_claude_json, model, thinking)
    if name == "codex":
        # prompt via stdin (codex exec -) so it never enters a shell-escaped argv.
        argv = ["codex", "exec", "-", "--json",
                "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            argv += ["-m", model]
        if thinking:
            # codex -c parses the value as TOML, so quote the string explicitly.
            argv += ["-c", f'model_reasoning_effort="{CODEX_EFFORT.get(thinking, thinking)}"']
        return ProviderSpec("codex", argv, prompt, extract_codex_json, model, thinking)
    if name == "agy":
        # agy uses Go-style flag parsing: -p/--print is a boolean and the prompt is a
        # positional arg. Go's flag package STOPS at the first positional, so every flag
        # must come BEFORE the prompt — otherwise it's silently dropped, which leaves
        # --dangerously-skip-permissions un-applied and agy returns empty in seconds.
        # Since agy 1.1.1, `--model` pins the model per-run (thinking tier is encoded in
        # the model string, e.g. "Gemini 3.6 Flash (High)"; `agy models` prints the slug
        # form of the same set, and both resolve) —
        # the settings.json read remains only as manifest-provenance fallback. Since 1.1.2
        # an unresolvable --model hard-fails non-zero instead of silently downgrading to
        # the default, so a stale label here surfaces as a dead seat, never as a wrong
        # model reported under the right name.
        # --log-file captures agy's real failure reason: on a 429 it prints nothing to
        # stdout/stderr and only logs e.g. "RESOURCE_EXHAUSTED ... Individual quota
        # reached" — run_provider scans this file to turn an opaque `empty` into a clear
        # `auth_or_quota`. print-timeout self-terminates agy on a CLEAN idle wait (e.g. a
        # quota wall) just inside the engine timeout.
        #
        # RECALIBRATED 2026-07-30 — the fixed 120s cap is GONE. It was calibrated when
        # completions ran 42-100s (1.1.1 / 1.1.7) and the comment here named its own
        # trigger: "if agy answers ever truncate near 120s, re-probe and raise it". That
        # trigger fired. On 1.1.8 EVERY agy attempt in a 10-skill eval sweep died at ~124s
        # with agy's own structured `timeout waiting for response`, costing three skills
        # their receipts. Re-probed directly: a SIMPLE 400-word prompt took **608 seconds**
        # and returned SUCCESS with 2941 chars. agy is not hanging, it is ~6-10x slower
        # than when the cap was set, so any fixed sub-engine cap now converts a slow
        # SUCCESS into a manufactured failure — and an invalid run does not merely add
        # noise, it fails the receipt gate closed.
        # The fail-fast-on-a-quota-wall property is deliberately traded away: it saved
        # ~13 minutes on a wall that the ENGINE timeout bounds anyway, at the cost of
        # killing every legitimate answer. Re-probe before reinstating any fixed cap.
        # HISTORY: pre-1.1.1 (verified 2026-06-26), agy's headless `-p` mode
        # churned without emitting on non-trivial prompts and rode the window to `timeout`;
        # agy 1.1.1's release notes fixed `-p` hanging in subprocesses, and on 2026-07-11
        # agy completed multiple substantive council reviews in 54–97s. Timeouts can still
        # happen — treat them per the failure table, not as a certainty.
        pt = max(5, int(timeout) - 5)   # bounded by the ENGINE timeout, nothing tighter
        logf = str(Path(workdir) / "agy.cli.log")
        argv = ["agy", "--dangerously-skip-permissions", "--print-timeout", f"{pt}s",
                "--output-format", "json", "--log-file", logf]
        if model:
            argv += ["--model", model]
        argv += ["-p", prompt]
        # agy's tier is encoded in the model label — derive the recorded tier from the
        # FINAL string so a cross-tier --model-agy override can't leave stale provenance.
        final_model = model or agy_configured_model()
        m = re.search(r"\((Low|Medium|High)\)", final_model or "")
        return ProviderSpec("agy", argv, None, extract_agy_json,
                            final_model, m.group(1).lower() if m else None, log_file=logf)
    raise ValueError(f"unknown provider: {name}")


def _replace_flag(argv: list, old: str, new_tokens: list) -> list:
    out: list = []
    for a in argv:
        out.extend(new_tokens) if a == old else out.append(a)
    return out


READONLY_REVIEWER_NOTE = (
    "You are a read-only council reviewer. Answer directly and completely in your final "
    "message. Do not write, create, or update any plan file, and do not use ExitPlanMode."
)


def make_readonly(spec: ProviderSpec) -> ProviderSpec:
    """Swap a provider's "bypass everything" flag for a read-and-plan-only posture, in
    place. Unlike sandboxing HOME, this keeps the real HOME (so auth still resolves) but
    forbids writes — the executor can read/plan but cannot mutate config. Shared by the
    eval harness (executor runs) and reused by any read-only council mode.

    agy's read-only flag is `--mode plan` (1.1.1+). `--sandbox` (the earlier candidate) BROKE agy
    non-interactively: agy locates/reads files via terminal commands (find/grep) that the
    sandbox's terminal restrictions block, so it stalls on "searching…" and hangs the full
    engine window with EMPTY output — verified 2026-06-26 (--sandbox, even WITH
    --dangerously-skip-permissions, never completes a file read; plain
    --dangerously-skip-permissions reads + answers in seconds). So agy stays headless and its
    read-only posture no longer rests on intent alone: since agy 1.1.1, `--mode plan`
    works headless (probed 2026-07-11: reads files, answers fast, and mechanically blocked
    a write it claimed to have made) — so the bypass flag is swapped for it, mirroring
    claude's plan mode. Two soft layers remain on top: the READONLY_POSTURE prompt line
    and isolate_agy_worktree (cwd-relative mutations land in a throwaway git worktree).
    HISTORY: `--sandbox` (the pre-1.1.1 candidate) hung agy headless — verified
    2026-06-26; do not resurrect it without re-probing."""
    if spec.name == "claude":
        # Plan mode is the read-only mechanism, but its harness invites writing a plan
        # FILE (the one write plan mode allows) — suppress that side effect mechanically
        # (deny the plan-approval tool) and by instruction (answer inline).
        spec.argv = _replace_flag(spec.argv, "--dangerously-skip-permissions",
                                  ["--permission-mode", "plan",
                                   "--disallowedTools", "ExitPlanMode",
                                   "--append-system-prompt", READONLY_REVIEWER_NOTE])
    elif spec.name == "codex":
        spec.argv = _replace_flag(spec.argv, "--dangerously-bypass-approvals-and-sandbox",
                                  ["--sandbox", "read-only"])
    elif spec.name == "agy":
        # Plan mode is ADDED to the auto-approve flag, not swapped for it. Per
        # `agy --help` the two are orthogonal: --dangerously-skip-permissions is
        # "auto-approve all tool permission requests without prompting" (a prompting
        # policy) while --mode sets the execution mode (accept-edits|plan). Swapping
        # one for the other left agy headless with no way to approve its OWN reads:
        # it soft-denied its ReadFile at tool_confirmation_manager.go:183 and answered
        # from an empty context, which the engine then scored ok. Plan mode remains the
        # write barrier; auto-approve only removes a prompt no one can answer.
        spec.argv = _replace_flag(spec.argv, "--dangerously-skip-permissions",
                                  ["--dangerously-skip-permissions", "--mode", "plan"])
    return spec


def _pid_alive(pid: int) -> bool:
    """Test-only oracle. EPERM means the pid EXISTS but is not ours, so it must read as
    alive — returning False there would let D5's orphan check pass on a live orphan."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


_LIVE_PGIDS: set = set()   # process groups of in-flight members; drained by the signal handler


def _kill_group(proc: "subprocess.Popen", pgid) -> None:
    """SIGTERM the whole group, then ALWAYS SIGKILL it after the grace period.

    The SIGKILL is unconditional on purpose. Gating it on `proc.wait()` asks whether the
    DIRECT CHILD died, which is the wrong question: a well-behaved CLI exits promptly on
    SIGTERM, so the escalation would be skipped in exactly the common case, leaving any
    helper that ignores SIGTERM alive — holding the inherited stdout/stderr write ends
    open, which is what made the post-timeout drain hang forever.
    """
    if pgid is None:
        proc.kill()
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=3.0)          # grace for a clean exit; the sweep runs regardless
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_member(argv, *, stdin, timeout, env, cwd):
    """`subprocess.run`, except the child leads its own PROCESS GROUP.

    subprocess.run's timeout kills only the direct child. An agent CLI is not a leaf: it
    spawns language servers, MCP servers and node workers, and those survive the parent —
    holding their share of the machine and, for agy, the worktree we are trying to remove.
    The same gap exists on SIGTERM, where `_signal_cleanup` hard-exits via os._exit and
    nothing reaps the members at all. Leading a group makes the whole subtree addressable.

    Contract is deliberately identical to subprocess.run: FileNotFoundError from the
    constructor, TimeoutExpired carrying partial output, CompletedProcess otherwise.
    """
    new_session = hasattr(os, "killpg") and hasattr(os, "setsid")
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, cwd=cwd,
                            start_new_session=new_session)
    pgid = None
    if new_session:
        try:
            pgid = os.getpgid(proc.pid)
            _LIVE_PGIDS.add(pgid)
        except OSError:      # already exited; nothing to track
            pgid = None
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired as first:
        _kill_group(proc, pgid)
        # BOUNDED drain. communicate() returns only at EOF on BOTH pipes — i.e. when every
        # descendant holding the inherited write ends has exited — so an unbounded call
        # here turns `--timeout` into "no bound at all" whenever one helper survives, and
        # the hang sits inside a pool worker so the council yields no manifest at all.
        # Worse than the orphan it was added to fix. CPython's own subprocess.run skips
        # this second drain on POSIX for the same reason; the partial output is already
        # attached by the first _communicate.
        try:
            out, err = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            # Fall back to whatever the FIRST communicate already buffered rather than
            # "" — a timed-out seat's partial answer is the only evidence of what it was
            # doing, and the log-tail promotion downstream reads it.
            out, err = _coerce_text(first.output), _coerce_text(first.stderr)
            # DOUBLE TIMEOUT: a pipe holder escaped the group (e.g. it called setsid), so
            # the drain gave up with the fds still open and the child unreaped. Close our
            # ends and reap what we can.
            # DELIBERATELY UNTESTED: an fd-count assertion was written and then REMOVED
            # after in-place mutation testing showed it passes with this loop disabled —
            # CPython's GC closes the handles once `proc` falls out of scope, so the count
            # recovers either way and the check would have claimed coverage it lacks. This
            # is deterministic hygiene (release at a known point, not at GC's convenience),
            # not a defect fix, and it is recorded as such rather than dressed in a test.
            for _p in (proc.stdout, proc.stderr, proc.stdin):
                try:
                    if _p is not None:
                        _p.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass          # truly wedged; the group already got SIGKILL
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    except BaseException:
        # KeyboardInterrupt/SystemExit land here. Without this the `finally` would
        # DEREGISTER the pgid while the group was still running, so neither this handler
        # nor _signal_cleanup would ever reap it — Ctrl-C would leak the whole subtree.
        _kill_group(proc, pgid)
        raise
    finally:
        if pgid is not None:
            _LIVE_PGIDS.discard(pgid)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


_LIVE_WORKTREES: set = set()   # (repo, wt) handles; registered the moment `worktree add` succeeds
_STATE = {"handler_fired": False}   # mutable container: a facade re-export of a
                                    # rebindable bool would go permanently stale (spec §17)


def _signal_cleanup(signum, frame):
    """SIGTERM/SIGINT: a default-disposition SIGTERM kills Python without running
    `finally`, and sys.exit here would unwind into the executor's __exit__, which blocks
    on live member subprocesses for minutes — so remove the registered worktrees
    directly and hard-exit with the conventional 128+signum."""
    if not _STATE["handler_fired"]:   # re-entry guard: a second signal skips straight to exit
        _STATE["handler_fired"] = True
        for pgid in list(_LIVE_PGIDS):   # members first: agy's group holds the worktree open
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for handle in list(_LIVE_WORKTREES):
            remove_agy_worktree(handle)
    os._exit(128 + signum)


def install_cleanup_handler(force: bool = False) -> bool:
    """Install _signal_cleanup for SIGTERM/SIGINT — unless the caller already owns the
    handler. run_council once installed unconditionally, which silently replaced an
    embedding orchestrator's own handler (spec §17: 'calling run_council does not
    replace a pre-existing SIGTERM handler'). Returns True iff installed.

    Install in main()/smoke() BEFORE any worktree is created. Main-thread only —
    off-main-thread callers get ValueError, which is ignored (they also never create
    worktrees without a main-thread orchestrator)."""
    current = signal.getsignal(signal.SIGTERM)
    foreign = current not in (signal.SIG_DFL, signal.SIG_IGN, None, _signal_cleanup)
    if foreign and not force:
        return False
    try:
        signal.signal(signal.SIGTERM, _signal_cleanup)
        signal.signal(signal.SIGINT, _signal_cleanup)
    except ValueError:  # noqa: PERF203 — not the main thread; nothing to protect here
        return False
    return True


def _warn_isolation(detail: str) -> None:
    sys.stderr.write(f"WARNING: agy worktree isolation degraded — {detail}; "
                     "agy runs in the real cwd (plan mode + posture line still apply).\n")


def isolate_agy_worktree(spec: ProviderSpec, workdir: Path,
                         repo_dir: Optional[str] = None, *,
                         prune: bool = True, branch: Optional[str] = None,
                         register: bool = True) -> Optional[tuple]:
    """Point agy's cwd at a throwaway git worktree so cwd-relative mutations — the
    observed breakout class (2026-07-11: editing files, re-seeding receipts, `git add`)
    — land in a discarded copy instead of the real checkout. Since agy 1.1.1 the primary
    write barrier is `--mode plan` (see make_readonly); this worktree is defense in depth
    for the day plan mode fails or regresses. Identical conditions beat containment: the worktree mirrors the working
    tree (uncommitted tracked changes incl. binary; untracked files are absent), the
    caller's position inside the repo is preserved so relative paths resolve the same
    for every member, and if the mirror cannot be reproduced faithfully the isolation
    is ABANDONED with a stderr warning rather than letting agy silently review
    HEAD-only content. Quiet no-op outside a git repo.

    `register=False` withholds the handle from `_LIVE_WORKTREES`, so the SIGTERM handler
    will not delete it — for a caller (Plan B: forge) that owns its own handle disposition
    instead of the council's default "kill on signal" behaviour. Default True preserves
    council behaviour exactly.

    Returns (repo_root, worktree_path) for remove_agy_worktree."""
    handle = None
    try:
        top = subprocess.run(["git", "-C", repo_dir or ".", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
        if top.returncode != 0:
            return None  # not a git repo — nothing to isolate, nothing to warn about
        repo = top.stdout.strip()
        # Unregister worktrees leaked by previously crashed runs (temp dirs vanish but
        # their .git/worktrees/ registrations do not).
        if prune:
            subprocess.run(["git", "-C", repo, "worktree", "prune"],
                           capture_output=True, text=True, timeout=10)
        wt = str(Path(workdir) / "agy-worktree")
        if branch:
            add_cmd = ["git", "-C", repo, "worktree", "add", "-b", branch, wt, "HEAD"]
        else:
            add_cmd = ["git", "-C", repo, "worktree", "add", "--detach", wt, "HEAD"]
        add = subprocess.run(add_cmd, capture_output=True, text=True, timeout=30)
        if add.returncode != 0:
            _warn_isolation(f"worktree add failed: {add.stderr.strip()[:120]}")
            return None
        handle = (repo, wt)
        if register:
            _LIVE_WORKTREES.add(handle)   # from this instant a signal can clean it up
        # bytes mode end-to-end: text=True would newline-translate CRLF patch content
        # and raise on non-UTF-8 files, silently degrading isolation for such repos.
        diff = subprocess.run(["git", "-C", repo, "diff", "--binary", "--full-index", "HEAD"],
                              capture_output=True, timeout=30)
        if diff.returncode != 0:
            remove_agy_worktree(handle)
            _warn_isolation("could not read the working-tree diff")
            return None
        if diff.stdout.strip():
            ap = subprocess.run(["git", "-C", wt, "apply"], input=diff.stdout,
                                capture_output=True, timeout=30)
            if ap.returncode != 0:
                remove_agy_worktree(handle)
                _warn_isolation("could not mirror uncommitted changes")
                return None
        try:
            rel = Path(repo_dir or os.getcwd()).resolve().relative_to(Path(repo).resolve())
            spec.cwd = str(Path(wt) / rel)
        except ValueError:
            spec.cwd = wt
        Path(spec.cwd).mkdir(parents=True, exist_ok=True)  # subdir may hold only untracked files
        return handle
    except Exception:  # noqa: BLE001 — isolation is best-effort, never fail the run
        remove_agy_worktree(handle)
        _warn_isolation("unexpected error during setup")
        return None


def remove_agy_worktree(handle: Optional[tuple]) -> None:
    """Discard the throwaway worktree (and anything agy wrote into it). Idempotent —
    also deregisters the handle, so handler-then-finally double-removal is harmless."""
    if not handle:
        return
    repo, wt = handle
    try:
        # Double --force: the worktree is expected to be dirty if agy misbehaved.
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", "--force", wt],
                       capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001 — best-effort cleanup; workdir is a temp dir
        pass
    # Deregister AFTER the attempt: a signal landing mid-removal still sees the handle,
    # so the handler can retry; double removal is idempotent.
    _LIVE_WORKTREES.discard(handle)


# --------------------------------------------------------------------------- #
# Validation.
# --------------------------------------------------------------------------- #
def evaluate(exit_code: Optional[int], stdout: str, stderr: str,
             spec: ProviderSpec) -> tuple[bool, str, str, bool]:
    """Return (valid, reason, result_text, structured).

    `structured` is True only when the reason came from a provider's OWN error field
    rather than a stderr scan. Provenance — not the phrase — is what decides whether a
    reason may be terminal, because a scanned phrase can be a file the seat merely read.

    A clean exit is necessary but NOT sufficient: the answer must also clear
    score_seat (substantive length + the per-run sentinel proving the seat read its
    input). Before that check existed, a seat that soft-denied its own ReadFile and
    replied with one sentence scored `ok` and silently shrank the panel.

    Sentinels never veto a real answer — they only refine the *reason* of an
    already-failing attempt. This is essential because some CLIs stream their whole
    session to stderr: codex echoes the files it reads (e.g. this very SKILL.md, whose
    failure table lists "quota reached" / "not logged in") into stderr, and an answer
    must not be discarded just because that noise mentions a sentinel phrase. claude is
    the exception — it reports its own errors structurally (is_error in the JSON), not
    via the exit code, so that path is checked explicitly."""
    result_text, extract_err = spec.extract(stdout)
    if extract_err == "parse_failure":
        return False, "parse_failure", result_text, False
    if extract_err == "claude_error":
        # STRUCTURED: this text is claude's own `is_error`/`result` field. Merging stderr in
        # used to hand it the same phantom surface the structured path exists to remove —
        # a phrase in the transcript could decide the reason. Scan only the provider's field.
        return False, classify_sentinel(result_text) or "claude_error", result_text, True
    if extract_err == "codex_structured_error":
        # Scan ONLY the provider's own error message, never the NDJSON stream — that
        # stream carries every file the seat read.
        return False, classify_sentinel(result_text) or "codex_error", result_text, True
    if extract_err == "agy_structured_error":
        # AUTHORITATIVE: this text came out of agy's own `error` field, so it is the CLI
        # speaking about itself — not a file it happened to read. Scan ONLY that field,
        # never the merged stderr, or the provenance advantage is thrown away and the
        # phantom is back. A reason derived here is eligible to be terminal.
        low = (result_text or "").lower()
        if any(p in low for p in AGY_STRUCTURED_TOOL_PERMISSION):
            # Our invocation defect, not agy's wall: the seat authenticated fine and was
            # simply never granted a tool. RETRYABLE like every other tool_permission —
            # reproduction is the signal, and a wrong `--mode`/flag combination recurs.
            return False, "tool_permission", result_text, True
        sent = classify_sentinel(result_text)
        if sent:
            # A QUOTA WALL REPORTED AS A TIMEOUT IS A QUOTA WALL, and this ordering is why.
            # `auth_or_quota` IS in STRUCTURED_TERMINAL_REASONS and `timeout` is not, so
            # matching the timeout phrase first would turn a wall into three retried attempts.
            return False, sent, result_text, True
        if any(p in low for p in AGY_STRUCTURED_TIMEOUT):
            # DO NOT ADD `timeout` TO `STRUCTURED_TERMINAL_REASONS`. `run_provider` terminates
            # only on `structured and reason in STRUCTURED_TERMINAL_REASONS`, so putting it
            # there would SILENTLY REMOVE COUNCIL'S TIMEOUT RETRIES — a seat that rode a slow
            # window once would lose its remaining attempts, which is exactly the failure the
            # removed 120s cap caused. `structured=True` here buys the PROVENANCE (this is agy
            # speaking about itself, not a file it read), not terminality.
            return False, "timeout", result_text, True
        return False, "agy_error", result_text, True
    if exit_code == 0:
        seat = score_seat(result_text, spec.sentinel, spec.min_chars)
        if seat["status"] == "ok":
            return True, "ok", result_text, False
        # `empty` keeps its historical name; the richer causes are new.
        return False, seat["cause"], result_text, False
    return False, classify_sentinel(stderr) or "nonzero_exit", result_text, False


# --------------------------------------------------------------------------- #
# Execution.
# --------------------------------------------------------------------------- #
def child_env(base=None) -> dict:
    """Child env with the recursion-depth guard incremented, over `base` or this process's own.

    `base` COMPOSES RATHER THAN REPLACES. A caller that has already SCRUBBED an environment
    passes it here and gets the council's guard applied ON TOP of that scrub; reading
    `os.environ` unconditionally would hand the child back every name the caller removed. The
    caller this exists for is `forge.fleet.forge_child_env`, which strips `gitcmd.HOSTILE_ENV`,
    pins config discovery to /dev/null and increments `LLM_FORGE_DEPTH` — three defences that
    a seat launched through `run_provider` would otherwise never receive, because
    `LLM_COUNCIL_DEPTH` guards the council and bars nothing about `/llm-forge`.
    """
    env = dict(os.environ if base is None else base)
    cur = int(env.get("LLM_COUNCIL_DEPTH", "0") or "0")
    env["LLM_COUNCIL_DEPTH"] = str(cur + 1)
    return env


def _coerce_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def run_provider(spec: ProviderSpec, retries: int, timeout: int,
                 backoff: float, workdir: Path, *, env=None) -> dict:
    """Run one provider through its bounded attempt loop and return its record.

    `env` is the environment the provider's child process runs under, BEFORE this function's
    own depth guard is applied to it; `None` keeps the previous behaviour exactly
    (`os.environ` plus the guard). Keyword-only and defaulted, so no existing caller changes.
    """
    attempt_log: list = []
    final = {"stdout": "", "stderr": "", "exit_code": None,
             "reason": "unknown", "result_text": "", "valid": False, "structured": False,
             "duration_sec": 0.0, "status": "failed"}

    for attempt in range(retries + 1):
        n = attempt + 1
        t0 = time.monotonic()
        try:
            cp = run_member(spec.argv, stdin=spec.stdin, timeout=timeout,
                            env=child_env(env), cwd=spec.cwd)
        except FileNotFoundError:
            dur = round(time.monotonic() - t0, 2)
            attempt_log.append({"attempt": n, "reason": "not_installed",
                                "exit_code": None, "duration_sec": dur})
            final.update(stdout="", stderr=f"binary not found: {spec.argv[0]}",
                         exit_code=None, reason="not_installed", result_text="",
                         valid=False, duration_sec=dur, status="not_installed")
            _write_attempt(workdir, spec.name, n, "", final["stderr"])
            break  # missing binary won't appear on a retry — fail fast
        except OSError as e:
            # A SPAWN THAT NEVER HAPPENED IS A DEAD SEAT, NOT AN ENGINE CRASH. The prompt is
            # one argv element for claude and agy, and Linux caps a single argv string at
            # MAX_ARG_STRLEN (131_072 bytes) — a longer one raises E2BIG here. Before this
            # branch that OSError propagated out of the worker future, out of run_council and
            # out of every caller: forge's --collect journalled its intent, crashed with a
            # raw traceback, and the orphaned intent made the next --collect re-spend the
            # seats that HAD started. Degrading to an invalid seat keeps the panel's own
            # contract — a seat that could not answer is reported, never raised.
            dur = round(time.monotonic() - t0, 2)
            detail = f"{type(e).__name__}: {e}"
            attempt_log.append({"attempt": n, "reason": "spawn_failed",
                                "exit_code": None, "duration_sec": dur})
            final.update(stdout="", stderr=detail, exit_code=None,
                         reason="spawn_failed", result_text="", valid=False,
                         duration_sec=dur, status="failed")
            _write_attempt(workdir, spec.name, n, "", detail)
            break  # an argv this kernel refuses is refused identically on a retry
        except subprocess.TimeoutExpired as e:
            dur = round(time.monotonic() - t0, 2)
            stdout, stderr = _coerce_text(e.stdout), _coerce_text(e.stderr)
            valid, reason, result_text, structured = False, "timeout", stdout.strip(), False
            exit_code = None
        else:
            dur = round(time.monotonic() - t0, 2)
            stdout, stderr, exit_code = cp.stdout or "", cp.stderr or "", cp.returncode
            valid, reason, result_text, structured = (spec.validator or evaluate)(exit_code, stdout, stderr, spec)

        if final["status"] != "not_installed":
            # A provider with a log file may hide its real failure there: agy prints
            # nothing on a 429 but logs the quota error. Promote an opaque `empty`/
            # `timeout` to a precise `auth_or_quota`/`error_sentinel` from the log.
            # SKIPPED when we already have a STRUCTURED reason: the provider's own error
            # field outranks a scan of its log. Promoting over it would replace an
            # authoritative reason with a scanned one and clear `structured`, so the
            # structured path would stop being authoritative the moment agy also logged a
            # matching phrase — which is exactly when it matters most.
            if not valid and spec.log_file and not structured:
                try:
                    logtail = Path(spec.log_file).read_text()[-8000:]
                except OSError:
                    logtail = ""
                sent_log = classify_sentinel(logtail)
                if sent_log:
                    reason = sent_log
                    # The log tail is SCANNED text, so this reason has no structured
                    # provenance no matter what evaluate() concluded. Leaving `structured`
                    # True here would let a phrase the seat merely logged become terminal —
                    # exactly the phantom class the structured path exists to escape.
                    structured = False
            _write_attempt(workdir, spec.name, n, stdout, stderr)
            attempt_log.append({"attempt": n, "reason": reason,
                                "usage": extract_usage(spec.name, stdout),
                                "exit_code": exit_code, "duration_sec": dur})
            final.update(stdout=stdout, stderr=stderr, exit_code=exit_code,
                         reason=reason, result_text=result_text, valid=valid,
                         # SERIALIZED: a consumer cannot otherwise tell a reason that came
                         # from the provider's own error field from one scanned out of a
                         # merged stream — and that distinction is the whole basis for
                         # trusting it. It drove the retry decision internally while being
                         # invisible in the manifest, so every doc telling an operator to
                         # "check structured" was describing a field that did not exist.
                         structured=structured,
                         duration_sec=dur, status="ok" if valid else "failed")
            if valid:
                break
            if structured and reason in STRUCTURED_TERMINAL_REASONS:
                # The provider said so itself, in its own error field — retrying cannot
                # change it, and unlike a scanned phrase this cannot be a file it read.
                break
            if reason in NONRETRYABLE_REASONS:
                # Currently unreachable: the sole member, `not_installed`, short-circuits
                # in its own FileNotFoundError handler above. Kept as the declared policy
                # both test suites assert on, and as the hook any FUTURE terminal reason
                # attaches to — not as a claim that it fires today.
                break
            if attempt < retries:
                # MODEL FALLBACK, once per seat: retrying the SAME model against a wall
                # spends an attempt to learn what the last one already proved. Only for a
                # model-attributable reason (FALLBACK_REASONS) — see that constant for
                # why masking any other cause here would be worse than the wall.
                fb = FALLBACK_MODELS.get(spec.name)
                if fb and reason in FALLBACK_REASONS and not final.get("model_fallback") \
                        and spec.model != fb:
                    swapped = swap_model(spec.argv, spec.name, spec.model, fb)
                    if swapped is not None:
                        final["model_fallback"] = {"from": spec.model, "to": fb,
                                                   "reason": reason, "attempt": n}
                        spec = replace(spec, argv=swapped, model=fb)
                time.sleep(backoff * (2 ** attempt))

    # Persist final raw + extracted text and reference the files in the record.
    result_file = workdir / f"{spec.name}.result.txt"
    stdout_file = workdir / f"{spec.name}.stdout.txt"
    stderr_file = workdir / f"{spec.name}.stderr.txt"
    result_blob = final["result_text"].encode("utf-8")
    result_file.write_bytes(result_blob)
    stdout_file.write_text(final["stdout"])
    stderr_file.write_text(final["stderr"])

    return {
        "name": spec.name,
        "status": final["status"],
        "attempts": len(attempt_log),
        "exit_code": final["exit_code"],
        "duration_sec": final["duration_sec"],
        "valid": final["valid"],
        "reason": final["reason"],
        # PROVENANCE. Without this a consumer cannot tell a reason taken from the
        # provider's own error field from one scanned out of a merged stream — which is
        # the entire basis for trusting it, and what decides whether it may be terminal.
        "structured": final["structured"],
        # Measured, not estimated — None when a CLI's envelope carries no usage object,
        # or when the seat's stdout did not parse. All three seats are handled today.
        # Summed over EVERY attempt: a retried seat really did spend its earlier attempts,
        # and reporting only the survivor put an undercount on the same line as "3x" and
        # called it a total. This function refuses to derive cost from pricing.toml
        # precisely so a measurement is never mixed with an estimate — an undercount
        # presented as a total is the same lie by a shorter route.
        "usage": sum_usage(a.get("usage") for a in attempt_log),
        "hint": REASON_HINTS.get(final["reason"]),
        "result_text": _truncate(final["result_text"]),
        "result_file": str(result_file),
        # THE DIGEST OF WHAT WAS VALIDATED, beside the path it was written to. `result_text`
        # above is TRUNCATED, so a consumer that needs the whole answer must re-read the file
        # — `forge.review._result_text` does, and says why — and without this there is nothing
        # binding the bytes that passed validation to the bytes that get parsed. The window is
        # not hypothetical: seats run concurrently as the same user with a shell, one can
        # finish and be overwritten by another still running, and a `[blocker]` replaced by
        # "this candidate is clean" parses exactly as cleanly as the original.
        "result_sha256": hashlib.sha256(result_blob).hexdigest(),
        "raw_stdout_file": str(stdout_file),
        "raw_stderr_file": str(stderr_file),
        # `spec` is rebound on a model fallback, so this is the model that ACTUALLY
        # answered — never the one originally requested. `model_fallback` is what makes
        # the difference visible; without it a substituted seat is indistinguishable from
        # one that ran the configured panel.
        "model": spec.model,
        "model_fallback": final.get("model_fallback"),
        "thinking": spec.thinking,
        "isolated_cwd": spec.cwd,
        "attempt_log": attempt_log,
    }


def run_council(specs: list[ProviderSpec], *, retries: int, timeout: int,
                backoff: float, workdir: Path,
                prompt: Optional[str] = None,
                requested: Optional[list] = None,
                mode: Optional[str] = None,
                read_only: Optional[bool] = None,
                install_signal_handler: bool = True,
                env=None) -> dict:
    # `env` is handed to every member, BEFORE `child_env` applies the council's own depth
    # guard on top of it; `None` keeps the previous behaviour exactly (`os.environ` plus the
    # guard). Keyword-only and defaulted, so no existing caller changes. The caller this
    # exists for is `forge.review.run_round`, whose panel must not be able to write bytecode
    # into the tree it is reviewing — `run_provider` already took an `env`, and a council of
    # members that could not be given one meant that seam stopped at the single-member call.
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    # Install at the CHOKE POINT, not in the callers. Members now lead their own session,
    # so a terminal Ctrl-C no longer reaches them and this handler is their only reaper.
    # It used to be installed only under `if args.read_only:` — correct when its sole job
    # was removing the agy worktree (read-only mode only), but the handler's
    # responsibilities grew and its installation condition did not. That left
    # `--allow-writes` runs, which carry the bypass flags and NO worktree isolation, and
    # every eval_harness run (it calls run_council directly, never main) with no teardown
    # at all: abort, and three detached members keep editing for up to timeout x attempts.
    if install_signal_handler:
        install_cleanup_handler()
    started = _now_iso()
    with ThreadPoolExecutor(max_workers=max(1, len(specs))) as ex:
        futures = [ex.submit(run_provider, s, retries, timeout, backoff, workdir, env=env)
                   for s in specs]
        providers = [f.result() for f in futures]
    finished = _now_iso()

    valid = sum(1 for p in providers if p["valid"])
    requested = requested if requested is not None else [s.name for s in specs]
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "prompt_sha256": (hashlib.sha256(prompt.encode()).hexdigest()
                          if prompt is not None else None),
        "workdir": str(workdir),
        "started_at": started,
        "finished_at": finished,
        "config": {"retries": retries, "timeout": timeout, "backoff": backoff,
                   "providers": requested, "mode": mode, "read_only": read_only},
        "summary": {"requested": len(requested), "valid": valid,
                    "failed": len(requested) - valid,
                    "degraded": valid < len(requested),
                    # Explicit seat accounting so the synthesizer cannot present a
                    # reduced panel as a full one; `header` is emitted verbatim.
                    "seats_attempted": len(requested), "seats_responded": valid},
        "providers": providers,
    }
    manifest["summary"]["header"] = council_header(manifest)
    (workdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(s: str, n: int = RESULT_TRUNCATE) -> str:
    return s if len(s) <= n else s[:n] + f"\n…[truncated {len(s) - n} chars; read result_file]"


def _write_attempt(workdir: Path, name: str, n: int, stdout: str, stderr: str) -> None:
    (workdir / f"{name}.stdout.attempt-{n}.txt").write_text(stdout)
    (workdir / f"{name}.stderr.attempt-{n}.txt").write_text(stderr)


def _render_text(manifest: dict) -> str:
    s = manifest["summary"]
    lines = [s.get("header") or council_header(manifest)]
    cfg = manifest.get("config", {})
    tags = []
    if cfg.get("mode"):
        tags.append(f"mode: {cfg['mode']}")
    if cfg.get("read_only") is not None:
        tags.append("read-only" if cfg["read_only"] else "writes")
    if tags:
        lines[0] += "  [" + ", ".join(tags) + "]"
    for p in manifest["providers"]:
        mark = "✓" if p["valid"] else "✗"
        meta = f"{p.get('model') or '-'}/{p.get('thinking') or '-'}"
        lines.append(f"  {mark} {p['name']:<7} {p['reason']:<14} {p['attempts']}x  "
                     f"{p['duration_sec']}s  {meta}{usage_tag(p.get('usage'))}"
                     f"  → {p['result_file']}")
    # Council total, but ONLY over seats that actually priced themselves — a sum that
    # silently omitted the unpriced seats would understate the run while looking complete.
    priced = [p for p in manifest["providers"] if (p.get("usage") or {}).get("cost_usd") is not None]
    if priced:
        tot = sum(p["usage"]["cost_usd"] for p in priced)
        n, all_n = len(priced), len(manifest["providers"])
        scope = "all seats" if n == all_n else f"{n} of {all_n} seats — the rest do not report cost"
        lines.append(f"  cost: ${tot:.4f} ({scope})")
    return "\n".join(lines)


# Prepended to the prompt (identically for every member) when the council runs
# read-only. Defense in depth: every member is now mechanically constrained (claude
# plan mode, codex sandbox, agy --mode plan since 1.1.1) — this line and the agy
# worktree are the soft layers on top, added after agy executed a review-framed
# prompt (editing files, re-seeding receipts, staging) on 2026-07-11. claude also gets
# the plan-mode-specific READONLY_REVIEWER_NOTE via make_readonly — keep both in mind
# when editing either wording. Says "as text", not "prose only": answers may still
# contain code blocks/diffs — the guard is against mutating state, not against code.
READONLY_POSTURE = ("COUNCIL POSTURE: read-only — do not create, modify, stage, or "
                    "commit files, or change any repo/system state; propose any "
                    "changes as text in your answer.")


def apply_readonly_posture(prompt: str) -> str:
    """One identical posture line for all members, preserving identical conditions."""
    return f"{READONLY_POSTURE}\n\n{prompt}"


# Members run with each CLI's full skill/plugin surface (verified 2026-07-13: claude -p,
# codex exec, and agy -p all discover installed skills, including the khenrix-ported set) —
# nudge them to USE those skills, but bar the one recursive skill. Engine-level defense in
# depth: the LLM_COUNCIL_DEPTH guard already hard-blocks a nested fan-out, but a member
# that tries llm-council anyway wastes its whole turn on the refusal — the note prevents
# the attempt. Applied only on the council paths (main/smoke), NEVER in build_real_spec:
# the eval harness reuses build_real_spec for executors, where "you are a council member"
# would be false and would distort the with-vs-without benchmark.
MEMBER_SKILLS_NOTE = ("COUNCIL MEMBER NOTE: use any skills/plugins available in your "
                      "environment when they materially help with this task — EXCEPT any "
                      "council/fan-out skill (e.g. llm-council). You are already answering "
                      "as a council member: never convene another council or delegate this "
                      "question to other CLIs. Your FINAL message is the only thing read: "
                      "it must carry your complete answer. If you use a tool after drafting "
                      "it, repeat the whole answer afterwards — never close with a remark "
                      "that refers back to it (\"my review above stands\").")


def apply_member_note(prompt: str) -> str:
    """One identical skills note for all members, preserving identical conditions."""
    return f"{MEMBER_SKILLS_NOTE}\n\n{prompt}"


def resolve_prompt(args) -> str:
    if args.prompt is not None:
        return args.prompt
    if args.prompt_file:
        return Path(args.prompt_file).read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


# --------------------------------------------------------------------------- #
# Live smoke (Layer B) — cheap reality check against real binaries.
# --------------------------------------------------------------------------- #
def smoke(args) -> int:
    prompt = "Reply with exactly one word and nothing else: pong"
    prompt = apply_member_note(prompt)           # smoke exercises the real prompt shape
    if args.read_only:
        prompt = apply_readonly_posture(prompt)
    providers = args.providers.split(",") if args.providers else ["claude"]
    workdir = Path(tempfile.mkdtemp(prefix="llm-council-smoke-"))
    timeout = effective_timeout(args)
    cfg = resolve_mode_config(args)
    specs = [build_real_spec(p, prompt, timeout, cfg, workdir) for p in providers]
    for s in specs:
        s.min_chars = 0   # the correct smoke answer is the single word "pong"
    agy_wt = None
    if args.read_only:
        for s in specs:
            make_readonly(s)
        agy_spec = next((s for s in specs if s.name == "agy"), None)
        if agy_spec:
            agy_wt = isolate_agy_worktree(agy_spec, workdir)
    try:
        manifest = run_council(specs, retries=args.retries, timeout=timeout,
                               backoff=args.backoff, workdir=workdir, prompt=prompt,
                               requested=providers, mode=args.mode, read_only=args.read_only)
    finally:
        remove_agy_worktree(agy_wt)
    print(_render_text(manifest))
    ok = all(p["valid"] and "pong" in Path(p["result_file"]).read_text().lower()
             for p in manifest["providers"])
    print(f"\nsmoke {'PASS' if ok else 'FAIL'}  (artifacts: {workdir})")
    return 0 if ok else 1


def effective_timeout(args) -> int:
    """--timeout if given, else the per-mode default (deep gets a longer window)."""
    if args.timeout is not None:
        return args.timeout
    return MODE_TIMEOUT.get(args.mode, MODE_TIMEOUT[DEFAULT_MODE])


def resolve_mode_config(args) -> dict:
    """Per-provider {model, thinking} for the chosen mode, with --model-* overrides
    applied on top (ad-hoc per-run model swaps without touching the MODES table)."""
    base = {p: dict(v) for p, v in MODES.get(args.mode, MODES[DEFAULT_MODE]).items()}
    for p, m in (("claude", args.model_claude), ("codex", args.model_codex),
                 ("agy", args.model_agy)):
        if m:
            base.setdefault(p, {})["model"] = m
    return base


# --------------------------------------------------------------------------- #
# Self-test (Layer A) — drive the REAL engine with stub providers.
# --------------------------------------------------------------------------- #
def _find_stub() -> Path:
    """tests/ stays under the SKILL (the wholesale skill copy carries it into rendered
    plugins; the SHARED_LIBS bundle strips tests/). From this file the skill sits at
    <root>/skills/llm-council in a plugin and <shared>/skills/llm-council in the repo —
    the same relative expression either way (spec §17.3)."""
    here = Path(__file__).resolve()
    for cand in (here.parent.parent / "tests" / "stub_provider.py",          # legacy layout
                 here.parents[2] / "skills" / "llm-council" / "tests" / "stub_provider.py"):
        if cand.is_file():
            return cand
    return here.parent.parent / "tests" / "stub_provider.py"   # best effort; self-test will say so

STUB = _find_stub()


def _stub_spec(name: str, mode: str, *, as_: str = "raw", sleep: float = 0.0,
               counter: Optional[Path] = None,
               extract: Optional[Callable] = None,
               answer: Optional[str] = None,
               sentinel: Optional[str] = None,
               model: Optional[str] = None,
               min_chars: int = 0) -> ProviderSpec:
    """min_chars defaults to 0 because most self-test checks exercise TRANSPORT
    (retries, timeouts, parallelism, extraction) with a deliberately tiny canned
    answer. The seat-substance checks (S18) opt into the real floor explicitly.

    `model` appends a real `--model` flag (the stub ignores it via parse_known_args)
    AND sets spec.model, because the fallback path swaps the value behind that flag and
    refuses to act when it is absent."""
    argv = [sys.executable, str(STUB), "--mode", mode, "--as", as_]
    if model is not None:
        argv += ["--model", model]
    if sleep:
        argv += ["--sleep", str(sleep)]
    if counter is not None:
        argv += ["--counter-file", str(counter)]
    if answer is not None:
        argv += ["--answer", answer]
    if extract is None:
        extract = extract_claude_json if as_ == "claude" else extract_raw
    return ProviderSpec(name, argv, None, extract, model=model, sentinel=sentinel,
                        min_chars=min_chars)


def self_test() -> int:
    root = Path(tempfile.mkdtemp(prefix="llm-council-selftest-"))
    results: list[tuple[str, bool, str]] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        results.append((label, bool(cond), detail))

    def wd(name: str) -> Path:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    # S1 — all valid, single attempt each.
    m = run_council([_stub_spec("claude", "ok", as_="claude"),
                     _stub_spec("codex", "ok"), _stub_spec("agy", "ok")],
                    retries=2, timeout=5, backoff=0.1, workdir=wd("all_ok"),
                    prompt="hi")
    check("all-ok: 3/3 valid", m["summary"]["valid"] == 3)
    check("all-ok: not degraded", m["summary"]["degraded"] is False)
    check("all-ok: single attempt each",
          all(p["attempts"] == 1 for p in m["providers"]))
    check("all-ok: claude result extracted",
          "Paris" in next(p for p in m["providers"] if p["name"] == "claude")["result_text"])

    # S2 — one empty: invalid, retried to exhaustion, degraded.
    m = run_council([_stub_spec("claude", "ok", as_="claude"),
                     _stub_spec("codex", "empty"), _stub_spec("agy", "ok")],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("one_empty"),
                    prompt="hi")
    cx = next(p for p in m["providers"] if p["name"] == "codex")
    check("empty: codex invalid reason=empty", not cx["valid"] and cx["reason"] == "empty")
    check("empty: codex retried (3 attempts)", cx["attempts"] == 3)
    check("empty: degraded, 2/3 valid", m["summary"]["valid"] == 2 and m["summary"]["degraded"])

    # S3 — one nonzero exit.
    m = run_council([_stub_spec("claude", "ok", as_="claude"),
                     _stub_spec("codex", "nonzero"), _stub_spec("agy", "ok")],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("one_nonzero"),
                    prompt="hi")
    cx = next(p for p in m["providers"] if p["name"] == "codex")
    check("nonzero: reason=nonzero_exit", cx["reason"] == "nonzero_exit" and cx["attempts"] == 3)

    # S4 — timeout: process killed, runtime bounded (~timeout, not timeout×attempts).
    t0 = time.monotonic()
    m = run_council([_stub_spec("claude", "ok", as_="claude"),
                     _stub_spec("codex", "ok"), _stub_spec("agy", "timeout")],
                    retries=1, timeout=1, backoff=0.05, workdir=wd("one_timeout"),
                    prompt="hi")
    wall = time.monotonic() - t0
    ag = next(p for p in m["providers"] if p["name"] == "agy")
    check("timeout: reason=timeout", ag["reason"] == "timeout")
    check("timeout: retried (2 attempts)", ag["attempts"] == 2)
    check("timeout: each attempt bounded ≈1s",
          all(a["duration_sec"] < 1.8 for a in ag["attempt_log"]))
    check("timeout: total wall bounded (<4s)", wall < 4.0, f"wall={wall:.2f}s")
    check("timeout: others still valid", m["summary"]["valid"] == 2)

    # S5 — error sentinel.
    m = run_council([_stub_spec("codex", "error-sentinel")],
                    retries=0, timeout=5, backoff=0.05, workdir=wd("sentinel"),
                    prompt="hi")
    check("sentinel: reason=error_sentinel", m["providers"][0]["reason"] == "error_sentinel")

    # S6 — flaky:2 with retries=2 recovers on attempt 3.
    counter = wd("flaky") / "counter.txt"
    m = run_council([_stub_spec("agy", "flaky:2", counter=counter)],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("flaky"),
                    prompt="hi")
    ag = m["providers"][0]
    # S6a — A SPAWN THE KERNEL REFUSES IS A DEAD SEAT, NOT A TRACEBACK. Measured on this
    # machine: a 200_000-byte argv element raises OSError E2BIG, 120_000 does not. Before
    # this was caught, the error left run_council entirely and crashed the caller.
    big = _stub_spec("claude", "ok")
    big = replace(big, argv=[*big.argv, "--pad", "x" * 200_000])
    m = run_council([big], retries=1, timeout=5, backoff=0.05, workdir=wd("e2big"),
                    prompt="hi")
    sp = m["providers"][0]
    check("an oversized argv is a failed seat, not an exception",
          sp["valid"] is False and sp["reason"] == "spawn_failed")
    check("the spawn failure names its cause",
          "Argument list too long" in Path(sp["raw_stderr_file"]).read_text())
    check("and it is not retried, because the kernel refuses it identically",
          sp["attempts"] == 1)

    # S6b — MODEL FALLBACK: a quota wall swaps the model for the retry, and says so.
    fbc = wd("fallback") / "counter.txt"
    m = run_council([_stub_spec("claude", "quota:1", counter=fbc, model="claude-fable-5")],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("fallback"), prompt="hi")
    fb = m["providers"][0]
    check("fallback: the seat recovers instead of burning retries on the wall", fb["valid"])
    check("fallback: recorded from/to/reason",
          (fb["model_fallback"] or {}).get("to") == "claude-opus-5"
          and fb["model_fallback"]["from"] == "claude-fable-5"
          and fb["model_fallback"]["reason"] == "auth_or_quota")
    check("fallback: the reported model is the one that ANSWERED",
          fb["model"] == "claude-opus-5")
    check("fallback: the header discloses the swap",
          "Model fallback" in m["summary"]["header"]
          and "claude-fable-5→claude-opus-5" in m["summary"]["header"])
    # NEGATIVE: a timeout is not model-attributable, so swapping there would mask a
    # window that needs resizing behind a silent panel change.
    m = run_council([_stub_spec("claude", "timeout", model="claude-fable-5")],
                    retries=1, timeout=1, backoff=0.05, workdir=wd("nofallback"),
                    prompt="hi")
    nf = m["providers"][0]
    check("no fallback on a timeout", nf.get("model_fallback") is None)
    check("no fallback leaves the header clean",
          "Model fallback" not in m["summary"]["header"])
    # swap_model is surgical: it repoints the value behind the provider's OWN flag, and
    # refuses (None) when there is no flag to swap rather than appending one.
    check("swap_model repoints claude --model",
          swap_model(["claude", "--model", "a", "-p", "q"], "claude", "a", "b")
          == ["claude", "--model", "b", "-p", "q"])
    check("swap_model uses codex's -m, not --model",
          swap_model(["codex", "exec", "-m", "a"], "codex", "a", "b")
          == ["codex", "exec", "-m", "b"])
    check("swap_model refuses when no model flag is present",
          swap_model(["claude", "-p", "q"], "claude", None, "b") is None)
    check("swap_model refuses a dangling flag at argv end",
          swap_model(["claude", "--model"], "claude", "a", "b") is None)

    check("flaky: recovers to valid", ag["valid"])
    check("flaky: took 3 attempts", ag["attempts"] == 3)

    # S6b — a RETRIED seat's accounting must cover every attempt. Guards the WIRING, not
    # just sum_usage: reverting the record to the survivor's usage alone must fail here.
    m = run_council([_stub_spec("claude", "ok", as_="claude", answer="short",
                                min_chars=400)],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("usagesum"),
                    prompt="hi")
    _u = m["providers"][0].get("usage") or {}
    check("usage: a retried seat's record sums ALL attempts, not just the last",
          m["providers"][0]["attempts"] == 3 and _u.get("cost_usd") == 0.75
          and _u.get("input") == 30)
    check("usage: and it discloses how many attempts were measured",
          _u.get("attempts_measured") == 3)

    # S7 — not installed: fast-fail, no retry.
    m = run_council([ProviderSpec("agy", ["/nonexistent/xyz-not-a-binary"], None, extract_raw)],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("not_installed"),
                    prompt="hi")
    ag = m["providers"][0]
    check("not_installed: status set", ag["status"] == "not_installed")
    check("not_installed: not retried (1 attempt)", ag["attempts"] == 1)

    # S8 — parallelism: 3 × ~1.5s sleeps finish well under serial 4.5s.
    t0 = time.monotonic()
    run_council([_stub_spec("claude", "ok", as_="claude", sleep=1.5),
                 _stub_spec("codex", "ok", sleep=1.5),
                 _stub_spec("agy", "ok", sleep=1.5)],
                retries=0, timeout=5, backoff=0.05, workdir=wd("parallel"),
                prompt="hi")
    wall = time.monotonic() - t0
    check("parallel: ran concurrently (<3s)", wall < 3.0, f"wall={wall:.2f}s")

    # S9 — manifest shape + provenance + on-disk artifacts.
    m = run_council([_stub_spec("claude", "ok", as_="claude")],
                    retries=0, timeout=5, backoff=0.05, workdir=wd("shape"),
                    prompt="hello world")
    required = {"schema", "prompt_sha256", "workdir", "started_at", "finished_at",
                "config", "summary", "providers"}
    check("manifest: required keys present", required <= set(m))
    check("manifest: prompt_sha256 correct",
          m["prompt_sha256"] == hashlib.sha256(b"hello world").hexdigest())
    p0 = m["providers"][0]
    check("manifest: result/raw files exist on disk",
          Path(p0["result_file"]).exists() and Path(p0["raw_stdout_file"]).exists())
    check("manifest: manifest.json written", (Path(m["workdir"]) / "manifest.json").exists())

    # S10 — claude parse failure on non-JSON stdout, retried.
    m = run_council([_stub_spec("claude", "garbage-json", as_="raw",
                                 extract=extract_claude_json)],
                    retries=1, timeout=5, backoff=0.05, workdir=wd("parse_fail"),
                    prompt="hi")
    check("parse: reason=parse_failure", m["providers"][0]["reason"] == "parse_failure")
    check("parse: retried (2 attempts)", m["providers"][0]["attempts"] == 2)

    # S11 — silent quota wall (agy's 429): empty stdout, but log says RESOURCE_EXHAUSTED.
    # The log scan must reclassify it auth_or_quota and NOT retry it.
    qdir = wd("quota")
    qlog = qdir / "agy.cli.log"
    qspec = ProviderSpec("agy", [sys.executable, str(STUB), "--mode", "quota-log",
                                 "--log-file", str(qlog)], None, extract_raw,
                         log_file=str(qlog))
    m = run_council([qspec], retries=2, timeout=5, backoff=0.05, workdir=qdir, prompt="hi")
    ag = m["providers"][0]
    check("quota: reason=auth_or_quota (from log)", ag["reason"] == "auth_or_quota")
    # RETRIED now: the reason is scan-derived, and this repo's own docs classify as
    # auth_or_quota, so a phantom must not cost the seat (verified 2026-07-28).
    check("quota: IS retried — scan-derived reasons must not cost a seat",
          ag["attempts"] > 1)

    # S12 — regression (found by a real eval): a valid exit-0 answer whose stderr is
    # full of session noise containing sentinel phrases (codex echoing files it read)
    # must stay VALID. Sentinels refine failures; they never veto a real answer.
    m = run_council([_stub_spec("codex", "noisy-ok")],
                    retries=2, timeout=5, backoff=0.05, workdir=wd("noisy"), prompt="hi")
    cx = m["providers"][0]
    check("noisy-ok: valid despite sentinel-laden stderr", cx["valid"] and cx["reason"] == "ok")
    check("noisy-ok: single attempt (not failed+retried)", cx["attempts"] == 1)

    # S13 — classify_sentinel directly covers the real-world strings folded into the tables.
    check("sentinel: unauthenticated → persistent", classify_sentinel("UNAUTHENTICATED") == "auth_or_quota")
    check("sentinel: heap OOM → transient", classify_sentinel("heap out of memory") == "error_sentinel")
    check("sentinel: clean text → None", classify_sentinel("here is your answer") is None)
    # The verbatim codex 0.143.0 rejection (2026-07-25) must CLASSIFY as auth_or_quota.
    # It still consumes retries (scan-derived, so it can be a phantom); retrying a version
    # gate only burns the budget. The narrow phrasing is load-bearing — the two cases below
    # pin that a member merely DISCUSSING CLI versions keeps its retry.
    check("sentinel: codex version gate → persistent",
          classify_sentinel("ERROR: {\"message\":\"The 'gpt-5.6-sol' model requires a "
                            "newer version of Codex. Please upgrade to the latest app or "
                            "CLI and try again.\"}") == "auth_or_quota")
    check("sentinel: prose about needing a newer version stays retryable",
          classify_sentinel("this feature requires a newer version of the agy CLI; "
                            "the request also hit a 503") == "error_sentinel")

    # S14 — make_readonly argv contracts (plan-file suppression is mechanical + prompt).
    cl = build_real_spec("claude", "q", 30, {"claude": {"model": "m", "thinking": "high"}}, wd("ro"))
    make_readonly(cl)
    check("readonly: claude bypass flag swapped out", "--dangerously-skip-permissions" not in cl.argv)
    check("readonly: claude gets plan mode", "--permission-mode" in cl.argv and "plan" in cl.argv)
    check("readonly: claude denies ExitPlanMode", "--disallowedTools" in cl.argv and "ExitPlanMode" in cl.argv)
    check("readonly: claude instructed to answer inline (no plan files)",
          any("plan file" in str(a) for a in cl.argv))
    cx14 = build_real_spec("codex", "q", 30, {}, wd("ro"))
    make_readonly(cx14)
    check("readonly: codex sandboxed read-only", "--sandbox" in cx14.argv and "read-only" in cx14.argv)
    ag14 = build_real_spec("agy", "q", 30, {}, wd("ro"))
    make_readonly(ag14)
    check("readonly: agy gets plan mode (the write barrier)",
          "--mode" in ag14.argv and "plan" in ag14.argv)
    # REGRESSION: plan mode used to REPLACE the auto-approve flag, which left agy
    # unable to approve its own reads headlessly — it denied its ReadFile at
    # tool_confirmation_manager.go:183 and answered from an empty context.
    check("readonly: agy KEEPS auto-approve alongside plan mode (can read its input)",
          "--dangerously-skip-permissions" in ag14.argv)
    # Index lookups are guarded: a missing flag must report FAIL, not raise and abort
    # the whole suite (a crashing check hides every check after it).
    def _before_prompt(argv: list, *flags: str) -> bool:
        return ("-p" in argv
                and all(f in argv and argv.index(f) < argv.index("-p") for f in flags))
    check("readonly: agy flags still precede the positional prompt (Go flag parsing)",
          _before_prompt(ag14.argv, "plan", "--dangerously-skip-permissions"))
    ag14m = build_real_spec("agy", "q", 30,
                            {"agy": {"model": "Gemini 3.5 Flash (High)", "thinking": "high"}},
                            wd("ro"))
    # print-timeout must track the ENGINE timeout, not a fixed ceiling. The old 120s cap
    # killed every agy attempt in a real sweep (measured 2026-07-30: agy needs ~608s on a
    # simple prompt), and an invalid run fails the receipt gate closed rather than just
    # adding noise.
    for _t in (300, 900, 1200):
        _sp = build_real_spec("agy", "p", _t, {}, "/tmp")
        _pt = _sp.argv[_sp.argv.index("--print-timeout") + 1]
        check(f"agy: print-timeout tracks the engine timeout ({_t}s -> {_pt})",
              _pt == f"{_t - 5}s")
    check("agy: print-timeout is never below a usable floor",
          build_real_spec("agy", "p", 3, {}, "/tmp").argv[
              build_real_spec("agy", "p", 3, {}, "/tmp").argv.index("--print-timeout") + 1] == "5s")

    check("agy: per-run --model passed and precedes the prompt (1.1.1)",
          "--model" in ag14m.argv and "Gemini 3.5 Flash (High)" in ag14m.argv
          and ag14m.argv.index("--model") < ag14m.argv.index("-p")
          and ag14m.model == "Gemini 3.5 Flash (High)")
    ag14x = build_real_spec("agy", "q", 30,
                            {"agy": {"model": "Gemini 3.5 Flash (Medium)", "thinking": "high"}},
                            wd("ro"))
    check("agy: cross-tier override records the LABEL's tier, not the mode's",
          ag14x.thinking == "medium")

    # S15 — read-only posture line (agy's defense-in-depth atop plan mode): prepended intact,
    # original prompt preserved, and identical for every member by construction.
    aug = apply_readonly_posture("original question")
    check("posture: line prepended", aug.startswith(READONLY_POSTURE))
    check("posture: original prompt preserved", aug.endswith("original question"))
    check("posture: main() honors --allow-writes wiring",
          parse_args(["--prompt", "x", "--allow-writes"]).read_only is False
          and parse_args(["--prompt", "x"]).read_only is True)
    ag15 = build_real_spec("agy", apply_readonly_posture("q"), 30, {}, wd("ro"))
    check("posture: reaches the agy argv (defense-in-depth layer)",
          any(READONLY_POSTURE in str(a) for a in ag15.argv))

    # S15b — member skills note: prepended intact, question preserved, composes with the
    # posture line (main() order: note first, then posture wraps it), bars llm-council by
    # name, and must NOT be baked into build_real_spec (the eval harness reuses it).
    mem = apply_member_note("original question")
    check("member-note: line prepended", mem.startswith(MEMBER_SKILLS_NOTE))
    check("member-note: original prompt preserved", mem.endswith("original question"))
    check("member-note: bars llm-council by name", "llm-council" in MEMBER_SKILLS_NOTE)
    both = apply_readonly_posture(apply_member_note("q"))
    check("member-note: composes with posture (posture outermost)",
          both.startswith(READONLY_POSTURE) and MEMBER_SKILLS_NOTE in both and both.endswith("q"))
    bare = build_real_spec("claude", "q", 30, {}, wd("memnote"))
    check("member-note: NOT injected by build_real_spec (harness reuses it)",
          all(MEMBER_SKILLS_NOTE not in str(a) for a in bare.argv))

    # S16 — agy worktree isolation: cwd redirected to a throwaway copy that mirrors the
    # working tree (incl. uncommitted tracked changes); cleanup removes it; non-repo no-op.
    repo16 = wd("wt_repo")
    gitc = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo16)]
    subprocess.run(gitc[:5] + ["-C", str(repo16), "init", "-q"], capture_output=True)
    (repo16 / "f.txt").write_text("committed")
    (repo16 / "sub").mkdir()
    (repo16 / "sub" / "g.txt").write_text("sub-file")
    (repo16 / "b.bin").write_bytes(bytes(range(256)))
    subprocess.run(gitc + ["add", "-A"], capture_output=True)
    subprocess.run(gitc + ["commit", "-q", "-m", "c1"], capture_output=True)
    (repo16 / "f.txt").write_text("working-tree")
    (repo16 / "b.bin").write_bytes(bytes(reversed(range(256))))  # dirty BINARY change
    # CRLF + non-UTF-8 (latin-1) content: a text-mode pipe would newline-translate or
    # raise UnicodeDecodeError — the mirror must stay byte-exact for such repos too.
    (repo16 / "crlf.txt").write_bytes(b"caf\xe9 line one\r\nline two\r\n")
    subprocess.run(gitc + ["add", "crlf.txt"], capture_output=True)
    subprocess.run(gitc + ["commit", "-q", "-m", "c2"], capture_output=True)
    (repo16 / "crlf.txt").write_bytes(b"caf\xe9 CHANGED\r\nline two\r\n")
    ag16 = build_real_spec("agy", "q", 30, {}, wd("wt_wd"))
    handle = isolate_agy_worktree(ag16, wd("wt_wd"), repo_dir=str(repo16))
    check("worktree: cwd redirected into workdir",
          handle is not None and ag16.cwd == handle[1]
          and str(wd("wt_wd")) in (ag16.cwd or ""))
    check("worktree: mirrors uncommitted working tree",
          handle is not None and (Path(handle[1]) / "f.txt").read_text() == "working-tree")
    check("worktree: mirrors dirty BINARY files (--binary diff)",
          handle is not None
          and (Path(handle[1]) / "b.bin").read_bytes() == bytes(reversed(range(256))))
    check("worktree: byte-exact for CRLF + non-UTF-8 content (bytes-mode pipe)",
          handle is not None
          and (Path(handle[1]) / "crlf.txt").read_bytes() == b"caf\xe9 CHANGED\r\nline two\r\n")
    if handle:
        (Path(handle[1]) / "escaped.txt").write_text("dirty")  # simulate a misbehaving agy
    remove_agy_worktree(handle)
    check("worktree: removed even when dirty",
          handle is not None and not Path(handle[1]).exists())
    # Invoked from a subdirectory: agy's cwd must be the SAME subdir inside the worktree,
    # so relative paths resolve identically for every member.
    ag16s = build_real_spec("agy", "q", 30, {}, wd("wt_wd_sub"))
    hs = isolate_agy_worktree(ag16s, wd("wt_wd_sub"), repo_dir=str(repo16 / "sub"))
    check("worktree: caller's subdir position preserved",
          hs is not None and ag16s.cwd == str(Path(hs[1]) / "sub")
          and Path(ag16s.cwd).is_dir())
    remove_agy_worktree(hs)
    ag16b = build_real_spec("agy", "q", 30, {}, wd("wt_wd2"))
    check("worktree: no-op outside a git repo",
          isolate_agy_worktree(ag16b, wd("wt_wd2"), repo_dir=str(wd("wt_norepo"))) is None
          and ag16b.cwd is None)

    # D7 — codex NDJSON. The design's whole claim is graceful degradation, so test the
    # DRIFT shapes, not just the happy path.
    _ok = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed",
                    "item": {"id": "i0", "type": "agent_message", "text": "pong"}}),
        json.dumps({"type": "turn.completed", "usage": {"output_tokens": 5}})])
    check("codex-json: agent_message text is extracted", extract_codex_json(_ok) == ("pong", None))
    _failed = _ok.replace(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 5}}),
                          json.dumps({"type": "turn.failed",
                                      "error": {"message": "quota reached"}}))
    check("codex-json: turn.failed is a structured error",
          extract_codex_json(_failed)[1] == "codex_structured_error"
          and "quota" in extract_codex_json(_failed)[0])
    check("codex-json: a top-level error event is structured too",
          extract_codex_json(json.dumps({"type": "error", "message": "boom"}))[1]
          == "codex_structured_error")
    # item-level errors are NOT outcomes: this warning appeared in a run that then completed
    _warn = "\n".join([
        json.dumps({"type": "item.completed",
                    "item": {"type": "error", "message": "Defaulting to fallback metadata"}}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "pong"}}),
        json.dumps({"type": "turn.completed"})])
    check("codex-json: an item-level error is not a turn outcome",
          extract_codex_json(_warn) == ("pong", None))
    check("codex-json: a malformed event line is a parse_failure",
          extract_codex_json('{"type":"turn.started"}\n{not json}')[1] == "parse_failure")
    check("codex-json: no text AND no outcome fails closed",
          extract_codex_json(json.dumps({"type": "turn.started"}))[1] == "parse_failure")
    # ...but a completed turn with an EMPTY answer is not a parse failure — it is an empty
    # answer, and score_seat owns that judgement. Widening the guard to "no text" would
    # relabel every terse-but-real seat.
    check("codex-json: a completed turn with empty text is NOT a parse_failure",
          extract_codex_json(json.dumps({"type": "turn.completed"})) == ("", None))
    # DRIFT: a renamed terminal event must degrade to today's behaviour, never kill the seat
    _renamed = "\n".join([
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "a real answer"}}),
        json.dumps({"type": "turn.finished_v2"})])
    check("codex-json: a RENAMED terminal event degrades to score_seat, not a dead seat",
          extract_codex_json(_renamed) == ("a real answer", None))
    check("codex-json: codex_error is NOT terminal (unrecognised => retry)",
          "codex_error" not in STRUCTURED_TERMINAL_REASONS)
    _cspec = build_real_spec("codex", "hi", 60, {"model": None, "thinking": "high"}, Path("/tmp"))
    check("codex-json: --json is on the argv", "--json" in _cspec.argv)
    # claude's is_error field is the provider speaking, so it must carry provenance too —
    # it was being merged with stderr and flattened to structured=False.
    _clspec = ProviderSpec("claude", ["x"], None, extract_claude_json, None, None)
    _cl = evaluate(0, json.dumps({"is_error": True, "result": "quota reached"}), "", _clspec)
    check("claude-json: an is_error result carries STRUCTURED provenance",
          _cl[1] == "auth_or_quota" and _cl[3] is True)
    check("claude-json: stderr cannot confer structured terminality",
          evaluate(1, "", "quota reached", ProviderSpec("codex", ["x"], None, extract_raw,
                                                        None, None))[3] is False)
    make_readonly(_cspec)
    check("codex-json: --json survives the read-only rewrite", "--json" in _cspec.argv)
    # Every reason the engine can EMIT must carry a hint. The three structured catch-alls
    # shipped with hint=None, so an operator got a bare token and skill-tuneup's pointer at
    # "llm-council has the per-reason semantics" dangled. `timeout` shipped the same way and
    # is the one an operator is most likely to be able to act on.
    for _r in ("claude_error", "codex_error", "agy_error", "timeout"):
        check(f"hints: {_r} carries an actionable hint", bool(REASON_HINTS.get(_r)))
    # The provenance flag must reach the MANIFEST, not just the retry decision — consumers
    # are told to read it, and it was internal-only.
    with tempfile.TemporaryDirectory() as _td:
        _ms = run_council([_stub_spec("claude", "ok", as_="claude")], retries=0, timeout=10,
                          backoff=0.05, workdir=Path(_td) / "m", prompt="hi")
        check("manifest: every provider record carries `structured`",
              all("structured" in p for p in _ms["providers"]))
        check("manifest: a scanned/ok reason reports structured=False",
              _ms["providers"][0]["structured"] is False)
    # E1/E2: the unit tests above exercise the parser directly, which leaves the WIRING
    # unasserted — reverting the seat to extract_raw, or deleting evaluate's branch, both
    # passed the whole suite. Assert the seat actually uses it, and that evaluate routes a
    # codex structured error WITH provenance, end to end.
    check("codex-json: the codex seat is wired to extract_codex_json",
          _cspec.extract is extract_codex_json)
    _cx_fail = json.dumps({"type": "turn.failed",
                           "error": {"message": "RESOURCE_EXHAUSTED: quota reached"}})
    _cv, _cr, _ct, _cs = evaluate(1, _cx_fail, "", _cspec)
    check("codex-json: evaluate routes a structured codex error with provenance",
          _cv is False and _cr == "auth_or_quota" and _cs is True)
    _cx_unknown = json.dumps({"type": "turn.failed", "error": {"message": "something odd"}})
    check("codex-json: an unrecognised codex error is structured but NOT terminal",
          evaluate(1, _cx_unknown, "", _cspec)[1] == "codex_error"
          and "codex_error" not in STRUCTURED_TERMINAL_REASONS)
    check("codex-json: non-JSON stdout in --json mode is a parse_failure, not an answer",
          extract_codex_json("plain crash text")[1] == "parse_failure")

    # D6 — agy structured errors (1.1.8). Provenance, not the phrase, decides terminality.
    _ok_json = json.dumps({"conversation_id": "x", "status": "SUCCESS",
                           "response": "pong\n", "usage": {"total_tokens": 5}})
    _err_json = json.dumps({"conversation_id": "", "status": "ERROR", "response": "",
                            "error": "RESOURCE_EXHAUSTED: Individual quota reached"})
    check("agy-json: SUCCESS yields the response text",
          extract_agy_json(_ok_json) == ("pong\n", None))
    check("agy-json: ERROR is flagged structured, carrying the error field",
          extract_agy_json(_err_json)[1] == "agy_structured_error"
          and "quota reached" in extract_agy_json(_err_json)[0].lower())
    check("agy-json: malformed output is a parse_failure, not a silent empty answer",
          extract_agy_json("not json at all")[1] == "parse_failure")

    # A RAW control char inside "response" must not cost the seat its answer. Python's
    # strict parser rejects it; agy has been observed emitting one. The escaped form must
    # keep parsing identically — that is what makes strict=False free rather than lax.
    _raw_nl = '{"status":"SUCCESS","response":"line one\nline two"}'      # literal 0x0A
    _esc_nl = '{"status":"SUCCESS","response":"line one\\nline two"}'    # proper escape
    check("agy-json: a RAW newline inside response still yields the answer",
          extract_agy_json(_raw_nl) == ("line one\nline two", None))
    check("agy-json: the escaped form parses to the same thing",
          extract_agy_json(_esc_nl) == extract_agy_json(_raw_nl))
    check("agy-json: a raw control char survives the stray-log-line fallback too",
          extract_agy_json("INFO up\n" + _raw_nl + "\nINFO done")[0] == "line one\nline two")

    # --- token accounting: read from each CLI's own envelope, never invented ---
    check("usage: agy envelope fields are normalized",
          extract_usage("agy", json.dumps({
              "status": "SUCCESS", "response": "x",
              "usage": {"input_tokens": 11, "output_tokens": 22, "thinking_tokens": 3,
                        "cache_read_tokens": 4, "total_tokens": 40}}))
          == {"input": 11, "output": 22, "thinking": 3, "cache_read": 4, "total": 40})
    check("usage: codex reads turn.completed, and the LAST one wins",
          extract_usage("codex", "\n".join([
              json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
              json.dumps({"type": "item.completed", "usage": {"input_tokens": 999}}),
              json.dumps({"type": "turn.completed",
                          "usage": {"input_tokens": 7, "cached_input_tokens": 5,
                                    "cache_write_input_tokens": 2, "output_tokens": 9}})]))
          == {"input": 7, "output": 9, "cache_read": 5, "cache_write": 2})
    check("usage: codex ignores item.completed as an accounting source",
          extract_usage("codex", json.dumps(
              {"type": "item.completed", "usage": {"input_tokens": 999}})) is None)
    check("usage: claude maps its cache_* field names and keeps the CLI's own cost",
          extract_usage("claude", json.dumps({
              "usage": {"input_tokens": 2, "output_tokens": 4,
                        "cache_creation_input_tokens": 17202,
                        "cache_read_input_tokens": 15273},
              "total_cost_usd": 0.0123}))
          == {"input": 2, "output": 4, "cache_write": 17202,
              "cache_read": 15273, "cost_usd": 0.0123})
    check("usage: a seat that reports nothing usable yields None, never a zero-filled guess",
          extract_usage("claude", json.dumps({"result": "hi"})) is None)
    check("usage: a JSON `true` cost is not a cost (bool is an int in Python)",
          "cost_usd" not in (extract_usage("claude", json.dumps(
              {"usage": {"input_tokens": 1}, "total_cost_usd": True})) or {}))
    check("usage: a JSON `true` is not a token count",
          extract_usage("agy", json.dumps({"usage": {"input_tokens": True}})) is None)
    check("usage: garbage in, None out — never a partial guess",
          extract_usage("agy", "not json") is None
          and extract_usage("codex", "not json") is None
          and extract_usage("agy", "") is None)
    _agy_spec = ProviderSpec("agy", ["x"], None, extract_agy_json, None, None)
    # agy soft-deny, read from its STRUCTURED error field (agy >= 1.1.3).
    _deny = json.dumps({"status": "ERROR",
                        "error": "Tool ReadFile was auto-denied: add it to permissions.allow"})
    check("agy: a structured soft-deny classifies as tool_permission, not a generic error",
          evaluate(1, _deny, "", _agy_spec)[1] == "tool_permission")
    check("agy: that classification is marked STRUCTURED (it came from agy's own field)",
          evaluate(1, _deny, "", _agy_spec)[3] is True)
    check("agy: tool_permission stays RETRYABLE — reproduction is the signal",
          "tool_permission" not in STRUCTURED_TERMINAL_REASONS
          and "tool_permission" not in NONRETRYABLE_REASONS)
    # The phrases must NOT leak into the stderr-scanned path: `permissions.allow` is a
    # config key and the skip-permissions flag is in our own argv.
    check("agy: soft-deny phrases are structured-only, never merged-stderr sentinels",
          not any(p in [x.lower() for x in TOOL_PERMISSION_SENTINELS]
                  for p in AGY_STRUCTURED_TOOL_PERMISSION))
    check("agy: a seat that merely PRINTS the phrase on stderr is not classified by it",
          classify_sentinel("I read a config containing permissions.allow") is None)

    # `status` decides independently of the exit code. (An earlier comment here claimed agy
    # exits 0 on a hard error "verified"; that was measured off a PIPELINE and is retracted
    # — both CLIs exit 1. Reading status is defence in depth, not a fix for a lying rc.)
    _v, _r, _txt, _structured = evaluate(0, _err_json, "", _agy_spec)
    check("agy-json: status is the discriminator, independent of exit code",
          _v is False and _r == "auth_or_quota" and _structured is True)
    check("agy-json: a structured reason is eligible to be terminal",
          _r in STRUCTURED_TERMINAL_REASONS)
    # ...and the SAME phrase arriving by stderr scan must stay RETRYABLE. This is the
    # whole design: a seat that merely READ a file naming the phrase must not lose its seat.
    _raw_spec = ProviderSpec("codex", ["x"], None, extract_raw, None, None)
    _v2, _r2, _t2, _structured2 = evaluate(1, "", "RESOURCE_EXHAUSTED: Individual quota reached", _raw_spec)
    check("agy-json: the same phrase via STDERR SCAN is not structured",
          _r2 == "auth_or_quota" and _structured2 is False)
    check("agy-json: provenance decides — scanned auth stays retryable",
          "auth_or_quota" not in NONRETRYABLE_REASONS)
    check("agy-json: a non-SUCCESS status fails CLOSED, not through as a valid seat",
          all(extract_agy_json(json.dumps({"status": s, "response": "ok"}))[1]
              == "agy_structured_error" for s in ("CANCELLED", "TIMEOUT", "WEIRD")))
    # The shape a schema change actually produces: no status at all, plus a long answer
    # that would sail through score_seat. `is not None and != SUCCESS` let this through.
    check("agy-json: a MISSING or null status fails closed too",
          extract_agy_json(json.dumps({"response": "x" * 500}))[1] == "agy_structured_error"
          and extract_agy_json(json.dumps({"status": None, "response": "x" * 500}))[1]
              == "agy_structured_error")
    check("agy-json: the catch-all agy_error is NOT terminal (unrecognised => retry)",
          "agy_error" not in STRUCTURED_TERMINAL_REASONS)
    # A log-tail reason is SCANNED. If it inherited structured=True it could go terminal —
    # the phantom class, reintroduced. Assert the seat still retries in that shape.
    with tempfile.TemporaryDirectory() as _td:
        # stdout = a STRUCTURED agy error matching no sentinel (agy_error, structured,
        # non-terminal); log tail = a sentinel match. If the overwrite kept structured
        # True, auth_or_quota would go terminal and attempts would stay 1.
        _qlog = Path(_td) / "agy.cli.log"
        _qs = _stub_spec("agy", "agy-structured-plus-log", as_="raw",
                         extract=extract_agy_json)
        _qs.argv = _qs.argv + ["--log-file", str(_qlog)]
        _qs.log_file = str(_qlog)
        _m = run_council([_qs], retries=1, timeout=10, backoff=0.05,
                         workdir=Path(_td) / "wd", prompt="hi")
        _p0 = _m["providers"][0]
        # The provider's OWN error field outranks a scan of its log. The structured
        # catch-all survives (it is not replaced by the scanned auth_or_quota) and, being
        # unrecognised, still retries. Previously the log tail overwrote it, which made the
        # structured path stop being authoritative exactly when agy also logged a match.
        check("agy-json: a log tail does not override a STRUCTURED reason",
              _p0["reason"] == "agy_error")
        check("agy-json: an unrecognised structured reason still retries",
              _p0["attempts"] > 1)

    # §19 — the forge window, and agy's own word for a wall.
    _fw = MODE_TIMEOUT.get("forge", 0)
    check("MODE_TIMEOUT has a forge entry of at least 3600", _fw >= 3600)
    check("deep is unchanged", MODE_TIMEOUT["deep"] == 1200)
    # A re-added cap bites hardest at the LONGEST window, so pin agy's print-timeout at the
    # forge one: it must still be the engine timeout less the margin, and nothing tighter.
    # The lookup is floored rather than indexed — a check that RAISES hides every check
    # after it, so an absent entry must fail on its own line above, not here.
    _fw_probe = max(_fw, 60)
    _fsp = build_real_spec("agy", "p", _fw_probe, {}, "/tmp")
    check("agy: print-timeout tracks the FORGE window too (no cap survived)",
          _fsp.argv[_fsp.argv.index("--print-timeout") + 1] == f"{_fw_probe - 5}s")
    _agy_to = json.dumps({"status": "ERROR",
                          "error": "timeout waiting for response from the model"})
    _tv, _tr, _tt, _ts = evaluate(0, _agy_to, "", _agy_spec)
    check("agy: a structured timeout maps to reason `timeout` with provenance",
          _tv is False and _tr == "timeout" and _ts is True)
    check("agy: a structured timeout is NOT terminal, so council keeps its retries",
          "timeout" not in STRUCTURED_TERMINAL_REASONS)
    check("STRUCTURED_TERMINAL_REASONS still has exactly one member",
          STRUCTURED_TERMINAL_REASONS == {"auth_or_quota"})
    # A quota wall REPORTED as a timeout is a quota wall. classify_sentinel runs first and
    # auth_or_quota IS terminal; ordering the timeout map ahead of it would retry a wall
    # three times.
    _both = json.dumps({"status": "ERROR",
                        "error": "timeout waiting for response: RESOURCE_EXHAUSTED, "
                                 "individual quota reached"})
    check("agy: a wall reported as a timeout is still auth_or_quota",
          evaluate(0, _both, "", _agy_spec)[1] == "auth_or_quota")
    # Structured-only, on the soft-deny list's argument: these are ordinary English, and a
    # seat that merely echoed them onto stderr must not be classified by them.
    check("agy: the timeout phrases are structured-only, never merged-stderr sentinels",
          all(classify_sentinel(p) is None for p in AGY_STRUCTURED_TIMEOUT))

    # D5 — process groups: subprocess.run's timeout reaps only the direct child, so an
    # agent CLI's helpers outlived the council. Prove the GRANDCHILD dies, not just the
    # child — that is the whole point, and the previous suite passed without it.
    if hasattr(os, "killpg"):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as _td:
            _marker = Path(_td) / "grandchild.pid"
            # child writes its grandchild's pid, then both outlive the timeout
            _script = f"sh -c 'sleep 60 & echo $! > {_marker}; sleep 60'"
            try:
                run_member(["sh", "-c", _script], stdin=None, timeout=1.5,
                           env=os.environ.copy(), cwd=None)
                check("pgroup: timed-out member raises TimeoutExpired", False)
            except subprocess.TimeoutExpired:
                check("pgroup: timed-out member raises TimeoutExpired", True)
            time.sleep(0.4)
            _gpid = int(_marker.read_text().strip()) if _marker.exists() else 0
            check("pgroup: the member's GRANDCHILD is reaped, not orphaned",
                  _gpid > 0 and not _pid_alive(_gpid))
            check("pgroup: the group is deregistered after the attempt", not _LIVE_PGIDS)
            # F1 regression: a helper that IGNORES SIGTERM keeps the inherited stdout/
            # stderr write ends open. The first version drained unbounded here, so the
            # timeout stopped bounding anything and the pool worker hung with no manifest.
            # Assert the ESCALATION directly. A pure elapsed-time bound does not: with the
            # SIGKILL gated on the direct child (the exact cycle-1 bug), the ignorer
            # survives, the drain burns its 5s cap and elapsed is ~6s — under any loose
            # threshold. Measured, not assumed.
            _ign_marker = Path(_td) / "ignorer.pid"
            _ign = (f"{sys.executable} -c \"import signal,time,os; "
                    f"signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    f"open('{_ign_marker}','w').write(str(os.getpid())); time.sleep(30)\" "
                    f"& sleep 30")
            _t0 = time.monotonic()
            try:
                run_member(["sh", "-c", _ign], stdin=None, timeout=1.0,
                           env=os.environ.copy(), cwd=None)
                _elapsed = time.monotonic() - _t0
                check("pgroup: SIGTERM-ignoring helper still raises TimeoutExpired", False)
            except subprocess.TimeoutExpired:
                _elapsed = time.monotonic() - _t0
                check("pgroup: SIGTERM-ignoring helper still raises TimeoutExpired", True)
            time.sleep(0.3)
            _ipid = int(_ign_marker.read_text().strip()) if _ign_marker.exists() else 0
            check("pgroup: a SIGTERM-IGNORING helper is still SIGKILLed (F1 regression)",
                  _ipid > 0 and not _pid_alive(_ipid))
            # below timeout + drain cap, so the gated-SIGKILL regression trips this too
            check("pgroup: the post-timeout drain is BOUNDED (F1 regression)", _elapsed < 4)
            # The 5s drain CAP needs its own witness: the checks above kill their helper,
            # so the pipes hit EOF instantly and an unbounded drain would still look fine.
            # A `setsid` grandchild leaves the process group — surviving the killpg — while
            # still holding the inherited stdout/stderr. Only the cap ends this one.
            _t1 = time.monotonic()
            try:
                run_member(["sh", "-c", "setsid sleep 25 & sleep 25"], stdin=None,
                           timeout=1.0, env=os.environ.copy(), cwd=None)
                check("pgroup: an out-of-group pipe holder still times out", False)
            except subprocess.TimeoutExpired:
                check("pgroup: an out-of-group pipe holder still times out", True)
            _escaped = time.monotonic() - _t1
            check("pgroup: the 5s drain CAP bounds an unkillable pipe holder",
                  _escaped < 10)

    # S17 — signal cleanup: a default-disposition SIGTERM skips `finally` (observed leak
    # 2026-07-11); the handler must remove registered worktrees and hard-exit 128+signum.
    # Direct handler test with os._exit stubbed — a subprocess signal test would be
    # disproportionate.
    repo17 = wd("sig_repo")
    g17 = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo17)]
    subprocess.run(g17[:5] + ["-C", str(repo17), "init", "-q"], capture_output=True)
    (repo17 / "f.txt").write_text("x")
    subprocess.run(g17 + ["add", "-A"], capture_output=True)
    subprocess.run(g17 + ["commit", "-q", "-m", "c"], capture_output=True)
    ag17 = build_real_spec("agy", "q", 30, {}, wd("sig_wd"))
    h17 = isolate_agy_worktree(ag17, wd("sig_wd"), repo_dir=str(repo17))
    check("signal: live worktree is registered", h17 is not None and h17 in _LIVE_WORKTREES)
    exit_codes: list = []
    real_exit = os._exit
    os._exit = exit_codes.append  # type: ignore[assignment] — stub; handler never returns in prod
    _STATE["handler_fired"] = False
    _signal_cleanup(signal.SIGTERM, None)
    os._exit = real_exit  # type: ignore[assignment]
    check("signal: handler removed the worktree and deregistered it",
          h17 is not None and h17 not in _LIVE_WORKTREES and not Path(h17[1]).exists())
    check("signal: hard-exits with 128+signum (143)", exit_codes == [143])
    _STATE["handler_fired"] = False  # reset for any later checks

    # S18 — seat validity end-to-end through the REAL engine. Every case below exits 0
    # with non-empty stdout, i.e. every one of them scored `ok` before this existed.
    SENT = "SENTINEL-deadbeef01"
    long_ok = ("A substantive council answer with real reasoning. " * 12
               + f"\n{SENT}\n" + "Further detail and caveats. " * 12)
    long_unread = "A confident answer produced without ever opening the material. " * 20

    m = run_council([_stub_spec("agy", "ok", answer=long_ok, sentinel=SENT,
                                min_chars=MIN_SUBSTANTIVE_CHARS)],
                    retries=0, timeout=10, backoff=0.05, workdir=wd("seat_ok"), prompt="hi")
    check("seat: substantive answer citing the sentinel is ok",
          m["providers"][0]["valid"] and m["providers"][0]["reason"] == "ok")

    m = run_council([_stub_spec("agy", "ok", answer="Yes, that approach is fine.",
                                sentinel=SENT, min_chars=MIN_SUBSTANTIVE_CHARS)],
                    retries=0, timeout=10, backoff=0.05, workdir=wd("seat_short"), prompt="hi")
    ag = m["providers"][0]
    check("seat: one-sentence answer is failed, not ok",
          not ag["valid"] and ag["reason"] == "non_substantive")

    m = run_council([_stub_spec("agy", "ok", answer=long_unread, sentinel=SENT,
                                min_chars=MIN_SUBSTANTIVE_CHARS)],
                    retries=0, timeout=10, backoff=0.05, workdir=wd("seat_unread"), prompt="hi")
    ag = m["providers"][0]
    check("seat: long answer that never cites the sentinel is failed",
          not ag["valid"] and ag["reason"] == "did_not_read_input")

    # ACCEPTANCE: the deliberately-broken seat — the exact agy round-2 shape.
    m = run_council([_stub_spec("claude", "ok", as_="claude", answer=long_ok,
                                sentinel=SENT, min_chars=MIN_SUBSTANTIVE_CHARS),
                     _stub_spec("codex", "ok", answer=long_ok, sentinel=SENT,
                                min_chars=MIN_SUBSTANTIVE_CHARS),
                     _stub_spec("agy", "tool-denied", sentinel=SENT,
                                min_chars=MIN_SUBSTANTIVE_CHARS)],
                    retries=2, timeout=10, backoff=0.05, workdir=wd("seat_denied"),
                    prompt="hi", requested=["claude", "codex", "agy"])
    ag = next(p for p in m["providers"] if p["name"] == "agy")
    check("seat: tool-denied seat is FAILED despite exit 0 + non-empty output",
          not ag["valid"] and ag["status"] == "failed")
    check("seat: tool-denial gets its own cause (not auth_or_quota)",
          ag["reason"] == "tool_permission")
    check("seat: tool_permission carries an actionable hint",
          "auto-approve" in (ag.get("hint") or ""))
    check("seat: tool_permission IS retried — the reason is scan-derived and can be a phantom",
          ag["attempts"] > 1)
    check("seat: panel degrades to 2/3 in the summary",
          m["summary"]["seats_responded"] == 2 and m["summary"]["seats_attempted"] == 3
          and m["summary"]["degraded"])
    hdr = m["summary"]["header"]
    check("seat: header states the TRUE seat count", "2 of 3" in hdr and "3 of 3" not in hdr)
    check("seat: header names the failed seat and its cause",
          "agy" in hdr and "tool_permission" in hdr and "DEGRADED" in hdr)
    check("seat: rendered text opens with that header", _render_text(m).startswith(hdr))

    # S18b — a real answer must not be vetoed by keywords it legitimately discusses,
    # and the all-ok header must not cry degraded.
    quota_talk = ("Treat quota exceeded and permission denied as distinct failures; "
                  "unauthorized is auth, not a rate limit. " * 8) + f"\n{SENT}\n"
    m = run_council([_stub_spec("codex", "ok", answer=quota_talk, sentinel=SENT,
                                min_chars=MIN_SUBSTANTIVE_CHARS)],
                    retries=0, timeout=10, backoff=0.05, workdir=wd("seat_talk"), prompt="hi")
    check("seat: an answer discussing quota/permission text stays valid",
          m["providers"][0]["valid"])
    full = {"summary": {"requested": 3, "valid": 3},
            "providers": [{"name": n, "valid": True, "reason": "ok"}
                          for n in ("claude", "codex", "agy")]}
    check("seat: full panel header says 3 of 3 with no degraded note",
          council_header(full) == "**Council: 3 of 3 seats responded.**")

    # --- usage_tag: silent when unmeasured, never zero-filled ---
    check("usage-sum: a retried seat totals EVERY attempt, not just the survivor",
          sum_usage([{"input": 10, "cost_usd": 1.0}, {"input": 5, "cost_usd": 0.5}])
          == {"input": 15, "cost_usd": 1.5, "attempts_measured": 2})
    check("usage-sum: an unmeasured attempt is skipped, never counted as zero",
          sum_usage([None, {"input": 7}]) == {"input": 7})
    check("usage-sum: nothing measured at all stays None",
          sum_usage([None, None]) is None and sum_usage([]) is None)
    check("usage-sum: a single attempt carries no misleading attempts_measured tag",
          sum_usage([{"input": 3}]) == {"input": 3})
    check("usage-tag: nothing reported renders NOTHING, not '0 tok'",
          usage_tag(None) == "" and usage_tag({}) == "")
    check("usage-tag: prefers the CLI's own total over summing parts",
          "9.0k tok" in usage_tag({"input": 1, "output": 1, "total": 9000}))
    check("usage-tag: falls back to input+output when no total is given",
          "1.5k tok" in usage_tag({"input": 1000, "output": 500}))
    check("usage-tag: shows cost only when the CLI priced the turn itself",
          "$0.1234" in usage_tag({"total": 10, "cost_usd": 0.1234})
          and "$" not in usage_tag({"total": 10}))
    # A council total that quietly omitted unpriced seats would understate the run.
    _mixed = {"summary": {"valid": 2, "requested": 2, "header": "H"},
              "config": {},
              "providers": [
                  {"name": "claude", "valid": True, "reason": "ok", "attempts": 1,
                   "duration_sec": 1.0, "result_file": "/x",
                   "usage": {"total": 10, "cost_usd": 0.5}},
                  {"name": "agy", "valid": True, "reason": "ok", "attempts": 1,
                   "duration_sec": 1.0, "result_file": "/y", "usage": {"total": 99}}]}
    _out = _render_text(_mixed)
    check("usage: the council total says how many seats it actually covers",
          "$0.5000" in _out and "1 of 2 seats" in _out)
    check("usage: an all-unpriced panel prints no cost line at all",
          "cost:" not in _render_text(
              {"summary": {"valid": 1, "requested": 1, "header": "H"}, "config": {},
               "providers": [{"name": "agy", "valid": True, "reason": "ok", "attempts": 1,
                              "duration_sec": 1.0, "result_file": "/y",
                              "usage": {"total": 99}}]}))

    # S18c — sentinel plumbing: default floor is real, main() injects a unique token,
    # and the instruction reaches every seat's argv while preserving the prompt.
    check("seat: ProviderSpec defaults to the real substantive floor",
          ProviderSpec("x", [], None, extract_raw).min_chars == MIN_SUBSTANTIVE_CHARS
          and MIN_SUBSTANTIVE_CHARS > 0)
    check("sentinel: unique per run", make_sentinel() != make_sentinel())
    # smoke()'s correct answer is the single word "pong" — it opts out of the floor by
    # setting min_chars=0. Verify the exemption MECHANISM here; the smoke wiring itself
    # can only be exercised against live binaries (`make smoke-llm-council`, costs tokens).
    check("seat: min_chars=0 exempts a legitimately tiny answer (smoke's 'pong')",
          score_seat("pong", None, 0)["status"] == "ok"
          and score_seat("pong", None)["cause"] == "non_substantive")
    aug = apply_sentinel("original question", SENT)
    check("sentinel: instruction prepended, prompt preserved",
          SENT in aug and "verbatim" in aug.lower() and aug.endswith("original question"))
    # A seat reviewing THIS repo echoes our own sentinel lists into stderr via rg. The
    # observed failure (2026-07-27) matched fanout.py's own self-test line and returned a
    # NON-RETRYABLE tool_permission, costing the seat its retry for no real reason.
    # F1 regression: members lead their own session, so a terminal Ctrl-C cannot reach
    # them — this handler is their only reaper. It used to be installed only under
    # `if args.read_only:`, leaving --allow-writes and every eval_harness run (which calls
    # run_council directly) with no teardown. Assert the choke point, not the callers.
    check("teardown: run_council installs the signal handler on EVERY path",
          signal.getsignal(signal.SIGTERM) is _signal_cleanup)
    check("selfmatch: tool_permission is RETRYABLE — a phantom must not cost a seat",
          "tool_permission" not in NONRETRYABLE_REASONS)
    check("sentinel: tool-permission text classified ahead of auth_or_quota",
          classify_sentinel("tool_confirmation_manager.go:183: permission denied")
          == "tool_permission")
    check("sentinel: tool_permission is retryable (phantom-safe)",
          "tool_permission" not in NONRETRYABLE_REASONS)

    passed = sum(1 for _, ok, _ in results if ok)
    for label, ok, detail in results:
        line = f"  {'PASS' if ok else 'FAIL'}  {label}"
        if detail and not ok:
            line += f"   [{detail}]"
        print(line)
    print(f"\nself-test: {passed}/{len(results)} checks passed   (artifacts: {root})")
    return 0 if passed == len(results) else 1


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="llm-council fan-out engine")
    src = ap.add_argument_group("prompt source")
    src.add_argument("--prompt", help="prompt text (or use --prompt-file / stdin)")
    src.add_argument("--prompt-file", help="read prompt from a file (preferred)")
    ap.add_argument("--providers", default=",".join(DEFAULT_PROVIDERS),
                    help="comma list (default: claude,codex,agy)")
    ap.add_argument("--mode", choices=list(MODES), default=DEFAULT_MODE,
                    help=f"thinking mode → models+effort from the MODES table (default: {DEFAULT_MODE})")
    ro = ap.add_mutually_exclusive_group()
    ro.add_argument("--read-only", dest="read_only", action="store_true", default=True,
                    help="members read & plan only — they still use their skills but cannot "
                         "modify anything (default; the council's job is advice/synthesis)")
    ro.add_argument("--allow-writes", dest="read_only", action="store_false",
                    help="let members write/execute with full permissions (opt out of read-only)")
    ap.add_argument("--retries", type=int, default=2, help="max retries per provider")
    ap.add_argument("--timeout", type=int, default=None,
                    help=f"per-attempt seconds (default: per-mode — "
                         f"normal {MODE_TIMEOUT['normal']}, deep {MODE_TIMEOUT['deep']})")
    ap.add_argument("--backoff", type=float, default=5.0, help="base backoff seconds")
    ap.add_argument("--workdir", help="output dir (default: a fresh temp dir)")
    ap.add_argument("--model-claude")
    ap.add_argument("--model-codex")
    ap.add_argument("--model-agy")
    ap.add_argument("--out", choices=["json", "text"], default="json")
    ap.add_argument("--provider-cmd-override", action="append", default=[],
                    metavar="NAME=CMD", help="replace a provider's binary (test hook)")
    ap.add_argument("--self-test", action="store_true", help="run engine tests, exit")
    ap.add_argument("--smoke", action="store_true", help="live one-provider check")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return self_test()
    if args.smoke:
        return smoke(args)

    depth = int(os.environ.get("LLM_COUNCIL_DEPTH", "0") or "0")
    if depth >= 1:
        err = {"schema": MANIFEST_SCHEMA, "error": "recursion_blocked",
               "detail": f"LLM_COUNCIL_DEPTH={depth}; refusing to fan out from inside a council run."}
        print(json.dumps(err, indent=2))
        return 2

    prompt = resolve_prompt(args)
    if not prompt.strip():
        print(json.dumps({"error": "empty_prompt",
                          "detail": "provide --prompt, --prompt-file, or pipe via stdin"}))
        return 2

    prompt = apply_member_note(prompt)   # skills-encouraged, council-recursion-barred
    if args.read_only:
        prompt = apply_readonly_posture(prompt)
    # One sentinel for the whole run: identical conditions across seats, and each seat
    # must quote it back to prove it opened the material rather than guessing from the
    # question alone. Applied last so it is the first thing every member reads.
    sentinel = make_sentinel()
    prompt = apply_sentinel(prompt, sentinel)

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="llm-council-"))
    workdir.mkdir(parents=True, exist_ok=True)
    timeout = effective_timeout(args)
    cfg = resolve_mode_config(args)
    specs = [build_real_spec(p, prompt, timeout, cfg, workdir) for p in providers]
    for s in specs:
        s.sentinel = sentinel

    overrides = {}
    for item in args.provider_cmd_override:
        name, _, cmd = item.partition("=")
        overrides[name] = cmd.split()
    by_name = {s.name: s for s in specs}
    for name, tokens in overrides.items():
        if name in by_name and tokens:
            by_name[name].argv = tokens + by_name[name].argv[1:]

    # Read-only is the default council posture: claude/codex are mechanically
    # constrained (claude: plan mode + plan-file suppression; codex: read-only sandbox;
    # agy: --mode plan since 1.1.1) — agy additionally gets a throwaway-worktree cwd so
    # cwd-relative mutations are discarded (defense in depth). --allow-writes opts out. Applied after
    # overrides so a test override's binary is preserved (overrides replace argv[0]
    # only, so the bypass flag make_readonly swaps is always present).
    agy_wt = None
    if args.read_only:
        for s in specs:
            make_readonly(s)
        agy_spec = by_name.get("agy")  # plan-mode-constrained; worktree adds defense in depth
        if agy_spec:
            agy_wt = isolate_agy_worktree(agy_spec, workdir)

    try:
        manifest = run_council(specs, retries=args.retries, timeout=timeout,
                               backoff=args.backoff, workdir=workdir, prompt=prompt,
                               requested=providers, mode=args.mode, read_only=args.read_only)
    finally:
        remove_agy_worktree(agy_wt)
    print(json.dumps(manifest, indent=2) if args.out == "json" else _render_text(manifest))
    return 0 if manifest["summary"]["valid"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
