import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { chmod, mkdtemp, mkdir, readFile, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { blindPrompt, boundaryReasonCode, diagnoseAttempts, harborJudgeFailure, hostedJudgeRequest, hostProcessMatches, indeterminateReasonCode,
  judgeEnvelope, readArchivedBallot, readJudgeReceipt, readRuntimeEventBallot,
  runtimeBoundaryDigest } from './harbor_judge.mjs';

const SESSION = 'fanout-judge-0123456789abcdef0123456789abcdef';
const MODEL = 'gpt-6.1-sol';
const RELAY_HASH = 'b'.repeat(64);

test('the Host request explicitly selects the current API model and one bounded step', () => {
  const request = hostedJudgeRequest({
    rootPath: '/tmp/host', workspace: '/tmp/workspace', decoy: 'maka-decoy-abcdefgh',
    proxyUrl: 'http://maka-eval-mitmproxy:8080', sessionId: SESSION, prompt: 'blind prompt',
  });
  assert.deepEqual(request.execution.session.modelTarget, {
    kind: 'explicit', connectionSlug: 'eval-openai', model: 'gpt-6.1-sol',
  });
  assert.equal(request.execution.session.thinkingLevel, 'xhigh');
  assert.equal(request.execution.session.toolProfile, 'workhub-coordination-v1');
  assert.equal(request.execution.maxSteps, 1);
  assert.equal(request.execution.executionId, SESSION);
  assert.deepEqual(request.connection, { providerType: 'openai', apiKey: 'maka-decoy-abcdefgh' });
  assert.equal(request.baseUrl, 'https://api.openai.com/v1');
});

test('failure diagnostics name only a fixed stage, never dynamic error data', () => {
  assert.equal(harborJudgeFailure('archive'),
    'maka harbor judge: execution evidence unavailable (archive)\n');
  assert.equal(harborJudgeFailure('secret-token'),
    'maka harbor judge: execution evidence unavailable (unknown)\n');
  assert.equal(indeterminateReasonCode('Runtime Host connection failed before execution settlement'),
    'transport');
  assert.equal(indeterminateReasonCode('Runtime Host usage did not settle: missing_attempt_usage'),
    'usage-missing');
  assert.equal(indeterminateReasonCode('Runtime Host did not start: secret-token'), 'startup');
  assert.equal(indeterminateReasonCode('secret-token'), 'other');
  assert.equal(harborJudgeFailure('projection-indeterminate', 'transport'),
    'maka harbor judge: execution evidence unavailable (projection-indeterminate:transport)\n');
  assert.equal(harborJudgeFailure('projection-indeterminate', 'secret-token'),
    'maka harbor judge: execution evidence unavailable (projection-indeterminate:other)\n');
  assert.equal(boundaryReasonCode(new Error('Harbor judge install receipt is unsafe')), 'receipt');
  assert.equal(boundaryReasonCode(new Error('Harbor judge runtime tree differs from its manifest')),
    'runtime-tree');
  assert.equal(boundaryReasonCode(Object.assign(new Error('secret-token'), { code: 'EIO' })), 'io');
  assert.equal(boundaryReasonCode(new Error('secret-token')), 'other');
  assert.equal(harborJudgeFailure('source-boundary', 'expected-mismatch'),
    'maka harbor judge: execution evidence unavailable (source-boundary:expected-mismatch)\n');
  assert.equal(harborJudgeFailure('source-boundary', 'secret-token'),
    'maka harbor judge: execution evidence unavailable (source-boundary:other)\n');
});

test('failed Host attempts are summarized for the exact session without dynamic text', async (t) => {
  const root = await fixture(t);
  const path = join(root, 'runtime.sqlite');
  const db = new DatabaseSync(path);
  try {
    db.exec(`CREATE TABLE usage_model_call_attempts (
      session_id TEXT, status TEXT, error_class TEXT, usage_basis TEXT, call_kind TEXT
    )`);
    const insert = db.prepare('INSERT INTO usage_model_call_attempts VALUES (?, ?, ?, ?, ?)');
    insert.run(SESSION, 'failed', 'network', 'missing', 'main');
    insert.run('other-session', 'secret-status', 'secret-error', 'secret-basis', 'secret-kind');
    assert.deepEqual(diagnoseAttempts(path, SESSION), {
      state: 'ok', count: 1, statuses: ['failed'], error_classes: ['network'],
      usage_bases: ['missing'], call_kinds: ['main'],
    });
    insert.run(SESSION, 'secret-status', 'secret-error', 'secret-basis', 'secret-kind');
    const summary = diagnoseAttempts(path, SESSION);
    assert.deepEqual(summary, {
      state: 'ok', count: 2, statuses: ['failed', 'other'],
      error_classes: ['network', 'other'], usage_bases: ['missing', 'other'],
      call_kinds: ['main', 'other'],
    });
    assert.equal(JSON.stringify(summary).includes('secret'), false);
  } finally {
    db.close();
  }
  assert.deepEqual(diagnoseAttempts(join(root, 'missing.sqlite'), SESSION), { state: 'unavailable' });
});

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'maka-harbor-judge-test-'));
  t.after(async () => {
    const { rm } = await import('node:fs/promises');
    await rm(root, { recursive: true, force: true });
  });
  return root;
}

function archive(path, records) {
  const db = new DatabaseSync(path);
  try {
    db.exec('CREATE TABLE session_messages (session_id TEXT, sequence INTEGER, message_type TEXT, record_json TEXT)');
    db.exec('CREATE TABLE session_message_payloads (session_id TEXT, sequence INTEGER, record_bytes INTEGER, sha256 TEXT)');
    db.exec('CREATE TABLE session_message_chunks (session_id TEXT, sequence INTEGER, chunk_index INTEGER, data BLOB, sha256 TEXT)');
    const insert = db.prepare('INSERT INTO session_messages VALUES (?, ?, ?, ?)');
    const payload = db.prepare('INSERT INTO session_message_payloads VALUES (?, ?, ?, ?)');
    const chunk = db.prepare('INSERT INTO session_message_chunks VALUES (?, ?, ?, ?, ?)');
    records.forEach((record, index) => {
      const encoded = Buffer.from(JSON.stringify(record));
      if (encoded.length <= 65536) {
        insert.run(SESSION, index, record.type, encoded.toString());
        return;
      }
      insert.run(SESSION, index, record.type, '{"$maka":"session-message-chunks-v1"}');
      payload.run(SESSION, index, encoded.length, createHash('sha256').update(encoded).digest('hex'));
      for (let start = 0; start < encoded.length; start += 65536) {
        const data = encoded.subarray(start, start + 65536);
        chunk.run(SESSION, index, start / 65536, data,
          createHash('sha256').update(data).digest('hex'));
      }
    });
  } finally {
    db.close();
  }
}

test('the Harbor prompt is blind, bounded, and carries a fresh receipt nonce', () => {
  const prompt = blindPrompt({
    panel_id: 'harbor', criterion: 'Which is correct?', answer_a: '4', answer_b: '5',
    expected_boundary_sha256: 'a'.repeat(64),
  }, SESSION);
  assert.equal(prompt.split('\n')[0], `Judge receipt nonce: maka:${SESSION}`);
  assert.match(prompt, /Answer A: "4"/u);
  assert.match(prompt, /Answer B: "5"/u);
  assert.equal(prompt.includes('baseline'), false);
});

test('a private Host archive yields exactly one model-bound tool-free ballot', async (t) => {
  const root = await fixture(t);
  const prompt = `Judge receipt nonce: maka:${SESSION}\nCompare A and B.`;
  const path = join(root, 'runtime.sqlite');
  archive(path, [
    { type: 'user', turnId: SESSION, text: prompt },
    { type: 'assistant', turnId: SESSION, text: '{"winner":"B"}', modelId: MODEL },
  ]);
  const ballot = readArchivedBallot(path, SESSION, prompt);
  assert.equal(ballot.winner, 'B');
  assert.match(ballot.transcript_sha256, /^[0-9a-f]{64}$/u);
  assert.equal(ballot.response_answer_sha256,
    createHash('sha256').update('{"winner":"B"}').digest('hex'));
  for (const text of ['{"winner":"A","winner":"B"}', '{"winner":"A","\\u0077inner":"B"}']) {
    const duplicatePath = join(root, `duplicate-${createHash('sha256').update(text).digest('hex')}.sqlite`);
    archive(duplicatePath, [
      { type: 'user', turnId: SESSION, text: prompt },
      { type: 'assistant', turnId: SESSION, text, modelId: MODEL },
    ]);
    assert.throws(() => readArchivedBallot(duplicatePath, SESSION, prompt), /structured ballot/u);
  }
  archive(join(root, 'old.sqlite'), [
    { type: 'user', turnId: SESSION, text: prompt },
    { type: 'assistant', turnId: SESSION, text: '{"winner":"B"}', modelId: 'gpt-5.6-sol' },
  ]);
  assert.throws(() => readArchivedBallot(join(root, 'old.sqlite'), SESSION, prompt), /transcript/i);
});

test('a fresh hosted RuntimeEvent ledger attests a ballot without legacy session messages', async (t) => {
  const root = await fixture(t);
  const path = join(root, 'runtime.sqlite');
  const prompt = `Judge receipt nonce: maka:${SESSION}\nCompare A and B.`;
  const db = new DatabaseSync(path);
  try {
    db.exec(`CREATE TABLE runtime_events (
      event_id TEXT, session_id TEXT, invocation_id TEXT, run_id TEXT,
      turn_id TEXT, event_seq INTEGER, payload_json TEXT
    )`);
    const insert = db.prepare('INSERT INTO runtime_events VALUES (?, ?, ?, ?, ?, ?, ?)');
    const records = [
      { id: 'open', content: { kind: 'invocation_opened', route: { modelId: MODEL },
        root: { kind: 'user' }, source: { kind: 'fresh' } },
        actions: { runtimeProtocol: { toolBoundary: 't1_after_preflight_v1' } } },
      { id: 'user', role: 'user', content: { kind: 'text', text: prompt } },
      { id: 'answer', role: 'model', content: { kind: 'text', text: '{"winner":"B"}' } },
      { id: 'usage', role: 'system', actions: { tokenUsage: { input: 10, output: 3 } } },
      { id: 'terminal', role: 'system', status: 'completed',
        actions: { endInvocation: true, stateDelta: { stopReason: 'end_turn' } } },
    ];
    records.forEach((record, index) => insert.run(record.id, SESSION, 'invocation',
      'run', SESSION, index + 1, JSON.stringify({ sessionId: SESSION,
        invocationId: 'invocation', runId: 'run', turnId: SESSION, ...record })));
  } finally {
    db.close();
  }
  const adapter = {
    decodeEvent: (event) => event,
    projectEvents: (events) => ({ diagnostics: [], messages: [
      { type: 'user', turnId: SESSION, text: events[1].content.text },
      { type: 'assistant', turnId: SESSION, modelId: MODEL, text: events[2].content.text },
      { type: 'token_usage', turnId: SESSION, input: 10, output: 3 },
      { type: 'turn_state', turnId: SESSION, status: 'completed' },
    ] }),
  };
  const ballot = readRuntimeEventBallot(path, SESSION, prompt, adapter);
  assert.equal(ballot.winner, 'B');
  assert.equal(ballot.response_answer_sha256,
    createHash('sha256').update('{"winner":"B"}').digest('hex'));
  assert.throws(() => readRuntimeEventBallot(path, SESSION, prompt, {
    ...adapter,
    projectEvents: (events) => ({ diagnostics: [], messages:
      adapter.projectEvents(events).messages.map((message) => message.type === 'assistant' ?
        { ...message, text: '{"winner":"A","winner":"B"}' } : message) }),
  }), /structured ballot/u);
  assert.match(ballot.transcript_sha256, /^[0-9a-f]{64}$/u);
  assert.throws(() => readRuntimeEventBallot(path, SESSION, prompt, {
    ...adapter,
    projectEvents: (events) => ({ diagnostics: [], messages:
      adapter.projectEvents(events).messages.filter((message) => message.type !== 'token_usage') }),
  }), /attest one answer/u);
  const tampered = new DatabaseSync(path);
  try {
    tampered.prepare('UPDATE runtime_events SET payload_json = ? WHERE event_id = ?').run(
      JSON.stringify({ sessionId: SESSION, invocationId: 'invocation', runId: 'run',
        turnId: SESSION, id: 'usage', actions: { tokenUsage: { input: 10, output: 3 },
          toolDispatch: { hidden: true } } }), 'usage');
  } finally {
    tampered.close();
  }
  assert.throws(() => readRuntimeEventBallot(path, SESSION, prompt, adapter),
    /unsupported action/u);
  const silentlyClaimed = [
    'userQuestionRequest', 'formRequest', 'managedMutationTerminal',
    'artifactDelta', 'permissionClosureAccepted',
  ];
  for (const action of silentlyClaimed) {
    const tamperedAction = new DatabaseSync(path);
    try {
      tamperedAction.prepare('UPDATE runtime_events SET payload_json = ? WHERE event_id = ?').run(
        JSON.stringify({ sessionId: SESSION, invocationId: 'invocation', runId: 'run',
          turnId: SESSION, id: 'usage', role: 'system',
          actions: { tokenUsage: { input: 10, output: 3 }, [action]: { hidden: true } } }),
        'usage');
    } finally {
      tamperedAction.close();
    }
    assert.throws(() => readRuntimeEventBallot(path, SESSION, prompt, adapter),
      /unsupported action/u, action);
  }
});

test('the pinned RuntimeEvent decoder and projector attest only canonical no-tool evidence', async (t) => {
  const runtimeRoot = process.env.KHENRIX_MAKA_RUNTIME_ROOT;
  const installedPackage = process.env.KHENRIX_MAKA_PACKAGE_ROOT ??
    (runtimeRoot === undefined ? undefined : join(runtimeRoot, 'maka-agent'));
  if (installedPackage === undefined) return t.skip('set KHENRIX_MAKA_PACKAGE_ROOT or KHENRIX_MAKA_RUNTIME_ROOT to the installed pinned runtime');
  const packageRoot = join(installedPackage, 'node_modules', '@maka');
  const core = await import(pathToFileURL(join(packageRoot, 'core/dist/runtime-event.js')).href);
  const readModel = await import(pathToFileURL(join(packageRoot,
    'runtime/dist/runtime-event-read-model.js')).href);
  const root = await fixture(t);
  const path = join(root, 'runtime.sqlite');
  const prompt = `Judge receipt nonce: maka:${SESSION}\nCompare A and B.`;
  const opening = {
    kind: 'invocation_opened', protocol: 'invocation_opened_v1',
    route: { provenance: 'runtime', backendKind: 'ai-sdk',
      llmConnectionId: 'eval-openai', llmConnectionSlug: 'eval-openai', modelId: MODEL },
    configuration: { cwd: root, permissionMode: 'ask', collaborationMode: 'agent',
      orchestrationMode: 'default', orchestrationSource: 'session', toolMode: 'direct' },
    root: { kind: 'user' }, source: { kind: 'fresh' },
  };
  const base = (id, role, author, ts) => ({
    id, sessionId: SESSION, invocationId: 'invocation', runId: 'run',
    turnId: SESSION, ts, partial: false, role, author,
  });
  const records = [
    { ...base('open', 'system', 'system', 1), modelVisibility: 'hidden',
      content: opening, actions: { runtimeProtocol: { toolBoundary: 't1_after_preflight_v1' } } },
    { ...base('user', 'user', 'user', 2), content: { kind: 'text', text: prompt } },
    { ...base('answer', 'model', 'agent', 3),
      content: { kind: 'text', text: '{"winner":"B"}' } },
    { ...base('usage', 'system', 'system', 4),
      actions: { tokenUsage: { input: 10, output: 3 } } },
    { ...base('terminal', 'system', 'system', 5), status: 'completed',
      actions: { endInvocation: true, stateDelta: { stopReason: 'end_turn' } } },
  ];
  const db = new DatabaseSync(path);
  try {
    db.exec(`CREATE TABLE runtime_events (
      event_id TEXT, session_id TEXT, invocation_id TEXT, run_id TEXT,
      turn_id TEXT, event_seq INTEGER, payload_json TEXT
    )`);
    const insert = db.prepare('INSERT INTO runtime_events VALUES (?, ?, ?, ?, ?, ?, ?)');
    records.forEach((record, index) => {
      core.decodeRuntimeEvent(record);
      insert.run(record.id, SESSION, 'invocation', 'run', SESSION, index + 1,
        JSON.stringify(record));
    });
  } finally {
    db.close();
  }
  const adapter = {
    decodeEvent: core.decodeRuntimeEvent,
    projectEvents: (events, invocations) =>
      readModel.projectRuntimeEventsToStoredMessages(events, { invocations }),
  };
  assert.equal(readRuntimeEventBallot(path, SESSION, prompt, adapter).winner, 'B');
  const partial = core.decodeRuntimeEvent({ ...records[2], id: 'answer-partial',
    ts: 2.5, partial: true, content: { kind: 'text', text: '{"winner"' } });
  const withStreamDelta = adapter.projectEvents([
    ...records.slice(0, 2), partial, ...records.slice(2),
  ], [{ sessionId: SESSION, invocationId: 'invocation', runId: 'run',
    turnId: SESSION, opening }]);
  assert.deepEqual(withStreamDelta.diagnostics.map((diagnostic) => diagnostic.code),
    ['partial_skipped']);
  for (const content of [
    { ...opening, route: { ...opening.route, modelId: 'gpt-5.6-sol' } },
    { ...opening, root: { kind: 'scheduled_task', scheduledTaskId: 'task-id' } },
    { ...opening, source: { kind: 'continuation', sourceInvocationId: 'prior-invocation',
      sourceRunId: 'prior-run', sourceTurnId: 'prior-turn', sourceRuntimeEventHighWater: 1 } },
  ]) {
    const changedOpening = { ...records[0], content };
    core.decodeRuntimeEvent(changedOpening);
    const changed = new DatabaseSync(path);
    try {
      changed.prepare('UPDATE runtime_events SET payload_json = ? WHERE event_id = ?')
        .run(JSON.stringify(changedOpening), 'open');
    } finally {
      changed.close();
    }
    assert.throws(() => readRuntimeEventBallot(path, SESSION, prompt, adapter),
      /did not attest one fresh user run/u);
  }
  const tampered = new DatabaseSync(path);
  try {
    tampered.prepare('UPDATE runtime_events SET payload_json = ? WHERE event_id = ?')
      .run(JSON.stringify(records[0]), 'open');
    const hiddenAction = { ...records[3], actions: {
      tokenUsage: { input: 10, output: 3 }, artifactDelta: { count: 1 },
    } };
    core.decodeRuntimeEvent(hiddenAction);
    tampered.prepare('UPDATE runtime_events SET payload_json = ? WHERE event_id = ?')
      .run(JSON.stringify(hiddenAction), 'usage');
  } finally {
    tampered.close();
  }
  assert.throws(() => readRuntimeEventBallot(path, SESSION, prompt, adapter),
    /unsupported action/u);
});

test('a long escaped prompt is read from pinned hashed session message chunks', async (t) => {
  const root = await fixture(t);
  const prompt = `Judge receipt nonce: maka:${SESSION}\n${'\\'.repeat(40_000)}`;
  const path = join(root, 'runtime.sqlite');
  archive(path, [
    { type: 'user', turnId: SESSION, text: prompt },
    { type: 'assistant', turnId: SESSION, text: '{"winner":"A"}', modelId: MODEL },
  ]);
  assert.equal(readArchivedBallot(path, SESSION, prompt).winner, 'A');
  const db = new DatabaseSync(path);
  try {
    db.prepare('UPDATE session_message_chunks SET chunk_index = 3 WHERE session_id = ? AND sequence = 0 AND chunk_index = 1')
      .run(SESSION);
  } finally {
    db.close();
  }
  assert.throws(() => readArchivedBallot(path, SESSION, prompt), /transcript/i);
  const hashDb = new DatabaseSync(path);
  try {
    hashDb.prepare('UPDATE session_message_chunks SET chunk_index = 1, sha256 = ? WHERE session_id = ? AND sequence = 0 AND chunk_index = 3')
      .run('0'.repeat(64), SESSION);
  } finally {
    hashDb.close();
  }
  assert.throws(() => readArchivedBallot(path, SESSION, prompt), /transcript/i);
  const lengthDb = new DatabaseSync(path);
  try {
    const data = lengthDb.prepare('SELECT data FROM session_message_chunks WHERE session_id = ? AND sequence = 0 AND chunk_index = 1')
      .get(SESSION).data;
    lengthDb.prepare('UPDATE session_message_chunks SET sha256 = ? WHERE session_id = ? AND sequence = 0 AND chunk_index = 1')
      .run(createHash('sha256').update(data).digest('hex'), SESSION);
    lengthDb.prepare('UPDATE session_message_payloads SET record_bytes = record_bytes + 1 WHERE session_id = ? AND sequence = 0')
      .run(SESSION);
  } finally {
    lengthDb.close();
  }
  assert.throws(() => readArchivedBallot(path, SESSION, prompt), /transcript/i);
});

test('a Host archive with any tool call or ambiguous answer cannot attest a ballot', async (t) => {
  const root = await fixture(t);
  const prompt = `Judge receipt nonce: maka:${SESSION}\nCompare A and B.`;
  const withTool = join(root, 'with-tool.sqlite');
  archive(withTool, [
    { type: 'user', turnId: SESSION, text: prompt },
    { type: 'tool_call', turnId: SESSION, toolName: 'Read' },
    { type: 'assistant', turnId: SESSION, text: '{"winner":"B"}', modelId: MODEL },
  ]);
  assert.throws(() => readArchivedBallot(withTool, SESSION, prompt), /transcript/i);
  const duplicate = join(root, 'duplicate.sqlite');
  archive(duplicate, [
    { type: 'user', turnId: SESSION, text: prompt },
    { type: 'assistant', turnId: SESSION, text: '{"winner":"B"}', modelId: MODEL },
    { type: 'assistant', turnId: SESSION, text: '{"winner":"A"}', modelId: MODEL },
  ]);
  assert.throws(() => readArchivedBallot(duplicate, SESSION, prompt), /transcript/i);
});

test('a Harbor receipt must match this nonce, prompt, source, model, effort, and zero tools', async (t) => {
  const root = await fixture(t);
  await mkdir(join(root, 'relay-judge-receipts'), { mode: 0o700 });
  const prompt = `Judge receipt nonce: maka:${SESSION}\nCompare A and B.`;
  const digest = createHash('sha256').update(`maka:${SESSION}`).digest('hex');
  const receipt = {
    schema: 'khenrix-maka-harbor-judge-request-v2',
    time_utc_ms: 1_000_000,
    request_marker_sha256: digest,
    prompt_sha256: createHash('sha256').update(prompt).digest('hex'),
    requested_model: MODEL,
    observed_model: MODEL,
    requested_service_tier: 'default',
    observed_service_tier: 'default',
    outbound_effort: 'xhigh',
    tool_count: 0,
    http_status: 200,
    relay_source_sha256: RELAY_HASH,
    response_answer_sha256: createHash('sha256').update('{"winner":"B"}').digest('hex'),
  };
  const path = join(root, 'relay-judge-receipts', `${digest}-${'1'.repeat(32)}.json`);
  await writeFile(path, JSON.stringify(receipt), { mode: 0o600 });
  assert.equal((await readJudgeReceipt(join(root, 'relay-judge-receipts'), SESSION,
    prompt, RELAY_HASH)).observed_model, MODEL);
  const ballot = { winner: 'B', transcript_sha256: 'c'.repeat(64),
    response_answer_sha256: receipt.response_answer_sha256 };
  assert.equal(judgeEnvelope(ballot, receipt, 'd'.repeat(64)).result.winner, 'B');
  assert.throws(() => judgeEnvelope(ballot,
    { ...receipt, response_answer_sha256: 'f'.repeat(64) }, 'd'.repeat(64)), /answer/u);
  for (const field of ['requested_model', 'observed_model']) {
    await writeFile(path, JSON.stringify({ ...receipt, [field]: 'gpt-5.6-sol' }), { mode: 0o600 });
    await assert.rejects(readJudgeReceipt(join(root, 'relay-judge-receipts'), SESSION,
      prompt, RELAY_HASH), /receipt/i);
  }
  await writeFile(path, JSON.stringify({ ...receipt, tool_count: 1 }), { mode: 0o600 });
  await assert.rejects(readJudgeReceipt(join(root, 'relay-judge-receipts'), SESSION,
    prompt, RELAY_HASH), /receipt/i);
});

test('the actual egress receipt binds the native ballot through the flat shell gate', async (t) => {
  const root = await fixture(t);
  const prompt = `Judge receipt nonce: maka:${SESSION}\nCompare A and B.`;
  const answer = '{"winner":"B"}';
  const controller = dirname(fileURLToPath(import.meta.url));
  const producer = `
import importlib.util,json,pathlib,sys
path=pathlib.Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('egress_filter',path)
policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
raw=json.dumps({'model':'gpt-6.1-sol','reasoning':{'effort':'xhigh','summary':'auto'},
 'input':[{'role':'user','content':[{'type':'input_text','text':sys.argv[3]}]}],
 'tools':[{'type':'function','name':'Skill'}]}).encode()
outbound,evidence=policy.prepare_judge_request(raw)
assert json.loads(outbound)['tool_choice']=='none' and 'tools' not in json.loads(outbound)
created={'type':'response.created','response':{'id':'response','model':'gpt-6.1-sol','service_tier':'default'}}
completed={'type':'response.completed','response':{'id':'response','model':'gpt-6.1-sol',
 'service_tier':'default','status':'completed','output':[{'type':'message','id':'message',
 'role':'assistant','status':'completed','content':[{'type':'output_text','text':sys.argv[4]}]}]}}
sse=('data:'+json.dumps(created)+'\\n\\ndata: '+json.dumps(completed)+'\\n\\ndata:[DONE]\\n\\n').encode()
observed=policy.judge_response_evidence(200,sse)
assert observed is not None
policy.persist_judge_receipt(pathlib.Path(sys.argv[2]),{**evidence,**observed,'http_status':200})
print(policy.EGRESS_SOURCE_SHA256)
`;
  const receiptRoot = join(root, 'relay-judge-receipts');
  const sourceHash = execFileSync('python3', ['-c', producer,
    join(controller, 'egress-overlay/egress_filter.py'), receiptRoot, prompt, answer],
  {encoding: 'utf8', timeout: 5000}).trim();
  const receipt = await readJudgeReceipt(receiptRoot, SESSION, prompt, sourceHash);
  const archivePath = join(root, 'runtime.sqlite');
  archive(archivePath, [
    {type: 'user', turnId: SESSION, text: prompt},
    {type: 'assistant', turnId: SESSION, text: answer, modelId: MODEL},
  ]);
  const ballot = readArchivedBallot(archivePath, SESSION, prompt);
  const boundary = 'a'.repeat(64);
  const envelope = judgeEnvelope(ballot, receipt, boundary);
  const envelopePath = join(root, 'envelope.json');
  const resultPath = join(root, 'result.json');
  const evidencePath = join(root, 'evidence.json');
  await writeFile(envelopePath, JSON.stringify(envelope), {mode: 0o600});
  const shellSource = await readFile(join(controller, 'run-harbor-judge-inside.sh'), 'utf8');
  const shell = shellSource.slice(shellSource.indexOf('expected_boundary=$MAKA_JUDGE_EXPECTED_BOUNDARY\n'));
  const stdout = execFileSync('/bin/bash', ['-c',
    'set -eu\nenvelope_file=$1; result_file=$2; judge_evidence_file=$3\n' + shell,
    'bash', envelopePath, resultPath, evidencePath], {encoding: 'utf8', timeout: 5000,
    env: {...process.env, MAKA_JUDGE_EXPECTED_BOUNDARY: boundary}});
  assert.deepEqual(JSON.parse(stdout), envelope.result);
  assert.equal(Object.keys(JSON.parse(stdout)).length, 7);
  assert.deepEqual(JSON.parse(await readFile(evidencePath, 'utf8')), envelope.evidence);
  await assert.rejects(readFile(envelopePath), {code: 'ENOENT'});
  assert.throws(() => judgeEnvelope(ballot,
    {...receipt, response_answer_sha256: 'f'.repeat(64)}, boundary), /answer/u);
  await writeFile(envelopePath, JSON.stringify({...envelope, evidence: {
    ...envelope.evidence, relay_receipt: {...receipt, response_answer_sha256: undefined},
  }}));
  assert.throws(() => execFileSync('/bin/bash', ['-c',
    'set -eu\nenvelope_file=$1; result_file=$2; judge_evidence_file=$3\n' + shell,
    'bash', envelopePath, resultPath, evidencePath], {timeout: 5000,
    env: {...process.env, MAKA_JUDGE_EXPECTED_BOUNDARY: boundary}, stdio: 'pipe'}));
});

test('the read-only boundary pins installed source receipt and the entire staged runtime tree', async (t) => {
  const root = await fixture(t);
  const component = join(root, 'maka');
  const runtime = join(component, 'runtime/linux-amd64',
    'maka-0.2.0-dev.62.20260928-node-24.16.0-relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1-undici-h2-close-v2');
  const sourceComponent = dirname(dirname(fileURLToPath(import.meta.url)));
  const packageRoot = process.env.KHENRIX_MAKA_PACKAGE_ROOT ?? resolve(dirname(execFileSync(
    'mise', ['-C', sourceComponent, 'which', 'maka'], { encoding: 'utf8' },
  ).trim()), '..', 'maka-agent');
  const undiciPath = 'node_modules/@maka/runtime/node_modules/undici/lib/dispatcher/client-h2.js';
  const originalH2 = await readFile(join(packageRoot, undiciPath), 'utf8');
  assert.equal(createHash('sha256').update(originalH2).digest('hex'),
    '611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7');
  const stagedPackage = join(root, 'staged-package');
  const stagedH2 = join(stagedPackage, undiciPath);
  await mkdir(dirname(stagedH2), {recursive: true});
  await writeFile(stagedH2, originalH2);
  execFileSync('python', ['-B', join(sourceComponent, 'controller/apply_undici_h2_close_compat.py'),
    stagedPackage], {timeout: 5000});
  const patchedH2 = await readFile(stagedH2, 'utf8');
  assert.equal(createHash('sha256').update(patchedH2).digest('hex'),
    '32c2b747c1c1c9c7405e60dc70145f67f9b8f96db0b6264f55e1f0400a375dc4');
  const sourceFiles = new Map([
    ['controller/harbor_judge.mjs', 'runner'],
    ['controller/egress-overlay/egress_filter.py', 'relay'],
    ['mise.toml', 'node = "24.16.0"'],
    ['mise.lock', 'locked'],
  ]);
  sourceFiles.set('install-files.txt', `${[...sourceFiles.keys(), 'install-files.txt'].join('\n')}\n`);
  const sourceHash = createHash('sha256');
  for (const [name, content] of sourceFiles) {
    const target = join(component, name);
    await mkdir(join(target, '..'), { recursive: true });
    await writeFile(target, content);
    sourceHash.update(name).update('\0').update(content).update('\0');
  }
  const runtimeFiles = new Map([
    ['maka-agent/package.json', '{"version":"0.2.0-dev.62.20260928"}'],
    ['maka-agent/dist/cli.js', 'cli'],
    ['maka-agent/node_modules/@maka/runtime-host/dist/client/hosted-execution.js', 'host'],
    ['node/bin/node', 'node'],
    [`maka-agent/${undiciPath}`, patchedH2],
    ['.complete', `maka=0.2.0-dev.62.20260928
node=24.16.0
compatibility-overlay-bundle=relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1-undici-h2-close-v2
compatibility-overlays=relay-root-virtiofs-v2,eval-openai-onboarding-v2,harbor-judge-sol61-v1,undici-h2-close-v2
relay-agent-sha256=6214ed402d2d8345b20b625739b9a08905116bfdd944775938d5d78054033621
model-fetcher-sha256=7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5
maka-subject-sha256=f47c1278ed25434f2a1a012d26ed285eb930e737acbc182cbdcda6b914887775
model-metadata-sha256=9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7
undici-h2-base-sha256=611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7
undici-h2-sha256=32c2b747c1c1c9c7405e60dc70145f67f9b8f96db0b6264f55e1f0400a375dc4
`],
  ]);
  for (let index = 0; index < 512; index++) {
    runtimeFiles.set(`maka-agent/bulk/file-${index}.txt`, `file-${index}`);
  }
  let manifest = '';
  for (const [name, content] of runtimeFiles) {
    const target = join(runtime, name);
    await mkdir(join(target, '..'), { recursive: true });
    await writeFile(target, content);
    await chmod(target, 0o444);
    manifest += `${createHash('sha256').update(content).digest('hex')}  ./${name}\n`;
  }
  await mkdir(join(runtime, 'empty/nested'), { recursive: true });
  const entrypoint = await readFile(join(sourceComponent, 'controller/entrypoint.sh'), 'utf8');
  const stagingFragment = entrypoint.slice(entrypoint.indexOf('  printf \'%s\\n\' "$expected_complete"'),
    entrypoint.indexOf('  (\n    cd "$staging"'));
  await chmod(join(runtime, '.complete'), 0o644);
  execFileSync('/bin/bash', ['-c', `set -eu\n${stagingFragment}`], {
    env: { ...process.env, staging: runtime, expected_complete: runtimeFiles.get('.complete').trimEnd() },
  });
  await chmod(join(runtime, '.complete'), 0o444);
  await assert.rejects(readFile(join(runtime, 'empty/nested')), { code: 'ENOENT' });
  manifest = execFileSync('python', [join(sourceComponent, 'controller/runtime_manifest_verify.py'),
    runtime, execFileSync('which', ['sha256sum'], { encoding: 'utf8' }).trim()], { encoding: 'utf8' });
  await writeFile(join(runtime, 'RUNTIME_SHA256SUMS'), manifest, { mode: 0o444 });
  await writeFile(join(runtime, 'RUNTIME_SYMLINKS'), '', { mode: 0o444 });
  const receiptPath = join(root, 'install-receipt.json');
  const receipt = {
    schema: 'khenrix-maka-install-v2', component,
    source_digest: sourceHash.digest('hex'),
    runtime_package_digest: 'a'.repeat(64),
  };
  await writeFile(receiptPath, JSON.stringify(receipt), { mode: 0o600 });
  const started = performance.now();
  const boundary = await runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` });
  if (process.env.MAKA_BOUNDARY_BENCHMARK === '1') {
    t.diagnostic(`${runtimeFiles.size}-file boundary: ${(performance.now() - started).toFixed(1)} ms`);
  }
  assert.match(boundary, /^[0-9a-f]{64}$/u);
  const oldJudgeRuntime = runtime.replace('-undici-h2-close-v2', '');
  await assert.rejects(runtimeBoundaryDigest(oldJudgeRuntime, join(oldJudgeRuntime, 'maka-agent'),
    component, receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }),
  /runtime path/u);
  const h2RuntimeFile = join(runtime, `maka-agent/${undiciPath}`);
  await chmod(h2RuntimeFile, 0o644);
  await writeFile(h2RuntimeFile, originalH2);
  await chmod(h2RuntimeFile, 0o444);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SHA256SUMS'), manifest.replace(
    '32c2b747c1c1c9c7405e60dc70145f67f9b8f96db0b6264f55e1f0400a375dc4',
    '611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7'));
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o444);
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }),
  /runtime manifest/u);
  await chmod(h2RuntimeFile, 0o644);
  await writeFile(h2RuntimeFile, patchedH2);
  await chmod(h2RuntimeFile, 0o444);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SHA256SUMS'), manifest);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o444);
  const oldCompletion = runtimeFiles.get('.complete')
    .replace('-harbor-judge-sol61-v1', '').replace(',harbor-judge-sol61-v1', '')
    .replace('7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5',
      '427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7')
    .replace('model-metadata-sha256=9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7\n', '');
  await chmod(join(runtime, '.complete'), 0o644);
  await writeFile(join(runtime, '.complete'), oldCompletion);
  await chmod(join(runtime, '.complete'), 0o444);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SHA256SUMS'), manifest.replace(
    createHash('sha256').update(runtimeFiles.get('.complete')).digest('hex') + '  ./.complete',
    createHash('sha256').update(oldCompletion).digest('hex') + '  ./.complete'));
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o444);
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }),
  /completion marker/u);
  await chmod(join(runtime, '.complete'), 0o644);
  await writeFile(join(runtime, '.complete'), runtimeFiles.get('.complete'));
  await chmod(join(runtime, '.complete'), 0o444);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SHA256SUMS'), manifest);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o444);
  assert.equal(await runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), boundary);
  const swapped = await runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'c'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` });
  assert.notEqual(swapped, boundary);
  const swappedEgress = await runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'d'.repeat(64)}` });
  assert.notEqual(swappedEgress, boundary);
  await writeFile(receiptPath, JSON.stringify({ ...receipt,
    runtime_package_digest: 'c'.repeat(64) }), { mode: 0o600 });
  const swappedReceipt = await runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` });
  assert.notEqual(swappedReceipt, boundary);
  await writeFile(receiptPath, JSON.stringify(receipt), { mode: 0o600 });
  await writeFile(join(runtime, 'unmanifested.txt'), 'injected');
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /runtime tree/i);
  const { rm } = await import('node:fs/promises');
  await rm(join(runtime, 'unmanifested.txt'));
  await mkdir(join(runtime, 'unmanifested-directory'));
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /runtime tree/i);
  await rm(join(runtime, 'unmanifested-directory'), { recursive: true });
  await symlink('/etc/passwd', join(runtime, 'escape'));
  await chmod(join(runtime, 'RUNTIME_SYMLINKS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SYMLINKS'), './escape -> /etc/passwd\n');
  await chmod(join(runtime, 'RUNTIME_SYMLINKS'), 0o444);
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /symlink escapes/i);
  await rm(join(runtime, 'escape'));
  await chmod(join(runtime, 'RUNTIME_SYMLINKS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SYMLINKS'), '');
  await chmod(join(runtime, 'RUNTIME_SYMLINKS'), 0o444);
  await chmod(join(runtime, 'maka-agent/dist/cli.js'), 0o644);
  await writeFile(join(runtime, 'maka-agent/dist/cli.js'), 'changed');
  await chmod(join(runtime, 'maka-agent/dist/cli.js'), 0o444);
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /runtime differs/i);
  await chmod(join(runtime, 'maka-agent/dist/cli.js'), 0o644);
  await writeFile(join(runtime, 'maka-agent/dist/cli.js'), 'cli');
  await chmod(join(runtime, 'maka-agent/dist/cli.js'), 0o444);
  const outside = join(root, 'outside-cli');
  await writeFile(outside, 'cli');
  await rm(join(runtime, 'maka-agent/dist/cli.js'));
  await symlink(outside, join(runtime, 'maka-agent/dist/cli.js'));
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /runtime file is unsafe/i);
  await rm(join(runtime, 'maka-agent/dist/cli.js'));
  await writeFile(join(runtime, 'maka-agent/dist/cli.js'), 'cli');
  await chmod(join(runtime, 'maka-agent/dist/cli.js'), 0o444);
  await writeFile(join(component, 'mise.lock'), 'changed');
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /installed source/i);
});

test('the connected Host must be a fresh staged Node process at the pinned candidate entrypoint', () => {
  const expected = {
    nodePath: '/stage/node/bin/node',
    candidatePath: '/stage/maka-agent/node_modules/@maka/runtime-host/dist/execution-candidate-main.js',
    rootPath: '/tmp/fresh/host',
    clientStartTicks: 100,
  };
  const observed = {
    executable: expected.nodePath,
    argv: [expected.nodePath, expected.candidatePath, '--root', expected.rootPath],
    startTicksBefore: 101,
    startTicksAfter: 101,
  };
  assert.equal(hostProcessMatches(observed, expected), true);
  assert.equal(hostProcessMatches({ ...observed, executable: '/usr/bin/node' }, expected), false);
  assert.equal(hostProcessMatches({ ...observed, argv: [expected.nodePath,
    '/other/entrypoint.js', '--root', expected.rootPath] }, expected), false);
  assert.equal(hostProcessMatches({ ...observed, startTicksBefore: 99,
    startTicksAfter: 99 }, expected), false);
  assert.equal(hostProcessMatches({ ...observed, startTicksAfter: 102 }, expected), false);
});

test('a returned judge envelope contains hash-only evidence, never answer or transcript text', () => {
  const relayReceipt = { request_marker_sha256: 'a'.repeat(64),
    prompt_sha256: 'b'.repeat(64), observed_model: MODEL, outbound_effort: 'xhigh',
    response_answer_sha256: 'e'.repeat(64) };
  const envelope = judgeEnvelope({ winner: 'A', transcript_sha256: 'c'.repeat(64),
    response_answer_sha256: 'e'.repeat(64) },
    relayReceipt, 'd'.repeat(64));
  assert.equal(envelope.result.winner, 'A');
  assert.equal(envelope.evidence.transcript_sha256, 'c'.repeat(64));
  assert.deepEqual(envelope.evidence.relay_receipt, relayReceipt);
  assert.equal(JSON.stringify(envelope).includes('secret answer'), false);
});
