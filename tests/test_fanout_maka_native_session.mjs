import {test} from 'node:test';
import assert from 'node:assert/strict';
import {prepareSession, captureSession} from '../shared/lib/fanout/maka_native_session.mjs';
import * as nativeSession from '../shared/lib/fanout/maka_native_session.mjs';
import {createHash} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';

const input = {session_id: 'fanout-eval-123', cwd: '/task', connection: 'keychain-openai', auth_route: 'api-key-relay',
  model: 'gpt-6.1-sol', workspace_root: '/native', host: {pid: 12, hostEpoch: 'epoch'}};
const session = {id: input.session_id, model: input.model, llmConnectionSlug: input.connection,
  thinkingLevel: 'xhigh', permissionMode: 'bypass', collaborationMode: 'agent',
  workspace: {hostCwd: input.cwd, target: {kind: 'host_path', path: input.cwd}}};
const invocation = {sessionId: input.session_id, runId: 'run', turnId: 'turn', invocationId: 'inv',
  opening: {configuration: {permissionMode: 'bypass'}}};

function dependencies({existing = false, invocations = [invocation], truncated = false,
    readStatus = 'complete', wrongHost = false} = {}) {
  const requests = [];
  return {requests, rootPath: '/native', catalog: {connections: [{slug: input.connection,
    connectionId: 'connection', enabled: true, enabledModelIds: [input.model],
    providerType: 'custom', defaultApiProtocol: 'openai-responses', baseUrl: 'http://127.0.0.1:48173/v1'}]},
    connection: {request: async (name, payload) => {
      requests.push([name, payload]);
      if (name === 'host.diagnostics.query') return {pid: 12, hostEpoch: wrongHost ? 'changed' : 'epoch'};
      if (name === 'session.catalog.query') return {kind: 'session', session: existing ? session : null};
      if (name === 'session.create') return session;
      throw Error(name);
    }},
    persistence: {openRuntimeEventReadPersistence: async () => ({
      runtimeEventStore: {
        listSessionInvocationsBounded: async (id, limit) => {
          assert.equal(id, input.session_id); assert.equal(limit, 1);
          return {invocations, truncated};
        },
        readRuntimeEventsBounded: async (id, run) => {
          assert.equal(id, input.session_id); assert.equal(run, 'run');
          return {status: readStatus, records: []};
        },
      }, close: async () => {},
    })},
  };
}

test('trusted owned preparation keeps native discovery in explicit bypass mode', async () => {
  const deps = dependencies();
  const result = await prepareSession(input, deps);
  assert.equal(result.session_id, input.session_id);
  const created = deps.requests.find(([name]) => name === 'session.create')[1];
  assert.equal(created.permissionMode, 'bypass');
  assert.equal(result.permission_mode, 'bypass');
  assert.equal(created.thinkingLevel, 'xhigh');
  assert.equal(created.collaborationMode, 'agent');
  assert.equal(Object.hasOwn(created, 'toolProfile'), false);
});

test('preparation without a prior Host identity returns the allocated Host', async () => {
  const {host, ...unallocated} = input;
  const result = await prepareSession(unallocated, dependencies());
  assert.deepEqual(result.host, {pid: 12, hostEpoch: 'epoch'});
});

test('preparation refuses an existing session rather than adopting it', async () => {
  const deps = dependencies({existing: true});
  await assert.rejects(prepareSession(input, deps), /already exists/);
  assert.equal(deps.requests.some(([name]) => name === 'session.create'), false);
});

test('capture reads the sole exact session invocation', async () => {
  const result = await captureSession(input, dependencies({existing: true}));
  assert.equal(result.invocation.runId, 'run');
  assert.equal(result.permission_mode, 'bypass');
});

for (const [label, options] of [
  ['missing', {invocations: []}], ['multiple', {invocations: [invocation, invocation]}],
  ['truncated', {truncated: true}], ['wrong session', {invocations: [{...invocation, sessionId: 'wrong'}]}],
  ['incomplete events', {readStatus: 'limit_exceeded'}], ['changed host', {wrongHost: true}],
]) test(`capture refuses ${label} evidence`, async () => {
  await assert.rejects(captureSession(input, dependencies({existing: true, ...options})));
});

test('a mismatched connection cannot be labeled as the company route', async () => {
  const deps = dependencies();
  deps.catalog.connections[0].baseUrl = 'https://api.openai.com/v1';
  await assert.rejects(prepareSession(input, deps), /route/);
});

test('subscription portability is an explicit route and native provider', async () => {
  const deps = dependencies();
  await assert.rejects(prepareSession({...input, auth_route: 'chatgpt-subscription'}, deps), /route/);
  deps.catalog.connections[0].providerType = 'openai-codex';
  delete deps.catalog.connections[0].baseUrl;
  const result = await prepareSession({...input, auth_route: 'chatgpt-subscription'}, deps);
  assert.equal(result.session_id, input.session_id);
});

function cleanupDependencies({wrongSession = false, wrongPrompt = false, neverStops = false, overflow = false} = {}) {
  const deps = dependencies({existing: true});
  let stopped = false;
  const owned = {...invocation, ...(wrongSession ? {sessionId: 'not-owned'} : {}),
    opening: {route: {modelId: input.model, llmConnectionSlug: input.connection},
      configuration: {cwd: input.cwd, permissionMode: 'bypass'}}};
  const request = deps.connection.request;
  deps.connection.request = async (name, payload) => {
    if (name === 'turn.stop') {
      deps.requests.push([name, payload]); stopped = !neverStops;
      return {...invocation, status: stopped ? 'cancelled' : 'running', terminalEventId: 'end', abortSource: 'stop_button'};
    }
    if (name === 'turn.query') return {...invocation, status: stopped ? 'cancelled' : 'running'};
    return request(name, payload);
  };
  const current = () => ({...owned, ...(stopped ? {terminalEvent: {...invocation, id: 'end', status: 'aborted'}} : {})});
  deps.persistence = {openRuntimeEventReadPersistence: async () => ({runtimeEventStore: {
    listSessionInvocationsBounded: async () => ({invocations: [current()], truncated: false}),
    readRuntimeEventsBounded: async () => overflow ? {status: 'limit_exceeded'} : ({status: 'complete', records: [
      {...invocation, role: 'user', content: {kind: 'text', text: wrongPrompt ? 'other' : 'owned-prompt'}},
    ]}),
    readRunInvocation: async (sessionId, runId) => {
      assert.equal(sessionId, input.session_id); assert.equal(runId, 'run'); return current();
    },
  }, close: async () => {}})};
  deps.readOwnedPrompt = async (root, identity) => {
    assert.equal(root, '/native');
    assert.deepEqual(identity, {sessionId: input.session_id, turnId: 'turn', runId: 'run', invocationId: 'inv'});
    return wrongPrompt ? 'other' : 'owned-prompt';
  };
  deps.wait = async () => {};
  return deps;
}
const cleanupInput = {...input, prompt_sha256: createHash('sha256').update('owned-prompt').digest('hex')};

for (const operation of ['prepare', 'capture', 'cleanup']) {
  test(`${operation} refuses ask-mode native Session evidence`, async () => {
    const deps = cleanupDependencies();
    const request = deps.connection.request;
    deps.connection.request = async (name, payload) => name === 'session.create'
      ? {...session, permissionMode: 'ask'}
      : name === 'session.catalog.query'
        ? {kind: 'session', session: operation === 'prepare' ? null : {...session, permissionMode: 'ask'}}
        : request(name, payload);
    await assert.rejects(nativeSession[`${operation}Session`](cleanupInput, deps), /Session target/);
    assert.equal(deps.requests.some(([name]) => name === 'turn.stop'), false);
  });
}

for (const operation of ['capture', 'cleanup']) {
  for (const mode of ['ask', undefined]) {
    test(`${operation} refuses misbound invocation permission mode ${mode}`, async () => {
      const deps = cleanupDependencies();
      const open = deps.persistence.openRuntimeEventReadPersistence;
      deps.persistence.openRuntimeEventReadPersistence = async options => {
        const opened = await open(options);
        const list = opened.runtimeEventStore.listSessionInvocationsBounded;
        opened.runtimeEventStore.listSessionInvocationsBounded = async (...args) => {
          const result = await list(...args);
          result.invocations[0].opening.configuration.permissionMode = mode;
          return result;
        };
        return opened;
      };
      await assert.rejects(nativeSession[`${operation}Session`](cleanupInput, deps), /permission/);
      assert.equal(deps.requests.some(([name]) => name === 'turn.stop'), false);
    });
  }
}

for (const host of [undefined, null, false, {}, {pid: 12}, {hostEpoch: 'epoch'},
  {pid: '12', hostEpoch: 'epoch'}, {pid: 0, hostEpoch: 'epoch'}, {pid: 12, hostEpoch: ''},
  {pid: 99, hostEpoch: 'epoch'}, {pid: 12, hostEpoch: 'other'}]) {
  for (const operation of ['capture', 'cleanup']) {
    test(`${operation} refuses a missing, invalid or changed allocated Host ${JSON.stringify(host)}`, async () => {
      const deps = cleanupDependencies();
      const action = operation === 'capture' ? captureSession : nativeSession.cleanupSession;
      await assert.rejects(action({...cleanupInput, host}, deps), /Host/);
      assert.equal(deps.requests.some(([name]) => name === 'turn.stop'), false);
    });
  }
}

test('an exited startup refusal with no native turn needs no cancellation', async () => {
  const deps = dependencies({existing: true, invocations: []});
  const request = deps.connection.request;
  deps.connection.request = async (name, payload) => name === 'session.catalog.query'
    ? {kind: 'session', session: {...session, liveRunState: {
      schemaVersion: 1, runningTurnIds: [], runEpoch: 0,
    }}} : request(name, payload);
  const result = await nativeSession.cleanupSession({...cleanupInput,
    process_exited: true, process_returncode: 1}, deps);
  assert.equal(result.cleanup_complete, true);
  assert.equal(result.not_started, true);
  assert.equal(result.stop_requested, false);
  assert.equal(deps.requests.some(([name]) => name === 'turn.stop'), false);
  await assert.rejects(captureSession(cleanupInput, deps), /exactly one invocation/);
});

for (const host of [undefined, null]) {
  test(`unstarted cleanup refuses an unpinned Host ${JSON.stringify(host)}`, async () => {
    const deps = dependencies({existing: true, invocations: []});
    const request = deps.connection.request;
    deps.connection.request = async (name, payload) => name === 'session.catalog.query'
      ? {kind: 'session', session: {...session, liveRunState: {
        schemaVersion: 1, runningTurnIds: [], runEpoch: 0,
      }}} : request(name, payload);
    await assert.rejects(nativeSession.cleanupSession({...cleanupInput, host,
      process_exited: true, process_returncode: 1}, deps), /Host/);
    assert.equal(deps.requests.some(([name]) => name === 'turn.stop'), false);
  });
}

for (const change of ['unknown-process', 'successful-process', 'missing-live-state',
  'active-turn', 'prior-turn', 'truncated']) {
  test(`empty cleanup still refuses ${change}`, async () => {
    const deps = dependencies({existing: true, invocations: [], truncated: change === 'truncated'});
    const request = deps.connection.request;
    deps.connection.request = async (name, payload) => name === 'session.catalog.query'
      ? {kind: 'session', session: {...session, ...(change === 'missing-live-state' ? {} : {
        liveRunState: {schemaVersion: 1, runningTurnIds: change === 'active-turn' ? ['turn'] : [],
          runEpoch: change === 'prior-turn' ? 1 : 0},
      })}} : request(name, payload);
    await assert.rejects(nativeSession.cleanupSession({...cleanupInput,
      process_exited: change !== 'unknown-process',
      process_returncode: change === 'successful-process' ? 0 : 1}, deps));
    assert.equal(deps.requests.some(([name]) => name === 'turn.stop'), false);
  });
}

for (const workspace of [undefined, {hostCwd: '/different', target: {kind: 'host_path', path: '/different'}},
  {hostCwd: input.cwd, target: {kind: 'host_path', path: '/different'}},
  {hostCwd: '/different', target: {kind: 'host_path', path: input.cwd}}]) {
  test(`unstarted cleanup refuses a missing or mismatched workspace ${JSON.stringify(workspace)}`, async () => {
    const deps = dependencies({existing: true, invocations: []});
    const request = deps.connection.request;
    deps.connection.request = async (name, payload) => name === 'session.catalog.query'
      ? {kind: 'session', session: {...session, workspace, liveRunState: {
        schemaVersion: 1, runningTurnIds: [], runEpoch: 0,
      }}} : request(name, payload);
    await assert.rejects(nativeSession.cleanupSession({...cleanupInput,
      process_exited: true, process_returncode: 1}, deps), /workspace/);
    assert.equal(deps.requests.some(([name]) => name === 'turn.stop'), false);
  });
}

test('forced-exit cleanup stops only the exact owned native turn and verifies terminal journal', async () => {
  const deps = cleanupDependencies();
  const result = await nativeSession.cleanupSession(cleanupInput, deps);
  assert.equal(result.cleanup_complete, true);
  assert.deepEqual(deps.requests.filter(([name]) => name === 'turn.stop'), [
    ['turn.stop', {sessionId: input.session_id, turnId: 'turn', runId: 'run'}],
  ]);
});

for (const options of [{wrongSession: true}, {wrongPrompt: true}, {neverStops: true}]) {
  test(`cleanup refuses misbinding or an unconfirmed stop ${JSON.stringify(options)}`, async () => {
    const deps = cleanupDependencies(options);
    await assert.rejects(nativeSession.cleanupSession(cleanupInput, deps));
    if (!options.neverStops) assert.equal(deps.requests.some(([name]) => name === 'turn.stop'), false);
  });
}

test('journal overflow still stops the exact owned active turn and verifies terminal state', async () => {
  const deps = cleanupDependencies({overflow: true});
  await assert.rejects(captureSession(cleanupInput, deps), /budget/);
  const result = await nativeSession.cleanupSession(cleanupInput, deps);
  assert.equal(result.cleanup_complete, true);
  assert.equal(result.stop_requested, true);
  assert.equal(Object.hasOwn(result, 'records'), false);
  assert.deepEqual(deps.requests.filter(([name]) => name === 'turn.stop'), [
    ['turn.stop', {sessionId: input.session_id, turnId: 'turn', runId: 'run'}],
  ]);
});

test('overflow cleanup still refuses a mismatched owned prompt before stopping', async () => {
  const deps = cleanupDependencies({overflow: true, wrongPrompt: true});
  await assert.rejects(nativeSession.cleanupSession(cleanupInput, deps));
  assert.equal(deps.requests.some(([name]) => name === 'turn.stop'), false);
});

test('bounded native prompt projection ignores oversized unrelated tool results and checks all identities', () => {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE runtime_events (session_id TEXT, run_id TEXT, turn_id TEXT, invocation_id TEXT, event_kind TEXT, payload_json TEXT)');
  const insert = db.prepare('INSERT INTO runtime_events VALUES (?,?,?,?,?,?)');
  insert.run(input.session_id, 'run', 'turn', 'inv', 'text', JSON.stringify({role: 'user', partial: false,
    content: {kind: 'text', text: 'owned-prompt'}}));
  insert.run(input.session_id, 'run', 'turn', 'inv', 'function_response', JSON.stringify({role: 'tool',
    content: {kind: 'function_response', result: 'x'.repeat(3 * 1024 * 1024)}}));
  insert.run('other', 'run', 'turn', 'inv', 'text', JSON.stringify({role: 'user', partial: false,
    content: {kind: 'text', text: 'not-owned'}}));
  const store = {OPERATIONAL_STATE_DATABASE_NAME: 'runtime.sqlite', createSqliteRuntimeStore: (path, options) => {
    assert.equal(path, '/native/runtime.sqlite'); assert.deepEqual(options, {readOnly: true});
    return {db, close: () => {}};
  }};
  const identity = {sessionId: input.session_id, runId: 'run', turnId: 'turn', invocationId: 'inv'};
  assert.equal(nativeSession.readOwnedPrompt('/native', identity, store), 'owned-prompt');
  assert.throws(() => nativeSession.readOwnedPrompt('/native', {...identity, turnId: 'wrong'}, store));
  insert.run(input.session_id, 'run', 'turn', 'inv', 'text', JSON.stringify({role: 'user', partial: false,
    content: {kind: 'text', text: 'ambiguous'}}));
  assert.throws(() => nativeSession.readOwnedPrompt('/native', identity, store));
  db.exec('DELETE FROM runtime_events');
  insert.run(input.session_id, 'run', 'turn', 'inv', 'text', JSON.stringify({role: 'user', partial: false,
    content: {kind: 'text', text: 'x'.repeat(4 * 1024 * 1024 + 1)}}));
  assert.throws(() => nativeSession.readOwnedPrompt('/native', identity, store));
  db.close();
});
