import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { test } from 'node:test';

import {
  attestExistingHost, prepareRoutingSession, verifyHostIdentity,
  verifyRoutingSessionHost, runRoutingTurn, verifyInstalledToolCeiling,
  verifyNativeHostDenial, verifyPrivateHostWitness,
  previewPrivateHostRoot,
  runPrivateHostPreload,
  startPrivateRoutingHost,
  verifyPrivateHostFiles,
  readRoutingDurableEvidence,
} from './maka_skill_routing.mjs';

const BOOT = '12345678-1234-1234-1234-123456789abc';
const ENTRYPOINT = '/installed/maka/node_modules/@maka/runtime-host/dist/execution-candidate-main.js';
const ROOT = '/profile/workspaces/default';

test('the authentic installed readonly store measures and refuses oversized durable content without mutation', async (t) => {
  const component = dirname(dirname(fileURLToPath(import.meta.url)));
  const packageRoot = process.env.KHENRIX_MAKA_PACKAGE_ROOT ?? resolve(dirname(execFileSync(
    'mise', ['-C', component, 'which', 'maka'], { encoding: 'utf8' }).trim()), '..', 'maka-agent');
  assert.equal(JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8')).version,
    '0.2.0-dev.70.20261005');
  const root = await mkdtemp(join(tmpdir(), 'maka-durable-content-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const { createSqliteRuntimeStore } = await import(pathToFileURL(join(packageRoot,
    'node_modules/@maka/storage/dist/sqlite-runtime-store.js')));
  const persistence = await import(pathToFileURL(join(packageRoot,
    'node_modules/@maka/storage/dist/runtime-event-persistence.js')));
  const database = join(root, 'runtime.sqlite');
  const writer = createSqliteRuntimeStore(database);
  await writer.appendRuntimeEvent(SESSION, 'run', {
    id: 'user-event', sessionId: SESSION, invocationId: 'invocation', runId: 'run',
    turnId: 'turn', ts: 1, partial: false, role: 'user', author: 'user',
    content: { kind: 'text', text: 'canonical text 😀'.repeat(1000) },
  });
  await writer.close();
  const before = await readFile(database);
  const reader = await persistence.openRuntimeEventReadPersistence({ workspaceRoot: root });
  try {
    const complete = await reader.runtimeEventStore.readRuntimeEventsBounded(
      SESSION, 'run', { maxRecords: 256, maxBytes: 2 * 1024 * 1024 });
    assert.equal(complete.status, 'complete');
    assert.equal(complete.records[0].content.text, 'canonical text 😀'.repeat(1000));
    assert.equal((await reader.runtimeEventStore.readRuntimeEventsBounded(
      SESSION, 'run', { maxRecords: 256, maxBytes: 32 })).status, 'limit_exceeded');
  } finally {
    await reader.close();
  }
  assert.deepEqual(await readFile(database), before);
  await assert.rejects(readRoutingDurableEvidence(root,
    { sessionId: SESSION, runId: 'run', turnId: 'turn' }, persistence), /incomplete/u);
});

test('routing durable export uses the licensed bounded readonly consumer and refuses incomplete evidence', async () => {
  const identity = { sessionId: SESSION, turnId: 'turn', runId: 'run', invocationId: 'invocation' };
  const invocation = { ...identity };
  let closed = false;
  const calls = [];
  const persistence = {
    openRuntimeEventReadPersistence: async (input) => {
      assert.deepEqual(input, { workspaceRoot: '/fresh/private' });
      return {
        close: () => { closed = true; },
        runtimeEventStore: {
          readRunInvocation: async (...args) => { calls.push(args); return invocation; },
          readRuntimeEventsBounded: async (...args) => {
            calls.push(args);
            return { status: 'complete', records: [{ ...identity, id: 'event', partial: false }] };
          },
        },
      };
    },
  };
  const proof = await readRoutingDurableEvidence('/fresh/private', identity, persistence);
  assert.equal(proof.runId, 'run');
  assert.deepEqual(calls[1], [SESSION, 'run', { maxRecords: 256, maxBytes: 2 * 1024 * 1024 }]);
  assert.equal(closed, true);
  for (const status of ['limit_exceeded', 'partial']) {
    const broken = { openRuntimeEventReadPersistence: async () => ({
      close: () => {}, runtimeEventStore: {
        readRunInvocation: async () => invocation,
        readRuntimeEventsBounded: async () => ({ status }),
      },
    }) };
    await assert.rejects(readRoutingDurableEvidence('/fresh/private', identity, broken),
      /durable.*budget|durable.*incomplete/u);
  }
});

function hostInput(overrides = {}) {
  return {
    diagnostics: {
      pid: 4242, hostEpoch: 'host-epoch-1', compositionId: 'maka.interactive',
      compositionRevision: '3', processUptimeSeconds: 5,
      ...overrides.diagnostics,
    },
    process: {
      pid: 4242, uid: 501, node: '/installed/node', entrypoint: ENTRYPOINT,
      rootPath: ROOT, startedAtMs: 100_000,
      ...overrides.process,
    },
    expected: {
      uid: 501, node: '/installed/node', entrypoint: ENTRYPOINT, rootPath: ROOT,
      installBootSession: BOOT, currentBootSession: BOOT,
      installMonotonicNs: '1000000000', monotonicNowNs: '100000000000',
      ...overrides.expected,
    },
    activeSessions: overrides.activeSessions ?? [],
  };
}

test('accepts only the exact installed, post-install Host identity', () => {
  assert.deepEqual(verifyHostIdentity(hostInput()), {
    pid: 4242, hostEpoch: 'host-epoch-1', startedAtMs: 100_000,
  });
  assert.throws(
    () => verifyHostIdentity(hostInput({process: {entrypoint: '/old/maka/host.js'}})),
    /Host entrypoint|installed runtime/u,
  );
  assert.throws(
    () => verifyHostIdentity(hostInput({diagnostics: {pid: 9999}})),
    /Host identity/u,
  );
  assert.throws(
    () => verifyHostIdentity(hostInput({expected: {startedAtMs: 100_001}})),
    /Host entrypoint/u,
  );
});

test('kernel-start proof never resolves Python through ambient PATH',
  {skip: process.platform !== 'darwin'}, async () => {
    const {processProof} = await import('./maka_skill_routing.mjs');
    assert.equal(typeof processProof, 'function');
    const pid = 4242;
    const packageRoot = '/installed/maka';
    const coarse = Date.UTC(2026, 8, 25, 18, 5, 0);
    const calls = [];
    const proof = await processProof(pid, packageRoot, ROOT, {
      execFile: async (program, args) => {
        calls.push(program);
        if (program === '/bin/ps' && args.includes('uid=')) {
          return {stdout: `${process.getuid()}\n`};
        }
        if (program === '/bin/ps' && args.includes('args=')) {
          return {stdout: `${process.execPath} ${packageRoot}/node_modules/@maka/runtime-host/dist/execution-candidate-main.js --root ${ROOT} --expected-root-id ${'a'.repeat(64)}\n`};
        }
        if (program === '/bin/ps' && args.includes('lstart=')) {
          return {stdout: 'Fri Sep 25 18:05:00 2026\n'};
        }
        if (program === '/usr/bin/python3') {
          return {stdout: JSON.stringify({pid, uid: process.getuid(),
            seconds: coarse / 1000, microseconds: 123000})};
        }
        throw new Error(`untrusted interpreter: ${program}`);
      },
    });
    assert.deepEqual(calls, ['/bin/ps', '/bin/ps', '/bin/ps', '/usr/bin/python3']);
    assert.equal(proof.startedAtMs, coarse + 123);
  });

test('stale Host fails closed without a restart, including with active sessions', () => {
  const stale = hostInput({
    diagnostics: {processUptimeSeconds: 9},
    expected: {installMonotonicNs: '95000000000'},
  });
  assert.throws(() => verifyHostIdentity(stale), /Host is stale/u);
  assert.throws(
    () => verifyHostIdentity({...stale, activeSessions: ['active-session']}),
    /active session.*restart refused/u,
  );
});

test('a changed boot permits a fresh Host even when its monotonic clock is lower', () => {
  assert.equal(verifyHostIdentity(hostInput({ expected: {
    currentBootSession: 'aaaaaaaa-1234-1234-1234-123456789abc',
    installMonotonicNs: '100000000000', monotonicNowNs: '1000000000',
  } })).pid, 4242);
});

function preparedDependencies(input = hostInput()) {
  const requests = [];
  const connection = {
    request: async (operation, payload) => {
      requests.push({operation, payload});
      if (operation === 'host.diagnostics.query') return input.diagnostics;
      if (operation === 'session.catalog.query') return {
        kind: 'page', revision: 1, nextCursor: null,
        sessions: [{id: 'existing', liveRunState: {
          runningTurnIds: ['active-turn'],
        }}],
      };
      if (operation === 'session.create') return {
        id: payload.sessionId, model: 'gpt-6.1-sol',
        llmConnectionId: 'relay-connection-1', llmConnectionSlug: 'keychain-openai',
        thinkingLevel: 'xhigh', permissionMode: 'ask',
      };
      if (operation === 'session.execution_boundary.query') return {
        kind: 'managed', access: 'writable', revision: 1,
      };
      throw new Error(`unexpected Host operation: ${operation}`);
    },
  };
  return {
    requests,
    deps: {
      connection, rootPath: ROOT, expected: input.expected,
      processProof: async () => input.process,
      catalog: {connections: [{
        connectionId: 'relay-connection-1', slug: 'keychain-openai', enabled: true,
        providerType: 'custom', defaultApiProtocol: 'openai-responses',
        enabledModelIds: ['gpt-6.1-sol'],
        baseUrl: 'http://127.0.0.1:48173/v1',
      }]},
    },
  };
}

const SESSION = 'fanout-routing-0123456789abcdef0123456789abcdef';

test('actual Host probe requires fork and same-process exec denial with no marker', () => {
  const marker = '/private/probe/marker';
  const absolute = '/private/probe/absolute-codex';
  const self = '/installed/node';
  assert.deepEqual(verifyNativeHostDenial(absolute, self, marker, {
    spawnSync: () => ({error: {code: 'EPERM'}, status: null, signal: null}),
    probeExec: () => 1,
    markerExists: () => false,
  }), {absoluteExecutable: absolute, selfExecutable: self, denied: true,
    forkErrorCode: 'EPERM', absoluteExecErrno: 1, selfExecErrno: 1});
  assert.throws(() => verifyNativeHostDenial(absolute, self, marker, {
    spawnSync: () => ({error: {code: 'ENOENT'}}), probeExec: () => 1,
    markerExists: () => false,
  }), /native sandbox/u);
  assert.throws(() => verifyNativeHostDenial(absolute, self, marker, {
    spawnSync: () => ({error: {code: 'EPERM'}}), probeExec: () => 0,
    markerExists: () => false,
  }), /native sandbox/u);
  assert.throws(() => verifyNativeHostDenial(absolute, self, marker, {
    spawnSync: () => ({error: {code: 'EPERM'}}), probeExec: () => 1,
    markerExists: () => true,
  }), /native sandbox/u);
});

test('private Host witness binds root, process epoch, start, addon, compiler, and source', () => {
  const digest = 'a'.repeat(64);
  const expected = {
    rootPath: '/private/root/workspaces/default', clientDataRoot: '/private/root',
    rootId: 'b'.repeat(64), controlPath: `/Users/test/Library/Caches/Maka/runtime-hosts/${'b'.repeat(64)}`,
    pid: 4242, hostEpoch: 'epoch-1', startedAtMs: 100_000,
    packageDigest: digest, nodeDigest: digest, helperDigest: digest,
    sealSourceDigest: digest, sealAddonDigest: digest, compilerDigest: digest,
    nonce: 'c'.repeat(64),
  };
  const witness = {...expected, schema: 'khenrix-maka-private-host-v1',
    denial: {absoluteExecutable: '/private/probe/absolute-codex', selfExecutable: '/installed/node',
      denied: true, forkErrorCode: 'EPERM', absoluteExecErrno: 1, selfExecErrno: 1},
  };
  assert.deepEqual(verifyPrivateHostWitness(witness, expected), witness);
  for (const [key, value] of Object.entries({
    pid: 4243, hostEpoch: 'epoch-2', startedAtMs: 101_000,
    rootId: 'd'.repeat(64), sealSourceDigest: 'd'.repeat(64),
    sealAddonDigest: 'd'.repeat(64), compilerDigest: 'd'.repeat(64),
  })) {
    assert.throws(() => verifyPrivateHostWitness({...witness, [key]: value}, expected),
      /private Host witness/u);
  }
  assert.throws(() => verifyPrivateHostWitness({...witness,
    denial: {...witness.denial, selfExecErrno: 0}}, expected), /private Host witness/u);
});

test('private root preview resolves one fresh rootId without touching default control', async () => {
  const rootId = 'b'.repeat(64);
  const controlNamespace = '/Users/test/Library/Caches/Maka/runtime-hosts';
  const rootPath = '/private/tmp/private-maka/client/workspaces/default';
  const preview = await previewPrivateHostRoot({
    rootPath, clientDataRoot: '/private/tmp/private-maka/client',
    defaultRootPath: ROOT,
  }, {
    resolveStorageRoot: async (input) => {
      assert.deepEqual(input, {path: rootPath, kind: 'interactive'});
      return {canonicalPath: rootPath, rootId};
    },
    resolveRootControlNamespace: () => controlNamespace,
    pathExists: async (path) => {
      assert.equal(path, `${controlNamespace}/${rootId}`);
      return false;
    },
  });
  assert.deepEqual(preview, {
    rootPath, clientDataRoot: '/private/tmp/private-maka/client', rootId,
    controlPath: `${controlNamespace}/${rootId}`, controlPreexisted: false,
  });
  await assert.rejects(() => previewPrivateHostRoot({
    rootPath: ROOT, clientDataRoot: '/profile', defaultRootPath: ROOT,
  }, {}), /private Host root/u);
  await assert.rejects(() => previewPrivateHostRoot({
    rootPath, clientDataRoot: '/private/tmp/other', defaultRootPath: ROOT,
  }, {}), /private Host root/u);
  await assert.rejects(() => previewPrivateHostRoot({
    rootPath, clientDataRoot: '/private/tmp/private-maka/client', defaultRootPath: ROOT,
  }, {
    resolveStorageRoot: async () => ({canonicalPath: rootPath, rootId}),
    resolveRootControlNamespace: () => controlNamespace,
    pathExists: async () => true,
  }), /control.*exists/u);
});

test('Host preload denies the absolute launch and writes a nonce-bound witness', async () => {
  const rootId = 'b'.repeat(64);
  const args = [ENTRYPOINT, '--root', '/private/host', '--expected-root-id', rootId];
  const spec = {
    schema: 'khenrix-maka-private-host-spec-v1',
    rootPath: '/private/host', rootId, nonce: 'c'.repeat(64),
    candidateEntrypoint: ENTRYPOINT,
    nodePath: '/installed/node',
    absoluteExecutable: '/private/probe/absolute-codex',
    markerPath: '/private/probe/marker', witnessPath: '/private/probe/witness.json',
  };
  const written = [];
  const result = await runPrivateHostPreload(spec, {
    argv: ['/installed/node', ...args], pid: 4242,
    spawnSync: () => ({error: {code: 'EPERM'}}), probeExec: () => 1,
    markerExists: () => false,
    writeWitness: async (path, value) => written.push([path, value]),
  });
  assert.equal(result.pid, 4242);
  assert.equal(result.rootId, rootId);
  assert.deepEqual(result.denial, {
    absoluteExecutable: spec.absoluteExecutable, selfExecutable: spec.nodePath,
    denied: true, forkErrorCode: 'EPERM', absoluteExecErrno: 1, selfExecErrno: 1,
  });
  assert.deepEqual(written, [[spec.witnessPath, result]]);
  await assert.rejects(() => runPrivateHostPreload(spec, {
    argv: ['/installed/node', ...args.slice(0, -1), 'different'], pid: 4242,
    spawnSync: () => ({error: {code: 'EPERM'}}), probeExec: () => 1,
    markerExists: () => false,
    writeWitness: async () => assert.fail('must not write'),
  }), /private Host preload/u);
});

test('owned private Host launch binds native denial before relay configuration', async () => {
  const rootId = 'b'.repeat(64);
  const digest = 'a'.repeat(64);
  const rootPath = '/private/tmp/private-maka/client/workspaces/default';
  const controlPath = `/Users/test/Library/Caches/Maka/runtime-hosts/${rootId}`;
  const spec = {
    schema: 'khenrix-maka-private-host-spec-v1',
    rootPath, clientDataRoot: '/private/tmp/private-maka/client',
    defaultRootPath: ROOT, rootId, controlPath, nonce: 'c'.repeat(64),
    nodePath: '/installed/node',
    absoluteExecutable: '/private/probe/absolute-codex',
    witnessPath: '/private/tmp/private-maka/witness.json',
    packageDigest: digest, nodeDigest: digest, helperDigest: digest,
    sealSourceDigest: digest, sealAddonDigest: digest, compilerDigest: digest,
  };
  const requests = [];
  const connection = {
    rootId,
    request: async (operation) => {
      requests.push(operation);
      return hostInput().diagnostics;
    },
    close: async () => requests.push('close'),
  };
  const host = {pid: 4242, settle: async () => requests.push('settle')};
  const deps = {
    resolveStorageRoot: async () => ({canonicalPath: rootPath, rootId}),
    resolveRootControlNamespace: () => '/Users/test/Library/Caches/Maka/runtime-hosts',
    pathExists: async () => false,
    connectOwnedRuntimeHost: async (input) => {
      assert.equal(input.rootPath, rootPath);
      assert.equal(input.candidateExecutable, undefined);
      assert.equal(input.closeOnLauncherExit, true);
      return {kind: 'connected', connection, host};
    },
    readPreloadWitness: async () => ({
      schema: 'khenrix-maka-private-host-preload-v1', pid: 4242,
      rootPath, rootId, nonce: spec.nonce,
      denial: {absoluteExecutable: '/private/probe/absolute-codex',
        selfExecutable: '/installed/node', denied: true, forkErrorCode: 'EPERM',
        absoluteExecErrno: 1, selfExecErrno: 1},
    }),
    processProof: async () => hostInput({process: {rootPath}}).process,
    expected: {...hostInput({expected: {rootPath}}).expected},
    configureRelay: async () => requests.push('configure-relay'),
  };
  const started = await startPrivateRoutingHost(spec, deps);
  assert.equal(started.connection, connection);
  assert.equal(started.host, host);
  assert.equal(started.attestation.pid, 4242);
  assert.equal(started.attestation.rootId, rootId);
  assert.deepEqual(requests, ['host.diagnostics.query', 'configure-relay']);
  await assert.rejects(() => startPrivateRoutingHost(spec, {
    ...deps, readPreloadWitness: async () => ({
      ...await deps.readPreloadWitness(), denial: {
        absoluteExecutable: '/private/probe/absolute-codex', selfExecutable: '/installed/node',
        denied: true, forkErrorCode: 'EPERM', absoluteExecErrno: 1, selfExecErrno: 0,
      },
    }),
  }), /private Host witness/u);
  assert.deepEqual(requests.slice(-3), ['host.diagnostics.query', 'close', 'settle']);
});

test('private Host launch refuses changed addon, source, compiler, Node, or helper', async () => {
  const nodePath = '/installed/node';
  const helperPath = '/installed/maka/interactive/maka_skill_routing.mjs';
  const sealSourcePath = '/installed/maka/interactive/maka_host_seal.c';
  const sealAddonPath = '/private/root/maka-host-seal.node';
  const compilerPath = '/Library/Developer/CommandLineTools/usr/bin/clang';
  const hash = (value) => createHash('sha256').update(value).digest('hex');
  const files = new Map([
    [nodePath, Buffer.from('pinned node')],
    [helperPath, Buffer.from('installed helper')],
    [sealSourcePath, Buffer.from('installed source')],
    [sealAddonPath, Buffer.from('compiled addon')],
    [compilerPath, Buffer.from('pinned compiler')],
  ]);
  const spec = {
    packageRoot: '/installed/maka/runtime/package',
    witnessPath: '/private/root/host-preload-witness.json',
    nodePath, helperPath, sealSourcePath, sealAddonPath, compilerPath,
    nodeDigest: hash(files.get(nodePath)), helperDigest: hash(files.get(helperPath)),
    sealSourceDigest: hash(files.get(sealSourcePath)),
    sealAddonDigest: hash(files.get(sealAddonPath)),
    compilerDigest: hash(files.get(compilerPath)),
  };
  await verifyPrivateHostFiles(spec, {readFile: async (path) => files.get(path)});
  for (const path of files.keys()) {
    const changed = new Map(files);
    changed.set(path, Buffer.from('changed'));
    await assert.rejects(() => verifyPrivateHostFiles(spec, {
      readFile: async (name) => changed.get(name),
    }), /private Host (?:source|addon|compiler)/u);
  }
  await assert.rejects(() => verifyPrivateHostFiles({
    ...spec, sealSourcePath: '/installed/maka/interactive/alternate.c',
  }, {readFile: async (path) => files.get(path) ?? files.get(sealSourcePath)}),
  /private Host source path/u);
  await assert.rejects(() => verifyPrivateHostFiles({
    ...spec, sealAddonPath: '/private/elsewhere/maka-host-seal.node',
  }, {readFile: async (path) => files.get(path) ?? files.get(sealAddonPath)}),
  /private Host addon path/u);
});

test('installed tool projection keeps Skill/SkillSearch and rejects Bash without a model', () => {
  const skill = {name: 'Skill'};
  const search = {name: 'SkillSearch'};
  const bash = {name: 'Bash'};
  const profile = {toolNames: ['Skill', 'SkillSearch'], memoryExtraction: false};
  verifyInstalledToolCeiling({
    hostedExecutionRunProfile: () => profile,
    projectHostedExecutionTools: (tools) => profile.toolNames.map((name) => {
      const found = tools.find((tool) => tool.name === name);
      if (!found) throw new Error('missing tool');
      return found;
    }),
  });
  assert.throws(() => verifyInstalledToolCeiling({
    hostedExecutionRunProfile: () => ({...profile, toolNames: [...profile.toolNames, 'Bash']}),
    projectHostedExecutionTools: (tools) => tools,
  }), /tool ceiling/u);
  assert.throws(() => verifyInstalledToolCeiling({
    hostedExecutionRunProfile: () => profile,
    projectHostedExecutionTools: () => [skill, search, bash],
  }), /tool ceiling/u);
  assert.throws(() => verifyInstalledToolCeiling({
    hostedExecutionRunProfile: () => profile,
    projectHostedExecutionTools: () => [skill],
  }), /tool ceiling/u);
});

test('preflight creates the exact skill-only Session before a model Turn', async () => {
  const {deps, requests} = preparedDependencies();
  const result = await prepareRoutingSession({
    packageRoot: '/installed/maka', cwd: '/tmp/task', sessionId: SESSION,
  }, deps);
  assert.equal(result.sessionId, SESSION);
  assert.deepEqual(requests.map(({operation}) => operation), [
    'host.diagnostics.query', 'session.create', 'session.execution_boundary.query',
  ]);
  const created = requests[1].payload;
  assert.equal(created.toolProfile, 'khenrix-skill-routing-v1');
  assert.equal(created.permissionMode, 'ask');
  assert.equal(created.modelTarget.connectionId, 'relay-connection-1');
});

test('wrong or stale Host never reaches Session creation or restart', async () => {
  const wrong = preparedDependencies(hostInput({process: {entrypoint: '/old/host.js'}}));
  await assert.rejects(() => prepareRoutingSession({
    packageRoot: '/installed/maka', cwd: '/tmp/task', sessionId: SESSION,
  }, wrong.deps), /Host entrypoint/u);
  assert.deepEqual(wrong.requests.map(({operation}) => operation), ['host.diagnostics.query']);

  const stale = preparedDependencies(hostInput({
    diagnostics: {processUptimeSeconds: 9},
    expected: {installMonotonicNs: '95000000000'},
  }));
  await assert.rejects(() => prepareRoutingSession({
    packageRoot: '/installed/maka', cwd: '/tmp/task', sessionId: SESSION,
  }, stale.deps), /active session.*restart refused/u);
  assert.deepEqual(stale.requests.map(({operation}) => operation), [
    'host.diagnostics.query', 'session.catalog.query',
  ]);
});

test('each settled turn reattests the same Host and existing Session', async () => {
  const {deps, requests} = preparedDependencies();
  const input = {
    packageRoot: '/installed/maka', cwd: '/tmp/task', sessionId: SESSION,
    expectedHost: {pid: 4242, hostEpoch: 'host-epoch-1'},
  };
  const original = deps.connection.request;
  deps.connection.request = async (operation, payload) => {
    if (operation === 'session.catalog.query') {
      requests.push({operation, payload});
      return {kind: 'session', session: {
        id: SESSION, model: 'gpt-6.1-sol', llmConnectionSlug: 'keychain-openai',
        thinkingLevel: 'xhigh', permissionMode: 'ask',
      }};
    }
    return original(operation, payload);
  };
  await verifyRoutingSessionHost(input, deps);
  assert.deepEqual(requests.map(({operation}) => operation), [
    'host.diagnostics.query', 'session.catalog.query',
    'session.execution_boundary.query',
  ]);
  assert.deepEqual(requests[1].payload, {kind: 'get', sessionId: SESSION});
  const drifted = preparedDependencies(hostInput({diagnostics: {hostEpoch: 'new-host'}}));
  await assert.rejects(() => verifyRoutingSessionHost(input, drifted.deps),
    /Host changed/u);
  assert.deepEqual(drifted.requests.map(({operation}) => operation),
    ['host.diagnostics.query']);
});

test('Host-only preflight rejects drift without Session or Turn operations', async () => {
  const {deps, requests} = preparedDependencies();
  const original = await attestExistingHost({packageRoot: '/installed/maka'}, deps);
  assert.equal(original.pid, 4242);
  assert.deepEqual(requests.map(({operation}) => operation), ['host.diagnostics.query']);
  const drifted = preparedDependencies(hostInput({diagnostics: {hostEpoch: 'new-host'}}));
  await assert.rejects(() => attestExistingHost({
    packageRoot: '/installed/maka', expectedHost: original,
  }, drifted.deps), /Host changed/u);
  assert.deepEqual(drifted.requests.map(({operation}) => operation),
    ['host.diagnostics.query']);
});

test('native run receives only the already-attested Host connection', async () => {
  const {deps, requests} = preparedDependencies();
  deps.profile = {id: 'local', kind: 'local', name: 'Local'};
  deps.clientDataRoot = '/profile/client';
  const original = deps.connection.request;
  deps.connection.request = async (operation, payload) => {
    if (operation === 'session.catalog.query') {
      requests.push({operation, payload});
      return {kind: 'session', session: {
        id: SESSION, model: 'gpt-6.1-sol', llmConnectionSlug: 'keychain-openai',
        thinkingLevel: 'xhigh', permissionMode: 'ask',
      }};
    }
    return original(operation, payload);
  };
  deps.runRuntimeHostTextCli = async (argv, overrides, commandOverrides, options) => {
    assert.deepEqual(argv, ['prompt', '--cwd', '/tmp/task', '--timeout', '120',
      '--max-steps', '2', '--thinking', 'xhigh', '--resume', SESSION]);
    assert.equal(overrides.workspaceRoot(), ROOT);
    assert.equal(options.cliCommand, 'maka');
    const connected = await commandOverrides.connect(ROOT, undefined, '/profile/client');
    assert.equal(connected.connection, deps.connection);
    assert.equal(connected.profile.kind, 'local');
    await connected.close();
    return 0;
  };
  const argv = ['prompt', '--cwd', '/tmp/task', '--timeout', '120', '--max-steps', '2',
    '--thinking', 'xhigh', '--resume', SESSION];
  const result = await runRoutingTurn(argv, {
    packageRoot: '/installed/maka', cwd: '/tmp/task', sessionId: SESSION,
    expectedHost: {pid: 4242, hostEpoch: 'host-epoch-1'},
    enforceSessionProfile: true,
  }, deps);
  assert.equal(result, 0);
  assert.deepEqual(requests.map(({operation}) => operation), [
    'host.diagnostics.query', 'session.catalog.query',
    'session.execution_boundary.query', 'host.diagnostics.query',
    'session.catalog.query', 'session.execution_boundary.query',
    'host.diagnostics.query',
    'session.catalog.query', 'session.execution_boundary.query',
  ]);
});

test('native run refuses Host drift and unbound run options before invoking the engine', async () => {
  const {deps} = preparedDependencies(hostInput({diagnostics: {hostEpoch: 'new-host'}}));
  let invoked = 0;
  deps.runRuntimeHostTextCli = async () => { invoked += 1; return 0; };
  const input = {
    packageRoot: '/installed/maka', cwd: '/tmp/task', sessionId: SESSION,
    expectedHost: {pid: 4242, hostEpoch: 'host-epoch-1'},
    enforceSessionProfile: true,
  };
  const safe = ['prompt', '--cwd', '/tmp/task', '--timeout', '120', '--max-steps', '2',
    '--thinking', 'xhigh', '--resume', SESSION];
  await assert.rejects(() => runRoutingTurn(safe, input, deps), /Host changed/u);
  await assert.rejects(() => runRoutingTurn([...safe, '--yolo'], input, deps),
    /run input is invalid/u);
  await assert.rejects(() => runRoutingTurn(safe.filter((part) => part !== '--resume'), input, deps),
    /run input is invalid/u);
  assert.equal(invoked, 0);
});

test('durable retention receives exactly the actual settled native turn admission', async () => {
  for (const fault of [null, 'foreign', 'missing', 'duplicate']) {
    const {deps} = preparedDependencies();
    deps.profile = {id: 'local', kind: 'local', name: 'Local'};
    deps.clientDataRoot = '/profile/client';
    const original = deps.connection.request;
    const actual = {sessionId: SESSION, runId: 'actual-run', turnId: 'actual-turn'};
    deps.connection.request = async (operation, payload) => {
      if (operation === 'session.catalog.query') return {kind: 'session', session: {
        id: SESSION, model: 'gpt-6.1-sol', llmConnectionSlug: 'keychain-openai',
        thinkingLevel: 'xhigh', permissionMode: 'ask',
      }};
      if (operation === 'turn.start') return {kind: 'started', turn: {
        ...actual, ...(fault === 'foreign' ? {sessionId: 'foreign'} : {}),
      }};
      return original(operation, payload);
    };
    const retained = [];
    deps.retainDurableEvidence = async (turn) => retained.push(turn);
    deps.runRuntimeHostTextCli = async (_argv, _overrides, commands) => {
      const connected = await commands.connect(ROOT, undefined, '/profile/client');
      if (fault !== 'missing') await connected.connection.request('turn.start', {prompt: 'prompt'});
      if (fault === 'duplicate') await connected.connection.request('turn.start', {prompt: 'prompt'});
      return 0;
    };
    const argv = ['prompt', '--cwd', '/tmp/task', '--timeout', '120', '--max-steps', '2',
      '--thinking', 'xhigh', '--resume', SESSION];
    const run = () => runRoutingTurn(argv, {
      packageRoot: '/installed/maka', cwd: '/tmp/task', sessionId: SESSION,
      expectedHost: {pid: 4242, hostEpoch: 'host-epoch-1'}, enforceSessionProfile: true,
    }, deps);
    if (fault) {
      await assert.rejects(run, /durable admission/u);
      assert.deepEqual(retained, []);
    } else {
      assert.equal(await run(), 0);
      assert.deepEqual(retained, [actual]);
    }
  }
});
