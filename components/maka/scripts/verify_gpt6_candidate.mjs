#!/usr/bin/env node
/** Exercise the patched package's public model paths without credentials. */

import assert from 'node:assert/strict';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const candidate = resolve(process.argv[2] ?? '');
if (!process.argv[2]) throw new Error('candidate path required');
const packageRoot = join(candidate, 'runtime', 'package');
const load = async (relative) => import(pathToFileURL(join(packageRoot, relative)).href);
const metadata = await load('node_modules/@maka/core/dist/model-metadata.js');
const thinking = await load('node_modules/@maka/core/dist/model-thinking.js');
const registry = await load('node_modules/@maka/core/dist/provider-registry.js');

const api = metadata.lookupModelMetadata('openai', 'gpt-6-sol');
const subscription = metadata.lookupModelMetadata('openai-codex', 'gpt-6-sol');
const old = metadata.lookupModelMetadata('openai-codex', 'gpt-5.6-sol');
assert.equal(api.contextWindow, 1_050_000);
assert.equal(api.maxOutputTokens, 128_000);
assert.deepEqual(api.thinkingOptions.efforts,
  ['none', 'low', 'medium', 'high', 'xhigh', 'max']);
assert(subscription.contextWindow >= 272_000);
assert(subscription.thinkingOptions.efforts.includes('xhigh'));
assert(!subscription.thinkingOptions.efforts.includes('max'));
assert(old.thinkingOptions.efforts.includes('xhigh'));
assert.equal(metadata.openAiAdapterApiProtocol('gpt-6-sol', 'openai'), 'openai-responses');
assert.equal(thinking.modelApplyPatchEnabled('gpt-6-sol'), true);
assert(registry.PROVIDER_REGISTRY.openai.fallbackModels.includes('gpt-6-sol'));
assert(registry.PROVIDER_REGISTRY['openai-codex'].fallbackModels.includes('gpt-6-sol'));
console.log(JSON.stringify({ schema: 'khenrix-maka-gpt6-compat-v1', ok: true,
  api: 'gpt-6-sol', subscription: 'gpt-6-sol', resumable: 'gpt-5.6-sol' }));
