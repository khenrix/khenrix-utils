#!/usr/bin/env python3
"""Apply the hash-pinned hosted OpenAI Eval onboarding compatibility overlay."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


OVERLAY_ID = "eval-openai-onboarding-v2"
MODEL_FETCHER_PATH = Path("node_modules/@maka/runtime/dist/model-fetcher.js")
MODEL_FETCHER_BASE_SHA256 = (
    "044173c4dfcfc40c371038fda6880941533c1a13b54119248f97a496ab4d3aba"
)
MODEL_FETCHER_PATCHED_SHA256 = (
    "427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7"
)
MAKA_SUBJECT_PATH = Path("node_modules/@maka/eval/dist/maka-subject.js")
MAKA_SUBJECT_BASE_SHA256 = (
    "84258ed57890af57d88b917007e52c53ac71a69836c9b72bd7a7d9239f0a320c"
)
MAKA_SUBJECT_PATCHED_SHA256 = (
    "f47c1278ed25434f2a1a012d26ed285eb930e737acbc182cbdcda6b914887775"
)

DISCOVERY_OLD = b'''const PROVIDER_PAGE_TOKEN_MAX_LENGTH = 2_048;
export async function runConnectionModelDiscoveryEffect(connection, apiKey, options) {
    try {
        return {
            ok: true,
            models: normalizeConnectionEffectModels(await fetchProviderModelsStrict(connection, apiKey, options.fetch)),
        };
'''

DISCOVERY_NEW = b'''const PROVIDER_PAGE_TOKEN_MAX_LENGTH = 2_048;
const MAKA_EVAL_PINNED_OPENAI_MODEL = 'gpt-5.6-sol';
const MAKA_EVAL_DECOY_CREDENTIAL = /^maka-decoy-[A-Za-z0-9_-]{8,128}$/u;
export async function runConnectionModelDiscoveryEffect(connection, apiKey, options) {
    try {
        const evalPinnedModel = connection.providerType === 'openai' &&
            connection.slug === 'eval-openai' &&
            effectiveBaseUrl(connection) === 'https://api.openai.com/v1' &&
            MAKA_EVAL_DECOY_CREDENTIAL.test(apiKey)
            ? [{ id: MAKA_EVAL_PINNED_OPENAI_MODEL }]
            : undefined;
        return {
            ok: true,
            models: normalizeConnectionEffectModels(evalPinnedModel ?? await fetchProviderModelsStrict(connection, apiKey, options.fetch)),
        };
'''

SESSION_OLD = b'''                session: {
                    workspace: { kind: 'host_path', path: context.cwd },
'''

SESSION_NEW = b'''                session: {
                    name: 'Maka Eval',
                    workspace: { kind: 'host_path', path: context.cwd },
'''


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def model_fetcher_patched_bytes(data: bytes) -> bytes:
    if digest(data) != MODEL_FETCHER_BASE_SHA256:
        raise RuntimeError(
            "pinned model fetcher hash does not match hosted onboarding overlay"
        )
    if data.count(DISCOVERY_OLD) != 1:
        raise RuntimeError(
            "pinned model fetcher does not contain the exact hosted onboarding target"
        )
    patched = data.replace(DISCOVERY_OLD, DISCOVERY_NEW, 1)
    if digest(patched) != MODEL_FETCHER_PATCHED_SHA256:
        raise RuntimeError(
            "hosted onboarding overlay produced an unexpected model fetcher"
        )
    return patched


def maka_subject_patched_bytes(data: bytes) -> bytes:
    if digest(data) != MAKA_SUBJECT_BASE_SHA256:
        raise RuntimeError(
            "pinned Maka Eval subject hash does not match hosted onboarding overlay"
        )
    if data.count(SESSION_OLD) != 1:
        raise RuntimeError(
            "pinned Maka Eval subject does not contain the exact Session target"
        )
    patched = data.replace(SESSION_OLD, SESSION_NEW, 1)
    if digest(patched) != MAKA_SUBJECT_PATCHED_SHA256:
        raise RuntimeError(
            "hosted onboarding overlay produced an unexpected Maka Eval subject"
        )
    return patched


def regular_target(package_root: Path, relative_path: Path, label: str) -> Path:
    target = package_root / relative_path
    if target.is_symlink() or not target.is_file():
        raise RuntimeError(f"pinned {label} is not a regular file")
    return target


def atomic_replace(target: Path, data: bytes) -> None:
    temporary = target.with_name(f".{target.name}.{OVERLAY_ID}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def apply(package_root: Path) -> tuple[Path, Path]:
    package_root = package_root.resolve(strict=True)
    model_fetcher = regular_target(package_root, MODEL_FETCHER_PATH, "model fetcher")
    maka_subject = regular_target(package_root, MAKA_SUBJECT_PATH, "Maka Eval subject")

    # Validate and build both exact patches before changing either file.
    patched_model_fetcher = model_fetcher_patched_bytes(model_fetcher.read_bytes())
    patched_maka_subject = maka_subject_patched_bytes(maka_subject.read_bytes())
    atomic_replace(model_fetcher, patched_model_fetcher)
    atomic_replace(maka_subject, patched_maka_subject)

    if digest(model_fetcher.read_bytes()) != MODEL_FETCHER_PATCHED_SHA256:
        raise RuntimeError("hosted onboarding model fetcher did not persist exactly")
    if digest(maka_subject.read_bytes()) != MAKA_SUBJECT_PATCHED_SHA256:
        raise RuntimeError("hosted onboarding Maka Eval subject did not persist exactly")
    return model_fetcher, maka_subject


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} PACKAGE_ROOT")
    apply(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
