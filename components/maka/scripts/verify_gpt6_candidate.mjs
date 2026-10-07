#!/usr/bin/env node
/** Exercise the patched package's public model paths without credentials. */

import assert from 'node:assert/strict';
import { realpath } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { isAbsolute, join, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const candidate = resolve(process.argv[2] ?? '');
if (!process.argv[2]) throw new Error('candidate path required');
const packageRoot = join(candidate, 'runtime', 'package');
const runtimeRequire = createRequire(pathToFileURL(join(
  packageRoot, 'node_modules/@maka/runtime/package.json')));
const aiPath = await realpath(runtimeRequire.resolve('ai'));
const aiRelative = relative(await realpath(packageRoot), aiPath);
assert(aiRelative !== '..' && !aiRelative.startsWith(`..${sep}`) && !isAbsolute(aiRelative),
  'AI SDK resolves outside the candidate package');
const ai = await import(pathToFileURL(aiPath).href);
const load = async (relative) => import(pathToFileURL(join(packageRoot, relative)).href);
const metadata = await load('node_modules/@maka/core/dist/model-metadata.js');
const thinking = await load('node_modules/@maka/core/dist/model-thinking.js');
const registry = await load('node_modules/@maka/core/dist/provider-registry.js');
const modelFactory = await load('node_modules/@maka/runtime/dist/model-factory.js');
const modelRuntime = await load('node_modules/@maka/runtime/dist/model-runtime.js');

const api = metadata.lookupModelMetadata('openai', 'gpt-6.1-sol');
const subscription = metadata.lookupModelMetadata('openai-codex', 'gpt-6.1-sol');
const old = metadata.lookupModelMetadata('openai-codex', 'gpt-5.6-sol');
const previous = metadata.lookupModelMetadata('openai-codex', 'gpt-6-sol');
assert.deepEqual(previous.thinkingOptions.efforts, ['low', 'medium', 'high', 'xhigh']);
assert.equal(api.capabilities.reasoning, true);
assert.equal(api.capabilities.functionCalling, true);
assert.equal(api.capabilities.vision, true);
assert.equal(api.contextWindow, 1_050_000);
assert.equal(api.maxOutputTokens, 128_000);
assert.deepEqual(api.thinkingOptions.efforts,
  ['low', 'medium', 'high', 'xhigh', 'max']);
assert.equal(subscription.contextWindow, 1_050_000);
assert(subscription.thinkingOptions.efforts.includes('xhigh'));
assert(!subscription.thinkingOptions.efforts.includes('max'));
assert(old.thinkingOptions.efforts.includes('xhigh'));
assert.equal(metadata.openAiAdapterApiProtocol('gpt-6.1-sol', 'openai'), 'openai-responses');
assert.equal(thinking.modelApplyPatchEnabled('gpt-6.1-sol'), true);
assert(registry.PROVIDER_REGISTRY.openai.fallbackModels.includes('gpt-6.1-sol'));
assert(registry.PROVIDER_REGISTRY['openai-codex'].fallbackModels.includes('gpt-6.1-sol'));

const relayConnection = {
  slug: 'keychain-openai',
  providerType: 'custom',
  defaultApiProtocol: 'openai-responses',
  baseUrl: 'https://relay.invalid/v1',
  enabledModelIds: ['gpt-6.1-sol', 'gpt-5.6-sol'],
  modelOverrides: {
    'gpt-6.1-sol': {
      thinkingLevels: ['xhigh', 'max'], defaultThinkingLevel: 'xhigh',
      capabilities: { parallelToolCalls: true },
    },
    'gpt-5.6-sol': {
      thinkingLevels: ['xhigh', 'max'], defaultThinkingLevel: 'xhigh',
      capabilities: { parallelToolCalls: true },
    },
  },
};
async function captureRequest(connection, modelId, withTools = false) {
  const resolvedRuntime = modelRuntime.resolveModelRuntime(connection, modelId);
  let requestUrl;
  let requestBody;
  const model = modelFactory.getAIModel({
    connection,
    apiKey: 'synthetic-decoy',
    modelId,
    resolvedRuntime,
    fetch: async (url, init) => {
      requestUrl = String(url);
      requestBody = JSON.parse(String(init.body));
      throw new Error('contract-capture');
    },
  });
  const result = ai.streamText({
    model,
    prompt: 'Synthetic relay request',
    providerOptions: modelFactory.buildProviderOptions(
      connection, modelId, 'xhigh', resolvedRuntime,
    ),
    ...(withTools ? {
      tools: {
        Read: {
          description: 'Read a synthetic path',
          inputSchema: ai.jsonSchema({
            type: 'object',
            properties: { path: { type: 'string' } },
            required: ['path'],
            additionalProperties: false,
          }),
        },
      },
    } : {}),
    maxRetries: 0,
    onError: () => {},
  });
  try { await result.text; } catch { /* The fake transport stops after capture. */ }
  assert(requestBody, `${connection.providerType} Responses request did not reach the transport`);
  return { requestUrl, requestBody };
}
relayConnection.enabledModelIds.splice(1, 0, 'gpt-6-sol');
relayConnection.modelOverrides['gpt-6-sol'] = structuredClone(relayConnection.modelOverrides['gpt-6.1-sol']);
for (const modelId of ['gpt-6.1-sol', 'gpt-6-sol', 'gpt-5.6-sol']) {
  for (const withTools of [false, true]) {
    const { requestUrl, requestBody } = await captureRequest(relayConnection, modelId, withTools);
    assert.equal(new URL(requestUrl).pathname, '/v1/responses');
    assert.equal(requestBody.model, modelId);
    assert.equal(requestBody.reasoning.effort, 'xhigh');
    assert.equal(requestBody.parallel_tool_calls, true,
      `${modelId} ${withTools ? 'tool' : 'tool-free'} request must allow parallel calls`);
    if (withTools) {
      assert(Array.isArray(requestBody.tools));
      assert(requestBody.tools.some((tool) => tool.type === 'function' && tool.name === 'Read'));
    }
  }
}
for (const providerType of ['custom', 'opencode', 'opencode-go', 'github-copilot']) {
  const modelId = providerType === 'custom' ? 'gpt-6.1-sol' : 'synthetic-model';
  const connection = {
    slug: `unrelated-${providerType}`,
    providerType,
    baseUrl: 'https://unrelated.invalid/v1',
    enabledModelIds: [modelId],
    ...(providerType === 'custom'
      ? { defaultApiProtocol: 'openai-responses' }
      : { modelOverrides: { [modelId]: { apiProtocol: 'openai-responses' } } }),
  };
  const { requestBody } = await captureRequest(connection, modelId, true);
  assert.equal(requestBody.parallel_tool_calls, undefined,
    `${providerType} without an explicit capability must retain its default`);
}
console.log(JSON.stringify({ schema: 'khenrix-maka-gpt6-compat-v1', ok: true,
  api: 'gpt-6.1-sol', subscription: 'gpt-6.1-sol', resumable: 'gpt-5.6-sol' }));
