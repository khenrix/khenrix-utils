#!/usr/bin/env node
/** Enforce secret-free defaults before Maka uses its own ChatGPT device OAuth. */

import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import process from 'node:process';

export const MODEL_ID = 'gpt-6-sol';
export const LEGACY_MODEL_ID = 'gpt-5.6-sol';
export const MODEL_IDS = [MODEL_ID, LEGACY_MODEL_ID];
const DEFAULT_THINKING_LEVEL = 'xhigh';
const LEGACY_OPENCODE_BOOTSTRAP = {
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

class SafeConfigurationError extends Error {
  constructor(code) {
    super(code);
    this.name = 'SafeConfigurationError';
    this.code = code;
  }
}

function safeFailureCode(error) {
  if (error instanceof SafeConfigurationError) return error.code;
  if (error && typeof error === 'object') {
    const name =
      typeof error.name === 'string' && /^[A-Za-z]{1,80}$/u.test(error.name)
        ? error.name.replace(/([a-z])([A-Z])/gu, '$1_$2').toLowerCase()
        : undefined;
    if (name) return name;
  }
  return 'configuration_failed';
}

function expectCommitted(result) {
  if (result?.kind === 'committed') return;
  throw new SafeConfigurationError('chat_defaults_update_failed');
}

function sameStrings(left, right) {
  return (
    Array.isArray(left) &&
    Array.isArray(right) &&
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function isExactBootstrapSeed(catalog, seed) {
  if (!seed || catalog.connections.length !== 1) return false;
  const candidate = catalog.connections[0];
  const target = catalog.defaultTarget;
  const targetIsSeed =
    target === null ||
    (target?.connectionId === candidate.connectionId && target?.modelId === seed.defaultModelId);
  return (
    targetIsSeed &&
    candidate.slug === 'opencode-free' &&
    candidate.name === 'OpenCode Free' &&
    candidate.providerType === 'opencode-free' &&
    candidate.enabled === true &&
    sameStrings(candidate.enabledModelIds, seed.enabledModelIds) &&
    candidate.baseUrl === undefined &&
    candidate.modelOverrides === undefined &&
    candidate.requestBodyOverlay === undefined
  );
}

function desiredModelOverrides() {
  return Object.fromEntries(MODEL_IDS.map((model) =>
    [model, { defaultThinkingLevel: DEFAULT_THINKING_LEVEL }]));
}

function isManagedOverride(value, modelIds) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const entries = Object.entries(value);
  if (!sameStrings(entries.map(([model]) => model), modelIds)) return false;
  return entries.every(([, override]) =>
    override !== null &&
    typeof override === 'object' &&
    !Array.isArray(override) &&
    Object.keys(override).length === 1 &&
    override.defaultThinkingLevel === DEFAULT_THINKING_LEVEL
  );
}

function hasDesiredModelOverrides(value) {
  return isManagedOverride(value, MODEL_IDS);
}

function isSafeCodexConnection(candidate) {
  return (
    candidate.providerType === 'openai-codex' &&
    candidate.baseUrl === undefined &&
    candidate.requestBodyOverlay === undefined &&
    (candidate.modelOverrides === undefined ||
      isManagedOverride(candidate.modelOverrides, [LEGACY_MODEL_ID]) ||
      hasDesiredModelOverrides(candidate.modelOverrides))
  );
}

async function prepareSubscriptionCatalog(connection, readCatalog, bootstrapSeed) {
  let catalog = await readCatalog(connection);
  if (!catalog || !Array.isArray(catalog.connections)) {
    throw new SafeConfigurationError('subscription_catalog_invalid');
  }
  if (isExactBootstrapSeed(catalog, bootstrapSeed)) {
    let candidate = catalog.connections[0];
    if (catalog.defaultTarget !== null) {
      expectCommitted(
        await connection.request('connection.catalog.set-default-target', {
          expectedCatalogRevision: catalog.revision,
          target: null,
        }),
      );
      catalog = await readCatalog(connection);
      if (!isExactBootstrapSeed(catalog, bootstrapSeed) || catalog.defaultTarget !== null) {
        throw new SafeConfigurationError('subscription_bootstrap_default_clear_failed');
      }
      candidate = catalog.connections[0];
    }
    expectCommitted(
      await connection.request('connection.catalog.update', {
        expected: { connectionId: candidate.connectionId, revision: candidate.revision },
        changes: {
          name: candidate.name,
          enabled: false,
          enabledModelIds: candidate.enabledModelIds,
        },
      }),
    );
    catalog = await readCatalog(connection);
  }
  const enabled = catalog.connections.filter((candidate) => candidate.enabled === true);
  const target = catalog.defaultTarget;
  if (target === null || target === undefined) {
    if (enabled.length !== 0) {
      throw new SafeConfigurationError('subscription_default_missing');
    }
    return;
  }
  let selected = catalog.connections.filter(
    (candidate) => candidate.connectionId === target.connectionId,
  );
  if (
    selected.length !== 1 ||
    selected[0].enabled !== true ||
    !isSafeCodexConnection(selected[0]) ||
    !Array.isArray(selected[0].enabledModelIds) ||
    selected[0].enabledModelIds.length === 0 ||
    !selected[0].enabledModelIds.every((model) => MODEL_IDS.includes(model))
  ) {
    throw new SafeConfigurationError('subscription_default_invalid');
  }
  if (enabled.length !== 1 || enabled[0].connectionId !== selected[0].connectionId) {
    throw new SafeConfigurationError('subscription_other_provider_enabled');
  }
  if (!sameStrings(selected[0].enabledModelIds, MODEL_IDS) ||
      !hasDesiredModelOverrides(selected[0].modelOverrides)) {
    expectCommitted(
      await connection.request('connection.catalog.update', {
        expected: {
          connectionId: selected[0].connectionId,
          revision: selected[0].revision,
        },
        changes: {
          enabledModelIds: MODEL_IDS,
          modelOverrides: desiredModelOverrides(),
        },
      }),
    );
    catalog = await readCatalog(connection);
    selected = catalog.connections.filter(
      (candidate) => candidate.connectionId === target.connectionId,
    );
    if (
      selected.length !== 1 ||
      !sameStrings(selected[0].enabledModelIds, MODEL_IDS) ||
      !hasDesiredModelOverrides(selected[0].modelOverrides)
    ) {
      throw new SafeConfigurationError('subscription_model_default_update_failed');
    }
  }
  if (target.modelId !== MODEL_ID) {
    expectCommitted(
      await connection.request('connection.catalog.set-default-target', {
        expectedCatalogRevision: catalog.revision,
        target: { connectionId: selected[0].connectionId, modelId: MODEL_ID },
      }),
    );
    catalog = await readCatalog(connection);
    if (catalog.defaultTarget?.connectionId !== selected[0].connectionId ||
        catalog.defaultTarget?.modelId !== MODEL_ID) {
      throw new SafeConfigurationError('subscription_default_update_failed');
    }
  }
}

export async function reconcileMakaSubscriptionDefaults(
  connection,
  { readCatalog, bootstrapSeed },
) {
  if (typeof readCatalog !== 'function') {
    throw new SafeConfigurationError('catalog_reader_missing');
  }
  const enrollment = await connection.request('oauth.enrollment.query', {
    provider: 'openai-codex',
  });
  if (enrollment?.enabled !== true) {
    throw new SafeConfigurationError('chatgpt_subscription_unavailable');
  }

  const snapshot = await connection.request('runtime.policy.query', {});
  if (snapshot.policy?.networkProxy?.enabled === true) {
    throw new SafeConfigurationError('subscription_proxy_enabled');
  }
  await prepareSubscriptionCatalog(connection, readCatalog, bootstrapSeed);
  const current = snapshot.policy?.chatDefaults;
  if (!current || typeof current !== 'object') {
    throw new SafeConfigurationError('chat_defaults_missing');
  }
  if (current.permissionMode === 'ask' && current.thinkingLevel === 'xhigh') return;
  expectCommitted(
    await connection.request('runtime.policy.mutate', {
      expectedRevision: snapshot.revision,
      operation: {
        kind: 'set_chat_defaults',
        value: { ...current, permissionMode: 'ask', thinkingLevel: 'xhigh' },
      },
    }),
  );
}

function scrubAmbientProviderKeys() {
  for (const name of Object.keys(process.env)) {
    if (name.endsWith('_API_KEY')) delete process.env[name];
  }
}

function parseArguments(commandLine) {
  if (commandLine.length !== 2 || commandLine[0] !== '--package-root') {
    throw new SafeConfigurationError('invalid_arguments');
  }
  return { packageRoot: resolve(commandLine[1]) };
}

async function validatePackageRoot(root) {
  const manifest = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  if (manifest?.name !== 'maka-agent') throw new SafeConfigurationError('maka_package_invalid');
  return root;
}

async function run(commandLine) {
  scrubAmbientProviderKeys();
  const options = parseArguments(commandLine);
  const packageRoot = await validatePackageRoot(options.packageRoot);
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
  const context = await cliContext.connectRuntimeHostCliConnection({
    rootPath: workspace.resolveMakaWorkspaceRoot(),
  });
  try {
    await reconcileMakaSubscriptionDefaults(
      context.connection,
      {
        readCatalog: catalogClient.readRuntimeHostConnectionCatalog,
        bootstrapSeed: LEGACY_OPENCODE_BOOTSTRAP,
      },
    );
  } finally {
    await context.close().catch(() => undefined);
  }
  process.stdout.write('Maka ChatGPT subscription defaults are configured.\n');
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
  run(process.argv.slice(2)).catch((error) => {
    process.stderr.write(
      `Maka subscription configuration failed safely (${safeFailureCode(error)}).\n`,
    );
    process.exitCode = 78;
  });
}
