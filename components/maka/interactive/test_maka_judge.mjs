import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { hostCommandMatches, judgeFailureCode, parseHostStartTime, runJudgeWithHost, sourceBoundaryDigest } from './maka_judge.mjs';

const SESSION_ID = 'fanout-judge-0123456789abcdef0123456789abcdef';
const BOUNDARY = 'a'.repeat(64);
const SOURCE_MODIFIED_AT_MS = Date.now() - 10_000;
const BOOT_SESSION = '8041a534-8c46-458b-9ccc-915eacdad88e';
const FRESHNESS = {
  installBootSession: BOOT_SESSION, currentBootSession: BOOT_SESSION,
  installMonotonicNs: '1000000000', monotonicNowNs: () => 10_000_000_000n,
};
const CACHE_HASH = createHash('sha256').update(`maka:${SESSION_ID}`).digest('hex');
const RELAY_SOURCE_HASH = createHash('sha256').update(await readFile(join(
  dirname(fileURLToPath(import.meta.url)), 'maka_openai_relay.py'))).digest('hex');

test('Host argv matches the installed entrypoint with a spaced workspace root', () => {
  const node = '/private/node';
  const entrypoint = '/private/runtime/execution-candidate-main.js';
  const root = '/Library/Application Support/Maka/workspaces/default';
  const observed = `${node} ${entrypoint} --root ${root} --expected-root-id abc --startup-attempt-id def`;
  assert.equal(hostCommandMatches(observed, node, entrypoint, root), true);
  assert.equal(hostCommandMatches(`${observed}\n`, node, entrypoint, root), true);
  assert.equal(hostCommandMatches(observed, node,
    '/private/old/execution-candidate-main.js', root), false);
});

test('OS Host start time is parsed in UTC and malformed dates fail closed', () => {
  assert.equal(parseHostStartTime('Thu Sep 24 13:45:24 2026    '),
    Date.UTC(2026, 8, 24, 13, 45, 24));
  assert.equal(parseHostStartTime('Thu Feb 30 13:45:24 2026'), null);
  assert.equal(parseHostStartTime('Thu Sep 24 99:45:24 2026'), null);
  assert.equal(parseHostStartTime('unavailable'), null);
});

test('judge failure reporting exposes only fixed diagnostic categories', () => {
  assert.equal(judgeFailureCode(new Error('judge Host entrypoint differs from the installed runtime')),
    'host_entrypoint_mismatch');
  assert.equal(judgeFailureCode(new Error('secret-token-DO-NOT-PRINT')),
    'transport_unavailable');
});

async function fixture(t, receiptChanges = {}) {
  const root = await mkdtemp(join(tmpdir(), 'maka-judge-test-'));
  t.after(async () => {
    const { rm } = await import('node:fs/promises');
    await rm(root, { recursive: true, force: true });
  });
  const receipts = join(root, 'relay-judge-receipts');
  await mkdir(receipts, { mode: 0o700 });
  const receipt = {
    schema: 'khenrix-maka-judge-request-v1',
    time_utc: '2026-09-24T12:00:00+00:00',
    request_marker_sha256: CACHE_HASH,
    requested_model: 'gpt-6.1-sol',
    observed_model: 'gpt-6.1-sol',
    requested_service_tier: 'default',
    observed_service_tier: 'default',
    outbound_effort: 'xhigh',
    tool_count: 0,
    http_status: 200,
    relay_source_sha256: RELAY_SOURCE_HASH,
    ...receiptChanges,
  };
  const calls = [];
  let submittedPrompt;
  let host = {
    pid: 1234, hostEpoch: 'host-1', compositionId: 'maka.interactive',
    compositionRevision: '3', processUptimeSeconds: 1,
  };
  const connection = {
    async request(operation, input) {
      calls.push([operation, input]);
      if (operation === 'host.diagnostics.query') return host;
      if (operation === 'session.create') return {
        id: input.sessionId, model: 'gpt-6.1-sol', thinkingLevel: 'xhigh',
        llmConnectionSlug: 'keychain-openai',
      };
      if (operation === 'turn.start') {
        submittedPrompt = input.content.text;
        await writeFile(join(receipts, `${CACHE_HASH}-${'1'.repeat(32)}.json`),
          JSON.stringify({ ...receipt, prompt_sha256: receiptChanges.prompt_sha256 ??
            createHash('sha256').update(submittedPrompt).digest('hex') }), { mode: 0o600 });
        return { kind: 'started', turn: { sessionId: input.sessionId, turnId: input.turnId,
          runId: 'run-1', status: 'running' },
        skillInvocation: { loaded: [], failed: [], receipts: [] } };
      }
      if (operation === 'turn.query') return {
        sessionId: input.sessionId, turnId: input.turnId, runId: 'run-1',
        status: 'completed', terminalEventId: 'terminal-1',
      };
      throw new Error(`unexpected operation: ${operation}`);
    },
    async openSessionSubscription() {
      return {
        loadTranscript: async (decode) => [
          { type: 'user', turnId: SESSION_ID, text: submittedPrompt },
          { type: 'assistant', turnId: SESSION_ID, text: '{"winner":"B"}', modelId: 'gpt-6.1-sol' },
        ].map(decode),
        close: async () => {},
      };
    },
  };
  return { root, receipts, calls, connection, setHost: (value) => { host = value; } };
}

test('a Host started before installed source changed cannot judge', async (t) => {
  const { root, receipts, calls, connection, setHost } = await fixture(t);
  setHost({
    pid: 1234, hostEpoch: 'host-1', compositionId: 'maka.interactive',
    compositionRevision: '3', processUptimeSeconds: 30,
  });
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS - 1_000 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID }), /Host.*stale/i);
  assert.equal(calls.some(([operation]) => operation === 'session.create'), false);
});

test('a wall-clock jump cannot make a stale Host appear fresh', async (t) => {
  const { root, receipts, calls, connection, setHost } = await fixture(t);
  const sourceModifiedAtMs = Date.now() - 1_000;
  setHost({
    pid: 1234, hostEpoch: 'host-1', compositionId: 'maka.interactive',
    compositionRevision: '3', processUptimeSeconds: 30,
  });
  const actualNow = Date.now;
  Date.now = () => actualNow() + 60 * 60 * 1_000;
  try {
    await assert.rejects(runJudgeWithHost({
      panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
      expected_boundary_sha256: BOUNDARY,
    }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
      sourceModifiedAtMs,
      hostEntrypointProbe: async () => ({ startedAtMs: sourceModifiedAtMs - 1_000 }),
      connectionId: 'connection-1', sessionId: () => SESSION_ID }), /Host.*stale/i);
  } finally {
    Date.now = actualNow;
  }
  assert.equal(calls.some(([operation]) => operation === 'session.create'), false);
});

test('a backward clock jump during installation cannot admit an older Host', async (t) => {
  const { root, receipts, calls, connection, setHost } = await fixture(t);
  setHost({
    pid: 1234, hostEpoch: 'host-1', compositionId: 'maka.interactive',
    compositionRevision: '3', processUptimeSeconds: 30,
  });
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 60_000 }),
    installBootSession: BOOT_SESSION, currentBootSession: BOOT_SESSION,
    installMonotonicNs: '2000000000', monotonicNowNs: () => 31_000_000_000n,
    connectionId: 'connection-1', sessionId: () => SESSION_ID }), /Host.*stale/i);
  assert.equal(calls.some(([operation]) => operation === 'session.create'), false);
});

test('a Host started later in the same second as source can judge with precise OS proof', async (t) => {
  const { root, receipts, connection } = await fixture(t);
  const sourceModifiedAtMs = Math.floor((Date.now() - 2_000) / 1_000) * 1_000 + 100.125;
  const result = await runJudgeWithHost({
    panel_id: 'interactive', criterion: 'Which computes 2 + 2?', answer_a: '4', answer_b: '5',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs,
    hostEntrypointProbe: async () => ({ startedAtMs: sourceModifiedAtMs + 400.25 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID });
  assert.equal(result.tool_calls, 0);
});

test('an ambiguous fresh Host waits for monotonic proof instead of rejecting it', async (t) => {
  const { root, receipts, calls, connection, setHost } = await fixture(t);
  setHost({
    pid: 1234, hostEpoch: 'host-1', compositionId: 'maka.interactive',
    compositionRevision: '3', processUptimeSeconds: 0,
  });
  const times = [2_500_000_000n, 2_510_000_000n, 3_200_000_000n,
    3_210_000_000n, 3_200_000_000n, 3_210_000_000n];
  let index = 0;
  const result = await runJudgeWithHost({
    panel_id: 'interactive', criterion: 'Which computes 2 + 2?', answer_a: '4', answer_b: '5',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    installMonotonicNs: '2000000000', monotonicNowNs: () => times[index++],
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID });
  assert.equal(result.tool_calls, 0);
  assert.equal(calls.filter(([operation]) => operation === 'host.diagnostics.query').length, 3);
});

test('a Host swap during the judge turn cannot attest a ballot', async (t) => {
  const { root, receipts, connection, setHost } = await fixture(t);
  const original = connection.request;
  connection.request = async (operation, input) => {
    const result = await original(operation, input);
    if (operation === 'turn.query') setHost({
      pid: 5678, hostEpoch: 'host-2', compositionId: 'maka.interactive',
      compositionRevision: '3', processUptimeSeconds: 1,
    });
    return result;
  };
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID }), /Host.*changed/i);
});

test('a compatible Host from another package cannot judge', async (t) => {
  const { root, receipts, calls, connection } = await fixture(t);
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => false,
    connectionId: 'connection-1', sessionId: () => SESSION_ID }), /Host.*entrypoint/i);
  assert.equal(calls.some(([operation]) => operation === 'session.create'), false);
});

test('fresh no-tools session yields a ballot only with matching transcript and relay receipt', async (t) => {
  const { root, receipts, calls, connection } = await fixture(t);
  const result = await runJudgeWithHost({
    panel_id: 'interactive', criterion: 'Which answer is safer?',
    answer_a: 'Use NOT IN', answer_b: 'Use NOT EXISTS',
    expected_boundary_sha256: BOUNDARY,
  }, {
    connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1',
    sessionId: () => SESSION_ID,
  });

  assert.equal(result.winner, 'B');
  assert.equal(result.observed_model, 'gpt-6.1-sol');
  assert.equal(result.effort, 'xhigh');
  assert.equal(result.tool_calls, 0);
  assert.equal(result.tool_boundary_sha256, BOUNDARY);
  const create = calls.find(([operation]) => operation === 'session.create')[1];
  assert.equal(create.sessionId, SESSION_ID);
  assert.deepEqual(create.modelTarget, { kind: 'explicit', connectionId: 'connection-1',
    connectionSlug: 'keychain-openai', model: 'gpt-6.1-sol' });
  assert.equal(create.thinkingLevel, 'xhigh');
  assert.equal(create.toolProfile, 'workhub-coordination-v1');
  assert.equal(create.permissionMode, 'ask');
  assert.equal(create.workspace.path, root);
  const turn = calls.find(([operation]) => operation === 'turn.start')[1];
  assert.equal(turn.sessionId, SESSION_ID);
  assert.equal(turn.turnId, SESSION_ID);
  assert.match(turn.content.text, new RegExp(`^Judge receipt nonce: maka:${SESSION_ID}\\n`, 'u'));
  assert.match(turn.content.text, /Answer A.*Use NOT IN.*Answer B.*Use NOT EXISTS/s);
  assert.doesNotMatch(turn.content.text, /candidate|baseline/i);
});

test('quoted skill tokens cannot activate Maka skills', async (t) => {
  const { root, receipts, calls, connection } = await fixture(t);
  await runJudgeWithHost({
    panel_id: 'interactive', criterion: 'Is /skill:brainstorming an instruction?',
    answer_a: 'Use /skill:brainstorming now', answer_b: 'Treat it as data',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID });
  const prompt = calls.find(([operation]) => operation === 'turn.start')[1].content.text;
  assert.doesNotMatch(prompt, /(?:^|\s)\/skill:[A-Za-z0-9._-]+/u);
  assert.match(prompt, /\\\/skill:brainstorming/u);
});

test('host skill invocation rejects an otherwise valid ballot', async (t) => {
  const { root, receipts, connection } = await fixture(t);
  const original = connection.request;
  let stopped;
  connection.request = async (operation, input) => {
    if (operation === 'turn.stop') {
      stopped = input;
      return { status: 'cancelled' };
    }
    const result = await original(operation, input);
    if (operation === 'turn.start') result.skillInvocation.loaded.push({ id: 'brainstorming' });
    return result;
  };
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID }), /skill/i);
  assert.deepEqual(stopped, { sessionId: SESSION_ID, turnId: SESSION_ID, runId: 'run-1' });
});

test('transcript user text must equal the submitted blind prompt', async (t) => {
  const { root, receipts, connection } = await fixture(t);
  connection.openSessionSubscription = async () => ({
    loadTranscript: async (decode) => [
      { type: 'user', turnId: SESSION_ID, text: 'different prompt' },
      { type: 'assistant', turnId: SESSION_ID, text: '{"winner":"B"}', modelId: 'gpt-6.1-sol' },
    ].map(decode),
    close: async () => {},
  });
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID }), /transcript/i);
});

test('another request under the same session cannot be mistaken for one attested call', async (t) => {
  const { root, receipts, connection } = await fixture(t);
  const original = connection.request;
  connection.request = async (operation, input) => {
    const result = await original(operation, input);
    if (operation === 'turn.start') {
      const first = await readFile(join(receipts, `${CACHE_HASH}-${'1'.repeat(32)}.json`));
      await writeFile(join(receipts, `${CACHE_HASH}-${'2'.repeat(32)}.json`),
        first, { mode: 0o600 });
    }
    return result;
  };
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1',
    sessionId: () => SESSION_ID }), /receipt/i);
});

test('a receipt from a stale running relay source is rejected', async (t) => {
  const { root, receipts, connection } = await fixture(t, {
    relay_source_sha256: '0'.repeat(64),
  });
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID }), /relay source/i);
});

test('a receipt for changed provider-visible answer text is rejected', async (t) => {
  const { root, receipts, connection } = await fixture(t, {
    prompt_sha256: '0'.repeat(64),
  });
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID }), /prompt/i);
});

test('a failed turn observation stops the started run', async (t) => {
  const { root, receipts, connection } = await fixture(t);
  const original = connection.request;
  let stopped;
  connection.request = async (operation, input) => {
    if (operation === 'turn.query') throw new Error('query unavailable');
    if (operation === 'turn.stop') {
      stopped = input;
      return { status: 'cancelled' };
    }
    return original(operation, input);
  };
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID }), /query unavailable/);
  assert.deepEqual(stopped, { sessionId: SESSION_ID, turnId: SESSION_ID, runId: 'run-1' });
});

for (const [name, change] of [
  ['provider model', { observed_model: 'gpt-5.6-sol' }],
  ['outbound effort', { outbound_effort: 'medium' }],
  ['outbound tools', { tool_count: 1 }],
  ['service tier', { observed_service_tier: 'priority' }],
]) {
  test(`unobserved ${name} rejects the ballot`, async (t) => {
    const { root, receipts, connection } = await fixture(t, change);
    await assert.rejects(runJudgeWithHost({
      panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
      expected_boundary_sha256: BOUNDARY,
    }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
      connectionId: 'connection-1', sessionId: () => SESSION_ID }), /receipt/i);
  });
}

test('a changed structural boundary prevents session creation', async (t) => {
  const { root, receipts, calls, connection } = await fixture(t);
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: 'b'.repeat(64),
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID }), /characterization/i);
  assert.equal(calls.length, 0);
});

test('a transcript tool call rejects a ballot even when the relay receipt is clean', async (t) => {
  const { root, receipts, connection } = await fixture(t);
  connection.openSessionSubscription = async () => ({
    loadTranscript: async (decode) => [
      { type: 'assistant', turnId: SESSION_ID, text: '{"winner":"B"}', modelId: 'gpt-6.1-sol' },
      { type: 'tool_call', turnId: SESSION_ID, toolName: 'Bash' },
    ].map(decode),
    close: async () => {},
  });
  await assert.rejects(runJudgeWithHost({
    panel_id: 'interactive', criterion: 'criterion', answer_a: 'A', answer_b: 'B',
    expected_boundary_sha256: BOUNDARY,
  }, { connection, cwd: root, receiptsRoot: receipts, boundarySha256: BOUNDARY, ...FRESHNESS,
    sourceModifiedAtMs: SOURCE_MODIFIED_AT_MS,
    hostEntrypointProbe: async () => ({ startedAtMs: SOURCE_MODIFIED_AT_MS + 1_000 }),
    connectionId: 'connection-1', sessionId: () => SESSION_ID }), /transcript/i);
});

test('source boundary fingerprint changes with the tool profile implementation', async (t) => {
  const { root } = await fixture(t);
  const packageRoot = join(root, 'package');
  await mkdir(packageRoot);
  const launcher = join(root, 'maka');
  await writeFile(launcher, 'managed launcher');
  await writeFile(join(packageRoot, 'package.json'),
    JSON.stringify({ version: '0.2.0-dev.62.20260928' }));
  const sources = [
    'dist/cli.js',
    'node_modules/@maka/runtime-host/dist/server/hosted-execution-tool-profile.js',
    'node_modules/@maka/runtime-host/dist/server/interactive-run-composer.js',
    'node_modules/@maka/runtime/dist/openai-responses-continuation.js',
    'node_modules/@maka/runtime/dist/model-adapter.js',
    'node_modules/@maka/runtime/dist/model-factory.js',
    'node_modules/@maka/runtime-host/dist/server/interactive-turn-coordinator.js',
    'node_modules/@maka/runtime/dist/skill-invocation.js',
    'node_modules/@maka/core/dist/skill-invocation-token.js',
    'node_modules/@ai-sdk/openai/dist/index.js',
    'dist/runtime-host-cli-context.js',
    'dist/workspace-root.js',
  ];
  for (const source of sources) {
    const path = join(packageRoot, source);
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, 'fixture source');
  }
  const first = await sourceBoundaryDigest(packageRoot, launcher);
  assert.match(first, /^[0-9a-f]{64}$/u);
  await writeFile(join(packageRoot, sources[0]), 'changed source');
  assert.notEqual(await sourceBoundaryDigest(packageRoot, launcher), first);
  await writeFile(join(packageRoot, sources[0]), 'fixture source');
  const previouslyOmitted = join(packageRoot, 'node_modules/@maka/core/dist/session.js');
  await writeFile(previouslyOmitted, 'new executed module');
  assert.notEqual(await sourceBoundaryDigest(packageRoot, launcher), first);
  await writeFile(previouslyOmitted, 'original executed module');
  const withModule = await sourceBoundaryDigest(packageRoot, launcher);
  await writeFile(previouslyOmitted, 'changed executed module');
  assert.notEqual(await sourceBoundaryDigest(packageRoot, launcher), withModule);
  await writeFile(previouslyOmitted, 'original executed module');
  await writeFile(launcher, 'changed launcher');
  assert.notEqual(await sourceBoundaryDigest(packageRoot, launcher), withModule);
});

test('source boundary refuses a package that differs from its install receipt', async (t) => {
  const { root } = await fixture(t);
  const packageRoot = join(root, 'package');
  await mkdir(join(packageRoot, 'dist'), { recursive: true });
  await writeFile(join(packageRoot, 'package.json'),
    JSON.stringify({ version: '0.2.0-dev.62.20260928' }));
  await writeFile(join(packageRoot, 'dist/cli.js'), 'fixture cli');
  const launcher = join(root, 'maka');
  await writeFile(launcher, 'fixture launcher');
  const receipt = join(root, 'install-receipt.json');
  await writeFile(receipt, JSON.stringify({
    schema: 'khenrix-maka-install-v2', runtime_package_digest: '0'.repeat(64),
  }), { mode: 0o600 });
  await assert.rejects(sourceBoundaryDigest(packageRoot, launcher, receipt),
    /installed runtime differs/i);
});

test('source boundary changes if the installed mise runtime selection drifts', async (t) => {
  const { root } = await fixture(t);
  const packageRoot = join(root, 'package');
  await mkdir(join(packageRoot, 'dist'), { recursive: true });
  await writeFile(join(packageRoot, 'package.json'),
    JSON.stringify({ version: '0.2.0-dev.62.20260928' }));
  await writeFile(join(packageRoot, 'dist/cli.js'), 'fixture cli');
  const launcher = join(root, 'maka');
  await writeFile(launcher, 'fixture launcher');
  const component = join(root, 'component');
  await mkdir(component);
  await writeFile(join(component, 'mise.toml'), 'node = "24.16.0"\n');
  await writeFile(join(component, 'mise.lock'), 'pinned lock\n');
  const first = await sourceBoundaryDigest(packageRoot, launcher, undefined, component);
  await writeFile(join(component, 'mise.toml'), 'node = "24.17.0"\n');
  assert.notEqual(await sourceBoundaryDigest(packageRoot, launcher, undefined, component), first);
});
