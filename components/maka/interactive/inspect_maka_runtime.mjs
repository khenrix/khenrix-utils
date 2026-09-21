#!/usr/bin/env node
/** Verify the managed Maka runtime policy without reading credential payloads. */

import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import process from 'node:process';

const EXPECTED_VERSION = '0.2.0-dev.44.20260920';
const MODEL_ID = 'gpt-5.6-sol';
const RELAY_SLUG = 'keychain-openai';
const RELAY_NAME = 'OpenAI via local Keychain relay';
const RELAY_PROVIDER = 'openai-responses-compatible';
const RELAY_BASE_URL = 'http://127.0.0.1:48173/v1';
const RELAY_PROXY = {
  enabled: true,
  protocol: 'http',
  host: '127.0.0.1',
  port: 48173,
  authEnabled: true,
  username: 'maka-local',
  bypassList: ['127.0.0.1', 'localhost'],
  autoBypassDomains: [],
};

class SafeInspectionError extends Error {
  constructor(code) {
    super(code);
    this.name = 'SafeInspectionError';
    this.code = code;
  }
}

function fail(code) {
  throw new SafeInspectionError(code);
}

function sameStrings(left, right) {
  return (
    Array.isArray(left) &&
    Array.isArray(right) &&
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function exactKeys(value, names) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    sameStrings(Object.keys(value).sort(), [...names].sort())
  );
}

function hasExactApiOverride(value) {
  if (!exactKeys(value, [MODEL_ID])) return false;
  const model = value[MODEL_ID];
  return (
    exactKeys(model, ['thinkingLevels', 'defaultThinkingLevel']) &&
    sameStrings(model.thinkingLevels, ['xhigh', 'max']) &&
    model.defaultThinkingLevel === 'xhigh'
  );
}

function hasExactSubscriptionOverride(value) {
  if (!exactKeys(value, [MODEL_ID])) return false;
  const model = value[MODEL_ID];
  return (
    exactKeys(model, ['defaultThinkingLevel']) &&
    model.defaultThinkingLevel === 'xhigh'
  );
}

function hasExactRelayProxy(value) {
  return (
    exactKeys(value, Object.keys(RELAY_PROXY)) &&
    value.enabled === RELAY_PROXY.enabled &&
    value.protocol === RELAY_PROXY.protocol &&
    value.host === RELAY_PROXY.host &&
    value.port === RELAY_PROXY.port &&
    value.authEnabled === RELAY_PROXY.authEnabled &&
    value.username === RELAY_PROXY.username &&
    sameStrings(value.bypassList, RELAY_PROXY.bypassList) &&
    sameStrings(value.autoBypassDomains, RELAY_PROXY.autoBypassDomains)
  );
}

export function validateMakaRuntimeSnapshot(mode, { policySnapshot, catalog, enrollment }) {
  const chatDefaults = policySnapshot?.policy?.chatDefaults;
  if (
    chatDefaults === null ||
    typeof chatDefaults !== 'object' ||
    chatDefaults.permissionMode !== 'ask' ||
    chatDefaults.thinkingLevel !== 'xhigh'
  ) {
    fail('chat_defaults_invalid');
  }
  if (!catalog || !Array.isArray(catalog.connections)) fail('catalog_invalid');
  const target = catalog.defaultTarget;
  if (!target || target.modelId !== MODEL_ID) fail('default_model_invalid');
  const selected = catalog.connections.filter(
    (candidate) => candidate.connectionId === target.connectionId,
  );
  if (selected.length !== 1 || selected[0].enabled !== true) fail('default_connection_invalid');
  const enabled = catalog.connections.filter((candidate) => candidate.enabled === true);
  if (enabled.length !== 1 || enabled[0].connectionId !== selected[0].connectionId) {
    fail('other_connection_enabled');
  }
  const connection = selected[0];
  if (!sameStrings(connection.enabledModelIds, [MODEL_ID])) fail('enabled_models_invalid');

  if (mode === 'api-key-relay') {
    if (
      connection.slug !== RELAY_SLUG ||
      connection.name !== RELAY_NAME ||
      connection.providerType !== RELAY_PROVIDER ||
      connection.baseUrl !== RELAY_BASE_URL ||
      Object.hasOwn(connection, 'requestBodyOverlay') ||
      !hasExactApiOverride(connection.modelOverrides)
    ) {
      fail('api_connection_invalid');
    }
    if (!hasExactRelayProxy(policySnapshot.policy.networkProxy)) fail('api_proxy_invalid');
    return;
  }

  if (mode === 'chatgpt-subscription') {
    if (enrollment?.enabled !== true) fail('subscription_unavailable');
    if (
      connection.providerType !== 'openai-codex' ||
      Object.hasOwn(connection, 'baseUrl') ||
      Object.hasOwn(connection, 'requestBodyOverlay') ||
      !hasExactSubscriptionOverride(connection.modelOverrides)
    ) {
      fail('subscription_connection_invalid');
    }
    if (policySnapshot.policy.networkProxy?.enabled === true) fail('subscription_proxy_enabled');
    return;
  }

  fail('auth_mode_invalid');
}

function scrubAmbientProviderKeys() {
  for (const name of Object.keys(process.env)) {
    if (name.endsWith('_API_KEY')) delete process.env[name];
  }
}

function parseArguments(commandLine) {
  if (commandLine.length === 1 && commandLine[0] === '--self-test') return { selfTest: true };
  if (
    commandLine.length !== 4 ||
    commandLine[0] !== '--mode' ||
    !['api-key-relay', 'chatgpt-subscription'].includes(commandLine[1]) ||
    commandLine[2] !== '--package-root'
  ) {
    fail('invalid_arguments');
  }
  return { mode: commandLine[1], packageRoot: resolve(commandLine[3]) };
}

async function validatePackageRoot(root) {
  let manifest;
  try {
    manifest = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  } catch {
    fail('maka_package_invalid');
  }
  if (manifest?.name !== 'maka-agent' || manifest?.version !== EXPECTED_VERSION) {
    fail('maka_package_invalid');
  }
  return root;
}

async function inspectRuntime({ mode, packageRoot }) {
  scrubAmbientProviderKeys();
  const root = await validatePackageRoot(packageRoot);
  const cliContext = await import(
    pathToFileURL(join(root, 'dist/runtime-host-cli-context.js')).href
  );
  const catalogClient = await import(
    pathToFileURL(join(root, 'node_modules/@maka/runtime-host/dist/client/catalog-reader.js')).href
  );
  const workspace = await import(
    pathToFileURL(join(root, 'node_modules/@maka/storage/dist/workspace-root.js')).href
  );
  const context = await cliContext.connectRuntimeHostCliConnection({
    rootPath: workspace.resolveMakaWorkspaceRoot(),
  });
  try {
    const policySnapshot = await context.connection.request('runtime.policy.query', {});
    const catalog = await catalogClient.readRuntimeHostConnectionCatalog(context.connection);
    const enrollment =
      mode === 'chatgpt-subscription'
        ? await context.connection.request('oauth.enrollment.query', { provider: 'openai-codex' })
        : undefined;
    validateMakaRuntimeSnapshot(mode, { policySnapshot, catalog, enrollment });
  } finally {
    await context.close().catch(() => undefined);
  }
}

async function selfTest() {
  const policySnapshot = {
    policy: {
      chatDefaults: { permissionMode: 'ask', thinkingLevel: 'xhigh' },
      networkProxy: structuredClone(RELAY_PROXY),
    },
  };
  const base = {
    connectionId: '00000000-0000-4000-8000-000000000001',
    enabled: true,
    enabledModelIds: [MODEL_ID],
  };
  const api = {
    ...base,
    slug: RELAY_SLUG,
    name: RELAY_NAME,
    providerType: RELAY_PROVIDER,
    baseUrl: RELAY_BASE_URL,
    modelOverrides: {
      [MODEL_ID]: { thinkingLevels: ['xhigh', 'max'], defaultThinkingLevel: 'xhigh' },
    },
  };
  validateMakaRuntimeSnapshot('api-key-relay', {
    policySnapshot,
    catalog: {
      defaultTarget: { connectionId: api.connectionId, modelId: MODEL_ID },
      connections: [api],
    },
  });
  const subscription = {
    ...base,
    providerType: 'openai-codex',
    modelOverrides: { [MODEL_ID]: { defaultThinkingLevel: 'xhigh' } },
  };
  validateMakaRuntimeSnapshot('chatgpt-subscription', {
    policySnapshot: {
      policy: {
        chatDefaults: { permissionMode: 'ask', thinkingLevel: 'xhigh' },
        networkProxy: { enabled: false },
      },
    },
    catalog: {
      defaultTarget: { connectionId: subscription.connectionId, modelId: MODEL_ID },
      connections: [subscription],
    },
    enrollment: { enabled: true },
  });
  const malformed = structuredClone(subscription);
  malformed.modelOverrides[MODEL_ID].thinkingLevels = ['max'];
  let rejected = false;
  try {
    validateMakaRuntimeSnapshot('chatgpt-subscription', {
      policySnapshot: {
        policy: {
          chatDefaults: { permissionMode: 'ask', thinkingLevel: 'xhigh' },
          networkProxy: { enabled: false },
        },
      },
      catalog: {
        defaultTarget: { connectionId: malformed.connectionId, modelId: MODEL_ID },
        connections: [malformed],
      },
      enrollment: { enabled: true },
    });
  } catch (error) {
    rejected = error instanceof SafeInspectionError;
  }
  if (!rejected) throw new Error('self_test_expected_rejection');
}

function safeFailureCode(error) {
  if (error instanceof SafeInspectionError) return error.code;
  if (error && typeof error === 'object') {
    const name =
      typeof error.name === 'string' && /^[A-Za-z]{1,80}$/u.test(error.name)
        ? error.name.replace(/([a-z])([A-Z])/gu, '$1_$2').toLowerCase()
        : undefined;
    if (name) return name;
  }
  return 'inspection_failed';
}

async function run(commandLine) {
  const options = parseArguments(commandLine);
  if (options.selfTest) {
    await selfTest();
    process.stdout.write('Maka runtime inspection self-test OK.\n');
    return;
  }
  await inspectRuntime(options);
  process.stdout.write(`Maka runtime policy OK: ${options.mode}.\n`);
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
  run(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`Maka runtime inspection failed safely (${safeFailureCode(error)}).\n`);
    process.exitCode = 78;
  });
}
