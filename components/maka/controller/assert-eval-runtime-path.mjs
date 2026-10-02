#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { lstat, readFile, realpath } from 'node:fs/promises';
import { basename, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const EXPECTED_RELAY_SHA256 =
  '6214ed402d2d8345b20b625739b9a08905116bfdd944775938d5d78054033621';
const EXPECTED_MODEL_FETCHER_SHA256 =
  '427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7';
const EXPECTED_MAKA_SUBJECT_SHA256 =
  'f47c1278ed25434f2a1a012d26ed285eb930e737acbc182cbdcda6b914887775';

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

if (process.argv.length !== 4 && process.argv.length !== 5) {
  fail('usage: assert-eval-runtime-path.mjs RUNTIME_ROOT MAKA_BUNDLE_ROOT [harbor-judge-sol61-v1|harbor-judge-sol61-h2-v1]');
}
const variant = process.argv[4];
const h2Variant = variant === 'harbor-judge-sol61-h2-v1';
const judgeVariant = variant === 'harbor-judge-sol61-v1' || h2Variant;
if (process.argv.length === 5 && !judgeVariant) fail('unknown runtime compatibility variant');

const runtimeRoot = await realpath(resolve(process.argv[2]));
const h2Suffix = '-harbor-judge-sol61-v1-undici-h2-close-v1';
if ((h2Variant && basename(runtimeRoot) !==
    `maka-0.2.0-dev.62.20260928-node-24.16.0-relay-root-virtiofs-v2-eval-openai-onboarding-v2${h2Suffix}`) ||
    (!h2Variant && basename(runtimeRoot).endsWith(h2Suffix))) {
  fail('staged runtime path does not match the compatibility variant');
}
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
const expectedModelMetadata = resolve(bundleRoot, 'node_modules/@maka/core/dist/model-metadata.js');

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
if (modelFetcherSha256 !== (judgeVariant ?
  '7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5' : EXPECTED_MODEL_FETCHER_SHA256)) {
  fail('staged model fetcher does not match the pinned compatibility overlay');
}
const makaSubjectSha256 = createHash('sha256')
  .update(await readFile(resolvedMakaSubject))
  .digest('hex');
if (makaSubjectSha256 !== EXPECTED_MAKA_SUBJECT_SHA256) {
  fail('staged Maka Eval subject does not match the pinned compatibility overlay');
}
let judgeMetadata = {};
if (judgeVariant) {
  await requireRegularFile(expectedModelMetadata, 'staged model metadata');
  const resolvedModelMetadata = await realpath(expectedModelMetadata);
  const cliModelMetadata = await realpath(createRequire(resolvedCli).resolve('@maka/core/model-metadata'));
  if (cliModelMetadata !== resolvedModelMetadata) fail('staged CLI resolves model metadata outside its bundle');
  const modelMetadata = requireWithin(runtimeRoot, resolvedModelMetadata, 'model metadata');
  const modelMetadataSha256 = createHash('sha256').update(await readFile(resolvedModelMetadata)).digest('hex');
  if (modelMetadataSha256 !== '9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7') {
    fail('staged model metadata does not match the pinned judge overlay');
  }
  judgeMetadata = { compatibilityVariant: variant, modelMetadata, modelMetadataSha256 };
}
let h2Metadata = {};
if (h2Variant) {
  const expectedUndiciH2 = resolve(bundleRoot,
    'node_modules/@maka/runtime/node_modules/undici/lib/dispatcher/client-h2.js');
  await requireRegularFile(expectedUndiciH2, 'staged Undici H2 client');
  const resolvedUndiciH2 = await realpath(expectedUndiciH2);
  const runtimeUndiciH2 = await realpath(createRequire(resolve(bundleRoot,
    'node_modules/@maka/runtime/package.json')).resolve('undici/lib/dispatcher/client-h2.js'));
  if (resolvedUndiciH2 !== runtimeUndiciH2) fail('staged runtime resolves Undici outside its bundle');
  const undiciH2 = requireWithin(runtimeRoot, resolvedUndiciH2, 'Undici H2 client');
  const undiciH2Sha256 = createHash('sha256').update(await readFile(resolvedUndiciH2)).digest('hex');
  if (undiciH2Sha256 !== '521fef59ccb84909a3c68d52c15e972f61836e0d386093d5d4f4e64ba7d85087') {
    fail('staged Undici H2 client does not match the pinned close overlay');
  }
  h2Metadata = { undiciH2,
    undiciH2BaseSha256: '611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7',
    undiciH2Sha256 };
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
  ...judgeMetadata,
  ...h2Metadata,
  stagedRuntime: true,
})}\n`);
