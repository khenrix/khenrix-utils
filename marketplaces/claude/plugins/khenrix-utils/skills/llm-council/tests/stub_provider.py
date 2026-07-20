#!/usr/bin/env python3
"""Fake provider used only by `fanout.py --self-test`.

It impersonates a headless CLI (claude/codex/agy) so the council engine can be
exercised deterministically — every failure mode without spending a token. The
engine drives this via ProviderSpec argvs; the real provider flags it doesn't
recognise are ignored (parse_known_args), so it can stand in for any of them.

Modes (--mode):
  ok              print a canned answer, exit 0
  empty           print nothing, exit 0            (validity: empty)
  nonzero         write to stderr, exit 1          (validity: nonzero_exit)
  timeout         sleep far past the engine's --timeout so it gets killed
  error-sentinel  emit a sentinel string, exit 1   (validity: error_sentinel)
  flaky:K         fail the first K attempts (counter file), then succeed
  tool-denied     exit 0 with a one-line "my own tool call was denied" answer —
                  the exact shape of the incident that motivated seat scoring:
                  non-empty and clean-exit, but the seat never read its input

--as claude wraps the answer in the same JSON shape `claude -p --output-format
json` emits, so the engine's claude extractor (json.loads(...)["result"]) works.
"""
from __future__ import annotations

import argparse
import json
import sys
import time


def bump_counter(path: str | None) -> int:
    """Return how many times this counter has been hit (1-based), persisting across
    subprocess attempts so `flaky:K` can fail the first K and then recover."""
    if not path:
        return 1
    try:
        n = int((open(path).read().strip() or "0"))
    except (FileNotFoundError, ValueError):
        n = 0
    n += 1
    with open(path, "w") as f:
        f.write(str(n))
    return n


def emit_answer(answer: str, as_: str) -> None:
    if as_ == "claude":
        print(json.dumps({"type": "result", "subtype": "success",
                          "is_error": False, "result": answer}))
    else:
        print(answer)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="ok")
    ap.add_argument("--as", dest="as_", default="raw", choices=["raw", "claude"])
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="delay before responding (parallelism test)")
    ap.add_argument("--timeout-sleep", type=float, default=30.0,
                    help="how long `timeout` mode hangs before the engine kills it")
    ap.add_argument("--counter-file", default=None, help="state file for flaky:K")
    ap.add_argument("--log-file", default=None,
                    help="where quota-log mode writes its silent error")
    ap.add_argument("--answer", default="The capital of France is Paris.")
    # Ignore the real provider flags the engine appends (-p, --output-format, …).
    args, _ = ap.parse_known_args(argv)

    mode = args.mode
    if mode.startswith("flaky:"):
        k = int(mode.split(":", 1)[1])
        if bump_counter(args.counter_file) <= k:
            sys.stderr.write(f"transient failure (attempt ≤ {k})\n")
            return 1
        mode = "ok"

    if args.sleep:
        time.sleep(args.sleep)

    if mode == "ok":
        emit_answer(args.answer, args.as_)
        return 0
    if mode == "noisy-ok":
        # A valid answer on stdout (exit 0), but the whole session — including
        # sentinel phrases echoed from a file the CLI read — goes to stderr, the way
        # codex behaves. The engine must NOT let that noise veto the real answer.
        sys.stderr.write("[session] read SKILL.md … failure table mentions "
                         "'RESOURCE_EXHAUSTED … Individual quota reached' and "
                         "'not logged in' and 'rate limit'.\n")
        emit_answer(args.answer, args.as_)
        return 0
    if mode == "empty":
        return 0
    if mode == "nonzero":
        sys.stderr.write("boom: provider crashed\n")
        return 1
    if mode == "timeout":
        time.sleep(args.timeout_sleep)   # engine SIGKILLs this before it returns
        emit_answer(args.answer, args.as_)
        return 0
    if mode == "error-sentinel":
        sys.stderr.write("Error: rate limit exceeded — try again later\n")
        return 1
    if mode == "garbage-json":
        # exit 0 with non-JSON stdout — exercises the claude parse_failure path.
        print("this is not json at all")
        return 0
    if mode == "tool-denied":
        # agy's observed round-2 failure: headless mode cannot prompt, so the CLI
        # soft-denies its own ReadFile and the model answers anyway. Exit 0, non-empty
        # — under the old "non-empty is valid" rule this scored a pass.
        emit_answer("I was unable to read the document: ReadFile permission denied "
                    "(tool_confirmation_manager.go:183).", args.as_)
        return 0
    if mode == "quota-log":
        # Mimic agy's silent 429: nothing on stdout/stderr, exit 0, but the real
        # reason is only in the log file. Exercises the log-scan + non-retry path.
        if args.log_file:
            with open(args.log_file, "w") as f:
                f.write("E agent executor error: RESOURCE_EXHAUSTED (code 429): "
                        "Individual quota reached. Resets in 160h.\n")
        return 0

    sys.stderr.write(f"unknown stub mode: {mode}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
