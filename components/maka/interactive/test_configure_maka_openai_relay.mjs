#!/usr/bin/env node
/** Fully offline tests for the Runtime Host protocol reconciler. */

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHmac } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { chmod, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

import {
  CONNECTION_NAME,
  CONNECTION_SLUG,
  DEFAULT_PORT,
  HEALTH_CHALLENGE_HEADER,
  LOOPBACK_HOST,
  MODEL_ID,
  MODEL_IDS,
  PROXY_USERNAME,
  RELAY_PROVIDER_TYPE,
  assertAttestedRelayReady,
  readPrivateToken,
  reconcileMakaRelay,
} from './configure_maka_openai_relay.mjs';

const TOKEN = 'B'.repeat(64);
const OPENCODE_CONNECTION_ID = '00000000-0000-4000-8000-000000000001';
const RELAY_CONNECTION_ID = '00000000-0000-4000-8000-000000000002';
const FOREIGN_CONNECTION_ID = '00000000-0000-4000-8000-000000000003';
const API_KEY_CONNECTION_ID = '00000000-0000-4000-8000-000000000004';
const OAUTH_CONNECTION_ID = '00000000-0000-4000-8000-000000000005';
const CONNECTION_CREDENTIAL_ID = '00000000-0000-4000-8000-000000000011';
const PROXY_CREDENTIAL_ID = '00000000-0000-4000-8000-000000000012';
const LEGACY_CREDENTIAL_IDS = [
  '00000000-0000-4000-8000-000000000021',
  '00000000-0000-4000-8000-000000000022',
  '00000000-0000-4000-8000-000000000023',
  '00000000-0000-4000-8000-000000000024',
];

const TEST_PROVIDER_AUTH_KINDS = {
  'opencode-free': 'none',
  openai: 'api_key',
  'openai-responses-compatible': 'api_key',
  anthropic: 'api_key',
  'openai-codex': 'oauth_token',
};

function testProviderAuthKind(providerType) {
  return TEST_PROVIDER_AUTH_KINDS[providerType];
}

function clone(value) {
  return structuredClone(value);
}

class FakeRuntimeHost {
  constructor(options = {}) {
    this.catalogRevision = 1;
    this.policyRevision = 1;
    this.defaultTarget = null;
    this.connections = clone(
      options.connections ?? [
        {
          connectionId: OPENCODE_CONNECTION_ID,
          revision: 1,
          slug: 'opencode-free',
          name: 'OpenCode Zen Free',
          providerType: 'opencode-free',
          enabled: true,
          enabledModelIds: ['big-pickle'],
          models: [],
          catalogEntries: [],
        },
      ],
    );
    this.policy = {
      networkProxy: {
        enabled: false,
        protocol: 'http',
        host: '',
        port: 8080,
        authEnabled: false,
        username: '',
        bypassList: [],
        autoBypassDomains: [],
      },
      personalization: { displayName: '', assistantTone: '' },
      memory: { enabled: true, agentReadEnabled: false },
      workspaceInstructions: { enabled: true },
      privacy: { incognitoActive: false },
      chatDefaults: { permissionMode: options.permissionMode ?? 'ask' },
      webSearch: { enabled: false, defaultProvider: 'model' },
      subagents: { presets: [] },
      shell: { preference: 'auto', executable: '' },
      externalAgents: { antigravity: { executable: '' } },
    };
    this.credentials = new Map();
    this.operations = [];
    this.failFirstCreateWithConflict = options.failFirstCreateWithConflict ?? false;
  }

  catalog = async () => ({
    revision: this.catalogRevision,
    defaultTarget: clone(this.defaultTarget),
    connections: clone(this.connections),
  });

  locatorKey(locator) {
    return JSON.stringify(locator);
  }

  credentialStatus(locator) {
    const stored = this.credentials.get(this.locatorKey(locator));
    return stored
      ? {
          locator: clone(locator),
          configured: true,
          credentialId: stored.credentialId,
          revision: stored.revision,
          updatedAt: 1,
        }
      : {
          locator: clone(locator),
          configured: false,
          credentialId: null,
          revision: null,
          updatedAt: null,
        };
  }

  seedCredential(locator, secret, credentialId) {
    this.credentials.set(this.locatorKey(locator), {
      locator: clone(locator),
      secret,
      credentialId,
      revision: 1,
    });
  }

  request = async (operation, input) => {
    this.operations.push({ operation, input: clone(input) });
    switch (operation) {
      case 'connection.catalog.create': {
        if (this.failFirstCreateWithConflict) {
          this.failFirstCreateWithConflict = false;
          return {
            kind: 'revision_conflict',
            expectedRevision: input.expectedCatalogRevision,
            actualRevision: this.catalogRevision,
          };
        }
        assert.equal(input.expectedCatalogRevision, this.catalogRevision);
        assert.equal(input.connection.providerType, RELAY_PROVIDER_TYPE);
        const created = {
          ...clone(input.connection),
          connectionId: RELAY_CONNECTION_ID,
          revision: 1,
          models: [],
          catalogEntries: [],
        };
        this.connections.push(created);
        this.catalogRevision += 1;
        return {
          kind: 'committed',
          catalogRevision: this.catalogRevision,
          connection: { connectionId: created.connectionId, revision: created.revision },
        };
      }
      case 'connection.catalog.update': {
        const target = this.connections.find(
          (entry) => entry.connectionId === input.expected.connectionId,
        );
        assert(target);
        assert.equal(target.revision, input.expected.revision);
        for (const [name, value] of Object.entries(input.changes)) {
          if ((name === 'modelOverrides' || name === 'requestBodyOverlay') && value === null) {
            delete target[name];
          } else {
            target[name] = clone(value);
          }
        }
        target.revision += 1;
        this.catalogRevision += 1;
        return {
          kind: 'committed',
          catalogRevision: this.catalogRevision,
          connection: { connectionId: target.connectionId, revision: target.revision },
        };
      }
      case 'connection.catalog.remove': {
        const index = this.connections.findIndex(
          (entry) => entry.connectionId === input.expected.connectionId,
        );
        assert.notEqual(index, -1);
        assert.equal(this.connections[index].revision, input.expected.revision);
        const [removed] = this.connections.splice(index, 1);
        if (this.defaultTarget?.connectionId === removed.connectionId) this.defaultTarget = null;
        this.catalogRevision += 1;
        return { kind: 'committed', catalogRevision: this.catalogRevision };
      }
      case 'connection.catalog.set-default-target': {
        assert.equal(input.expectedCatalogRevision, this.catalogRevision);
        this.defaultTarget = clone(input.target);
        this.catalogRevision += 1;
        return { kind: 'committed', catalogRevision: this.catalogRevision };
      }
      case 'credential.vault.query': {
        if (input.locator.scope === 'connection' && input.locator.kind !== 'request_headers') {
          const candidate = this.connections.find(
            (entry) => entry.connectionId === input.locator.connectionId,
          );
          assert(candidate);
          const authKind = testProviderAuthKind(candidate.providerType);
          const expectedKind = authKind === 'optional_api_key' ? 'api_key' : authKind;
          assert.equal(input.locator.kind, expectedKind, 'unsupported provider credential query');
        }
        return { kind: 'status', status: this.credentialStatus(input.locator) };
      }
      case 'credential.vault.set': {
        const key = this.locatorKey(input.locator);
        const previous = this.credentials.get(key);
        const stored = {
          locator: clone(input.locator),
          secret: input.secret,
          credentialId: previous?.credentialId ?? CONNECTION_CREDENTIAL_ID,
          revision: (previous?.revision ?? 0) + 1,
        };
        this.credentials.set(key, stored);
        return {
          kind: 'committed',
          vaultRevision: 1,
          status: this.credentialStatus(input.locator),
        };
      }
      case 'credential.vault.delete': {
        const key = this.locatorKey(input.expected.locator);
        const current = this.credentials.get(key);
        assert(current);
        assert.equal(current.credentialId, input.expected.credentialId);
        assert.equal(current.revision, input.expected.revision);
        this.credentials.delete(key);
        return {
          kind: 'committed',
          vaultRevision: 1,
          status: this.credentialStatus(input.expected.locator),
        };
      }
      case 'configuration.credentials.export': {
        const stored = this.credentials.get(this.locatorKey(input.locator));
        return {
          credential: stored
            ? {
                locator: clone(input.locator),
                secretBase64: Buffer.from(stored.secret).toString('base64'),
                ...(stored.proxyTarget ? { proxyTarget: clone(stored.proxyTarget) } : {}),
              }
            : null,
        };
      }
      case 'runtime.policy.query':
        return { revision: this.policyRevision, policy: clone(this.policy) };
      case 'runtime.policy.mutate': {
        assert.equal(input.expectedRevision, this.policyRevision);
        assert.equal(input.operation.kind, 'set_chat_defaults');
        this.policy.chatDefaults = clone(input.operation.value);
        this.policyRevision += 1;
        return { kind: 'committed', revision: this.policyRevision };
      }
      case 'runtime.policy.network-proxy.update': {
        assert.equal(input.expectedPolicyRevision, this.policyRevision);
        this.policy.networkProxy = clone(input.networkProxy);
        this.policyRevision += 1;
        if (input.credential.kind === 'replace') {
          const locator = { scope: 'network_proxy', kind: 'password' };
          const previous = this.credentials.get(this.locatorKey(locator));
          this.credentials.set(this.locatorKey(locator), {
            locator,
            secret: input.credential.secret,
            credentialId: previous?.credentialId ?? PROXY_CREDENTIAL_ID,
            revision: (previous?.revision ?? 0) + 1,
            proxyTarget: {
              protocol: input.networkProxy.protocol,
              host: input.networkProxy.host,
              port: input.networkProxy.port,
              username: input.networkProxy.username,
            },
          });
        }
        return {
          kind: 'committed',
          revision: this.policyRevision,
          credentialStatus: this.credentialStatus({
            scope: 'network_proxy',
            kind: 'password',
          }),
        };
      }
      default:
        throw new Error(`unexpected offline operation: ${operation}`);
    }
  };
}

function mutationOperations(host) {
  return host.operations.filter(({ operation }) =>
    [
      'connection.catalog.create',
      'connection.catalog.update',
      'connection.catalog.remove',
      'connection.catalog.set-default-target',
      'credential.vault.set',
      'credential.vault.delete',
      'runtime.policy.mutate',
      'runtime.policy.network-proxy.update',
    ].includes(operation),
  );
}

function pinnedPackageRoot() {
  const labRoot = fileURLToPath(new URL('..', import.meta.url));
  const shim = execFileSync(
    '/opt/homebrew/bin/mise',
    ['-C', labRoot, 'which', 'maka'],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
  ).trim();
  const source = readFileSync(shim, 'utf8');
  const target = source.match(/^# aube-bin-shim v2 target=([^\s]+)$/mu)?.[1];
  assert(target, 'the pinned Maka shim must declare its immutable target');
  return dirname(dirname(resolve(dirname(shim), target)));
}

test('fresh reconciliation installs only decoys and fails other providers closed', async () => {
  const host = new FakeRuntimeHost({ permissionMode: 'bypass' });
  let readyChecks = 0;
  await reconcileMakaRelay(host, {
    token: TOKEN,
    port: DEFAULT_PORT,
    readCatalog: host.catalog,
    providerAuthKind: testProviderAuthKind,
    assertReady: async ({ token, port }) => {
      readyChecks += 1;
      assert.equal(token, TOKEN);
      assert.equal(port, DEFAULT_PORT);
      assert.equal(host.operations.length, 0, 'readiness must precede live mutations');
    },
  });

  assert.equal(readyChecks, 1);
  const relay = host.connections.find((entry) => entry.slug === CONNECTION_SLUG);
  assert(relay);
  assert.equal(relay.name, CONNECTION_NAME);
  assert.equal(relay.providerType, RELAY_PROVIDER_TYPE);
  assert.equal(relay.baseUrl, `http://${LOOPBACK_HOST}:${DEFAULT_PORT}/v1`);
  assert.deepEqual(relay.enabledModelIds, MODEL_IDS);
  assert.deepEqual(relay.modelOverrides, Object.fromEntries(MODEL_IDS.map((model) => [model, {
    thinkingLevels: ['xhigh', 'max'], defaultThinkingLevel: 'xhigh',
  }])));
  assert.equal(relay.enabled, true);
  assert.equal(host.connections.find((entry) => entry.slug === 'opencode-free').enabled, false);
  assert.deepEqual(host.defaultTarget, {
    connectionId: relay.connectionId,
    modelId: MODEL_ID,
  });
  assert.deepEqual(host.policy.networkProxy, {
    enabled: true,
    protocol: 'http',
    host: LOOPBACK_HOST,
    port: DEFAULT_PORT,
    authEnabled: true,
    username: PROXY_USERNAME,
    bypassList: [LOOPBACK_HOST, 'localhost'],
    autoBypassDomains: [],
  });
  assert.deepEqual(host.policy.chatDefaults, {
    permissionMode: 'ask',
    thinkingLevel: 'xhigh',
  });

  const storedSecrets = [...host.credentials.values()].map(({ secret }) => secret);
  assert.deepEqual(storedSecrets, [TOKEN, TOKEN]);
  const operationNames = host.operations.map(({ operation }) => operation);
  assert(
    operationNames.indexOf('runtime.policy.network-proxy.update') <
      operationNames.lastIndexOf('connection.catalog.update'),
    'the deny proxy must commit before OpenCode is disabled',
  );
});

test('private token reads are bounded, owner-only, and no-follow', async () => {
  const directory = await mkdtemp(resolve(tmpdir(), 'maka-relay-token-'));
  try {
    await chmod(directory, 0o700);
    const tokenFile = resolve(directory, 'token');
    await writeFile(tokenFile, `${TOKEN}\n`, { mode: 0o600 });
    assert.equal(await readPrivateToken(tokenFile), TOKEN);

    const link = resolve(directory, 'token-link');
    await symlink(tokenFile, link);
    await assert.rejects(readPrivateToken(link));

    const oversized = resolve(directory, 'oversized');
    await writeFile(oversized, Buffer.alloc(1025, 0x41), { mode: 0o600 });
    await assert.rejects(readPrivateToken(oversized));

    const nonAscii = resolve(directory, 'non-ascii');
    await writeFile(nonAscii, Buffer.concat([Buffer.alloc(63, 0x41), Buffer.from([0xff])]), {
      mode: 0o600,
    });
    await assert.rejects(readPrivateToken(nonAscii));
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('readiness uses fresh HMAC challenges before sending the bearer', async () => {
  const attestation = 'C'.repeat(64);
  const challenges = [];
  const modelAuthorizations = [];
  let returnValidProof = false;
  const server = createServer((request, response) => {
    let document;
    if (request.url === '/healthz') {
      assert.equal(request.headers.authorization, undefined);
      const challenge = request.headers[HEALTH_CHALLENGE_HEADER.toLowerCase()];
      assert.equal(typeof challenge, 'string');
      assert.match(challenge, /^[A-Za-z0-9_-]{43}$/u);
      challenges.push(challenge);
      const proof = createHmac('sha256', Buffer.from(attestation, 'ascii'))
        .update(Buffer.from('maka-relay-health-v1\0', 'ascii'))
        .update(challenge, 'ascii')
        .digest('hex');
      document = { status: 'ready', proof: returnValidProof ? proof : '0'.repeat(64) };
    } else if (request.url === '/v1/models') {
      modelAuthorizations.push(request.headers.authorization);
      document = { data: MODEL_IDS.map((id) => ({ id })) };
    } else {
      response.writeHead(404, { Connection: 'close' });
      response.end();
      return;
    }
    const body = Buffer.from(JSON.stringify(document));
    response.writeHead(200, {
      'Content-Type': 'application/json',
      'Content-Length': String(body.length),
      Connection: 'close',
    });
    response.end(body);
  });
  await new Promise((resolvePromise, rejectPromise) => {
    server.once('error', rejectPromise);
    server.listen(0, LOOPBACK_HOST, resolvePromise);
  });
  const address = server.address();
  assert(address && typeof address === 'object');
  try {
    await assert.rejects(
      assertAttestedRelayReady({ token: TOKEN, attestation, port: address.port }),
    );
    assert.deepEqual(modelAuthorizations, []);
    returnValidProof = true;
    await assertAttestedRelayReady({ token: TOKEN, attestation, port: address.port });
    await assertAttestedRelayReady({ token: TOKEN, attestation, port: address.port });
    assert.deepEqual(modelAuthorizations, [`Bearer ${TOKEN}`, `Bearer ${TOKEN}`]);
    assert.equal(new Set(challenges).size, 3);
  } finally {
    await new Promise((resolvePromise) => server.close(resolvePromise));
  }
});

test('second reconciliation is mutation-free', async () => {
  const host = new FakeRuntimeHost();
  const options = {
    token: TOKEN,
    readCatalog: host.catalog,
    providerAuthKind: testProviderAuthKind,
    assertReady: async () => {},
  };
  await reconcileMakaRelay(host, options);
  host.operations.length = 0;
  await reconcileMakaRelay(host, options);
  assert.deepEqual(mutationOperations(host), []);
});

test('legacy connection and web-search secrets are deleted, not merely disabled', async () => {
  const host = new FakeRuntimeHost({
    connections: [
      {
        connectionId: OPENCODE_CONNECTION_ID,
        revision: 1,
        slug: 'opencode-free',
        name: 'OpenCode Zen Free',
        providerType: 'opencode-free',
        enabled: true,
        enabledModelIds: ['big-pickle'],
        models: [],
        catalogEntries: [],
      },
      {
        connectionId: API_KEY_CONNECTION_ID,
        revision: 1,
        slug: 'legacy-anthropic',
        name: 'Legacy Anthropic',
        providerType: 'anthropic',
        enabled: true,
        enabledModelIds: ['legacy-model'],
        models: [],
        catalogEntries: [],
      },
      {
        connectionId: OAUTH_CONNECTION_ID,
        revision: 1,
        slug: 'legacy-codex',
        name: 'Legacy Codex',
        providerType: 'openai-codex',
        enabled: true,
        enabledModelIds: ['legacy-model'],
        models: [],
        catalogEntries: [],
      },
    ],
  });
  const legacyLocators = [
    {
      scope: 'connection',
      connectionId: API_KEY_CONNECTION_ID,
      kind: 'api_key',
    },
    {
      scope: 'connection',
      connectionId: OAUTH_CONNECTION_ID,
      kind: 'oauth_token',
    },
    {
      scope: 'connection',
      connectionId: OPENCODE_CONNECTION_ID,
      kind: 'request_headers',
    },
    { scope: 'web_search', provider: 'tavily', kind: 'api_key' },
  ];
  legacyLocators.forEach((locator, index) =>
    host.seedCredential(locator, `legacy-secret-${index}`, LEGACY_CREDENTIAL_IDS[index]),
  );
  await reconcileMakaRelay(host, {
    token: TOKEN,
    readCatalog: host.catalog,
    providerAuthKind: testProviderAuthKind,
    assertReady: async () => {},
    purgeCredentials: true,
  });
  assert.equal(
    host.operations.filter(({ operation }) => operation === 'credential.vault.delete').length,
    legacyLocators.length,
  );
  assert.deepEqual(
    [...host.credentials.values()].map(({ secret }) => secret),
    [TOKEN, TOKEN],
  );
});

test('ordinary relay reconciliation disables other providers without deleting credentials', async () => {
  const host = new FakeRuntimeHost({
    connections: [
      {
        connectionId: OAUTH_CONNECTION_ID,
        revision: 1,
        slug: 'existing-codex',
        name: 'Existing Codex',
        providerType: 'openai-codex',
        enabled: true,
        enabledModelIds: ['gpt-5.6-sol'],
        models: [],
        catalogEntries: [],
      },
    ],
  });
  const locator = {
    scope: 'connection',
    connectionId: OAUTH_CONNECTION_ID,
    kind: 'oauth_token',
  };
  host.seedCredential(locator, 'preserved-oauth-token', LEGACY_CREDENTIAL_IDS[0]);
  await reconcileMakaRelay(host, {
    token: TOKEN,
    readCatalog: host.catalog,
    providerAuthKind: testProviderAuthKind,
    assertReady: async () => {},
  });
  assert.equal(host.connections.find((entry) => entry.connectionId === OAUTH_CONNECTION_ID).enabled, false);
  assert.equal(host.credentials.get(host.locatorKey(locator)).secret, 'preserved-oauth-token');
  assert.equal(
    host.operations.some(({ operation }) => operation === 'credential.vault.delete'),
    false,
  );
});

test('legacy OpenAI relay is replaced by a narrow Responses relay', async () => {
  const host = new FakeRuntimeHost({
    connections: [
      {
        connectionId: RELAY_CONNECTION_ID,
        revision: 7,
        slug: CONNECTION_SLUG,
        name: 'Old relay',
        providerType: 'openai',
        baseUrl: 'https://example.invalid/v1',
        enabled: false,
        enabledModelIds: ['other-model'],
        modelOverrides: { 'other-model': { contextWindow: 1 } },
        requestBodyOverlay: { metadata: { unsafe: true } },
        models: [],
        catalogEntries: [],
      },
    ],
  });
  await reconcileMakaRelay(host, {
    token: TOKEN,
    readCatalog: host.catalog,
    providerAuthKind: testProviderAuthKind,
    assertReady: async () => {},
    purgeCredentials: true,
  });
  const configured = host.connections[0];
  assert.equal(configured.providerType, RELAY_PROVIDER_TYPE);
  assert.equal(configured.baseUrl, `http://${LOOPBACK_HOST}:${DEFAULT_PORT}/v1`);
  assert.deepEqual(configured.enabledModelIds, MODEL_IDS);
  assert.deepEqual(configured.modelOverrides, Object.fromEntries(MODEL_IDS.map((model) => [model, {
    thinkingLevels: ['xhigh', 'max'], defaultThinkingLevel: 'xhigh',
  }])));
  assert(!Object.hasOwn(configured, 'requestBodyOverlay'));
  assert.equal(
    host.operations.filter(({ operation }) => operation === 'connection.catalog.remove').length,
    1,
  );
});

test('ordinary reconciliation refuses a legacy reserved slug without deleting it', async () => {
  const host = new FakeRuntimeHost({
    connections: [
      {
        connectionId: RELAY_CONNECTION_ID,
        revision: 7,
        slug: CONNECTION_SLUG,
        name: 'Old relay',
        providerType: 'openai',
        baseUrl: 'https://example.invalid/v1',
        enabled: true,
        enabledModelIds: ['gpt-4o-mini'],
        models: [],
        catalogEntries: [],
      },
    ],
  });
  const locator = {
    scope: 'connection',
    connectionId: RELAY_CONNECTION_ID,
    kind: 'api_key',
  };
  host.seedCredential(locator, 'legacy-relay-key', LEGACY_CREDENTIAL_IDS[0]);
  await assert.rejects(
    reconcileMakaRelay(host, {
      token: TOKEN,
      readCatalog: host.catalog,
      providerAuthKind: testProviderAuthKind,
      assertReady: async () => {},
    }),
    /legacy_relay_requires_explicit_install/u,
  );
  assert.equal(host.credentials.get(host.locatorKey(locator)).secret, 'legacy-relay-key');
  assert.equal(
    host.operations.some(
      ({ operation }) =>
        operation === 'credential.vault.delete' || operation === 'connection.catalog.remove',
    ),
    false,
  );
});

test('a catalog race retries from authoritative state', async () => {
  const host = new FakeRuntimeHost({ failFirstCreateWithConflict: true });
  await reconcileMakaRelay(host, {
    token: TOKEN,
    readCatalog: host.catalog,
    providerAuthKind: testProviderAuthKind,
    assertReady: async () => {},
  });
  assert.equal(
    host.operations.filter(({ operation }) => operation === 'connection.catalog.create').length,
    2,
  );
  assert(host.connections.some((entry) => entry.slug === CONNECTION_SLUG));
});

test('a foreign provider owning the reserved slug stops before credentials change', async () => {
  const host = new FakeRuntimeHost({
    connections: [
      {
        connectionId: FOREIGN_CONNECTION_ID,
        revision: 1,
        slug: CONNECTION_SLUG,
        name: 'Foreign',
        providerType: 'openai-compatible',
        enabled: true,
        enabledModelIds: ['foreign-model'],
        models: [],
        catalogEntries: [],
      },
    ],
  });
  await assert.rejects(
    reconcileMakaRelay(host, {
      token: TOKEN,
      readCatalog: host.catalog,
      providerAuthKind: testProviderAuthKind,
      assertReady: async () => {},
    }),
  );
  assert.equal(host.credentials.size, 0);
  assert.deepEqual(mutationOperations(host), []);
});

test('every emitted request passes the pinned Runtime Host protocol decoders', async () => {
  const packageRoot = pinnedPackageRoot();
  const protocol = await import(
    pathToFileURL(
      resolve(packageRoot, 'node_modules/@maka/runtime-host/dist/protocol/index.js'),
    ).href
  );
  const providerRegistry = await import(
    pathToFileURL(
      resolve(packageRoot, 'node_modules/@maka/core/dist/provider-registry.js'),
    ).href
  );
  const relayProvider = providerRegistry.PROVIDER_REGISTRY[RELAY_PROVIDER_TYPE];
  assert.equal(relayProvider.authKind, 'api_key');
  assert.equal(relayProvider.runtimeAdapter.kind, 'openai');
  assert.equal(relayProvider.runtimeAdapter.apiProtocol, 'openai-responses');
  const host = new FakeRuntimeHost();
  host.seedCredential(
    {
      scope: 'connection',
      connectionId: OPENCODE_CONNECTION_ID,
      kind: 'request_headers',
    },
    'legacy-offline-secret',
    LEGACY_CREDENTIAL_IDS[0],
  );
  const fakeRequest = host.request;
  host.request = async (operation, input) => {
    const spec = protocol.HOST_OPERATION_SPECS[operation];
    spec.decodeInput(input);
    const result = await fakeRequest(operation, input);
    spec.decodeOutput(result);
    return result;
  };
  await reconcileMakaRelay(host, {
    token: TOKEN,
    readCatalog: host.catalog,
    providerAuthKind: testProviderAuthKind,
    assertReady: async () => {},
  });
  await reconcileMakaRelay(host, {
    token: TOKEN,
    readCatalog: host.catalog,
    providerAuthKind: testProviderAuthKind,
    assertReady: async () => {},
  });
  assert(host.operations.length > 0);
});
