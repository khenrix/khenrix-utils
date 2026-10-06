import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as runtime from './controller/harbor_judge.mjs';

test('capture runtime record binds exact input, original invocation, and both boundaries', () => {
  assert.equal(typeof runtime.captureRuntimeRecord, 'function');
  const record = runtime.captureRuntimeRecord({
    invocationId: '1'.repeat(32), rawInput: '{}\n', before: 'b'.repeat(64), after: 'b'.repeat(64),
    sessionId: `fanout-judge-${'2'.repeat(32)}`, prompt: 'fixture prompt', responseAnswerSha256: 'c'.repeat(64),
  });
  assert.equal(record.schema, 'khenrix-maka-harbor-capture-runtime-v1');
  assert.equal(record.input_sha256, 'ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356');
  assert.equal(record.boundary_before, 'b'.repeat(64));
  assert.equal(record.boundary_after, 'b'.repeat(64));
  assert.equal(record.invocation_id, '1'.repeat(32));
  assert.throws(() => runtime.captureRuntimeRecord({
    invocationId: '1'.repeat(32), rawInput: '{}\n', before: 'b'.repeat(64), after: 'e'.repeat(64),
    sessionId: `fanout-judge-${'2'.repeat(32)}`, prompt: 'fixture prompt', responseAnswerSha256: 'c'.repeat(64),
  }));
});

test('native input capture preserves exact UTF-8 split between chunks', async () => {
  assert.equal(typeof runtime.readJudgeInput, 'function');
  const bytes = Buffer.from('{"answer_a":"råd😀"}');
  async function* chunks() {
    for (const byte of bytes) yield Buffer.from([byte]);
  }
  assert.equal(await runtime.readJudgeInput(chunks()), '{"answer_a":"råd😀"}');
  async function* oversized() { yield Buffer.alloc(131073); }
  await assert.rejects(runtime.readJudgeInput(oversized()));
  async function* invalid() { yield Buffer.from([0xff]); }
  await assert.rejects(runtime.readJudgeInput(invalid()));
});
