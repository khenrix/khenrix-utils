#!/usr/bin/env python3
"""Add the current API judge model after the frozen Eval onboarding overlay."""

from __future__ import annotations

import sys
from pathlib import Path

from apply_maka_hosted_onboarding_compat import atomic_replace, digest, regular_target


OVERLAY_ID = "harbor-judge-sol61-v1"
MODEL_FETCHER_PATH = Path("node_modules/@maka/runtime/dist/model-fetcher.js")
MODEL_METADATA_PATH = Path("node_modules/@maka/core/dist/model-metadata.js")
MODEL_FETCHER_BASE_SHA256 = "427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7"
MODEL_FETCHER_PATCHED_SHA256 = "7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5"
MODEL_METADATA_BASE_SHA256 = "454ae64aa3a2dee4fe93d30f3d547e361b6b30f2c26987c69f0a8ab6981cd2cf"
MODEL_METADATA_PATCHED_SHA256 = "9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7"

DISCOVERY_OLD = b"? [{ id: MAKA_EVAL_PINNED_OPENAI_MODEL }]"
DISCOVERY_NEW = b"? [{ id: MAKA_EVAL_PINNED_OPENAI_MODEL }, { id: 'gpt-6.1-sol' }]"
METADATA_OLD = b"const OPENAI_GPT6_MODEL_OVERRIDES = {\n"
METADATA_NEW = METADATA_OLD + b"""    'gpt-6.1-sol': {
        displayName: 'GPT-6.1 Sol',
        capabilities: { ...REASONING_FUNCTION_CALLING, vision: true },
        contextWindow: 1_050_000,
        maxOutputTokens: 128_000,
        thinkingOptions: OPENAI_GPT6_THINKING_OPTIONS,
    },
"""

PATCHES = (
    (MODEL_FETCHER_PATH, MODEL_FETCHER_BASE_SHA256, MODEL_FETCHER_PATCHED_SHA256,
     DISCOVERY_OLD, DISCOVERY_NEW),
    (MODEL_METADATA_PATH, MODEL_METADATA_BASE_SHA256, MODEL_METADATA_PATCHED_SHA256,
     METADATA_OLD, METADATA_NEW),
)


def apply(package_root: Path) -> tuple[Path, Path]:
    package_root = package_root.resolve(strict=True)
    pending = []
    for relative, base_hash, patched_hash, old, new in PATCHES:
        target = regular_target(package_root, relative, str(relative))
        source = target.read_bytes()
        if digest(source) != base_hash:
            raise RuntimeError(f"pinned {relative} hash does not match {OVERLAY_ID}")
        if source.count(old) != 1:
            raise RuntimeError(f"pinned {relative} lacks the exact judge overlay target")
        patched = source.replace(old, new, 1)
        if digest(patched) != patched_hash:
            raise RuntimeError(f"{OVERLAY_ID} produced unexpected {relative}")
        pending.append((target, patched, patched_hash))
    # Refuse either unpinned input before changing any package bytes.
    for target, patched, patched_hash in pending:
        atomic_replace(target, patched)
        if digest(target.read_bytes()) != patched_hash:
            raise RuntimeError(f"{OVERLAY_ID} did not persist exactly")
    return pending[0][0], pending[1][0]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} PACKAGE_ROOT")
    apply(Path(sys.argv[1]))
