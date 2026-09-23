#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { lstat, readFile, realpath } from 'node:fs/promises';
import { relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const EXPECTED_RELAY_SHA256 =
  '8761f73c2940365ca8a5861a9057a62f0ea2de6276b393512e50f72cb66e3bd0';
const EXPECTED_MODEL_FETCHER_SHA256 =
  '69f457fd88c1f124c360c0f5bb0195999a0997f9d9170ef5c8f61ce6e9817d01';
const EXPECTED_MAKA_SUBJECT_SHA256 =
  '2f39e06a20291b7d2759d5bd9d54e912c3d32817c15f74140ef9cf62b51ec8f3';

function fail(message) {
  throw new Error(message);
}

function requireWithin(root, candidate, label) {
  const fromRoot = relative(root, candidate);
  if (fromRoot === '..' || fromRoot.startsWith(`..${sep}`) || fromRoot === '' || fromRoot.startsWith(sep)) {
    fail(`${label} is not a child of the staged runtime`);
  }
  return fromRoot.split(sep).join('/');
}

async function requireRegularFile(path, label) {
  const metadata = await lstat(path);
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    fail(`${label} is not a regular file`);
  }
}

async function requireDirectory(path, label) {
  const metadata = await lstat(path);
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
    fail(`${label} is not a directory`);
  }
}

if (process.argv.length !== 4) {
  fail('usage: assert-eval-runtime-path.mjs RUNTIME_ROOT MAKA_BUNDLE_ROOT');
}

const runtimeRoot = await realpath(resolve(process.argv[2]));
const bundleRoot = await realpath(resolve(process.argv[3]));
if (bundleRoot !== resolve(runtimeRoot, 'maka-agent')) {
  fail('Maka bundle root does not match the staged runtime');
}

const cliPath = resolve(bundleRoot, 'dist/cli.js');
const expectedEvalEntry = resolve(bundleRoot, 'node_modules/@maka/eval/dist/index.js');
const harnessEnvironmentPath = resolve(
  bundleRoot,
  'node_modules/@maka/eval/dist/harness-environment.js',
);
const expectedRelayRoot = resolve(bundleRoot, 'node_modules/@maka/eval/harbor');
const relayPath = resolve(expectedRelayRoot, 'relay_agent.py');
const expectedModelFetcher = resolve(
  bundleRoot,
  'node_modules/@maka/runtime/dist/model-fetcher.js',
);
const expectedMakaSubject = resolve(
  bundleRoot,
  'node_modules/@maka/eval/dist/maka-subject.js',
);

await Promise.all([
  requireRegularFile(cliPath, 'staged Maka CLI'),
  requireRegularFile(expectedEvalEntry, 'staged Eval entry point'),
  requireRegularFile(harnessEnvironmentPath, 'staged harness environment'),
  requireDirectory(expectedRelayRoot, 'staged Harbor relay root'),
  requireRegularFile(relayPath, 'staged RelayAgent'),
  requireRegularFile(expectedModelFetcher, 'staged model fetcher'),
  requireRegularFile(expectedMakaSubject, 'staged Maka Eval subject'),
]);

const [resolvedCli, resolvedExpectedEvalEntry, resolvedHarnessEnvironment,
  resolvedExpectedRelayRoot, resolvedRelay, resolvedExpectedModelFetcher,
  resolvedMakaSubject] = await Promise.all([
  realpath(cliPath),
  realpath(expectedEvalEntry),
  realpath(harnessEnvironmentPath),
  realpath(expectedRelayRoot),
  realpath(relayPath),
  realpath(expectedModelFetcher),
  realpath(expectedMakaSubject),
]);
requireWithin(runtimeRoot, resolvedCli, 'Maka CLI');
requireWithin(runtimeRoot, resolvedExpectedEvalEntry, 'expected Eval entry point');
requireWithin(runtimeRoot, resolvedHarnessEnvironment, 'harness environment');
requireWithin(runtimeRoot, resolvedExpectedRelayRoot, 'expected Harbor relay root');
requireWithin(runtimeRoot, resolvedRelay, 'RelayAgent');
requireWithin(runtimeRoot, resolvedExpectedModelFetcher, 'expected model fetcher');
requireWithin(runtimeRoot, resolvedMakaSubject, 'Maka Eval subject');

const resolvedEvalEntry = await realpath(createRequire(resolvedCli).resolve('@maka/eval'));
if (resolvedEvalEntry !== resolvedExpectedEvalEntry) {
  fail('staged Maka CLI resolves @maka/eval outside the staged bundle');
}
requireWithin(runtimeRoot, resolvedEvalEntry, 'resolved Eval entry point');

const resolvedModelFetcher = await realpath(
  createRequire(resolvedCli).resolve('@maka/runtime/model-fetcher'),
);
if (resolvedModelFetcher !== resolvedExpectedModelFetcher) {
  fail('staged Maka CLI resolves the model fetcher outside the staged bundle');
}
requireWithin(runtimeRoot, resolvedModelFetcher, 'resolved model fetcher');

const harnessEnvironment = await import(pathToFileURL(resolvedHarnessEnvironment).href);
const resolvedRelayRoot = await realpath(harnessEnvironment.BUNDLED_HARNESS_RELAY_ROOT);
if (resolvedRelayRoot !== resolvedExpectedRelayRoot) {
  fail('staged @maka/eval resolves its Harbor relay outside the staged bundle');
}
requireWithin(runtimeRoot, resolvedRelayRoot, 'resolved Harbor relay root');

const relaySha256 = createHash('sha256').update(await readFile(resolvedRelay)).digest('hex');
if (relaySha256 !== EXPECTED_RELAY_SHA256) {
  fail('staged RelayAgent does not match the pinned compatibility overlay');
}
const modelFetcherSha256 = createHash('sha256')
  .update(await readFile(resolvedModelFetcher))
  .digest('hex');
if (modelFetcherSha256 !== EXPECTED_MODEL_FETCHER_SHA256) {
  fail('staged model fetcher does not match the pinned compatibility overlay');
}
const makaSubjectSha256 = createHash('sha256')
  .update(await readFile(resolvedMakaSubject))
  .digest('hex');
if (makaSubjectSha256 !== EXPECTED_MAKA_SUBJECT_SHA256) {
  fail('staged Maka Eval subject does not match the pinned compatibility overlay');
}

process.stdout.write(`${JSON.stringify({
  schema: 'maka-eval-runtime-path-v3',
  cli: requireWithin(runtimeRoot, resolvedCli, 'Maka CLI'),
  evalEntry: requireWithin(runtimeRoot, resolvedEvalEntry, 'Eval entry point'),
  harborRelayRoot: requireWithin(runtimeRoot, resolvedRelayRoot, 'Harbor relay root'),
  relayAgent: requireWithin(runtimeRoot, resolvedRelay, 'RelayAgent'),
  relayAgentSha256: relaySha256,
  modelFetcher: requireWithin(runtimeRoot, resolvedModelFetcher, 'model fetcher'),
  modelFetcherSha256,
  makaSubject: requireWithin(runtimeRoot, resolvedMakaSubject, 'Maka Eval subject'),
  makaSubjectSha256,
  stagedRuntime: true,
})}\n`);
