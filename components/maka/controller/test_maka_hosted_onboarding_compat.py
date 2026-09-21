#!/usr/bin/env python3
"""Offline positive, negative, and fetch-trap tests for Eval onboarding."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import apply_maka_hosted_onboarding_compat as compat


IMPORT_TARGETS = {
    "@maka/core/llm-connections": Path(
        "node_modules/@maka/core/dist/llm-connections.js"
    ),
    "@maka/core/runtime-policy": Path(
        "node_modules/@maka/core/dist/runtime-policy.js"
    ),
}

SUBJECT_IMPORT_TARGETS = {
    "@maka/core/llm-connections": Path(
        "node_modules/@maka/core/dist/llm-connections.js"
    ),
    "@maka/core/model-thinking": Path(
        "node_modules/@maka/core/dist/model-thinking.js"
    ),
    "@maka/core/session": Path("node_modules/@maka/core/dist/session.js"),
    "@maka/runtime-host/protocol": Path(
        "node_modules/@maka/runtime-host/dist/protocol/index.js"
    ),
}


def copy_base_targets(source_package: Path, destination: Path) -> tuple[Path, Path]:
    targets = []
    for relative in (compat.MODEL_FETCHER_PATH, compat.MAKA_SUBJECT_PATH):
        source = source_package / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        targets.append(target)
    return targets[0], targets[1]


def stage_and_apply(source_package: Path, destination: Path) -> tuple[Path, Path]:
    model_fetcher, maka_subject = copy_base_targets(source_package, destination)
    compat.apply(destination)
    assert (
        compat.digest(model_fetcher.read_bytes())
        == compat.MODEL_FETCHER_PATCHED_SHA256
    )
    assert (
        compat.digest(maka_subject.read_bytes())
        == compat.MAKA_SUBJECT_PATCHED_SHA256
    )
    assert not model_fetcher.is_symlink()
    assert not maka_subject.is_symlink()
    return model_fetcher, maka_subject


def executable_test_module(package_root: Path, patched: bytes, destination: Path) -> Path:
    """Rewrite imports only, so Node executes the exact patched function body."""

    source_path = package_root / compat.MODEL_FETCHER_PATH
    source = patched.decode("utf-8")
    replacements = {
        **{
            specifier: package_root / relative
            for specifier, relative in IMPORT_TARGETS.items()
        },
        **{
            specifier: source_path.parent / specifier.removeprefix("./")
            for specifier in (
                "./provider-urls.js",
                "./subscription-auth.js",
                "./subscription-credentials.js",
                "./connection-effect-fetch.js",
                "./connection-effect-outcome.js",
            )
        },
    }
    for specifier, target in replacements.items():
        quoted = f"'{specifier}'"
        if source.count(quoted) != 1:
            raise AssertionError(f"unexpected import count for {specifier}")
        source = source.replace(quoted, json.dumps(target.resolve(strict=True).as_uri()), 1)
    destination.write_text(source, encoding="utf-8")
    return destination


def run_fetch_trap(package_root: Path, patched: bytes, test_root: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node is required for the hosted onboarding test")
    module_path = executable_test_module(
        package_root, patched, test_root / "patched-model-fetcher.mjs"
    )
    runner = test_root / "fetch-trap.mjs"
    runner.write_text(
        f'''import assert from 'node:assert/strict';
import {{ runConnectionModelDiscoveryEffect }} from {json.dumps(module_path.as_uri())};

const exactConnection = Object.freeze({{
  providerType: 'openai',
  slug: 'eval-openai',
  baseUrl: 'https://api.openai.com/v1',
}});
const exactCredential = 'maka-decoy-abcdefgh';

async function exercise(connection, credential, response) {{
  const calls = [];
  const result = await runConnectionModelDiscoveryEffect(connection, credential, {{
    fetch: async (input, init = {{}}) => {{
      calls.push({{ url: String(input), method: init.method ?? 'GET' }});
      if (response !== undefined) return response;
      throw new Error('model discovery fetch trap');
    }},
  }});
  return {{ calls, result }};
}}

const exact = await exercise(exactConnection, exactCredential);
assert.deepEqual(exact.result, {{ ok: true, models: [{{ id: 'gpt-5.6-sol' }}] }});
assert.deepEqual(exact.calls, []);
const maximumDecoy = await exercise(exactConnection, `maka-decoy-${{'a'.repeat(128)}}`);
assert.deepEqual(maximumDecoy.result, {{ ok: true, models: [{{ id: 'gpt-5.6-sol' }}] }});
assert.deepEqual(maximumDecoy.calls, []);

const nearMisses = [
  ['provider', {{ ...exactConnection, providerType: 'openai-compatible' }}, exactCredential],
  ['slug', {{ ...exactConnection, slug: 'eval-openai-other' }}, exactCredential],
  ['base URL', {{ ...exactConnection, baseUrl: 'https://api.openai.com/v1/' }}, exactCredential],
  ['short decoy', exactConnection, 'maka-decoy-abcdefg'],
  ['non-decoy', exactConnection, 'sk-synthetic-not-a-real-key'],
  ['long decoy', exactConnection, `maka-decoy-${{'a'.repeat(129)}}`],
  ['malformed decoy', exactConnection, 'maka-decoy-abcdefgh!'],
];
for (const [label, connection, credential] of nearMisses) {{
  const observed = await exercise(connection, credential);
  assert.equal(observed.result.ok, false, `${{label}} did not use discovery`);
  assert.deepEqual(observed.calls, [{{
    url: 'https://api.openai.com/v1/models',
    method: 'GET',
  }}], `${{label}} did not reach the fetch trap exactly once`);
}}

const remoteBody = JSON.stringify({{ data: [{{ id: 'remote-model' }}] }});
const remote = await exercise(
  {{ ...exactConnection, slug: 'ordinary-openai' }},
  exactCredential,
  new Response(remoteBody, {{
    status: 200,
    headers: {{ 'content-type': 'application/json', 'content-length': String(remoteBody.length) }},
  }}),
);
assert.deepEqual(remote.calls, [{{
  url: 'https://api.openai.com/v1/models',
  method: 'GET',
}}]);
assert.deepEqual(remote.result, {{ ok: true, models: [{{ id: 'remote-model' }}] }});
''',
        encoding="utf-8",
    )
    subprocess.run(
        [node, "--no-warnings", str(runner)],
        check=True,
        cwd=test_root,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )


def executable_subject_module(
    package_root: Path, patched: bytes, destination: Path
) -> Path:
    """Rewrite imports only, so Node executes the exact patched Eval adapter."""

    source_path = package_root / compat.MAKA_SUBJECT_PATH
    source = patched.decode("utf-8")
    replacements = {
        **{
            specifier: package_root / relative
            for specifier, relative in SUBJECT_IMPORT_TARGETS.items()
        },
        **{
            specifier: source_path.parent / specifier.removeprefix("./")
            for specifier in ("./maka-artifacts.js", "./provider-metering.js")
        },
    }
    for specifier, target in replacements.items():
        quoted = f"'{specifier}'"
        if source.count(quoted) != 1:
            raise AssertionError(f"unexpected subject import count for {specifier}")
        source = source.replace(quoted, json.dumps(target.resolve(strict=True).as_uri()), 1)
    destination.write_text(source, encoding="utf-8")
    return destination


def run_subject_payload_test(
    package_root: Path, patched: bytes, test_root: Path
) -> None:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node is required for the Maka Eval subject test")
    module_path = executable_subject_module(
        package_root, patched, test_root / "patched-maka-subject.mjs"
    )
    protocol_path = (
        package_root / "node_modules/@maka/runtime-host/dist/protocol/hosted-execution.js"
    ).resolve(strict=True)
    runner = test_root / "subject-payload.mjs"
    runner.write_text(
        f'''import assert from 'node:assert/strict';
import {{ createMakaSubjectAdapter }} from {json.dumps(module_path.as_uri())};
import {{ decodeHostedExecutionStartInput }} from {json.dumps(protocol_path.as_uri())};

const config = Object.freeze({{
  providerType: 'openai',
  apiKeyEnvironment: 'OPENAI_API_KEY',
  nodePath: '/opt/maka-node-toolchain/bin/node',
  shimPath: '/opt/maka-agent/node_modules/@maka/eval/dist/harbor-maka-subject.js',
  runtimeHostsPath: '/tmp/maka-runtime-hosts',
  connectionSlug: 'eval-openai',
  baseUrl: 'https://api.openai.com/v1',
  model: 'gpt-5.6-sol',
  thinkingLevel: 'xhigh',
  permissionMode: 'bypass',
  collaborationMode: 'agent',
  orchestrationMode: 'default',
  hostSettlementTimeoutMs: 120000,
  toolProfile: 'headless-coding-v1',
}});
let calls = 0;
let encodedPayload;
const result = await createMakaSubjectAdapter().execute({{
  cell: {{
    subject: {{ config, credentials: ['OPENAI_API_KEY'] }},
    budget: {{ maxSteps: 32 }},
  }},
  context: {{
    cwd: '/workspace',
    taskInput: 'Offline subject payload test',
    execute: async (invocation) => {{
      calls += 1;
      assert.equal(invocation.command, config.nodePath);
      assert.equal(invocation.args[0], config.shimPath);
      encodedPayload = invocation.args[1];
      throw new Error('offline execute trap');
    }},
  }},
}});
assert.equal(calls, 1);
assert.equal(result.status, 'infra_failed');
assert.equal(result.failureReason, 'Maka subject failed during relay-execute');
const payload = JSON.parse(Buffer.from(encodedPayload, 'base64url').toString('utf8'));
assert.equal(payload.execution.session.name, 'Maka Eval');
assert.equal(payload.execution.session.workspace.path, '/workspace');
assert.deepEqual(payload.execution.session.modelTarget, {{
  kind: 'explicit',
  connectionSlug: 'eval-openai',
  model: 'gpt-5.6-sol',
}});
// Hosted initialization resolves the configured connection slug to its entity id
// before this protocol decoder runs. Add only that resolved identity here.
const decoded = decodeHostedExecutionStartInput({{
  ...payload.execution,
  session: {{
    ...payload.execution.session,
    modelTarget: {{
      ...payload.execution.session.modelTarget,
      connectionId: '00000000-0000-4000-8000-000000000002',
    }},
  }},
}});
assert.equal(decoded.session.name, 'Maka Eval');
assert.equal(decoded.session.thinkingLevel, 'xhigh');
''',
        encoding="utf-8",
    )
    subprocess.run(
        [node, "--no-warnings", str(runner)],
        check=True,
        cwd=test_root,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )


def executable_session_effect_module(package_root: Path, test_root: Path) -> Path:
    """Load the pinned coordinator with only its artifact authenticator stubbed."""

    source_path = (
        package_root
        / "node_modules/@maka/runtime-host/dist/server/session-effect-coordinator.js"
    )
    source = source_path.read_text(encoding="utf-8")
    artifact_stub = test_root / "artifact-store-stub.mjs"
    artifact_stub.write_text(
        "export const authenticateInteractiveArtifactStoreWriter = value => value;\n",
        encoding="utf-8",
    )
    replacements = {
        "@maka/core/session-name": (
            package_root / "node_modules/@maka/core/dist/session-name.js"
        ),
        "@maka/runtime/session-recap": (
            package_root / "node_modules/@maka/runtime/dist/session-recap.js"
        ),
        "@maka/storage/artifact-stores": artifact_stub,
        "./session-title.js": source_path.parent / "session-title.js",
        "../protocol/index.js": source_path.parent.parent / "protocol/index.js",
    }
    for specifier, target in replacements.items():
        quoted = f"'{specifier}'"
        if source.count(quoted) != 1:
            raise AssertionError(f"unexpected coordinator import count for {specifier}")
        source = source.replace(quoted, json.dumps(target.resolve(strict=True).as_uri()), 1)
    destination = test_root / "session-effect-coordinator.mjs"
    destination.write_text(source, encoding="utf-8")
    return destination


def run_session_title_guard_test(package_root: Path, test_root: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node is required for the Session title guard test")
    coordinator_path = executable_session_effect_module(package_root, test_root)
    session_name_path = (
        package_root / "node_modules/@maka/core/dist/session-name.js"
    ).resolve(strict=True)
    runner = test_root / "session-title-guard.mjs"
    runner.write_text(
        f'''import assert from 'node:assert/strict';
import {{ HostSessionEffectCoordinator }} from {json.dumps(coordinator_path.as_uri())};
import {{ DEFAULT_SESSION_NAME }} from {json.dumps(session_name_path.as_uri())};

async function exercise(name) {{
  const observed = {{ titleCalls: 0, writes: 0, notifications: 0, releases: 0 }};
  let resolveReleased;
  const released = new Promise(resolve => {{ resolveReleased = resolve; }});
  const coordinator = new HostSessionEffectCoordinator({{
    model: {{
      generateTitle: async () => {{
        observed.titleCalls += 1;
        return 'Generated title';
      }},
    }},
    readModel: {{}},
    artifacts: {{}},
    sessions: {{}},
    readSessionHeader: async () => ({{ name, titleIsManual: false }}),
    sessionAdmission: {{}},
    nameSessionIfUnnamed: async () => {{ observed.writes += 1; return true; }},
    onSessionNamed: () => {{ observed.notifications += 1; }},
    acquireResidency: () => ({{ release: () => {{
      observed.releases += 1;
      resolveReleased();
    }} }}),
    requestDrain: () => {{}},
  }});
  coordinator.nameSessionFromRootMessage({{
    sessionId: '00000000-0000-4000-8000-000000000001',
    content: {{ text: 'Root task text' }},
  }});
  await Promise.race([
    released,
    new Promise((_, reject) => setTimeout(() => reject(new Error('title guard timeout')), 1000)),
  ]);
  await coordinator.close();
  return observed;
}}

assert.deepEqual(await exercise('Maka Eval'), {{
  titleCalls: 0,
  writes: 0,
  notifications: 0,
  releases: 1,
}});
assert.deepEqual(await exercise(DEFAULT_SESSION_NAME), {{
  titleCalls: 1,
  writes: 1,
  notifications: 1,
  releases: 1,
}});
''',
        encoding="utf-8",
    )
    subprocess.run(
        [node, "--no-warnings", str(runner)],
        check=True,
        cwd=test_root,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} PACKAGE_ROOT")
    package_root = Path(sys.argv[1]).resolve(strict=True)
    model_fetcher_base = (package_root / compat.MODEL_FETCHER_PATH).read_bytes()
    maka_subject_base = (package_root / compat.MAKA_SUBJECT_PATH).read_bytes()
    assert (
        compat.digest(model_fetcher_base) == compat.MODEL_FETCHER_BASE_SHA256
    )
    assert compat.digest(maka_subject_base) == compat.MAKA_SUBJECT_BASE_SHA256

    with tempfile.TemporaryDirectory() as temporary:
        test_root = Path(temporary)
        positive = test_root / "positive"
        patched_model_fetcher, patched_maka_subject = stage_and_apply(
            package_root, positive
        )
        model_fetcher = patched_model_fetcher.read_bytes()
        maka_subject = patched_maka_subject.read_bytes()
        assert model_fetcher.count(compat.DISCOVERY_NEW) == 1
        assert compat.DISCOVERY_OLD not in model_fetcher
        assert maka_subject.count(compat.SESSION_NEW) == 1
        assert compat.SESSION_OLD not in maka_subject

        model_negative = test_root / "model-negative"
        negative_model, untouched_subject = copy_base_targets(
            package_root, model_negative
        )
        mutated_model = model_fetcher_base + b"\n// unexpected mutation\n"
        negative_model.write_bytes(mutated_model)
        try:
            compat.apply(model_negative)
        except RuntimeError as error:
            assert "hash does not match" in str(error)
        else:
            raise AssertionError("overlay accepted an unpinned model fetcher")
        assert negative_model.read_bytes() == mutated_model
        assert untouched_subject.read_bytes() == maka_subject_base

        subject_negative = test_root / "subject-negative"
        untouched_model, negative_subject = copy_base_targets(
            package_root, subject_negative
        )
        mutated_subject = maka_subject_base + b"\n// unexpected mutation\n"
        negative_subject.write_bytes(mutated_subject)
        try:
            compat.apply(subject_negative)
        except RuntimeError as error:
            assert "hash does not match" in str(error)
        else:
            raise AssertionError("overlay accepted an unpinned Maka Eval subject")
        assert untouched_model.read_bytes() == model_fetcher_base
        assert negative_subject.read_bytes() == mutated_subject

        model_symlink = test_root / "model-symlink"
        model_symlink_target, _ = copy_base_targets(package_root, model_symlink)
        model_symlink_target.unlink()
        model_symlink_target.symlink_to(package_root / compat.MODEL_FETCHER_PATH)
        try:
            compat.apply(model_symlink)
        except RuntimeError as error:
            assert "not a regular file" in str(error)
        else:
            raise AssertionError("overlay accepted a model fetcher symlink")
        assert (
            compat.digest(model_fetcher_base) == compat.MODEL_FETCHER_BASE_SHA256
        )

        subject_symlink = test_root / "subject-symlink"
        untouched_model, subject_symlink_target = copy_base_targets(
            package_root, subject_symlink
        )
        subject_symlink_target.unlink()
        subject_symlink_target.symlink_to(package_root / compat.MAKA_SUBJECT_PATH)
        try:
            compat.apply(subject_symlink)
        except RuntimeError as error:
            assert "not a regular file" in str(error)
        else:
            raise AssertionError("overlay accepted a Maka Eval subject symlink")
        assert untouched_model.read_bytes() == model_fetcher_base

        run_fetch_trap(package_root, model_fetcher, test_root)
        run_subject_payload_test(package_root, maka_subject, test_root)
        run_session_title_guard_test(package_root, test_root)


if __name__ == "__main__":
    main()
