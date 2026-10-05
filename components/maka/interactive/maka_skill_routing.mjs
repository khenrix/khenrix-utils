#!/usr/bin/env node
/** Preflight a skill-only Maka Session without issuing a model Turn. */

import { execFile as execFileCallback, spawnSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { constants as fsConstants, existsSync } from 'node:fs';
import { lstat, open, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { constants as osConstants } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import process from 'node:process';
import { promisify } from 'node:util';
import { fileURLToPath, pathToFileURL } from 'node:url';

const execFile = promisify(execFileCallback);
const PROFILE = 'khenrix-skill-routing-v1';
const CONNECTION = 'keychain-openai';
const MODEL = 'gpt-6.1-sol';
const BOOT_PATTERN = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/u;
const SESSION_PATTERN = /^fanout-routing-[0-9a-f]{32}$/u;
const DIGEST_PATTERN = /^[0-9a-f]{64}$/u;

export async function verifyPrivateHostFiles(spec, deps = {}) {
  const read = deps.readFile ?? readFile;
  const componentRoot = dirname(dirname(spec.packageRoot ?? ''));
  if (spec.helperPath !== join(componentRoot, 'interactive', 'maka_skill_routing.mjs') ||
      spec.sealSourcePath !== join(componentRoot, 'interactive', 'maka_host_seal.c')) {
    throw new Error('Maka private Host source path is unverified');
  }
  if (spec.sealAddonPath !== join(dirname(spec.witnessPath ?? ''), 'maka-host-seal.node')) {
    throw new Error('Maka private Host addon path is unverified');
  }
  for (const [kind, pathKey, digestKey] of [
    ['source', 'nodePath', 'nodeDigest', null],
    ['source', 'helperPath', 'helperDigest', null],
    ['source', 'sealSourcePath', 'sealSourceDigest'],
    ['addon', 'sealAddonPath', 'sealAddonDigest'],
    ['compiler', 'compilerPath', 'compilerDigest'],
  ]) {
    if (!DIGEST_PATTERN.test(spec[digestKey])) {
      throw new Error(`Maka private Host ${kind} digest is invalid`);
    }
    const bytes = await read(spec[pathKey]);
    if (!Buffer.isBuffer(bytes) ||
        createHash('sha256').update(bytes).digest('hex') !== spec[digestKey]) {
      throw new Error(`Maka private Host ${kind} changed before launch`);
    }
  }
}

export function verifyNativeHostDenial(
  absoluteExecutable, selfExecutable, markerPath, deps = {},
) {
  if (typeof absoluteExecutable !== 'string' || !absoluteExecutable.startsWith('/') ||
      typeof selfExecutable !== 'string' || !selfExecutable.startsWith('/') ||
      typeof markerPath !== 'string' || !markerPath.startsWith('/')) {
    throw new Error('Maka native sandbox probe input is invalid');
  }
  const markerExists = deps.markerExists ?? existsSync;
  if (markerExists(markerPath)) throw new Error('Maka native sandbox probe marker already exists');
  const result = (deps.spawnSync ?? spawnSync)(absoluteExecutable, [], {
    encoding: 'utf8', timeout: 3000, stdio: 'ignore',
  });
  const absoluteExecErrno = deps.probeExec?.(absoluteExecutable, markerPath);
  const selfExecErrno = deps.probeExec?.(selfExecutable, markerPath);
  if (result?.error?.code !== 'EPERM' ||
      absoluteExecErrno !== osConstants.errno.EPERM ||
      selfExecErrno !== osConstants.errno.EPERM || markerExists(markerPath)) {
    throw new Error('Maka native sandbox did not deny absolute and self exec');
  }
  return {absoluteExecutable, selfExecutable, denied: true,
    forkErrorCode: 'EPERM', absoluteExecErrno, selfExecErrno};
}

export async function runPrivateHostPreload(spec, deps = {}) {
  const argv = deps.argv ?? process.argv;
  const pid = deps.pid ?? process.pid;
  if (spec?.schema !== 'khenrix-maka-private-host-spec-v1' ||
      !DIGEST_PATTERN.test(spec.rootId) || !DIGEST_PATTERN.test(spec.nonce) ||
      argv?.[1] !== spec.candidateEntrypoint || argv?.[2] !== '--root' ||
      argv?.[3] !== spec.rootPath || argv?.[4] !== '--expected-root-id' ||
      argv?.[5] !== spec.rootId || !Number.isSafeInteger(pid) || pid <= 0) {
    throw new Error('Maka private Host preload is unverified');
  }
  const denial = verifyNativeHostDenial(
    spec.absoluteExecutable, spec.nodePath, spec.markerPath, deps,
  );
  const witness = {
    schema: 'khenrix-maka-private-host-preload-v1', pid,
    rootPath: spec.rootPath, rootId: spec.rootId, nonce: spec.nonce, denial,
  };
  const writeWitness = deps.writeWitness ?? ((path, value) =>
    writeFile(path, JSON.stringify(value) + '\n', {flag: 'wx', mode: 0o600}));
  await writeWitness(spec.witnessPath, witness);
  return witness;
}

export function verifyPrivateHostWitness(witness, expected) {
  const binding = [
    'rootPath', 'clientDataRoot', 'rootId', 'controlPath',
    'pid', 'hostEpoch', 'startedAtMs', 'packageDigest', 'nodeDigest',
    'helperDigest', 'sealSourceDigest', 'sealAddonDigest', 'compilerDigest', 'nonce',
  ];
  if (!witness || typeof witness !== 'object' || Array.isArray(witness) ||
      witness.schema !== 'khenrix-maka-private-host-v1' ||
      binding.some((key) => witness[key] !== expected?.[key]) ||
      !Number.isSafeInteger(witness.pid) || witness.pid <= 0 ||
      !Number.isFinite(witness.startedAtMs) || witness.startedAtMs <= 0 ||
      !DIGEST_PATTERN.test(witness.rootId) ||
      !DIGEST_PATTERN.test(witness.packageDigest) ||
      !DIGEST_PATTERN.test(witness.nodeDigest) ||
      !DIGEST_PATTERN.test(witness.helperDigest) ||
      !DIGEST_PATTERN.test(witness.sealSourceDigest) ||
      !DIGEST_PATTERN.test(witness.sealAddonDigest) ||
      !DIGEST_PATTERN.test(witness.compilerDigest) ||
      !DIGEST_PATTERN.test(witness.nonce) ||
      witness.denial?.denied !== true ||
      witness.denial.forkErrorCode !== 'EPERM' ||
      witness.denial.absoluteExecErrno !== osConstants.errno.EPERM ||
      witness.denial.selfExecErrno !== osConstants.errno.EPERM ||
      typeof witness.denial.selfExecutable !== 'string' ||
      !witness.denial.selfExecutable.startsWith('/') ||
      typeof witness.denial.absoluteExecutable !== 'string' ||
      !witness.denial.absoluteExecutable.startsWith('/')) {
    throw new Error('Maka private Host witness is unverified');
  }
  return witness;
}

async function pathExists(path) {
  try {
    await lstat(path);
    return true;
  } catch (error) {
    if (error?.code === 'ENOENT') return false;
    throw error;
  }
}

export async function previewPrivateHostRoot(input, deps) {
  const rootPath = resolve(input?.rootPath ?? '');
  const clientDataRoot = resolve(input?.clientDataRoot ?? '');
  if (input?.rootPath !== rootPath || input?.clientDataRoot !== clientDataRoot ||
      input?.defaultRootPath === rootPath ||
      rootPath !== join(clientDataRoot, 'workspaces', 'default')) {
    throw new Error('Maka private Host root is unverified');
  }
  const capability = await deps.resolveStorageRoot({path: rootPath, kind: 'interactive'});
  if (capability?.canonicalPath !== rootPath ||
      !DIGEST_PATTERN.test(capability.rootId)) {
    throw new Error('Maka private Host root is unverified');
  }
  const controlPath = join(deps.resolveRootControlNamespace(), capability.rootId);
  if (await (deps.pathExists ?? pathExists)(controlPath)) {
    throw new Error('Maka private Host control already exists');
  }
  return {rootPath, clientDataRoot, rootId: capability.rootId,
    controlPath, controlPreexisted: false};
}

export async function startPrivateRoutingHost(spec, deps) {
  if (spec?.schema !== 'khenrix-maka-private-host-spec-v1' ||
      typeof deps?.connectOwnedRuntimeHost !== 'function' ||
      typeof deps?.readPreloadWitness !== 'function' ||
      typeof deps?.processProof !== 'function' ||
      typeof deps?.configureRelay !== 'function') {
    throw new Error('Maka private Host launch input is invalid');
  }
  const preview = await previewPrivateHostRoot(spec, deps);
  if (preview.rootId !== spec.rootId || preview.controlPath !== spec.controlPath) {
    throw new Error('Maka private Host root identity changed before launch');
  }
  const connected = await deps.connectOwnedRuntimeHost({
    ...deps.launchInput, rootPath: spec.rootPath,
    closeOnLauncherExit: true,
  });
  if (connected?.kind !== 'connected' || !connected.connection || !connected.host) {
    throw new Error('Maka private Host did not start as an owned process');
  }
  try {
    const {connection, host} = connected;
    if (connection.rootId !== spec.rootId) {
      throw new Error('Maka private Host root identity changed after launch');
    }
    const diagnostics = await connection.request('host.diagnostics.query', {});
    const observed = await deps.processProof(diagnostics.pid);
    const identity = verifyHostIdentity({
      diagnostics, process: observed, expected: deps.expected,
    });
    if (host.pid !== identity.pid) {
      throw new Error('Maka private Host candidate PID changed after launch');
    }
    const preload = await deps.readPreloadWitness(spec.witnessPath);
    if (preload?.schema !== 'khenrix-maka-private-host-preload-v1' ||
        preload.pid !== identity.pid || preload.rootId !== spec.rootId ||
        preload.rootPath !== spec.rootPath || preload.nonce !== spec.nonce ||
        preload.denial?.absoluteExecutable !== spec.absoluteExecutable ||
        preload.denial?.selfExecutable !== spec.nodePath) {
      throw new Error('Maka private Host witness is unverified');
    }
    const attestation = verifyPrivateHostWitness({
      schema: 'khenrix-maka-private-host-v1',
      rootPath: spec.rootPath, clientDataRoot: spec.clientDataRoot,
      rootId: spec.rootId, controlPath: spec.controlPath,
      pid: identity.pid, hostEpoch: identity.hostEpoch,
      startedAtMs: identity.startedAtMs,
      packageDigest: spec.packageDigest, nodeDigest: spec.nodeDigest,
      helperDigest: spec.helperDigest, sealSourceDigest: spec.sealSourceDigest,
      sealAddonDigest: spec.sealAddonDigest, compilerDigest: spec.compilerDigest,
      nonce: spec.nonce,
      denial: preload.denial,
    }, {
      ...spec, pid: identity.pid, hostEpoch: identity.hostEpoch,
      startedAtMs: identity.startedAtMs,
    });
    await deps.configureRelay(connection);
    return {...connected, attestation};
  } catch (error) {
    await connected.connection.close().catch(() => undefined);
    await connected.host.settle(1_000).catch(() => undefined);
    throw error;
  }
}

async function readPrivateJson(path) {
  const descriptor = await open(path, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
  try {
    const metadata = await descriptor.stat();
    if (!metadata.isFile() || metadata.uid !== process.getuid() ||
        (metadata.mode & 0o077) !== 0 || metadata.size > 16 * 1024) {
      throw new Error('Maka private Host receipt is unsafe');
    }
    return JSON.parse(await descriptor.readFile('utf8'));
  } finally {
    await descriptor.close();
  }
}

async function privateHostRootDependencies(spec) {
  const authority = await import(pathToFileURL(join(spec.packageRoot,
    'node_modules/@maka/storage/dist/root-authority.js')));
  return {
    resolveStorageRoot: authority.resolveStorageRoot,
    resolveRootControlNamespace: authority.resolveRootControlNamespace,
  };
}

async function privateHostPreview(spec) {
  await verifyPrivateHostFiles(spec);
  const roots = await import(pathToFileURL(join(spec.packageRoot, 'dist/workspace-root.js')));
  if (spec.defaultRootPath !== roots.resolveMakaWorkspaceRoot() ||
      spec.candidateEntrypoint !== join(spec.packageRoot,
        'node_modules/@maka/runtime-host/dist/execution-candidate-main.js') ||
      spec.nodePath !== process.execPath ||
      spec.helperPath !== fileURLToPath(import.meta.url)) {
    throw new Error('Maka private Host installed source changed');
  }
  const result = await previewPrivateHostRoot(spec, await privateHostRootDependencies(spec));
  if (result.rootId !== spec.rootId || result.controlPath !== spec.controlPath) {
    throw new Error('Maka private Host control binding changed');
  }
  return result;
}

async function defaultHostSnapshot(spec) {
  const connectionModule = await import(pathToFileURL(join(spec.packageRoot,
    'node_modules/@maka/runtime-host/dist/client/connection.js')));
  const protocol = await import(pathToFileURL(join(spec.packageRoot,
    'node_modules/@maka/runtime-host/dist/protocol/index.js')));
  const connected = await connectionModule.connectExistingRuntimeHost({
    rootPath: spec.defaultRootPath, clientInstanceId: randomUUID(),
    protocol: {min: protocol.RUNTIME_HOST_PROTOCOL_VERSION,
      max: protocol.RUNTIME_HOST_PROTOCOL_VERSION},
    compositionId: protocol.INTERACTIVE_RUNTIME_HOST_COMPOSITION_ID,
  });
  if (connected.kind === 'unavailable') return {kind: 'unavailable'};
  if (connected.kind !== 'connected') {
    throw new Error('Maka default Host snapshot is unavailable');
  }
  try {
    const diagnostics = await connected.connection.request('host.diagnostics.query', {});
    const sessions = [];
    let query = {kind: 'list_start'};
    for (let pageNumber = 0; pageNumber < 64; pageNumber += 1) {
      const page = await connected.connection.request('session.catalog.query', query);
      if (page?.kind !== 'page' || !Array.isArray(page.sessions)) {
        throw new Error('Maka default Host Sessions are unverified');
      }
      sessions.push(...page.sessions.map((item) => item.id).sort());
      if (page.nextCursor === null) {
        return {kind: 'connected', rootId: connected.connection.rootId,
          pid: diagnostics.pid, hostEpoch: diagnostics.hostEpoch, sessions: sessions.sort()};
      }
      if (typeof page.nextCursor !== 'string' || !page.nextCursor) {
        throw new Error('Maka default Host Sessions are unverified');
      }
      query = {kind: 'list_continue', revision: page.revision, cursor: page.nextCursor};
    }
    throw new Error('Maka default Host Sessions are unverified');
  } finally {
    await connected.connection.close();
  }
}

async function supervisePrivateHost(specPath) {
  const spec = await readPrivateJson(specPath);
  if (spec?.schema !== 'khenrix-maka-private-host-spec-v1' ||
      Object.keys(process.env).some((name) =>
        name.startsWith('NODE_') || name.startsWith('DYLD_') || name === 'LD_PRELOAD') ||
      process.env.KHENRIX_MAKA_PRIVATE_HOST_PRELOAD_SPEC ||
      process.env.KHENRIX_MAKA_PRIVATE_HOST_SEAL_ADDON ||
      process.env.KHENRIX_MAKA_PRIVATE_HOST_SEAL_DIGEST) {
    throw new Error('Maka private Host supervisor input is invalid');
  }
  const defaultBefore = await defaultHostSnapshot(spec);
  const preview = await privateHostPreview(spec);
  if (JSON.stringify(await defaultHostSnapshot(spec)) !== JSON.stringify(defaultBefore)) {
    throw new Error('Maka default Host changed before private launch');
  }
  const packageRoot = spec.packageRoot;
  const hostClient = await import(pathToFileURL(join(packageRoot,
    'node_modules/@maka/runtime-host/dist/client/connect-or-spawn.js')));
  const protocol = await import(pathToFileURL(join(packageRoot,
    'node_modules/@maka/runtime-host/dist/protocol/index.js')));
  const catalogReader = await import(pathToFileURL(join(packageRoot,
    'node_modules/@maka/runtime-host/dist/client/catalog-reader.js')));
  const providerRegistry = await import(pathToFileURL(join(packageRoot,
    'node_modules/@maka/core/dist/provider-registry.js')));
  const relay = await import('./configure_maka_openai_relay.mjs');
  const expected = {
    uid: process.getuid(), node: spec.nodePath,
    entrypoint: spec.candidateEntrypoint, rootPath: spec.rootPath,
    installBootSession: spec.installBootSession,
    currentBootSession: await currentBootSession(),
    installMonotonicNs: spec.installMonotonicNs,
    monotonicNowNs: process.hrtime.bigint().toString(),
  };
  process.env.NODE_OPTIONS = `--require=${spec.sealAddonPath} ` +
    `--import=${pathToFileURL(fileURLToPath(import.meta.url)).href}`;
  process.env.KHENRIX_MAKA_PRIVATE_HOST_PRELOAD_SPEC = specPath;
  process.env.KHENRIX_MAKA_PRIVATE_HOST_SEAL_ADDON = spec.sealAddonPath;
  process.env.KHENRIX_MAKA_PRIVATE_HOST_SEAL_DIGEST = spec.sealAddonDigest;
  let started;
  try {
    started = await startPrivateRoutingHost(spec, {
      ...await privateHostRootDependencies(spec),
      launchInput: {
        protocol: {min: protocol.RUNTIME_HOST_PROTOCOL_VERSION,
          max: protocol.RUNTIME_HOST_PROTOCOL_VERSION},
        compositionId: protocol.INTERACTIVE_RUNTIME_HOST_COMPOSITION_ID,
        initialization: {incognito: true},
      },
      connectOwnedRuntimeHost: hostClient.connectOwnedRuntimeHost,
      readPreloadWitness: readPrivateJson,
      processProof: (pid) => processProof(pid, packageRoot, spec.rootPath),
      expected,
      configureRelay: async (connection) => {
        const token = await relay.readPrivateToken(spec.tokenFile);
        const attestation = await relay.readPrivateToken(spec.attestationFile);
        await relay.reconcileMakaRelay(connection, {
          token, readCatalog: catalogReader.readRuntimeHostConnectionCatalog,
          providerAuthKind: (type) => providerRegistry.PROVIDER_REGISTRY[type]?.authKind,
          assertReady: ({token: callerToken, port}) =>
            relay.assertAttestedRelayReady({token: callerToken, attestation, port}),
        });
      },
    });
  } finally {
    delete process.env.NODE_OPTIONS;
    delete process.env.KHENRIX_MAKA_PRIVATE_HOST_PRELOAD_SPEC;
    delete process.env.KHENRIX_MAKA_PRIVATE_HOST_SEAL_ADDON;
    delete process.env.KHENRIX_MAKA_PRIVATE_HOST_SEAL_DIGEST;
  }
  let canSettle = false;
  try {
    if (JSON.stringify(await defaultHostSnapshot(spec)) !== JSON.stringify(defaultBefore)) {
      throw new Error('Maka default Host changed during private launch');
    }
    process.stdout.write(`${JSON.stringify({
      ...started.attestation, defaultHost: defaultBefore,
      controlPreexisted: preview.controlPreexisted,
    })}\n`);
    await new Promise((resolveEnd) => {
      process.stdin.once('end', resolveEnd);
      process.stdin.resume();
    });
    if ((await activeSessions(started.connection)).length > 0) {
      throw new Error('Maka private Host has active Sessions; settlement refused');
    }
    canSettle = true;
  } finally {
    if (!canSettle) {
      started.host.releaseToEnvironment();
      await started.connection.close().catch(() => undefined);
    } else {
      await started.connection.close();
      const settled = await started.host.settle(5_000);
      if (!settled ||
          JSON.stringify(await defaultHostSnapshot(spec)) !== JSON.stringify(defaultBefore)) {
        throw new Error('Maka private Host settlement or default Host identity is unverified');
      }
      process.stdout.write(`${JSON.stringify({schema: 'khenrix-maka-private-host-settled-v1',
        rootId: spec.rootId, pid: started.attestation.pid,
        hostEpoch: started.attestation.hostEpoch, activeSessions: 0,
        defaultHost: defaultBefore})}\n`);
    }
  }
}

export function verifyInstalledToolCeiling(module) {
  const profile = module.hostedExecutionRunProfile(PROFILE);
  if (profile?.memoryExtraction !== false ||
      JSON.stringify(profile.toolNames) !== '["Skill","SkillSearch"]') {
    throw new Error('Maka routing installed tool ceiling is unverified');
  }
  const skill = {name: 'Skill'};
  const search = {name: 'SkillSearch'};
  const bash = {name: 'Bash'};
  const selected = module.projectHostedExecutionTools([skill, search, bash], PROFILE);
  if (!Array.isArray(selected) || selected.length !== 2 ||
      selected[0] !== skill || selected[1] !== search) {
    throw new Error('Maka routing installed tool ceiling is unverified');
  }
  try {
    module.projectHostedExecutionTools([bash], PROFILE);
  } catch {
    return {profile: PROFILE, tools: ['Skill', 'SkillSearch']};
  }
  throw new Error('Maka routing installed tool ceiling is unverified');
}

export function verifyHostIdentity({diagnostics, process: observed, expected, activeSessions = []}) {
  if (!Number.isSafeInteger(diagnostics?.pid) || diagnostics.pid <= 0 ||
      typeof diagnostics.hostEpoch !== 'string' || !diagnostics.hostEpoch ||
      diagnostics.compositionId !== 'maka.interactive' ||
      diagnostics.compositionRevision !== '3' ||
      !Number.isSafeInteger(diagnostics.processUptimeSeconds) ||
      diagnostics.processUptimeSeconds < 0 ||
      !Number.isSafeInteger(observed?.pid) || diagnostics.pid !== observed.pid ||
      !Number.isSafeInteger(observed?.uid) || observed.uid !== expected?.uid) {
    throw new Error('Maka Host identity is unverified');
  }
  if (observed.node !== expected.node || observed.entrypoint !== expected.entrypoint ||
      observed.rootPath !== expected.rootPath ||
      (expected.startedAtMs !== undefined && observed.startedAtMs !== expected.startedAtMs) ||
      !Number.isFinite(observed.startedAtMs) || observed.startedAtMs <= 0) {
    throw new Error('Maka Host entrypoint differs from the installed runtime');
  }
  if (!BOOT_PATTERN.test(expected.installBootSession) ||
      !BOOT_PATTERN.test(expected.currentBootSession) ||
      !/^[1-9][0-9]{0,19}$/u.test(expected.installMonotonicNs) ||
      !/^[1-9][0-9]{0,19}$/u.test(expected.monotonicNowNs)) {
    throw new Error('Maka Host source freshness is unavailable');
  }
  const installed = BigInt(expected.installMonotonicNs);
  const now = BigInt(expected.monotonicNowNs);
  if (expected.installBootSession === expected.currentBootSession && now <= installed) {
    throw new Error('Maka Host source freshness is unavailable');
  }
  const uptimeNs = BigInt(diagnostics.processUptimeSeconds) * 1_000_000_000n;
  const fresh = expected.installBootSession !== expected.currentBootSession ||
    now - (uptimeNs + 1_000_000_000n) > installed;
  if (!fresh) {
    if (!Array.isArray(activeSessions)) throw new Error('Maka Host active Sessions are unverified');
    if (activeSessions.length > 0) {
      throw new Error('Maka Host is stale with an active session; restart refused');
    }
    throw new Error('Maka Host is stale; a scoped restart needs separate approval');
  }
  return {
    pid: diagnostics.pid, hostEpoch: diagnostics.hostEpoch,
    startedAtMs: observed.startedAtMs,
  };
}

async function currentBootSession() {
  const result = await execFile('/usr/sbin/sysctl', ['-n', 'kern.bootsessionuuid'], {
    encoding: 'utf8', timeout: 5000, maxBuffer: 1024,
  });
  const boot = result.stdout.trim().toLowerCase();
  if (!BOOT_PATTERN.test(boot)) throw new Error('Maka Host source freshness is unavailable');
  return boot;
}

export async function processProof(pid, packageRoot, rootPath, deps = {}) {
  const {hostCommandMatches, parseHostStartTime} = await import('./maka_judge.mjs');
  if (process.platform !== 'darwin') {
    throw new Error('Maka Host process proof requires macOS');
  }
  const runCommand = deps.execFile ?? execFile;
  const options = {encoding: 'utf8', timeout: 5000, maxBuffer: 16 * 1024};
  const uid = await runCommand('/bin/ps', ['-p', String(pid), '-o', 'uid='], options);
  const command = await runCommand('/bin/ps', ['-ww', '-p', String(pid), '-o', 'args='], options);
  const started = await runCommand('/bin/ps', ['-p', String(pid), '-o', 'lstart='], {
    ...options, env: {LC_ALL: 'C', TZ: 'UTC0'},
  });
  const entrypoint = join(packageRoot,
    'node_modules/@maka/runtime-host/dist/execution-candidate-main.js');
  if (uid.stdout.trim() !== String(process.getuid()) ||
      !hostCommandMatches(command.stdout, process.execPath, entrypoint, rootPath)) {
    throw new Error('Maka Host entrypoint differs from the installed runtime');
  }
  const coarse = parseHostStartTime(started.stdout);
  const precise = await runCommand('/usr/bin/python3', [
    join(dirname(fileURLToPath(import.meta.url)), 'maka_host_start.py'), String(pid),
  ], {encoding: 'utf8', timeout: 5000, maxBuffer: 1024,
    env: {LC_ALL: 'C'}});
  let proof;
  try {
    proof = JSON.parse(precise.stdout);
  } catch {
    throw new Error('Maka Host source freshness is unavailable');
  }
  if (coarse === null || proof?.pid !== pid || proof.uid !== process.getuid() ||
      !Number.isSafeInteger(proof.seconds) || proof.seconds <= 0 ||
      !Number.isSafeInteger(proof.microseconds) || proof.microseconds < 0 ||
      proof.microseconds >= 1_000_000) {
    throw new Error('Maka Host source freshness is unavailable');
  }
  const startedAtMs = proof.seconds * 1000 + proof.microseconds / 1000;
  if (Math.floor(startedAtMs / 1000) * 1000 !== coarse) {
    throw new Error('Maka Host source freshness is unavailable');
  }
  return {
    pid, uid: process.getuid(), node: process.execPath, entrypoint, rootPath, startedAtMs,
  };
}

async function activeSessions(connection) {
  const running = [];
  let query = {kind: 'list_start'};
  for (let pageNumber = 0; pageNumber < 64; pageNumber += 1) {
    const page = await connection.request('session.catalog.query', query);
    if (page?.kind !== 'page' || !Array.isArray(page.sessions)) {
      throw new Error('Maka Host active Sessions are unverified');
    }
    for (const session of page.sessions) {
      if (session?.liveRunState?.runningTurnIds?.length > 0) running.push(session.id);
    }
    if (page.nextCursor === null) return running;
    if (typeof page.nextCursor !== 'string' || !page.nextCursor) {
      throw new Error('Maka Host active Sessions are unverified');
    }
    query = {kind: 'list_continue', revision: page.revision, cursor: page.nextCursor};
  }
  throw new Error('Maka Host active Sessions are unverified');
}

export async function prepareRoutingSession(input, deps) {
  if (!SESSION_PATTERN.test(input.sessionId) ||
      input.packageRoot !== resolve(input.packageRoot) ||
      input.cwd !== resolve(input.cwd)) {
    throw new Error('Maka routing preflight input is invalid');
  }
  const diagnostics = await deps.connection.request('host.diagnostics.query', {});
  const observed = await deps.processProof(diagnostics.pid, input.packageRoot, deps.rootPath);
  let running = [];
  try {
    verifyHostIdentity({
      diagnostics, process: observed,
      expected: deps.expected, activeSessions: [],
    });
  } catch (error) {
    if (!String(error).includes('Host is stale')) throw error;
    running = await activeSessions(deps.connection);
  }
  const identity = verifyHostIdentity({
    diagnostics, process: observed, expected: deps.expected, activeSessions: running,
  });
  const matches = deps.catalog.connections.filter((item) =>
    item.slug === CONNECTION && item.enabled === true &&
    item.providerType === 'custom' &&
    item.defaultApiProtocol === 'openai-responses' &&
    item.enabledModelIds.includes(MODEL) &&
    new URL(item.baseUrl).toString() === 'http://127.0.0.1:48173/v1');
  if (matches.length !== 1) throw new Error('Maka routing relay connection is unverified');
  const created = await deps.connection.request('session.create', {
    sessionId: input.sessionId,
    workspace: {kind: 'host_path', path: input.cwd},
    name: 'Fanout skill routing smoke',
    modelTarget: {
      kind: 'explicit', connectionId: matches[0].connectionId,
      connectionSlug: CONNECTION, model: MODEL,
    },
    thinkingLevel: 'xhigh', toolProfile: PROFILE, permissionMode: 'ask',
    collaborationMode: 'agent', orchestrationMode: 'default',
  });
  if (created.id !== input.sessionId || created.model !== MODEL ||
      created.llmConnectionId !== matches[0].connectionId ||
      created.llmConnectionSlug !== CONNECTION || created.thinkingLevel !== 'xhigh' ||
      created.permissionMode !== 'ask') {
    throw new Error('Maka routing Session target is unverified');
  }
  const boundary = await deps.connection.request('session.execution_boundary.query', {
    sessionId: input.sessionId,
  });
  if (boundary?.kind !== 'managed') {
    throw new Error('Maka routing execution boundary is unverified');
  }
  return {sessionId: input.sessionId, host: identity, boundary};
}

export async function verifyRoutingSessionHost(input, deps) {
  if (!SESSION_PATTERN.test(input.sessionId) ||
      !Number.isSafeInteger(input.expectedHost?.pid) || input.expectedHost.pid <= 0 ||
      typeof input.expectedHost.hostEpoch !== 'string' || !input.expectedHost.hostEpoch) {
    throw new Error('Maka routing attachment input is invalid');
  }
  const diagnostics = await deps.connection.request('host.diagnostics.query', {});
  const observed = await deps.processProof(diagnostics.pid, input.packageRoot, deps.rootPath);
  const identity = verifyHostIdentity({
    diagnostics, process: observed,
    expected: {...deps.expected,
      monotonicNowNs: deps.monotonicNowNs?.().toString() ?? deps.expected.monotonicNowNs},
  });
  if (identity.pid !== input.expectedHost.pid ||
      identity.hostEpoch !== input.expectedHost.hostEpoch) {
    throw new Error('Maka routing Host changed between turns');
  }
  const result = await deps.connection.request('session.catalog.query', {
    kind: 'get', sessionId: input.sessionId,
  });
  const session = result?.kind === 'session' ? result.session : null;
  if (session?.id !== input.sessionId || session.model !== MODEL ||
      session.llmConnectionSlug !== CONNECTION || session.thinkingLevel !== 'xhigh' ||
      session.permissionMode !== 'ask') {
    throw new Error('Maka routing Session attachment changed');
  }
  const boundary = await deps.connection.request('session.execution_boundary.query', {
    sessionId: input.sessionId,
  });
  if (boundary?.kind !== 'managed') {
    throw new Error('Maka routing execution boundary changed');
  }
  return {sessionId: input.sessionId, host: identity, boundary};
}

export async function attestExistingHost(input, deps) {
  const diagnostics = await deps.connection.request('host.diagnostics.query', {});
  const observed = await deps.processProof(diagnostics.pid, input.packageRoot, deps.rootPath);
  const identity = verifyHostIdentity({
    diagnostics, process: observed,
    expected: {...deps.expected,
      monotonicNowNs: deps.monotonicNowNs?.().toString() ?? deps.expected.monotonicNowNs},
  });
  if (input.expectedHost &&
      (identity.pid !== input.expectedHost.pid ||
       identity.hostEpoch !== input.expectedHost.hostEpoch)) {
    throw new Error('Maka routing Host changed between turns');
  }
  return identity;
}

export async function runRoutingTurn(argv, input, deps) {
  if (!Number.isSafeInteger(input.expectedHost?.pid) || input.expectedHost.pid <= 0 ||
      typeof input.expectedHost.hostEpoch !== 'string' || !input.expectedHost.hostEpoch ||
      !input.enforceSessionProfile || !SESSION_PATTERN.test(input.sessionId) ||
      argv.length !== 11 || typeof argv[0] !== 'string' || !argv[0].trim() ||
      argv[1] !== '--cwd' || argv[2] !== input.cwd ||
      argv[3] !== '--timeout' || !/^(?:[1-9]|[1-9][0-9]|[12][0-9]{2}|300)$/u.test(argv[4]) ||
      argv[5] !== '--max-steps' || !/^[1-4]$/u.test(argv[6]) ||
      argv[7] !== '--thinking' || argv[8] !== 'xhigh' ||
      argv[9] !== '--resume' || argv[10] !== input.sessionId) {
    throw new Error('Maka routing run input is invalid');
  }
  const attest = () => verifyRoutingSessionHost(input, deps);
  await attest();
  let joined = false;
  const admitted = [];
  const connection = deps.retainDurableEvidence ? new Proxy(deps.connection, {
    get(target, name) {
      if (name === 'request') return async (operation, payload) => {
        const result = await target.request(operation, payload);
        if (operation === 'turn.start') {
          if (result.kind !== 'started' || result.turn?.sessionId !== input.sessionId ||
              typeof result.turn?.turnId !== 'string' || !result.turn.turnId ||
              typeof result.turn?.runId !== 'string' || !result.turn.runId) {
            throw new Error('Maka routing durable admission is unbound');
          }
          admitted.push(result.turn);
        }
        return result;
      };
      const value = Reflect.get(target, name, target);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  }) : deps.connection;
  try {
    const status = await deps.runRuntimeHostTextCli(argv, {
      workspaceRoot: () => deps.rootPath,
    }, {
      connect: async (rootPath, profileId, clientDataRoot) => {
        if (joined || rootPath !== deps.rootPath ||
            (profileId !== undefined && profileId !== 'local') ||
            clientDataRoot !== deps.clientDataRoot) {
          throw new Error('Maka routing Host attachment differs');
        }
        await attest();
        joined = true;
        return {
          connection, catalog: deps.catalog, profile: deps.profile,
          close: async () => {},
        };
      },
    }, {clientDataRoot: deps.clientDataRoot, cliCommand: 'maka'});
    if (!joined || !Number.isInteger(status) || status < 0 || status > 255) {
      throw new Error('Maka routing native CLI did not settle');
    }
    if (status === 0 && deps.retainDurableEvidence) {
      if (admitted.length !== 1) throw new Error('Maka routing durable admission is ambiguous');
      await deps.retainDurableEvidence(admitted[0]);
    }
    return status;
  } finally {
    if (joined) await attest();
  }
}

export async function readRoutingDurableEvidence(rootPath, turn, persistence) {
  const opened = await persistence.openRuntimeEventReadPersistence({ workspaceRoot: rootPath });
  try {
    const invocation = await opened.runtimeEventStore.readRunInvocation(turn.sessionId, turn.runId);
    const read = await opened.runtimeEventStore.readRuntimeEventsBounded(
      turn.sessionId, turn.runId, { maxRecords: 256, maxBytes: 2 * 1024 * 1024 });
    if (!invocation || invocation.sessionId !== turn.sessionId ||
        invocation.runId !== turn.runId || invocation.turnId !== turn.turnId ||
        read.status !== 'complete' || !Array.isArray(read.records)) {
      throw new Error('Maka routing durable evidence is incomplete or exceeds its budget');
    }
    const evidence = { schema: 'khenrix-maka-routing-durable-v1',
      sessionId: turn.sessionId, runId: turn.runId, turnId: turn.turnId,
      invocationId: invocation.invocationId, invocation, records: read.records };
    if (Buffer.byteLength(JSON.stringify(evidence)) > 4 * 1024 * 1024) {
      throw new Error('Maka routing durable evidence exceeds its budget');
    }
    return evidence;
  } finally {
    await opened.close();
  }
}

async function main(argv) {
  if (argv[0] === '--private-host-root-id' && argv.length === 3) {
    const packageRoot = resolve(argv[1]);
    const clientDataRoot = resolve(argv[2]);
    const roots = await import(pathToFileURL(join(packageRoot, 'dist/workspace-root.js')));
    const rootPath = join(clientDataRoot, 'workspaces', 'default');
    const preview = await previewPrivateHostRoot({
      rootPath, clientDataRoot, defaultRootPath: roots.resolveMakaWorkspaceRoot(),
    }, await privateHostRootDependencies({packageRoot}));
    process.stdout.write(`${JSON.stringify(preview)}\n`);
    return;
  }
  if (argv[0] === '--private-host-preview' && argv.length === 2) {
    const spec = await readPrivateJson(resolve(argv[1]));
    process.stdout.write(`${JSON.stringify(await privateHostPreview(spec))}\n`);
    return;
  }
  if (argv[0] === '--supervise-private-host' && argv.length === 2) {
    await supervisePrivateHost(resolve(argv[1]));
    return;
  }
  const runIndex = argv.indexOf('--run');
  const runMode = runIndex >= 0;
  const header = runMode ? argv.slice(0, runIndex) : argv;
  const hostOnly = header[header.length - 1] === '--host-only';
  const enforceSessionProfile = header[header.length - 1] === '--enforce-session-profile';
  const identityArgs = header.length >= 12 &&
    header[8] === '--expected-host-pid' && header[10] === '--expected-host-epoch';
  if (![8, 9, 12, 13].includes(header.length) || header[0] !== '--package-root' ||
      header[2] !== '--cwd' || header[4] !== '--session-id' ||
      header[6] !== '--install-monotonic-ns' ||
      (header.length >= 12 && !identityArgs) ||
      (hostOnly && (runMode || ![9, 13].includes(header.length))) ||
      (enforceSessionProfile && (!runMode || header.length !== 13)) ||
      (runMode && (!enforceSessionProfile || !identityArgs || argv[runIndex + 1] !== '--' ||
        argv.length <= runIndex + 2)) ||
      (!runMode && !hostOnly && ![8, 12].includes(header.length))) {
    throw new Error('Maka routing invocation is invalid');
  }
  const packageRoot = resolve(header[1]);
  const cwd = resolve(header[3]);
  const sessionId = header[5];
  const installMonotonicNs = header[7];
  const projection = verifyInstalledToolCeiling(await import(pathToFileURL(join(packageRoot,
    'node_modules/@maka/runtime-host/dist/server/hosted-execution-tool-profile.js'))));
  const rootsModule = await import(pathToFileURL(join(packageRoot, 'dist/workspace-root.js')));
  const connectionModule = await import(pathToFileURL(join(packageRoot,
    'node_modules/@maka/runtime-host/dist/client/connection.js')));
  const protocol = await import(pathToFileURL(join(packageRoot,
    'node_modules/@maka/runtime-host/dist/protocol/index.js')));
  const catalogReader = await import(pathToFileURL(join(packageRoot,
    'node_modules/@maka/runtime-host/dist/client/catalog-reader.js')));
  const profileModule = await import(pathToFileURL(join(packageRoot,
    'node_modules/@maka/runtime-host/dist/client/host-profile.js')));
  const privateRoot = process.env.KHENRIX_MAKA_PRIVATE_ROOT;
  const privateClientDataRoot = process.env.KHENRIX_MAKA_PRIVATE_CLIENT_DATA_ROOT;
  const privateRootId = process.env.KHENRIX_MAKA_PRIVATE_ROOT_ID;
  const privateStartedAtMs = process.env.KHENRIX_MAKA_PRIVATE_HOST_STARTED_AT_MS;
  if ((privateRoot === undefined) !== (privateClientDataRoot === undefined) ||
      (privateRoot === undefined) !== (privateRootId === undefined) ||
      (privateRoot === undefined) !== (privateStartedAtMs === undefined)) {
    throw new Error('Maka private Host attachment input is invalid');
  }
  const rootPath = privateRoot ?? rootsModule.resolveMakaWorkspaceRoot();
  if (privateRoot !== undefined &&
      (resolve(privateRoot) !== privateRoot ||
       rootPath !== join(privateClientDataRoot, 'workspaces', 'default') ||
       rootPath === rootsModule.resolveMakaWorkspaceRoot() ||
       !DIGEST_PATTERN.test(privateRootId) ||
       !Number.isFinite(Number(privateStartedAtMs)) ||
       Number(privateStartedAtMs) <= 0)) {
    throw new Error('Maka private Host attachment input is invalid');
  }
  const expected = {
    uid: process.getuid(), node: process.execPath,
    entrypoint: join(packageRoot,
      'node_modules/@maka/runtime-host/dist/execution-candidate-main.js'),
    rootPath, installBootSession: process.env.KHENRIX_MAKA_INSTALL_BOOT_SESSION,
    currentBootSession: await currentBootSession(), installMonotonicNs,
    monotonicNowNs: process.hrtime.bigint().toString(),
    ...(privateStartedAtMs === undefined ? {} : {startedAtMs: Number(privateStartedAtMs)}),
  };
  const connected = await connectionModule.connectExistingRuntimeHost({
    rootPath, clientInstanceId: randomUUID(),
    protocol: {min: protocol.RUNTIME_HOST_PROTOCOL_VERSION,
      max: protocol.RUNTIME_HOST_PROTOCOL_VERSION},
    compositionId: protocol.INTERACTIVE_RUNTIME_HOST_COMPOSITION_ID,
  });
  if (connected.kind !== 'connected') {
    throw new Error('Maka routing requires an already-running compatible Host');
  }
  try {
    if (privateRootId !== undefined && connected.connection.rootId !== privateRootId) {
      throw new Error('Maka private Host root identity changed');
    }
    const deps = {connection: connected.connection, rootPath, expected, processProof,
      monotonicNowNs: () => process.hrtime.bigint()};
    const expectedHost = identityArgs
      ? {pid: Number(header[9]), hostEpoch: header[11]}
      : undefined;
    if (runMode) {
      const cliCore = await import(pathToFileURL(join(packageRoot, 'dist/cli-core.js')));
      const nativeRun = await import(pathToFileURL(join(packageRoot, 'dist/runtime-host-run-command.js')));
      const peer = await import(pathToFileURL(join(packageRoot, 'dist/runtime-host-peer-artifact.js')));
      const parsed = cliCore.parseMakaCliArgs(argv.slice(runIndex + 2), '0', 'maka');
      if (parsed.kind !== 'run') throw new Error('Maka routing requires the native run command');
      const clientDataRoot = privateClientDataRoot ?? rootsModule.resolveMakaClientDataRoot();
      await peer.configureRuntimeHostPeerClient({
        cliPath: join(packageRoot, 'dist/cli.js'), clientDataRoot,
      });
      const status = await runRoutingTurn(parsed.args, {
        packageRoot, cwd, sessionId, expectedHost, enforceSessionProfile,
      }, {
        ...deps, clientDataRoot, profile: profileModule.LOCAL_RUNTIME_HOST_PROFILE,
        catalog: await catalogReader.readRuntimeHostConnectionCatalog(connected.connection),
        runRuntimeHostTextCli: nativeRun.runRuntimeHostTextCli,
        ...(privateRootId === undefined ? {} : { retainDurableEvidence: async (turn) => {
          const marker = /(?:^|\n)Receipt nonce: (maka:probe-[0-9a-f]{32}-[0-9a-f]{32})$/u.exec(parsed.args[0]);
          if (!marker || !marker[1].endsWith(sessionId.slice('fanout-routing-'.length))) {
            throw new Error('Maka routing durable nonce is unbound');
          }
          const persistence = await import(pathToFileURL(join(packageRoot,
            'node_modules/@maka/storage/dist/runtime-event-persistence.js')));
          const evidence = await readRoutingDurableEvidence(rootPath, turn, persistence);
          const markerHash = createHash('sha256').update(marker[1]).digest('hex');
          await writeFile(join(rootPath, `routing-${markerHash}.json`),
            JSON.stringify(evidence), { mode: 0o600, flag: 'wx' });
        } }),
      });
      process.exitCode = status;
      return;
    }
    const result = hostOnly
      ? {host: await attestExistingHost({packageRoot, expectedHost}, deps)}
      : argv.length === 8
      ? await prepareRoutingSession({packageRoot, cwd, sessionId}, {
        ...deps,
        catalog: await catalogReader.readRuntimeHostConnectionCatalog(connected.connection),
      })
      : await verifyRoutingSessionHost({
        packageRoot, cwd, sessionId, expectedHost,
      }, deps);
    process.stdout.write(`${JSON.stringify({...result, toolProjection: projection,
      ...(privateRootId === undefined ? {} : {privateHost: {
        rootId: privateRootId, rootPath,
      }}),
    })}\n`);
  } finally {
    await connected.connection.close();
  }
}

const preloadSpecPath = process.env.KHENRIX_MAKA_PRIVATE_HOST_PRELOAD_SPEC;
if (preloadSpecPath !== undefined) {
  const addonPath = process.env.KHENRIX_MAKA_PRIVATE_HOST_SEAL_ADDON;
  const addonDigest = process.env.KHENRIX_MAKA_PRIVATE_HOST_SEAL_DIGEST;
  if (process.platform !== 'darwin' || !addonPath?.startsWith('/') ||
      !DIGEST_PATTERN.test(addonDigest) ||
      process.env.NODE_OPTIONS !== `--require=${addonPath} ` +
        `--import=${pathToFileURL(fileURLToPath(import.meta.url)).href}` ||
      !process.argv[1]?.endsWith('/execution-candidate-main.js') ||
      createHash('sha256').update(await readFile(addonPath)).digest('hex') !== addonDigest) {
    throw new Error('Maka private Host native seal input is unverified');
  }
  const native = createRequire(import.meta.url)(addonPath);
  if (typeof native.probeExec !== 'function') {
    throw new Error('Maka private Host native seal failed');
  }
  const preloadSpec = await readPrivateJson(resolve(preloadSpecPath));
  if (preloadSpec.sealAddonPath !== addonPath ||
      preloadSpec.sealAddonDigest !== addonDigest) {
    throw new Error('Maka private Host native seal differs from its receipt');
  }
  await verifyPrivateHostFiles(preloadSpec);
  await runPrivateHostPreload(preloadSpec, {probeExec: native.probeExec});
  delete process.env.NODE_OPTIONS;
  delete process.env.KHENRIX_MAKA_PRIVATE_HOST_PRELOAD_SPEC;
  delete process.env.KHENRIX_MAKA_PRIVATE_HOST_SEAL_ADDON;
  delete process.env.KHENRIX_MAKA_PRIVATE_HOST_SEAL_DIGEST;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`Maka routing preflight failed: ${error.message}\n`);
    process.exitCode = 78;
  });
}
