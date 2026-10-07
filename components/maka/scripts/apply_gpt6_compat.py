#!/usr/bin/env python3
"""Guarded GPT-6.1 Sol compatibility overlay for the exact pinned Maka package.

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

if __package__:
    from .install_component import PACKAGE_NAME, PACKAGE_VERSION
else:
    from install_component import PACKAGE_NAME, PACKAGE_VERSION


class OverlayError(RuntimeError):
    pass


PATCHES: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "node_modules/@maka/runtime/dist/model-runtime.js": (
        "a8f069732226c670c3756a7fedc191d5a9fd56e14b05caaa6b034426bba9ec05",
        ((
            "function resolveParallelToolCalls(connection, modelId, adapter) {\n"
            "    const stored = connection.models?.find((model) => model.id === modelId)?.capabilities\n",
            "function resolveParallelToolCalls(connection, modelId, adapter) {\n"
            "    const declared = modelOverride(connection, modelId)?.capabilities?.parallelToolCalls;\n"
            "    if (declared !== undefined)\n"
            "        return declared;\n"
            "    const stored = connection.models?.find((model) => model.id === modelId)?.capabilities\n",
        ),),
    ),
    "node_modules/@maka/core/dist/session.js": (
        "423dd039034711b38eb4bb15a93821c1ca682e4fe4f7c2814d8a7e9823e22732",
        ((
            "    'workhub-coordination-v2',\n];",
            "    'workhub-coordination-v2',\n"
            "    'khenrix-skill-routing-v1',\n];",
        ),),
    ),
    "node_modules/@maka/runtime-host/dist/server/hosted-execution-tool-profile.js": (
        "b1af943a734250ba76e6e827583e833f8b813eba66950b8a1a92a576c5971b2c",
        ((
            "    if (profile === 'workhub-coordination-v1') {\n",
            "    if (profile === 'khenrix-skill-routing-v1') {\n"
            "        return {\n"
            "            toolNames: ['Skill', 'SkillSearch'],\n"
            "            systemPrompt: '',\n"
            "            memoryExtraction: false,\n"
            "        };\n"
            "    }\n"
            "    if (profile === 'workhub-coordination-v1') {\n",
        ),),
    ),
    "node_modules/@maka/runtime-host/dist/server/interactive-run-composer.js": (
        "ee88cf0c3fc8815f153791d6b44392ca5cd24f5579b4ca76051403e5f828af52",
        (
            (
                "    const runProfile = hostedExecutionRunProfile(input.toolProfile);\n"
                "    const resolvedBaseSystemPrompts = new Map();\n",
                "    const runProfile = hostedExecutionRunProfile(input.toolProfile);\n"
                "    const promptProfile = input.toolProfile === 'khenrix-skill-routing-v1'\n"
                "        ? undefined\n"
                "        : runProfile;\n"
                "    const resolvedBaseSystemPrompts = new Map();\n",
            ),
            (
                "        if (runProfile) {\n"
                "            return (input.resolveProfileSystemPrompt\n",
                "        if (promptProfile) {\n"
                "            return (input.resolveProfileSystemPrompt\n",
            ),
            (
                "        if (!input.resolveAdditionalSystemPrompt || runProfile)\n",
                "        if (!input.resolveAdditionalSystemPrompt || promptProfile)\n",
            ),
        ),
    ),
    "node_modules/@maka/runtime/dist/ai-sdk-backend.js": (
        "90d9f1a3f7ba62a4ac189597cac06ce1babc8005631fe89a1bb594278b186421",
        ((
            "            runtime: new ToolAvailabilityRuntime(bindToolResultArchiveDecoder([...modelTools, ...this.memoryTools], this.input.toolResultArchive), this.input.toolAvailability, buildInvalidMakaTool()),\n",
            "            runtime: new ToolAvailabilityRuntime(bindToolResultArchiveDecoder([...modelTools, ...this.memoryTools], this.input.header.toolProfile === 'khenrix-skill-routing-v1'\n"
            "                ? undefined : this.input.toolResultArchive), this.input.toolAvailability, buildInvalidMakaTool()),\n",
        ),),
    ),
    "node_modules/@maka/runtime/dist/skills-metadata.js": (
        "398220f18f9eca49e28f96ba072a5598eb6a7e4b2c23ef5a0be3ba42b0815aa5",
        ((
            "export const MAX_SKILL_TOOL_BODY_CHARS = 24_000;",
            "export const MAX_SKILL_TOOL_BODY_CHARS = 40_000;",
        ),),
    ),
    "node_modules/@maka/core/dist/model-metadata.js": (
        "454ae64aa3a2dee4fe93d30f3d547e361b6b30f2c26987c69f0a8ab6981cd2cf",
        (
            (
                "const OPENAI_GPT6_MODEL_OVERRIDES = {\n",
                "const OPENAI_GPT6_MODEL_OVERRIDES = {\n"
                "    'gpt-6.1-sol': {\n"
                "        displayName: 'GPT-6.1 Sol',\n"
                "        capabilities: { ...REASONING_FUNCTION_CALLING, vision: true },\n"
                "        contextWindow: 1_050_000,\n"
                "        maxOutputTokens: 128_000,\n"
                "        thinkingOptions: OPENAI_GPT6_THINKING_OPTIONS,\n"
                "    },\n",
            ),
            (
                "function openAiOAuthModelMetadata(active) {\n    return {\n",
                "function openAiOAuthModelMetadata(active) {\n    return {\n"
                "        'gpt-6.1-sol': {\n"
                "            ...openAiOAuthBase(active, 'gpt-6.1-sol'),\n"
                "            ...OPENAI_GPT6_MODEL_OVERRIDES['gpt-6.1-sol'],\n"
                "            thinkingOptions: { efforts: ['low', 'medium', 'high', 'xhigh'] },\n"
                "        },\n",
            ),
            (
                "        'gpt-6-sol': {\n"
                "            ...openAiOAuthBase(active, 'gpt-6-sol'),\n"
                "            ...OPENAI_GPT6_MODEL_OVERRIDES['gpt-6-sol'],\n"
                "        },",
                "        'gpt-6-sol': {\n"
                "            ...openAiOAuthBase(active, 'gpt-6-sol'),\n"
                "            ...OPENAI_GPT6_MODEL_OVERRIDES['gpt-6-sol'],\n"
                "            thinkingOptions: { efforts: ['low', 'medium', 'high', 'xhigh'] },\n"
                "        },",
            ),
        ),
    ),
    "node_modules/@maka/core/dist/provider-registry.js": (
        "cf153bc1fe45a97d6fb1bb8cc6a2698eecc667c7f4c6ccffab3a1bdea34767d0",
        (
            (
                "fallbackModels: ['gpt-5.5', 'gpt-5.5-pro', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5'],",
                "fallbackModels: ['gpt-6.1-sol', 'gpt-6-sol', 'gpt-5.5', 'gpt-5.5-pro', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5'],",
            ),
            (
                "fallbackModels: ['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'],",
                "fallbackModels: ['gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'],",
            ),
        ),
    ),
    "node_modules/@maka/core/dist/model-thinking.js": (
        "864d0f46c5716ede1e3f1208169a0b2fe6c15da8ee7f40fd1d6fedfb18a394d9",
        (("    'gpt-6-astra',\n", "    'gpt-6-astra',\n    'gpt-6-sol',\n    'gpt-6.1-sol',\n"),),
    ),
}
PATCHED_HASHES = {
    "node_modules/@maka/runtime/dist/model-runtime.js": "d38130ccc2dbc03b759c4890a5c038161ea3997c437c8e6c90783673452734a4",
    "node_modules/@maka/core/dist/session.js": "94dc36d9af33a53475a13b22bb90df3c0efd7bad4247858d4fdb1258e477033d",
    "node_modules/@maka/runtime-host/dist/server/hosted-execution-tool-profile.js": "cebbf7c82e934d2f328dad8e77270cc308356955ae315808cf7db334c7e98780",
    "node_modules/@maka/runtime-host/dist/server/interactive-run-composer.js": "92c07f7c713140bcc170985e978b73fbb4eb72f8285855da913f3e73b5495ebc",
    "node_modules/@maka/runtime/dist/ai-sdk-backend.js": "d8caed4dd0cf4c5072520c5c0dee4c1f0fcd4ebe6851262807cf38f8c1ac8e2e",
    "node_modules/@maka/runtime/dist/skills-metadata.js": "2fa1b79b305b7d394129163fb032fa4cadfb0f802b43c1314cfe77cfa8ed48a2",
    "node_modules/@maka/core/dist/model-metadata.js": "6a1e2886e7412b010ee60c4e314827cbac17229e816b489fe974a297e9a166af",
    "node_modules/@maka/core/dist/provider-registry.js": "ccb5a2c69b2a5b9b1292b34bf39855c7354a46451d6984c8687be0a4b6821821",
    "node_modules/@maka/core/dist/model-thinking.js": "ace4ab6b47d785d05a710bd9da64d127e451f18f6772170e9b063d6cde121c25",
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
