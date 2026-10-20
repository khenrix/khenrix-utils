/** Exact-session bookkeeping around the ordinary installed Maka CLI.
 * This helper attaches to an existing Host. It never starts a Host or a model turn.
 */
import {pathToFileURL, fileURLToPath} from 'node:url';
import {join, resolve} from 'node:path';
import {randomUUID, createHash} from 'node:crypto';

const LIMIT = 4 * 1024 * 1024;
function requireFact(value, message) {
  if (!value) throw Error(message);
}
function hostIdentity(diagnostics) {
  requireFact(Number.isSafeInteger(diagnostics?.pid) && diagnostics.pid > 0 &&
    typeof diagnostics.hostEpoch === 'string' && diagnostics.hostEpoch, 'Native Host identity unavailable');
  return {pid: diagnostics.pid, hostEpoch: diagnostics.hostEpoch};
}
function checkSession(session, input) {
  requireFact(session?.id === input.session_id && session.model === input.model &&
    session.llmConnectionSlug === input.connection && session.thinkingLevel === 'xhigh' &&
    session.permissionMode === 'bypass' && session.collaborationMode === 'agent', 'Native Session target changed');
  requireFact(session.workspace?.hostCwd === input.cwd &&
    session.workspace?.target?.kind === 'host_path' && session.workspace.target.path === input.cwd,
    'Native Session workspace changed');
}
async function checkHost(input, deps, {allowUnpinned = false} = {}) {
  const expected = allowUnpinned && input.host === undefined ? undefined : hostIdentity(input.host);
  const host = hostIdentity(await deps.connection.request('host.diagnostics.query', {}));
  if (expected) requireFact(host.pid === expected.pid && host.hostEpoch === expected.hostEpoch,
    'Native Host changed');
  return host;
}
function selectedConnection(input, deps) {
  const matches = deps.catalog.connections.filter(c => c.slug === input.connection && c.enabled === true &&
    c.enabledModelIds.includes(input.model));
  requireFact(matches.length === 1, 'Explicit native connection/model unavailable');
  const selected = matches[0];
  requireFact(input.auth_route === 'api-key-relay'
    ? selected.slug === 'keychain-openai' && selected.providerType === 'custom' &&
      selected.defaultApiProtocol === 'openai-responses' && selected.baseUrl === 'http://127.0.0.1:48173/v1'
    : input.auth_route === 'chatgpt-subscription' && selected.providerType === 'openai-codex' &&
      selected.baseUrl === undefined, 'Explicit native auth route differs from the connection');
  return selected;
}

export async function prepareSession(input, deps) {
  const host = await checkHost(input, deps, {allowUnpinned: true});
  const found = await deps.connection.request('session.catalog.query', {kind: 'get', sessionId: input.session_id});
  requireFact(found.kind === 'session' && found.session === null, 'Owned Session already exists or lookup is unsupported');
  const selected = selectedConnection(input, deps);
  const session = await deps.connection.request('session.create', {
    sessionId: input.session_id, workspace: {kind: 'host_path', path: input.cwd},
    name: 'Native fanout evaluation',
    modelTarget: {kind: 'explicit', connectionId: selected.connectionId,
      connectionSlug: input.connection, model: input.model},
    thinkingLevel: 'xhigh', permissionMode: 'bypass', collaborationMode: 'agent', orchestrationMode: 'default',
  });
  checkSession(session, input);
  return {session_id: input.session_id, workspace_root: deps.rootPath, host, permission_mode: 'bypass'};
}

async function readSessionInvocation(input, deps, {allowUnstarted = false} = {}) {
  const host = await checkHost(input, deps);
  selectedConnection(input, deps);
  requireFact(input.workspace_root === deps.rootPath, 'Native workspace changed');
  const found = await deps.connection.request('session.catalog.query', {kind: 'get', sessionId: input.session_id});
  checkSession(found.kind === 'session' ? found.session : null, input);
  const opened = await deps.persistence.openRuntimeEventReadPersistence({workspaceRoot: deps.rootPath});
  try {
    const inventory = await opened.runtimeEventStore.listSessionInvocationsBounded(input.session_id, 1);
    if (allowUnstarted && inventory.truncated === false && inventory.invocations.length === 0) {
      const live = found.session.liveRunState;
      requireFact(input.process_exited === true && Number.isSafeInteger(input.process_returncode) &&
        input.process_returncode !== 0 && live?.schemaVersion === 1 && live.runEpoch === 0 &&
        Array.isArray(live.runningTurnIds) && live.runningTurnIds.length === 0,
        'Unstarted cleanup lacks a failed process and idle native Session proof');
      await checkHost(input, deps);
      return {session_id: input.session_id, workspace_root: deps.rootPath, host, permission_mode: 'bypass',
        invocation: null, not_started: true};
    }
    requireFact(inventory.truncated === false && inventory.invocations.length === 1,
      'Owned Session must contain exactly one invocation');
    const invocation = inventory.invocations[0];
    requireFact(invocation.sessionId === input.session_id && typeof invocation.runId === 'string' &&
      invocation.runId && typeof invocation.turnId === 'string' && invocation.turnId &&
      typeof invocation.invocationId === 'string' && invocation.invocationId, 'Invocation identity unavailable');
    requireFact(invocation.opening?.configuration?.permissionMode === 'bypass',
      'Native invocation permission mode changed');
    await checkHost(input, deps);
    return {session_id: input.session_id, workspace_root: deps.rootPath, host, permission_mode: 'bypass', invocation};
  } finally { await opened.close(); }
}

export async function captureSession(input, deps) {
  const captured = await readSessionInvocation(input, deps);
  const opened = await deps.persistence.openRuntimeEventReadPersistence({workspaceRoot: deps.rootPath});
  try {
    const events = await opened.runtimeEventStore.readRuntimeEventsBounded(input.session_id, captured.invocation.runId,
      {maxRecords: 2048, maxBytes: 2 * 1024 * 1024});
    requireFact(events.status === 'complete' && Array.isArray(events.records), 'Native journal exceeds capture budget');
    await checkHost(input, deps);
    return {...captured, records: events.records};
  } finally { await opened.close(); }
}

export function readOwnedPrompt(rootPath, identity, storage) {
  // Native read persistence exposes only whole-journal bounded reads. This exact
  // read-only projection uses the installed runtime_events schema, independently
  // of tool-output volume, so overflow cannot disable cancellation ownership proof.
  const store = storage.createSqliteRuntimeStore(join(rootPath, storage.OPERATIONAL_STATE_DATABASE_NAME), {readOnly: true});
  try {
    const rows = store.db.prepare(`
      SELECT CASE WHEN length(CAST(json_extract(payload_json, '$.content.text') AS BLOB)) <= ?
        THEN json_extract(payload_json, '$.content.text') ELSE NULL END AS text
      FROM runtime_events
      WHERE session_id = ? AND run_id = ? AND turn_id = ? AND invocation_id = ?
        AND event_kind = 'text' AND json_extract(payload_json, '$.role') = 'user'
        AND json_extract(payload_json, '$.partial') = 0
      LIMIT 2
    `).all(LIMIT, identity.sessionId, identity.runId, identity.turnId, identity.invocationId);
    requireFact(rows.length === 1 && typeof rows[0].text === 'string', 'Owned native prompt is missing, ambiguous or oversized');
    return rows[0].text;
  } finally { store.close(); }
}

export async function cleanupSession(input, deps) {
  const captured = await readSessionInvocation(input, deps, {allowUnstarted: true});
  if (captured.not_started) return {...captured, cleanup_complete: true, stop_requested: false};
  const invocation = captured.invocation;
  const prompt = await deps.readOwnedPrompt(deps.rootPath, {sessionId: input.session_id,
    turnId: invocation.turnId, runId: invocation.runId, invocationId: invocation.invocationId});
  requireFact(invocation.opening?.route?.modelId === input.model &&
    invocation.opening.route.llmConnectionSlug === input.connection &&
    invocation.opening.configuration?.cwd === input.cwd &&
    createHash('sha256').update(prompt).digest('hex') === input.prompt_sha256,
    'Cleanup invocation is not bound to the owned prompt');
  const identity = {sessionId: input.session_id, turnId: invocation.turnId, runId: invocation.runId};
  const terminal = item => item?.sessionId === identity.sessionId && item.turnId === identity.turnId &&
    item.runId === identity.runId && item.invocationId === invocation.invocationId &&
    ['completed', 'failed', 'aborted', 'cancelled'].includes(item.status);
  if (terminal(invocation.terminalEvent)) return {...captured, cleanup_complete: true, stop_requested: false};
  const stopped = await deps.connection.request('turn.stop', identity);
  requireFact(stopped.sessionId === identity.sessionId && stopped.turnId === identity.turnId &&
    stopped.runId === identity.runId, 'Native stop returned a different turn');
  const wait = deps.wait ?? (ms => new Promise(resolve => setTimeout(resolve, ms)));
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const current = await readSessionInvocation(input, deps);
    requireFact(current.invocation.runId === identity.runId && current.invocation.turnId === identity.turnId &&
      current.invocation.invocationId === invocation.invocationId, 'Cleanup invocation changed');
    if (terminal(current.invocation.terminalEvent)) return {...current, cleanup_complete: true, stop_requested: true};
    await wait(50);
  }
  throw Error('Native owned turn stop did not reach a terminal journal');
}

async function main() {
  let raw = '';
  for await (const chunk of process.stdin) {
    raw += chunk;
    requireFact(Buffer.byteLength(raw) <= 16384, 'Session request exceeds budget');
  }
  const input = JSON.parse(raw);
  requireFact(['prepare', 'capture', 'cleanup'].includes(input.operation) &&
    /^fanout-eval-[a-f0-9]{32}$/u.test(input.session_id) &&
    input.package_root === resolve(input.package_root) && input.cwd === resolve(input.cwd), 'Invalid Session request');
  const load = path => import(pathToFileURL(join(input.package_root, path)));
  const roots = await load('dist/workspace-root.js');
  const protocol = await load('node_modules/@maka/runtime-host/dist/protocol/index.js');
  const client = await load('node_modules/@maka/runtime-host/dist/client/connection.js');
  const catalog = await load('node_modules/@maka/runtime-host/dist/client/catalog-reader.js');
  const rootPath = roots.resolveMakaWorkspaceRoot();
  const connected = await client.connectExistingRuntimeHost({rootPath, clientInstanceId: randomUUID(),
    protocol: {min: protocol.RUNTIME_HOST_PROTOCOL_VERSION, max: protocol.RUNTIME_HOST_PROTOCOL_VERSION},
    compositionId: protocol.INTERACTIVE_RUNTIME_HOST_COMPOSITION_ID});
  requireFact(connected.kind === 'connected', 'An existing compatible native Host is required');
  try {
    const sqlite = await load('node_modules/@maka/storage/dist/sqlite-runtime-store.js');
    const operational = await load('node_modules/@maka/storage/dist/operational-state-store.js');
    const deps = {connection: connected.connection, rootPath,
      catalog: await catalog.readRuntimeHostConnectionCatalog(connected.connection),
      persistence: await load('node_modules/@maka/storage/dist/runtime-event-persistence.js'),
      readOwnedPrompt: (root, identity) => readOwnedPrompt(root, identity, {...sqlite, ...operational})};
    const operation = {prepare: prepareSession, capture: captureSession, cleanup: cleanupSession}[input.operation];
    const result = await operation(input, deps);
    const output = JSON.stringify(result);
    requireFact(Buffer.byteLength(output) <= LIMIT, 'Session capture exceeds budget');
    process.stdout.write(output + '\n');
  } finally { await connected.connection.close(); }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(() => { process.stderr.write('Maka native Session bookkeeping failed\n'); process.exitCode = 1; });
}
