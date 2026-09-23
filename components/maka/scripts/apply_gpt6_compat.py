#!/usr/bin/env python3
"""Guarded GPT-6 Sol compatibility overlay for the exact pinned Maka package.

Apply only to an isolated copy of maka-agent. The mise-owned package and the
frozen evaluation controller are never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import tempfile

from install_component import PACKAGE_NAME, PACKAGE_VERSION


class OverlayError(RuntimeError):
    pass


PATCHES: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "node_modules/@maka/core/dist/model-metadata.js": (
        "e7712062254398e70adc333167864d09c4a1aca4775bd24daa2ad09876e4e122",
        (
            (
                "function openAiOAuthModelMetadata(active) {\n    return {\n",
                "function openAiOAuthModelMetadata(active) {\n    return {\n"
                "        'gpt-6-sol': {\n"
                "            ...openAiOAuthBase(active, 'gpt-6-sol'),\n"
                "            displayName: 'GPT-6 Sol',\n"
                "            contextWindow: 272_000,\n"
                "            maxOutputTokens: 128_000,\n"
                "            capabilities: { vision: true, reasoning: true, functionCalling: true },\n"
                "            thinkingOptions: { efforts: ['none', 'low', 'medium', 'high', 'xhigh'] },\n"
                "        },\n",
            ),
            (
                "        'openai-codex': openAiOAuthModelMetadata(active),\n",
                "        'openai-codex': openAiOAuthModelMetadata(active),\n"
                "        openai: {\n"
                "            'gpt-6-sol': {\n"
                "                displayName: 'GPT-6 Sol',\n"
                "                contextWindow: 1_050_000,\n"
                "                maxOutputTokens: 128_000,\n"
                "                capabilities: { vision: true, reasoning: true, functionCalling: true },\n"
                "                modalities: { input: ['text', 'image', 'pdf'], output: ['text'] },\n"
                "                thinkingOptions: { efforts: ['none', 'low', 'medium', 'high', 'xhigh', 'max'] },\n"
                "            },\n"
                "        },\n",
            ),
            ("        /^gpt-5/i.test(id) ||\n", "        /^gpt-(?:5|6)/i.test(id) ||\n"),
        ),
    ),
    "node_modules/@maka/core/dist/provider-registry.js": (
        "f367229b68b776b41e19fe718fb31f235aa986b9cdcc4c4c3298dada40906778",
        (
            (
                "fallbackModels: ['gpt-5.5', 'gpt-5.5-pro', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5'],",
                "fallbackModels: ['gpt-6-sol', 'gpt-5.5', 'gpt-5.5-pro', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5'],",
            ),
            (
                "fallbackModels: ['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'],",
                "fallbackModels: ['gpt-6-sol', 'gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'],",
            ),
        ),
    ),
    "node_modules/@maka/core/dist/model-thinking.js": (
        "f51d5ff23ea9a77028ee1e5c0c2394479836f62a4da9e488b170ffd65bec01e6",
        (("    'gpt-6-astra',\n", "    'gpt-6-astra',\n    'gpt-6-sol',\n"),),
    ),
}
PATCHED_HASHES = {
    "node_modules/@maka/core/dist/model-metadata.js": "0aef6601a94ea79b0810b9704af6687bd87d1dc42d71ef3e3d9040a758d41e86",
    "node_modules/@maka/core/dist/provider-registry.js": "b589767669d7fd7fad9e8a900398aa7896a72702a3f26dda850be5dcb66469bd",
    "node_modules/@maka/core/dist/model-thinking.js": "8b7061b1d188964d1fa6e2a5b534a019113020bdc2c3c601299ca6d5dea4d3e6",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patched_content(relative: str, data: bytes) -> bytes:
    expected, replacements = PATCHES[relative]
    if sha256(data) != expected:
        raise OverlayError(f"pinned Maka source hash differs: {relative}")
    source = data.decode("utf-8")
    for before, after in replacements:
        if source.count(before) != 1:
            raise OverlayError(f"pinned Maka patch anchor differs: {relative}")
        source = source.replace(before, after, 1)
    result = source.encode("utf-8")
    if sha256(result) != PATCHED_HASHES[relative]:
        raise OverlayError(f"pinned Maka patched hash differs: {relative}")
    return result


def apply_overlay(package: pathlib.Path) -> dict[str, dict[str, str]]:
    package = package.resolve(strict=True)
    descriptor = json.loads((package / "package.json").read_text(encoding="utf-8"))
    if descriptor.get("name") != PACKAGE_NAME or descriptor.get("version") != PACKAGE_VERSION:
        raise OverlayError("Maka package identity differs from the compatibility pin")
    changed: dict[pathlib.Path, bytes] = {}
    hashes: dict[str, dict[str, str]] = {}
    for relative, (expected, _) in PATCHES.items():
        path = package / relative
        if path.is_symlink() or not path.is_file():
            raise OverlayError(f"pinned Maka file is not a regular file: {relative}")
        original = path.read_bytes()
        revised = patched_content(relative, original)
        changed[path] = revised
        hashes[relative] = {"source": expected, "patched": sha256(revised)}
    # Every guard passes before the first write. The caller publishes this copy
    # only after all files and runtime probes pass.
    for path, content in changed.items():
        descriptor_id, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = pathlib.Path(name)
        try:
            os.fchmod(descriptor_id, 0o644)
            with os.fdopen(descriptor_id, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=pathlib.Path)
    args = parser.parse_args()
    print(json.dumps(apply_overlay(args.package), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
