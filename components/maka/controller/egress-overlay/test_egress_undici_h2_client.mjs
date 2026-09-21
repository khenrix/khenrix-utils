import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import {
  createProxiedFetchTransport,
} from '/opt/maka-agent/node_modules/@maka/runtime/dist/network/scoped-fetch-transport.js';
import {
  createOpenAiResponsesTransportState,
  OPENAI_RESPONSES_LANE_HEADER,
} from '/opt/maka-agent/node_modules/@maka/runtime/dist/openai-responses-websocket.js';

const undiciPackage = JSON.parse(await readFile(
  '/opt/maka-agent/node_modules/@maka/runtime/node_modules/undici/package.json',
  'utf8',
));
assert.equal(undiciPackage.version, '8.10.2');

const transport = createProxiedFetchTransport({
  enabled: true,
  type: 'http',
  host: '127.0.0.1',
  port: 8080,
  bypassList: [],
});
const state = createOpenAiResponsesTransportState({
  connectTimeoutMs: 3_000,
  failureCooldownMs: 60_000,
});
const body = {
  model: 'gpt-5.6-sol',
  reasoning: { effort: 'xhigh', summary: 'auto' },
  stream: true,
  store: false,
  parallel_tool_calls: true,
  include: ['reasoning.encrypted_content'],
  input: 'synthetic integration prompt',
  tools: [
    {
      type: 'function',
      name: 'Read',
      description: 'Read one synthetic path',
      parameters: { type: 'object', properties: {} },
    },
    { type: 'apply_patch' },
  ],
  tool_choice: 'auto',
  prompt_cache_key: 'maka:synthetic-integration',
};

try {
  const response = await state.wrapFetch(transport.fetch)(
    'https://api.openai.com/v1/responses',
    {
      method: 'POST',
      headers: {
        authorization: 'Bearer maka-decoy-integration123',
        'content-type': 'application/json',
        [OPENAI_RESPONSES_LANE_HEADER]: 'synthetic-lane',
      },
      body: JSON.stringify(body),
    },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(JSON.parse(await response.text()), { ok: true });
} finally {
  state.close();
  await transport.close();
}

console.log('Pinned Undici WebSocket-to-H2 fallback integration passed');
