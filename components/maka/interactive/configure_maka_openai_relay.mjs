#!/usr/bin/env node
/** Reconcile Maka's normal profile onto the authenticated local OpenAI relay. */

import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { constants as fsConstants, open, readFile } from 'node:fs/promises';
import { request as httpRequest } from 'node:http';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import process from 'node:process';

export const LOOPBACK_HOST = '127.0.0.1';
export const DEFAULT_PORT = 48173;
export const MODEL_ID = 'gpt-6-sol';
export const LEGACY_MODEL_ID = 'gpt-5.6-sol';
export const MODEL_IDS = [MODEL_ID, LEGACY_MODEL_ID];
export const CONNECTION_SLUG = 'keychain-openai';
export const CONNECTION_NAME = 'OpenAI via local Keychain relay';
export const RELAY_PROVIDER_TYPE = 'openai-responses-compatible';
export const PROXY_USERNAME = 'maka-local';
export const HEALTH_CHALLENGE_HEADER = 'X-Maka-Relay-Challenge';
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43,256}$/u;
const HEALTH_CHALLENGE_PATTERN = /^[A-Za-z0-9_-]{43}$/u;
const HEALTH_PROOF_PATTERN = /^[0-9a-f]{64}$/u;
const HEALTH_PROOF_CONTEXT = Buffer.from('maka-relay-health-v1\0', 'ascii');

class ReconcileConflict extends Error {
  constructor(operation) {
    super(`${operation} observed concurrent configuration`);
    this.name = 'ReconcileConflict';
  }
}

class SafeConfigurationError extends Error {
  constructor(code) {
    super(code);
    this.name = 'SafeConfigurationError';
    this.code = code;
  }
}

function safeFailureCode(error) {
  if (error instanceof SafeConfigurationError) return error.code;
  if (error instanceof ReconcileConflict) return 'reconciliation_conflict';
  if (error && typeof error === 'object') {
    const operation =
      typeof error.operation === 'string' && /^[a-z.-]{1,80}$/u.test(error.operation)
        ? error.operation.replaceAll('.', '_')
        : undefined;
    const hostCode =
      typeof error.code === 'string' && /^[a-z_]{1,80}$/u.test(error.code)
        ? error.code
        : undefined;
    if (operation && hostCode) return `host_${operation}_${hostCode}`;
    const name =
      typeof error.name === 'string' && /^[A-Za-z]{1,80}$/u.test(error.name)
        ? error.name.replace(/([a-z])([A-Z])/gu, '$1_$2').toLowerCase()
        : undefined;
    if (name && hostCode) return `${name}_${hostCode}`;
    if (name) return name;
  }
  return 'configuration_failed';
}

function sameStrings(left, right) {
  return (
    Array.isArray(left) &&
    Array.isArray(right) &&
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function sameSecret(left, right) {
  const leftBytes = Buffer.from(left, 'utf8');
  const rightBytes = Buffer.from(right, 'utf8');
  return leftBytes.length === rightBytes.length && timingSafeEqual(leftBytes, rightBytes);
}

function healthChallengeProof(attestation, challenge) {
  if (!TOKEN_PATTERN.test(attestation) || !HEALTH_CHALLENGE_PATTERN.test(challenge)) {
    throw new SafeConfigurationError('relay_not_ready');
  }
  return createHmac('sha256', Buffer.from(attestation, 'ascii'))
    .update(HEALTH_PROOF_CONTEXT)
    .update(challenge, 'ascii')
    .digest('hex');
}

function sameLocator(left, right) {
  return (
    left?.scope === right?.scope &&
    left?.kind === right?.kind &&
    left?.connectionId === right?.connectionId &&
    left?.provider === right?.provider
  );
}

function relayBaseUrl(port) {
  if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) {
    throw new SafeConfigurationError('invalid_port');
  }
  return new URL(`http://${LOOPBACK_HOST}:${port}/v1`).toString();
}

function desiredProxy(port) {
  return {
    enabled: true,
    protocol: 'http',
    host: LOOPBACK_HOST,
    port,
    authEnabled: true,
    username: PROXY_USERNAME,
    bypassList: [LOOPBACK_HOST, 'localhost'],
    autoBypassDomains: [],
  };
}

function sameProxy(left, right) {
  return (
    left?.enabled === right.enabled &&
    left?.protocol === right.protocol &&
    left?.host === right.host &&
    left?.port === right.port &&
    left?.authEnabled === right.authEnabled &&
    left?.username === right.username &&
    sameStrings(left?.bypassList, right.bypassList) &&
    sameStrings(left?.autoBypassDomains, right.autoBypassDomains)
  );
}

function desiredModelOverrides() {
  return Object.fromEntries(MODEL_IDS.map((model) => [model, {
    thinkingLevels: ['xhigh', 'max'], defaultThinkingLevel: 'xhigh',
  }]));
}

function hasDesiredModelOverrides(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const entries = Object.entries(value);
  if (!sameStrings(entries.map(([model]) => model), MODEL_IDS)) return false;
  return entries.every(([, profile]) =>
    profile !== null &&
    typeof profile === 'object' &&
    !Array.isArray(profile) &&
    Object.keys(profile).length === 2 &&
    sameStrings(profile.thinkingLevels, ['xhigh', 'max']) &&
    profile.defaultThinkingLevel === 'xhigh'
  );
}

function connectionCredentialTarget(connection) {
  if (connection.providerType !== RELAY_PROVIDER_TYPE || connection.baseUrl === undefined) {
    throw new SafeConfigurationError('relay_connection_invalid');
  }
  return {
    connectionId: connection.connectionId,
    revision: connection.revision,
    slug: connection.slug,
    providerType: connection.providerType,
    effectiveBaseUrl: new URL(connection.baseUrl).toString(),
  };
}

async function assertReservedSlugCompatible(connection, readCatalog) {
  const catalog = await readCatalog(connection);
  const relay = catalog.connections.find((entry) => entry.slug === CONNECTION_SLUG);
  if (
    relay !== undefined &&
    relay.providerType !== RELAY_PROVIDER_TYPE &&
    relay.providerType !== 'openai'
  ) {
    throw new SafeConfigurationError('relay_slug_owned_by_other_provider');
  }
}

async function removeLegacyOpenAIConnection(connection, readCatalog, relay) {
  if (relay.providerType !== 'openai') {
    throw new SafeConfigurationError('relay_slug_owned_by_other_provider');
  }
  for (const kind of ['api_key', 'request_headers']) {
    await clearCredentialIfConfigured(connection, {
      scope: 'connection',
      connectionId: relay.connectionId,
      kind,
    });
  }
  expectCommitted(
    'connection_remove',
    await connection.request('connection.catalog.remove', {
      expected: { connectionId: relay.connectionId, revision: relay.revision },
    }),
  );
  const catalog = await readCatalog(connection);
  if (catalog.connections.some((entry) => entry.slug === CONNECTION_SLUG)) {
    throw new ReconcileConflict('connection_remove');
  }
}

function credentialBasis(status) {
  if (!status.configured) return null;
  return {
    locator: status.locator,
    credentialId: status.credentialId,
    revision: status.revision,
  };
}

function decodeExportedSecret(exported, expectedLocator) {
  if (!exported?.credential) return null;
  if (!sameLocator(exported.credential.locator, expectedLocator)) {
    throw new SafeConfigurationError('credential_locator_mismatch');
  }
  const encoded = exported.credential.secretBase64;
  if (typeof encoded !== 'string' || encoded.length > 16 * 1024) {
    throw new SafeConfigurationError('credential_export_invalid');
  }
  const decoded = Buffer.from(encoded, 'base64');
  if (decoded.toString('base64') !== encoded) {
    throw new SafeConfigurationError('credential_export_invalid');
  }
  return {
    secret: decoded.toString('utf8'),
    proxyTarget: exported.credential.proxyTarget,
  };
}

function expectCommitted(operation, result) {
  if (result?.kind === 'committed') return result;
  if (
    result?.kind === 'revision_conflict' ||
    result?.kind === 'connection_stale' ||
    result?.kind === 'credential_stale' ||
    result?.kind === 'proxy_target_mismatch'
  ) {
    throw new ReconcileConflict(operation);
  }
  throw new SafeConfigurationError(`${operation}_failed`);
}

async function ensureRelayConnection(connection, readCatalog, port, purgeCredentials) {
  const baseUrl = relayBaseUrl(port);
  let catalog = await readCatalog(connection);
  let relay = catalog.connections.find((entry) => entry.slug === CONNECTION_SLUG);
  if (relay?.providerType === 'openai') {
    if (!purgeCredentials) {
      throw new SafeConfigurationError('legacy_relay_requires_explicit_install');
    }
    await removeLegacyOpenAIConnection(connection, readCatalog, relay);
    catalog = await readCatalog(connection);
    relay = undefined;
  }
  if (relay === undefined) {
    expectCommitted(
      'connection_create',
      await connection.request('connection.catalog.create', {
        expectedCatalogRevision: catalog.revision,
        connection: {
          slug: CONNECTION_SLUG,
          name: CONNECTION_NAME,
          providerType: RELAY_PROVIDER_TYPE,
          baseUrl,
          enabled: true,
          enabledModelIds: MODEL_IDS,
          modelOverrides: desiredModelOverrides(),
        },
      }),
    );
    catalog = await readCatalog(connection);
    relay = catalog.connections.find((entry) => entry.slug === CONNECTION_SLUG);
    if (relay === undefined) throw new SafeConfigurationError('relay_connection_missing');
  }
  if (relay.providerType !== RELAY_PROVIDER_TYPE) {
    throw new SafeConfigurationError('relay_slug_owned_by_other_provider');
  }
  const needsUpdate =
    relay.name !== CONNECTION_NAME ||
    relay.baseUrl !== baseUrl ||
    relay.enabled !== true ||
    !sameStrings(relay.enabledModelIds, MODEL_IDS) ||
    !hasDesiredModelOverrides(relay.modelOverrides) ||
    relay.requestBodyOverlay !== undefined;
  if (needsUpdate) {
    expectCommitted(
      'connection_update',
      await connection.request('connection.catalog.update', {
        expected: { connectionId: relay.connectionId, revision: relay.revision },
        changes: {
          name: CONNECTION_NAME,
          baseUrl,
          enabled: true,
          enabledModelIds: MODEL_IDS,
          modelOverrides: desiredModelOverrides(),
          requestBodyOverlay: null,
        },
      }),
    );
    catalog = await readCatalog(connection);
    relay = catalog.connections.find((entry) => entry.slug === CONNECTION_SLUG);
    if (relay === undefined) throw new SafeConfigurationError('relay_connection_missing');
  }
  return relay;
}

async function ensureConnectionCredential(connection, relay, token) {
  const locator = {
    scope: 'connection',
    connectionId: relay.connectionId,
    kind: 'api_key',
  };
  const queried = await connection.request('credential.vault.query', { locator });
  if (queried.kind !== 'status') throw new ReconcileConflict('credential_query');
  const target = connectionCredentialTarget(relay);
  let current = null;
  if (queried.status.configured) {
    const exported = await connection.request('configuration.credentials.export', {
      locator,
      expectedConnection: target,
    });
    if (exported.connectionStale !== undefined) {
      throw new ReconcileConflict('credential_export');
    }
    current = decodeExportedSecret(exported, locator);
  }
  if (current !== null && sameSecret(current.secret, token)) return;
  expectCommitted(
    'credential_set',
    await connection.request('credential.vault.set', {
      locator,
      expected:
        queried.status.configured
          ? {
              credentialId: queried.status.credentialId,
              revision: queried.status.revision,
            }
          : null,
      expectedConnection: target,
      secret: token,
    }),
  );
}

async function ensureDenyProxy(connection, token, port) {
  const policySnapshot = await connection.request('runtime.policy.query', {});
  const locator = { scope: 'network_proxy', kind: 'password' };
  const queried = await connection.request('credential.vault.query', { locator });
  if (queried.kind !== 'status') throw new SafeConfigurationError('proxy_credential_query_failed');
  let current = null;
  if (queried.status.configured) {
    current = decodeExportedSecret(
      await connection.request('configuration.credentials.export', { locator }),
      locator,
    );
    if (current === null || current.proxyTarget === undefined) {
      throw new SafeConfigurationError('proxy_credential_export_invalid');
    }
  }
  const networkProxy = desiredProxy(port);
  const targetMatches =
    current !== null &&
    current.proxyTarget.protocol === networkProxy.protocol &&
    current.proxyTarget.host === networkProxy.host &&
    current.proxyTarget.port === networkProxy.port &&
    current.proxyTarget.username === networkProxy.username;
  const secretMatches = current !== null && sameSecret(current.secret, token);
  if (sameProxy(policySnapshot.policy.networkProxy, networkProxy) && targetMatches && secretMatches) {
    return;
  }
  let credential;
  if (targetMatches && secretMatches) {
    credential = { kind: 'keep' };
  } else {
    credential = {
      kind: 'replace',
      secret: token,
      ...(current === null ? {} : { expectedTarget: current.proxyTarget }),
    };
  }
  expectCommitted(
    'network_proxy_update',
    await connection.request('runtime.policy.network-proxy.update', {
      expectedPolicyRevision: policySnapshot.revision,
      expectedCredential: credentialBasis(queried.status),
      networkProxy,
      credential,
    }),
  );
}

async function ensureChatDefaults(connection) {
  const snapshot = await connection.request('runtime.policy.query', {});
  const current = snapshot.policy.chatDefaults;
  if (current.permissionMode === 'ask' && current.thinkingLevel === 'xhigh') return;
  expectCommitted(
    'chat_defaults_update',
    await connection.request('runtime.policy.mutate', {
      expectedRevision: snapshot.revision,
      operation: {
        kind: 'set_chat_defaults',
        value: { ...current, permissionMode: 'ask', thinkingLevel: 'xhigh' },
      },
    }),
  );
}

async function disableOtherConnections(connection, readCatalog) {
  let catalog = await readCatalog(connection);
  for (const candidate of catalog.connections) {
    if (candidate.slug === CONNECTION_SLUG || candidate.enabled !== true) continue;
    expectCommitted(
      'connection_disable',
      await connection.request('connection.catalog.update', {
        expected: {
          connectionId: candidate.connectionId,
          revision: candidate.revision,
        },
        changes: {
          name: candidate.name,
          enabled: false,
          enabledModelIds: candidate.enabledModelIds,
        },
      }),
    );
    catalog = await readCatalog(connection);
  }
}

async function clearCredentialIfConfigured(connection, locator) {
  const queried = await connection.request('credential.vault.query', { locator });
  if (queried.kind === 'connection_not_found') {
    throw new ReconcileConflict('credential_clear_query');
  }
  if (queried.kind !== 'status') {
    throw new SafeConfigurationError('credential_clear_query_failed');
  }
  if (queried.status.configured) {
    expectCommitted(
      'credential_delete',
      await connection.request('credential.vault.delete', {
        expected: {
          locator,
          credentialId: queried.status.credentialId,
          revision: queried.status.revision,
        },
      }),
    );
  }
  const verified = await connection.request('credential.vault.query', { locator });
  if (verified.kind !== 'status' || verified.status.configured) {
    throw new SafeConfigurationError('credential_clear_verification_failed');
  }
}

async function clearNonDecoyCredentials(connection, readCatalog, providerAuthKind) {
  const catalog = await readCatalog(connection);
  for (const candidate of catalog.connections) {
    const authKind = providerAuthKind(candidate.providerType);
    if (!['api_key', 'oauth_token', 'optional_api_key', 'none'].includes(authKind)) {
      throw new SafeConfigurationError('provider_auth_contract_missing');
    }
    const credentialKinds = ['request_headers'];
    if (authKind === 'api_key' || authKind === 'optional_api_key') {
      credentialKinds.push('api_key');
    } else if (authKind === 'oauth_token') {
      credentialKinds.push('oauth_token');
    }
    for (const kind of credentialKinds) {
      if (candidate.slug === CONNECTION_SLUG && kind === 'api_key') continue;
      await clearCredentialIfConfigured(connection, {
        scope: 'connection',
        connectionId: candidate.connectionId,
        kind,
      });
    }
  }
  // The pinned credential locator enum has one provider-scoped web-search
  // secret. Clear it as well so the vault's only configured entries are the
  // two local relay decoys.
  await clearCredentialIfConfigured(connection, {
    scope: 'web_search',
    provider: 'tavily',
    kind: 'api_key',
  });
}

async function ensureDefaultTarget(connection, readCatalog) {
  const catalog = await readCatalog(connection);
  const relay = catalog.connections.find((entry) => entry.slug === CONNECTION_SLUG);
  if (relay === undefined || !relay.enabled || !relay.enabledModelIds.includes(MODEL_ID)) {
    throw new SafeConfigurationError('relay_not_selectable');
  }
  if (
    catalog.defaultTarget?.connectionId === relay.connectionId &&
    catalog.defaultTarget?.modelId === MODEL_ID
  ) {
    return;
  }
  expectCommitted(
    'default_target_update',
    await connection.request('connection.catalog.set-default-target', {
      expectedCatalogRevision: catalog.revision,
      target: { connectionId: relay.connectionId, modelId: MODEL_ID },
    }),
  );
}

export async function reconcileMakaRelay(
  connection,
  {
    token,
    port = DEFAULT_PORT,
    readCatalog,
    providerAuthKind,
    assertReady = async () => {},
    purgeCredentials = false,
  },
) {
  if (!TOKEN_PATTERN.test(token)) throw new SafeConfigurationError('invalid_token');
  if (typeof readCatalog !== 'function') throw new SafeConfigurationError('catalog_reader_missing');
  if (typeof providerAuthKind !== 'function') {
    throw new SafeConfigurationError('provider_auth_contract_missing');
  }
  await assertReady({ token, port });
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      // The deny proxy becomes authoritative before any third-party connection
      // is disabled or a legacy relay is migrated, so a partial reconciliation
      // still fails closed on egress.
      await assertReservedSlugCompatible(connection, readCatalog);
      await ensureDenyProxy(connection, token, port);
      const relay = await ensureRelayConnection(
        connection,
        readCatalog,
        port,
        purgeCredentials,
      );
      await ensureConnectionCredential(connection, relay, token);
      await ensureChatDefaults(connection);
      await disableOtherConnections(connection, readCatalog);
      if (purgeCredentials) {
        await clearNonDecoyCredentials(connection, readCatalog, providerAuthKind);
      }
      await ensureConnectionCredential(connection, relay, token);
      await ensureDenyProxy(connection, token, port);
      await ensureDefaultTarget(connection, readCatalog);
      return;
    } catch (error) {
      if (!(error instanceof ReconcileConflict) || attempt === 3) throw error;
    }
  }
}

export async function assertRelayReady({ token, attestation, port = DEFAULT_PORT }) {
  return assertAttestedRelayReady({ token, attestation, port });
}

async function localJsonRequest({ port, path, authorization, challenge }) {
  const response = await new Promise((resolvePromise, rejectPromise) => {
    const request = httpRequest(
      {
        hostname: LOOPBACK_HOST,
        port,
        path,
        method: 'GET',
        headers: {
          Host: `${LOOPBACK_HOST}:${port}`,
          ...(authorization === undefined ? {} : { Authorization: authorization }),
          ...(challenge === undefined ? {} : { [HEALTH_CHALLENGE_HEADER]: challenge }),
          Connection: 'close',
        },
        timeout: 2_000,
      },
      (incoming) => {
        const chunks = [];
        let size = 0;
        incoming.on('data', (chunk) => {
          size += chunk.length;
          if (size > 64 * 1024) request.destroy(new Error('relay response too large'));
          else chunks.push(chunk);
        });
        incoming.on('end', () =>
          resolvePromise({ status: incoming.statusCode, body: Buffer.concat(chunks) }),
        );
      },
    );
    request.once('timeout', () => request.destroy(new Error('relay readiness timed out')));
    request.once('error', rejectPromise);
    request.end();
  });
  if (response.status !== 200) throw new SafeConfigurationError('relay_not_ready');
  let document;
  try {
    document = JSON.parse(response.body.toString('utf8'));
  } catch {
    throw new SafeConfigurationError('relay_not_ready');
  }
  return document;
}

export async function assertAttestedRelayReady({
  token,
  attestation,
  port = DEFAULT_PORT,
}) {
  if (!TOKEN_PATTERN.test(token) || !TOKEN_PATTERN.test(attestation)) {
    throw new SafeConfigurationError('relay_not_ready');
  }
  // Authenticate the listener using an owner-only startup value before the
  // caller token is sent. A process squatting on the fixed port cannot obtain
  // the token by impersonating readiness.
  const challenge = randomBytes(32).toString('base64url');
  if (!HEALTH_CHALLENGE_PATTERN.test(challenge)) {
    throw new SafeConfigurationError('relay_not_ready');
  }
  const health = await localJsonRequest({ port, path: '/healthz', challenge });
  const expectedProof = healthChallengeProof(attestation, challenge);
  if (
    health?.status !== 'ready' ||
    typeof health.proof !== 'string' ||
    !HEALTH_PROOF_PATTERN.test(health.proof) ||
    !sameSecret(health.proof, expectedProof)
  ) {
    throw new SafeConfigurationError('relay_attestation_mismatch');
  }
  const document = await localJsonRequest({
    port,
    path: '/v1/models',
    authorization: `Bearer ${token}`,
  });
  if (!sameStrings(document?.data?.map((model) => model?.id), MODEL_IDS)) {
    throw new SafeConfigurationError('relay_model_mismatch');
  }
}

export async function readPrivateToken(path) {
  const noFollow = fsConstants.O_NOFOLLOW;
  if (typeof noFollow !== 'number') throw new SafeConfigurationError('token_platform_unsupported');
  let handle;
  try {
    handle = await open(
      path,
      fsConstants.O_RDONLY | noFollow | (fsConstants.O_CLOEXEC ?? 0),
    );
    const metadata = await handle.stat();
    if (!metadata.isFile()) throw new SafeConfigurationError('token_not_regular');
    if (metadata.uid !== process.getuid() || (metadata.mode & 0o077) !== 0) {
      throw new SafeConfigurationError('token_permissions_invalid');
    }
    const bytes = Buffer.alloc(1025);
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
    if (bytesRead > 1024) throw new SafeConfigurationError('token_invalid');
    const value = bytes.subarray(0, bytesRead);
    if (value.some((byte) => byte > 0x7f)) {
      throw new SafeConfigurationError('token_invalid');
    }
    const raw = value.toString('ascii');
    const token = raw.trim();
    if (!TOKEN_PATTERN.test(token)) throw new SafeConfigurationError('token_invalid');
    return token;
  } finally {
    await handle?.close().catch(() => undefined);
  }
}

export async function discoverMakaPackageRoot(explicitRoot) {
  if (!explicitRoot) throw new SafeConfigurationError('maka_package_root_required');
  return validatePackageRoot(resolve(explicitRoot));
}

async function validatePackageRoot(root) {
  const manifest = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  if (manifest?.name !== 'maka-agent') throw new SafeConfigurationError('maka_package_invalid');
  return root;
}

function scrubAmbientProviderKeys() {
  for (const name of Object.keys(process.env)) {
    if (name.endsWith('_API_KEY')) delete process.env[name];
  }
}

function parseArguments(commandLine) {
  const parsed = { port: DEFAULT_PORT, purgeCredentials: false };
  for (let index = 0; index < commandLine.length; index += 1) {
    const option = commandLine[index];
    if (option === '--purge-credentials') {
      parsed.purgeCredentials = true;
      continue;
    }
    const value = commandLine[index + 1];
    if (option === '--token-file' && value) parsed.tokenFile = value;
    else if (option === '--attestation-file' && value) parsed.attestationFile = value;
    else if (option === '--port' && value) parsed.port = Number(value);
    else if (option === '--package-root' && value) parsed.packageRoot = value;
    else throw new SafeConfigurationError('invalid_arguments');
    index += 1;
  }
  if (!parsed.tokenFile) throw new SafeConfigurationError('token_file_required');
  if (!parsed.attestationFile) throw new SafeConfigurationError('attestation_file_required');
  if (!parsed.packageRoot) throw new SafeConfigurationError('maka_package_root_required');
  return parsed;
}

async function run(commandLine) {
  scrubAmbientProviderKeys();
  const options = parseArguments(commandLine);
  const token = await readPrivateToken(options.tokenFile);
  const attestation = await readPrivateToken(options.attestationFile);
  const packageRoot = await discoverMakaPackageRoot(options.packageRoot);
  const cliContext = await import(
    pathToFileURL(join(packageRoot, 'dist/runtime-host-cli-context.js')).href
  );
  const catalogClient = await import(
    pathToFileURL(
      join(packageRoot, 'node_modules/@maka/runtime-host/dist/client/catalog-reader.js'),
    ).href
  );
  const workspace = await import(
    pathToFileURL(join(packageRoot, 'node_modules/@maka/storage/dist/workspace-root.js')).href
  );
  const providerRegistry = await import(
    pathToFileURL(join(packageRoot, 'node_modules/@maka/core/dist/provider-registry.js')).href
  );
  const context = await cliContext.connectRuntimeHostCliConnection({
    rootPath: workspace.resolveMakaWorkspaceRoot(),
  });
  try {
    await reconcileMakaRelay(context.connection, {
      token,
      port: options.port,
      readCatalog: catalogClient.readRuntimeHostConnectionCatalog,
      providerAuthKind: (providerType) =>
        providerRegistry.PROVIDER_REGISTRY[providerType]?.authKind,
      assertReady: ({ token: callerToken, port }) =>
        assertAttestedRelayReady({ token: callerToken, attestation, port }),
      purgeCredentials: options.purgeCredentials,
    });
  } finally {
    await context.close().catch(() => undefined);
  }
  process.stdout.write('Maka local OpenAI relay policy is configured.\n');
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
  run(process.argv.slice(2)).catch((error) => {
    const code = safeFailureCode(error);
    process.stderr.write(`Maka relay configuration failed safely (${code}).\n`);
    process.exitCode = 78;
  });
}
