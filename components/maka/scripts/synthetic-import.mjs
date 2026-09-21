import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { DatabaseSync } from 'node:sqlite';

const [packageRoot, evidenceDir] = process.argv.slice(2);
if (!packageRoot || !evidenceDir) throw new Error('usage: synthetic-import.mjs <package-root> <evidence-dir>');

const storageDist = join(packageRoot, 'node_modules', '@maka', 'storage', 'dist');
const runtimeDist = join(packageRoot, 'node_modules', '@maka', 'runtime', 'dist');
const { createExternalSessionAdapterRegistry, ExternalSessionImporter } = await import(
  pathToFileURL(join(storageDist, 'external-sessions.js')).href
);
const { createSessionStore } = await import(pathToFileURL(join(storageDist, 'session-store.js')).href);
const { exportSessionBundle } = await import(pathToFileURL(join(runtimeDist, 'session-export.js')).href);
const { importSessionBundle } = await import(pathToFileURL(join(runtimeDist, 'session-import.js')).href);

const fixturesRoot = join(evidenceDir, 'synthetic-sources');
const claudeHome = join(fixturesRoot, 'claude');
const codexHome = join(fixturesRoot, 'codex');
const cwd = '/workspace/maka-synthetic-import';
const claudeId = 'aaaaaaaa-0000-4000-8000-000000000001';
const codexId = 'codex-synthetic-0001';
const claudePath = join(claudeHome, 'projects', cwd.replaceAll('/', '-'), `${claudeId}.jsonl`);
const codexPath = join(codexHome, 'sessions', '2026', '09', '17', `rollout-2026-09-17T00-00-00-${codexId}.jsonl`);

await mkdir(join(claudePath, '..'), { recursive: true });
await mkdir(join(codexPath, '..'), { recursive: true });
const claudeRecords = [
  {
    type: 'user', cwd, timestamp: '2026-09-17T00:00:00.000Z',
    message: { role: 'user', content: 'CLAUDE_SYNTHETIC_FIRST' }
  },
  {
    type: 'assistant', cwd, timestamp: '2026-09-17T00:00:01.000Z',
    message: { role: 'assistant', id: 'msg_synthetic', model: 'claude-opus-5', content: [{ type: 'text', text: 'CLAUDE_SYNTHETIC_LAST' }], stop_reason: 'end_turn' }
  }
];
await writeFile(claudePath, `${claudeRecords.map((value) => JSON.stringify(value)).join('\n')}\n`, { flag: 'wx', mode: 0o600 });
const codexRecords = [
  { timestamp: '2026-09-17T00:00:00.000Z', type: 'session_meta', payload: { session_id: codexId, id: codexId, cwd, source: 'cli' } },
  { timestamp: '2026-09-17T00:00:01.000Z', type: 'event_msg', payload: { type: 'user_message', message: 'CODEX_SYNTHETIC_FIRST' } },
  { timestamp: '2026-09-17T00:00:02.000Z', type: 'event_msg', payload: { type: 'agent_message', message: 'CODEX_SYNTHETIC_LAST' } }
];
await writeFile(codexPath, `${codexRecords.map((value) => JSON.stringify(value)).join('\n')}\n`, { flag: 'wx', mode: 0o600 });

const digest = async (path) => createHash('sha256').update(await readFile(path)).digest('hex');
const sourceBefore = { claude: await digest(claudePath), codex: await digest(codexPath) };
const registry = createExternalSessionAdapterRegistry({
  claudeCode: { claudeHome },
  codex: { codexHome },
  opencode: { opencodeHome: join(fixturesRoot, 'disabled-opencode') }
});

const workspace = join(evidenceDir, 'workspace');
await mkdir(workspace, { recursive: true });
const sessions = createSessionStore(workspace);
const importer = new ExternalSessionImporter(registry, sessions);
const imported = [];
const markerChecks = {};
try {
  for (const source of [
    { adapterId: 'claude-code', sourceSessionId: claudeId },
    { adapterId: 'codex', sourceSessionId: codexId }
  ]) {
    const header = await importer.import({
      ...source,
      target: { llmConnectionSlug: 'synthetic-unconfigured', model: 'synthetic-model', permissionMode: 'ask' }
    });
    assert.equal(header.permissionMode, 'ask');
    const messages = await sessions.readMessages(header.id);
    assert.ok(messages.some((message) => message.type === 'user'));
    const serialized = JSON.stringify(messages);
    const prefix = source.adapterId === 'claude-code' ? 'CLAUDE' : 'CODEX';
    assert.ok(serialized.includes(`${prefix}_SYNTHETIC_FIRST`), `${prefix} first marker missing`);
    assert.ok(serialized.includes(`${prefix}_SYNTHETIC_LAST`), `${prefix} last marker missing`);
    markerChecks[source.adapterId] = { first: true, last: true };
    imported.push({ ...source, sessionId: header.id, permissionMode: header.permissionMode, messageCount: messages.length });
  }
} finally {
  await sessions.close?.();
}

const ledgerDb = new DatabaseSync(join(workspace, 'runtime.sqlite'), { readOnly: true });
const sessionMetadataRows = ledgerDb.prepare('SELECT count(*) AS count FROM session_metadata').get().count;
const sessionMessageRows = ledgerDb.prepare('SELECT count(*) AS count FROM session_messages').get().count;
const runtimeEventRows = ledgerDb.prepare('SELECT count(*) AS count FROM runtime_events').get().count;
ledgerDb.close();
assert.equal(sessionMetadataRows, 2);
assert.ok(sessionMessageRows >= 4);

const archiveChecks = [];
for (const item of imported) {
  const prefix = item.adapterId === 'claude-code' ? 'CLAUDE' : 'CODEX';
  const safeAdapter = item.adapterId.replaceAll(/[^a-z0-9-]/giu, '-');
  const bundle = join(evidenceDir, `${safeAdapter}-synthetic.maka-session`);
  const exported = await exportSessionBundle({ workspaceRoot: workspace, sessionId: item.sessionId, destination: bundle });
  assert.equal(exported.ok, true);
  const rehydrated = join(evidenceDir, `rehydrated-${safeAdapter}`);
  await mkdir(rehydrated, { recursive: true });
  const emptyStore = createSessionStore(rehydrated);
  await emptyStore.close?.();
  const reimported = await importSessionBundle({ workspaceRoot: rehydrated, source: bundle });
  assert.equal(reimported.ok, true);
  assert.equal(reimported.sessionIds.length, 1);
  const rehydratedStore = createSessionStore(rehydrated);
  let rehydratedMessages;
  try {
    rehydratedMessages = await rehydratedStore.readMessages(reimported.sessionIds[0]);
  } finally {
    await rehydratedStore.close?.();
  }
  const rehydratedSerialized = JSON.stringify(rehydratedMessages);
  assert.ok(rehydratedSerialized.includes(`${prefix}_SYNTHETIC_FIRST`), `rehydrated ${prefix} first marker missing`);
  assert.ok(rehydratedSerialized.includes(`${prefix}_SYNTHETIC_LAST`), `rehydrated ${prefix} last marker missing`);
  archiveChecks.push({
    adapterId: item.adapterId,
    archiveDigest: exported.artifact.archiveDigest,
    diagnosticsOmitted: exported.export.diagnosticsOmitted,
    sessionCount: reimported.sessionIds.length,
    artifactFiles: reimported.artifactFiles,
    markerChecks: { first: true, last: true }
  });
}

const sourceAfter = { claude: await digest(claudePath), codex: await digest(codexPath) };
assert.deepEqual(sourceAfter, sourceBefore);
const result = {
  schema: 'maka-synthetic-import-smoke-v1',
  sources: ['claude-code', 'codex'],
  agyHistoryCovered: false,
  sourceBefore,
  sourceAfter,
  sourceUnchanged: true,
  markerChecks,
  imported,
  sessionLedger: {
    database: 'workspace/runtime.sqlite',
    sessionMetadataRows,
    sessionMessageRows,
    runtimeEventRows,
    note: 'Synthetic external imports exercise Maka session persistence. They do not create a native model turn, so the runtime_events turn ledger remains empty.'
  },
  archiveChecks
};
await writeFile(join(evidenceDir, 'result.json'), `${JSON.stringify(result, null, 2)}\n`, { flag: 'wx', mode: 0o444 });
console.log(JSON.stringify({ ok: true, imported: imported.length, sourceUnchanged: true }));
