#!/usr/bin/env node
// Supplemental normal native Session evidence; no routing-profile certification.
import {readFile, writeFile} from 'node:fs/promises';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';

if (process.argv.length !== 3) {
  throw new Error('One explicit normal-probe request file is required');
} else {
  const request = JSON.parse(await readFile(process.argv[2], 'utf8'));
  const packageRoot = request.packageRoot;
  const manifest = JSON.parse(await readFile(new URL('./maka_native_api.json', import.meta.url), 'utf8'));
  const {createHash} = await import('node:crypto');
  const descriptor = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8'));
  if (descriptor.name !== manifest.package || descriptor.version !== manifest.version) throw new Error('Native package identity differs');
  for (const [relative, expected] of Object.entries(manifest.files)) {
    if (createHash('sha256').update(await readFile(join(packageRoot, relative))).digest('hex') !== expected) throw new Error('Native internal API hash differs: ' + relative);
  }
  const load = (relative) => import(pathToFileURL(join(packageRoot, relative)));
  const {connectExistingRuntimeHost} = await load('node_modules/@maka/runtime-host/dist/client/connection.js');
  const protocol = await load('node_modules/@maka/runtime-host/dist/protocol/index.js');
  const {readRuntimeHostConnectionCatalog} = await load('node_modules/@maka/runtime-host/dist/client/catalog-reader.js');
  const {LOCAL_RUNTIME_HOST_PROFILE} = await load('node_modules/@maka/runtime-host/dist/client/host-profile.js');
  const {runRuntimeHostTextCli} = await load('dist/runtime-host-run-command.js');
  const {buildWorkspaceInstructionsPromptFragment} = await load('node_modules/@maka/runtime/dist/system-prompt/workspace-instructions.js');
  const {readRoutingDurableEvidence} = await import(pathToFileURL(request.installedHelper));
  const persistence = await load('node_modules/@maka/storage/dist/runtime-event-persistence.js');
  if (!resolve(request.outputDir).startsWith(resolve(request.evidenceRoot) + '/') ||
      !resolve(request.rootPath).startsWith(resolve(request.evidenceRoot) + '/')) {
    throw new Error('Normal probe paths must stay inside its owned evidence tree');
  }
  const connected = await connectExistingRuntimeHost({
    rootPath: request.rootPath, clientInstanceId: randomUUID(),
    protocol: {min: protocol.RUNTIME_HOST_PROTOCOL_VERSION, max: protocol.RUNTIME_HOST_PROTOCOL_VERSION},
    compositionId: protocol.INTERACTIVE_RUNTIME_HOST_COMPOSITION_ID,
  });
  if (connected.kind !== 'connected') throw new Error('Owned private Host is unavailable');
  const connection = connected.connection;
  const save = (name, value) => writeFile(join(request.outputDir, name), JSON.stringify(value, null, 2) + '\n', {mode: 0o600, flag: 'wx'});
  let status = 2;
  try {
    const diagnostics = await connection.request('host.diagnostics.query', {});
    if (connection.rootId !== request.rootId || diagnostics.pid !== request.host.pid || diagnostics.hostEpoch !== request.host.hostEpoch) {
      throw new Error('Supplemental Host identity differs from its owned private Host');
    }
    const catalog = await readRuntimeHostConnectionCatalog(connection);
    const target = catalog.connections.filter((item) => item.slug === 'keychain-openai' && item.enabled === true && item.enabledModelIds.includes(request.model));
    if (target.length !== 1) throw new Error('Supplemental model route is ambiguous');
    if (request.create) {
      const session = await connection.request('session.create', {
        sessionId: request.sessionId, workspace: {kind: 'host_path', path: request.cwd},
        name: 'Supplemental installed Khenrix quality reference reads',
        modelTarget: {kind: 'explicit', connectionId: target[0].connectionId, connectionSlug: 'keychain-openai', model: request.model},
        thinkingLevel: request.thinking, mode: 'bot', permissionMode: 'explore',
        collaborationMode: 'agent', orchestrationMode: 'default',
      });
      await save('native-session-created.json', session);
    }
    const session = await connection.request('session.catalog.query', {kind: 'get', sessionId: request.sessionId});
    if (session.kind !== 'session' || session.session.permissionMode !== 'explore' || session.session.toolProfile !== undefined) {
      throw new Error('Supplemental Session must use normal tools and explore permissions');
    }
    await save('native-profile-before.json', {session, host: diagnostics, certification: false});
    await save('native-workspace-instruction-preview.json', {
      fragment: await buildWorkspaceInstructionsPromptFragment(request.cwd),
      scope: 'Installed native loader preview only; actual provider admission requires matching wire evidence.',
    });
    const admissions = [];
    const observed = new Proxy(connection, {
      get(target, name) {
        if (name === 'request') return async (operation, payload) => {
          const result = await target.request(operation, payload);
          if (operation === 'turn.start') admissions.push({payload, result});
          return result;
        };
        const value = Reflect.get(target, name, target);
        return typeof value === 'function' ? value.bind(target) : value;
      },
    });
    status = await runRuntimeHostTextCli([
      request.prompt, '--cwd', request.cwd, '--timeout', String(request.timeout), '--max-steps', String(request.maxSteps),
      '--thinking', request.thinking, '--resume', request.sessionId,
    ], {workspaceRoot: () => request.rootPath}, {
      connect: async (rootPath, profileId, clientDataRoot) => {
        if (rootPath !== request.rootPath || (profileId !== undefined && profileId !== 'local') || clientDataRoot !== request.clientDataRoot) {
          throw new Error('Supplemental native CLI connection differs');
        }
        return {connection: observed, catalog, profile: LOCAL_RUNTIME_HOST_PROFILE, close: async () => {}};
      },
    }, {clientDataRoot: request.clientDataRoot, cliCommand: 'maka'});
    await save('native-turn-admissions.json', admissions);
    if (admissions.length !== 1 || admissions[0].result.kind !== 'started') throw new Error('Supplemental native turn admission is ambiguous');
    const durable = await readRoutingDurableEvidence(request.rootPath, admissions[0].result.turn, persistence);
    await save('native-durable.json', durable);
    await save('native-profile-after.json', await connection.request('session.catalog.query', {kind: 'get', sessionId: request.sessionId}));
  } finally {
    await connection.close();
  }
  process.exitCode = status;
}
