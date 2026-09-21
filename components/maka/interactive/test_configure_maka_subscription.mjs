#!/usr/bin/env node
/** Offline protocol tests for subscription-mode default reconciliation. */

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { reconcileMakaSubscriptionDefaults } from './configure_maka_subscription.mjs';

const CODEX_ID = '00000000-0000-4000-8000-000000000001';
const OTHER_ID = '00000000-0000-4000-8000-000000000002';
const BOOTSTRAP_ID = '00000000-0000-4000-8000-000000000003';
const MANAGED_OVERRIDE = {
  'gpt-5.6-sol': { defaultThinkingLevel: 'xhigh' },
};
const BOOTSTRAP_SEED = {
  enabledModelIds: [
    'nemotron-3-ultra-free',
    'big-pickle',
    'ling-3.0-flash-fin-free',
    'mimo-v2.5-free',
    'muse-spark-1.3-contributor-free',
    'nemotron-3.5-lightning-free',
  ],
  defaultModelId: 'nemotron-3-ultra-free',
};

function pinnedPackageRoot() {
  const home = process.env.HOME;
  const candidates = [
    home ? resolve(home, '.local/bin/mise') : '',
    '/opt/homebrew/bin/mise',
    '/usr/local/bin/mise',
  ];
  const mise = candidates.find((candidate) => candidate && existsSync(candidate));
  assert(mise, 'mise must be installed at an admitted standard path');
  const labRoot = fileURLToPath(new URL('..', import.meta.url));
  const shim = execFileSync(mise, ['-C', labRoot, 'which', 'maka'], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'ignore'],
  }).trim();
  const source = readFileSync(shim, 'utf8');
  const target = source.match(/^# aube-bin-shim v2 target=([^\s]+)$/mu)?.[1];
  assert(target, 'the pinned Maka shim must declare its immutable target');
  return dirname(dirname(resolve(dirname(shim), target)));
}

function policy(overrides = {}) {
  return {
    revision: 'policy-1',
    policy: {
      chatDefaults: {
        permissionMode: 'ask',
        thinkingLevel: 'xhigh',
      },
      networkProxy: {
        enabled: false,
        protocol: 'http',
        host: '',
        port: 0,
        authEnabled: false,
        username: '',
        bypassList: [],
        autoBypassDomains: [],
      },
      ...overrides,
    },
  };
}

function fakeConnection(
  snapshot,
  { enrollment = true, commit = { kind: 'committed' }, catalog = safeCatalog() } = {},
) {
  const calls = [];
  const catalogState = structuredClone(catalog);
  return {
    calls,
    readCatalog: async () => structuredClone(catalogState),
    replaceCatalog(next) {
      for (const key of Object.keys(catalogState)) delete catalogState[key];
      Object.assign(catalogState, structuredClone(next));
    },
    connection: {
      async request(operation, input) {
        calls.push({ operation, input });
        if (operation === 'oauth.enrollment.query') return { enabled: enrollment };
        if (operation === 'runtime.policy.query') return snapshot;
        if (operation === 'runtime.policy.mutate') return commit;
        if (operation === 'connection.catalog.set-default-target') {
          assert.equal(input.expectedCatalogRevision, catalogState.revision);
          catalogState.defaultTarget = structuredClone(input.target);
          catalogState.revision += 1;
          return { kind: 'committed', catalogRevision: catalogState.revision };
        }
        if (operation === 'connection.catalog.update') {
          const candidate = catalogState.connections.find(
            (entry) => entry.connectionId === input.expected.connectionId,
          );
          assert(candidate);
          assert.equal(candidate.revision, input.expected.revision);
          Object.assign(candidate, structuredClone(input.changes));
          candidate.revision += 1;
          catalogState.revision += 1;
          return { kind: 'committed', catalogRevision: catalogState.revision };
        }
        throw new Error(`unexpected operation: ${operation}`);
      },
    },
  };
}

function codexConnection(overrides = {}) {
  return {
    connectionId: CODEX_ID,
    revision: 1,
    slug: 'openai-codex',
    name: 'OpenAI OAuth',
    providerType: 'openai-codex',
    enabled: true,
    enabledModelIds: ['gpt-5.6-sol'],
    modelOverrides: structuredClone(MANAGED_OVERRIDE),
    ...overrides,
  };
}

function safeCatalog(overrides = {}) {
  return {
    revision: 1,
    defaultTarget: { connectionId: CODEX_ID, modelId: 'gpt-5.6-sol' },
    connections: [codexConnection()],
    ...overrides,
  };
}

function bootstrapCatalog(overrides = {}) {
  const connection = {
    connectionId: BOOTSTRAP_ID,
    revision: 1,
    slug: 'opencode-free',
    name: 'OpenCode Free',
    providerType: 'opencode-free',
    enabled: true,
    enabledModelIds: [...BOOTSTRAP_SEED.enabledModelIds],
  };
  return {
    revision: 1,
    defaultTarget: {
      connectionId: BOOTSTRAP_ID,
      modelId: BOOTSTRAP_SEED.defaultModelId,
    },
    connections: [connection],
    ...overrides,
  };
}

async function reconcile(host, catalog = safeCatalog()) {
  if (arguments.length > 1) host.replaceCatalog(catalog);
  return reconcileMakaSubscriptionDefaults(host.connection, {
    readCatalog: host.readCatalog,
    bootstrapSeed: BOOTSTRAP_SEED,
  });
}

test('an already-safe subscription profile is read-only and idempotent', async () => {
  const host = fakeConnection(policy());
  await reconcile(host);
  await reconcile(host);
  assert.deepEqual(
    host.calls.map((call) => call.operation),
    [
      'oauth.enrollment.query',
      'runtime.policy.query',
      'oauth.enrollment.query',
      'runtime.policy.query',
    ],
  );
});

test('subscription reconciliation changes only safe chat defaults', async () => {
  const snapshot = policy({
    chatDefaults: {
      permissionMode: 'bypass',
      thinkingLevel: 'medium',
    },
  });
  const host = fakeConnection(snapshot);
  await reconcile(host);
  assert.deepEqual(host.calls.at(-1), {
    operation: 'runtime.policy.mutate',
    input: {
      expectedRevision: 'policy-1',
      operation: {
        kind: 'set_chat_defaults',
        value: {
          permissionMode: 'ask',
          thinkingLevel: 'xhigh',
        },
      },
    },
  });
  assert.equal(
    host.calls.some((call) =>
      call.operation.startsWith('credential.') || call.operation.startsWith('connection.catalog'),
    ),
    false,
  );
});

test('every enabled network proxy is rejected without mutation', async () => {
  const host = fakeConnection(
    policy({
      networkProxy: {
        enabled: true,
        protocol: 'socks5',
        host: 'proxy.example.invalid',
        port: 1080,
        authEnabled: false,
        username: '',
        bypassList: [],
        autoBypassDomains: [],
      },
    }),
  );
  await assert.rejects(reconcile(host), /subscription_proxy_enabled/u);
  assert.equal(host.calls.some((call) => call.operation === 'runtime.policy.mutate'), false);
});

test('first-run onboarding allows no default only when no connection is enabled', async () => {
  const clean = fakeConnection(policy());
  await reconcile(clean, { revision: 1, defaultTarget: null, connections: [] });

  const stale = fakeConnection(policy());
  await assert.rejects(
    reconcile(stale, {
      revision: 1,
      defaultTarget: null,
      connections: [
        {
          connectionId: OTHER_ID,
          providerType: 'openai',
          enabled: true,
          enabledModelIds: ['gpt-5.6-sol'],
        },
      ],
    }),
    /subscription_default_missing/u,
  );
  assert.equal(stale.calls.some((call) => call.operation === 'runtime.policy.mutate'), false);
});

test('the exact pinned OpenCode bootstrap seed is disabled without credential access', async () => {
  const host = fakeConnection(policy(), { catalog: bootstrapCatalog() });
  await reconcile(host);
  assert.deepEqual(
    host.calls
      .filter((call) => call.operation.startsWith('connection.catalog'))
      .map((call) => call.operation),
    ['connection.catalog.set-default-target', 'connection.catalog.update'],
  );
  assert.equal(
    host.calls.some(
      (call) =>
        call.operation.startsWith('credential.') ||
        call.operation === 'configuration.credentials.export',
    ),
    false,
  );
  const finalCatalog = await host.readCatalog();
  assert.equal(finalCatalog.defaultTarget, null);
  assert.equal(finalCatalog.connections[0].enabled, false);
});

test('an OpenCode bootstrap near-miss is rejected without catalog mutation', async () => {
  const nearMiss = bootstrapCatalog();
  nearMiss.connections[0].baseUrl = 'https://example.invalid/v1';
  const host = fakeConnection(policy(), { catalog: nearMiss });
  await assert.rejects(reconcile(host), /subscription_default_invalid/u);
  assert.equal(
    host.calls.some((call) => call.operation.startsWith('connection.catalog')),
    false,
  );
});

test('the pinned Runtime Host first launch reaches safe subscription onboarding', async () => {
  const home = await mkdtemp(resolve(tmpdir(), 'maka-subscription-first-run-'));
  try {
    const packageRoot = pinnedPackageRoot();
    const script = fileURLToPath(new URL('./configure_maka_subscription.mjs', import.meta.url));
    const environment = {
      HOME: home,
      USER: process.env.USER ?? 'maka-test',
      LOGNAME: process.env.LOGNAME ?? process.env.USER ?? 'maka-test',
      PATH: '/usr/bin:/bin',
      LANG: 'C',
      LC_ALL: 'C',
      MAKA_CODEX_SUBSCRIPTION_EXPERIMENTAL: '1',
    };
    const runConfigurer = () =>
      execFileSync(process.execPath, [script, '--package-root', packageRoot], {
        env: environment,
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 30_000,
      });
    runConfigurer();
    runConfigurer();
    const inspect = `
      import { pathToFileURL } from 'node:url';
      const root = process.argv[1];
      const cli = await import(pathToFileURL(root + '/dist/runtime-host-cli-context.js'));
      const reader = await import(pathToFileURL(root + '/node_modules/@maka/runtime-host/dist/client/catalog-reader.js'));
      const workspace = await import(pathToFileURL(root + '/node_modules/@maka/storage/dist/workspace-root.js'));
      const context = await cli.connectRuntimeHostCliConnection({ rootPath: workspace.resolveMakaWorkspaceRoot() });
      const catalog = await reader.readRuntimeHostConnectionCatalog(context.connection);
      const policy = await context.connection.request('runtime.policy.query', {});
      await context.close();
      process.stdout.write(JSON.stringify({ catalog, policy }));
    `;
    const inspected = JSON.parse(
      execFileSync(process.execPath, ['--input-type=module', '--eval', inspect, packageRoot], {
        env: environment,
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 30_000,
      }),
    );
    const catalog = inspected.catalog;
    assert.equal(catalog.defaultTarget, null);
    assert.equal(catalog.connections.length, 0);
    const runtimePolicy = inspected.policy;
    assert.equal(runtimePolicy.policy.networkProxy.enabled, false);
    assert.equal(runtimePolicy.policy.chatDefaults.permissionMode, 'ask');
    assert.equal(runtimePolicy.policy.chatDefaults.thinkingLevel, 'xhigh');
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test('a stale direct-API default is rejected', async () => {
  const host = fakeConnection(policy());
  await assert.rejects(
    reconcile(
      host,
      safeCatalog({
        defaultTarget: { connectionId: OTHER_ID, modelId: 'gpt-5.6-sol' },
        connections: [
          {
            connectionId: OTHER_ID,
            providerType: 'openai-responses-compatible',
            enabled: true,
            enabledModelIds: ['gpt-5.6-sol'],
          },
        ],
      }),
    ),
    /subscription_default_invalid/u,
  );
});

test('the subscription default model must be enabled', async () => {
  const host = fakeConnection(policy());
  await assert.rejects(
    reconcile(host, safeCatalog({ connections: [codexConnection({ enabledModelIds: [] })] })),
    /subscription_default_invalid/u,
  );
});

test('a custom OpenAI Codex base URL is rejected before OAuth use', async () => {
  const host = fakeConnection(policy());
  await assert.rejects(
    reconcile(
      host,
      safeCatalog({
        connections: [codexConnection({ baseUrl: 'https://attacker.example.invalid/v1' })],
      }),
    ),
    /subscription_default_invalid/u,
  );
  assert.equal(host.calls.some((call) => call.operation === 'runtime.policy.mutate'), false);
});

test('custom OpenAI Codex payload and model overlays are rejected', async () => {
  for (const changes of [
    { requestBodyOverlay: { metadata: { unsafe: true } } },
    { modelOverrides: { 'gpt-5.6-sol': { contextWindow: 1 } } },
  ]) {
    const host = fakeConnection(policy());
    await assert.rejects(
      reconcile(host, safeCatalog({ connections: [codexConnection(changes)] })),
      /subscription_default_invalid/u,
    );
  }
});

test('a non-default enabled Codex row with a custom endpoint is rejected', async () => {
  const host = fakeConnection(policy());
  await assert.rejects(
    reconcile(
      host,
      safeCatalog({
        connections: [
          codexConnection(),
          codexConnection({
            connectionId: OTHER_ID,
            slug: 'second-codex',
            baseUrl: 'https://attacker.example.invalid/v1',
          }),
        ],
      }),
    ),
    /subscription_other_provider_enabled/u,
  );
});

test('an enabled third-party connection is rejected beside a valid subscription default', async () => {
  const host = fakeConnection(policy());
  await assert.rejects(
    reconcile(
      host,
      safeCatalog({
        connections: [
          codexConnection(),
          {
            connectionId: OTHER_ID,
            providerType: 'anthropic',
            enabled: true,
            enabledModelIds: ['claude'],
          },
        ],
      }),
    ),
    /subscription_other_provider_enabled/u,
  );
});

test('disabled remnants do not block a valid subscription default', async () => {
  const host = fakeConnection(policy());
  await reconcile(
    host,
    safeCatalog({
      connections: [
        codexConnection(),
        {
          connectionId: OTHER_ID,
          providerType: 'anthropic',
          enabled: false,
          enabledModelIds: ['claude'],
        },
      ],
    }),
  );
});

test('disabled upstream subscription enrollment fails before mutation', async () => {
  const host = fakeConnection(policy(), { enrollment: false });
  await assert.rejects(
    reconcile(host),
    /chatgpt_subscription_unavailable/u,
  );
  assert.deepEqual(host.calls.map((call) => call.operation), ['oauth.enrollment.query']);
});

test('a rejected defaults mutation fails closed', async () => {
  const host = fakeConnection(
    policy({ chatDefaults: { permissionMode: 'bypass', thinkingLevel: 'medium' } }),
    { commit: { kind: 'revision_conflict' } },
  );
  await assert.rejects(
    reconcile(host),
    /chat_defaults_update_failed/u,
  );
});
