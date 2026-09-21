#!/usr/bin/env node
/** Verify the pinned ModelAdapter WS-to-HTTP wire contract without external I/O. */
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

const packageRoot = process.argv[2];
assert(packageRoot, 'maka package root argument is required');
const runtime = await import(pathToFileURL(`${packageRoot}/node_modules/@maka/runtime/dist/model-factory.js`));
const modelRuntime = await import(pathToFileURL(`${packageRoot}/node_modules/@maka/runtime/dist/model-runtime.js`));
const adapterModule = await import(pathToFileURL(`${packageRoot}/node_modules/@maka/runtime/dist/model-adapter.js`));
const transportModule = await import(pathToFileURL(`${packageRoot}/node_modules/@maka/runtime/dist/openai-responses-websocket.js`));
const ai = await import(pathToFileURL(`${packageRoot}/node_modules/@maka/runtime/node_modules/ai/dist/index.js`));

let websocketRequest;
const server = createServer();
server.on('upgrade', (request, socket) => {
  websocketRequest = {
    method: request.method,
    path: request.url,
    headers: request.headers,
  };
  socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
});
server.listen(0, '127.0.0.1');
await once(server, 'listening');
const address = server.address();
assert(address && typeof address === 'object');

const fallbackRequests = [];
const fakeFetch = async (input, init = {}) => {
  fallbackRequests.push({
    url: String(input),
    method: init.method,
    headers: Object.fromEntries(new Headers(init.headers)),
    body: JSON.parse(String(init.body)),
  });
  throw new Error('contract-capture');
};
const connection = {
  id: 'synthetic',
  name: 'synthetic',
  slug: 'eval-openai',
  providerType: 'openai',
  baseUrl: 'https://api.openai.com/v1',
  enabledModels: ['gpt-5.6-sol'],
  modelOverrides: {},
};
const resolvedRuntime = modelRuntime.resolveModelRuntime(connection, 'gpt-5.6-sol');
const transportState = transportModule.createOpenAiResponsesTransportState({
  webSocketUrl: `ws://127.0.0.1:${address.port}/v1/responses`,
  connectTimeoutMs: 1_000,
});
const adapter = new adapterModule.ModelAdapter({
  sessionId: 'synthetic-contract-session',
  connection,
  apiKey: 'maka-decoy-contract123',
  modelId: 'gpt-5.6-sol',
  resolvedRuntime,
  providerOptions: runtime.buildProviderOptions(connection, 'gpt-5.6-sol', 'xhigh', resolvedRuntime),
  modelFactory: (input) => runtime.getAIModel({ ...input, fetch: fakeFetch }),
  openAiResponsesTransportState: transportState,
  newId: () => 'synthetic-id',
  now: () => 0,
});

const modelTools = {
  apply_patch: { kind: 'provider', providerTool: { kind: 'openai-apply-patch' } },
  Read: {
    kind: 'client',
    description: 'Read one synthetic path',
    inputSchema: ai.jsonSchema({
      type: 'object',
      properties: { path: { type: 'string' } },
      required: ['path'],
      additionalProperties: false,
    }),
  },
};
const captureStream = async (model, activeTools, continuationKey) => {
  const result = await adapter.startStream({
    model,
    system: 'synthetic system contract',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'synthetic contract prompt' }] }],
    tools: modelTools,
    activeTools,
    continuationKey,
    abortSignal: new AbortController().signal,
    onStreamActivity: () => {},
    repairToolCall: async () => undefined,
  });
  for await (const _event of result.events) {
    // The synthetic fallback throws after capturing; consume adapter settlement.
  }
  await result.outcome;
};

try {
  const model = adapter.resolveModel();
  await captureStream(model, ['Read', 'apply_patch'], 'synthetic-contract-lane-tools');
  await captureStream(model, [], 'synthetic-contract-lane-tool-free');
} finally {
  adapter.dispose();
  server.close();
  await once(server, 'close');
}

assert(websocketRequest, 'the pinned ModelAdapter did not issue its WebSocket probe');
assert.equal(websocketRequest.method, 'GET');
assert.equal(websocketRequest.path, '/v1/responses');
assert.equal(websocketRequest.headers.authorization, 'Bearer maka-decoy-contract123');
assert.equal(websocketRequest.headers.upgrade?.toLowerCase(), 'websocket');
assert.equal(websocketRequest.headers.connection?.toLowerCase(), 'upgrade');
assert.equal(websocketRequest.headers['sec-websocket-version'], '13');
assert.equal(websocketRequest.headers['openai-beta'], 'responses_websockets=2026-02-06');
assert.match(websocketRequest.headers['sec-websocket-key'], /^[A-Za-z0-9+/]{22}==$/);

assert.equal(fallbackRequests.length, 2, 'the pinned ModelAdapter did not make both HTTP fallbacks');
const [fallbackRequest, toolFreeRequest] = fallbackRequests;
assert.equal(fallbackRequest.url, 'https://api.openai.com/v1/responses');
assert.equal(fallbackRequest.method, 'POST');
assert.equal(fallbackRequest.headers['content-type'], 'application/json');
assert.equal(fallbackRequest.headers.authorization, 'Bearer maka-decoy-contract123');
assert.deepEqual(
  new Set(Object.keys(fallbackRequest.body)),
  new Set(['model', 'input', 'parallel_tool_calls', 'store', 'include', 'reasoning', 'tools', 'tool_choice', 'prompt_cache_key', 'stream']),
);
assert.equal(fallbackRequest.body.model, 'gpt-5.6-sol');
assert.deepEqual(fallbackRequest.body.reasoning, { effort: 'xhigh', summary: 'auto' });
assert.equal(fallbackRequest.body.stream, true);
assert.equal(fallbackRequest.body.store, false);
assert.equal(fallbackRequest.body.parallel_tool_calls, true);
assert.deepEqual(fallbackRequest.body.include, ['reasoning.encrypted_content']);
assert.equal(fallbackRequest.body.prompt_cache_key, 'maka:synthetic-contract-session');
assert.equal(fallbackRequest.body.tools.length, 2);
assert(fallbackRequest.body.tools.some((tool) => tool.type === 'function' && tool.name === 'Read'));
assert(fallbackRequest.body.tools.some((tool) => tool.type === 'apply_patch'));
assert(fallbackRequest.body.tools.every((tool) => tool.type === 'function' || tool.type === 'apply_patch'));
assert.equal(fallbackRequest.body.tool_choice, 'auto');
assert(!Object.hasOwn(fallbackRequest.body, 'service_tier'));
assert(!Object.hasOwn(fallbackRequest.body, 'max_output_tokens'));
assert(!Object.hasOwn(fallbackRequest.body, 'previous_response_id'));

assert.equal(toolFreeRequest.url, 'https://api.openai.com/v1/responses');
assert.equal(toolFreeRequest.method, 'POST');
assert.equal(toolFreeRequest.headers.authorization, 'Bearer maka-decoy-contract123');
assert.deepEqual(
  new Set(Object.keys(toolFreeRequest.body)),
  new Set(['model', 'input', 'parallel_tool_calls', 'store', 'include', 'reasoning', 'prompt_cache_key', 'stream']),
);
assert.equal(toolFreeRequest.body.prompt_cache_key, 'maka:synthetic-contract-session');
assert(!Object.hasOwn(toolFreeRequest.body, 'tools'));
assert(!Object.hasOwn(toolFreeRequest.body, 'tool_choice'));
assert(!Object.hasOwn(toolFreeRequest.body, 'previous_response_id'));

if (process.argv[3]) {
  await writeFile(process.argv[3], `${JSON.stringify({
    fallbackBodies: fallbackRequests.map((request) => request.body),
    websocket: websocketRequest,
  })}\n`, { mode: 0o600 });
}
console.log('Pinned Maka ModelAdapter WebSocket fallback contract passed');
