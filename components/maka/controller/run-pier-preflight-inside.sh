#!/usr/bin/env bash
set -euo pipefail
test -n "${MAKA_EVIDENCE_DIR:-}" || { echo 'MAKA_EVIDENCE_DIR is required' >&2; exit 64; }
test -n "${MAKA_EVAL_RUNTIME_ROOT:-}" || { echo 'MAKA_EVAL_RUNTIME_ROOT is required' >&2; exit 64; }
cp "$MAKA_EVAL_RUNTIME_ROOT/RUNTIME_SHA256SUMS" "$MAKA_EVIDENCE_DIR/runtime-file-SHA256SUMS"
cp "$MAKA_EVAL_RUNTIME_ROOT/RUNTIME_SYMLINKS" "$MAKA_EVIDENCE_DIR/runtime-symlinks.txt"
mkdir -p "$MAKA_EVIDENCE_DIR/trials"
export MAKA_EVAL_TRIALS_DIR="$MAKA_EVIDENCE_DIR/trials"
export MAKA_EVAL_PIER_TASKS="$MAKA_LAB_ROOT/fixtures/pier-tasks"
node controller/pier-preflight.mjs > "$MAKA_EVIDENCE_DIR/pier-preflight.json"
