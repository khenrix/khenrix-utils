import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { chmod, mkdtemp, mkdir, readFile, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { runtimeBoundaryDigest } from './harbor_judge.mjs';

assert(process.env.KHENRIX_MAKA_PACKAGE_ROOT, 'explicit unpatched .62 baseline package required');

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'maka-harbor-judge-test-'));
  t.after(async () => {
    const { rm } = await import('node:fs/promises');
    await rm(root, { recursive: true, force: true });
  });
  return root;
}

test('the read-only boundary pins installed source receipt and the entire staged runtime tree', async (t) => {
  const root = await fixture(t);
  const component = join(root, 'maka');
  const runtime = join(component, 'runtime/linux-amd64',
    'maka-0.2.0-dev.62.20260928-node-24.16.0-relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1-undici-h2-close-v2');
  const sourceComponent = dirname(dirname(fileURLToPath(import.meta.url)));
  const packageRoot = process.env.KHENRIX_MAKA_PACKAGE_ROOT ?? resolve(dirname(execFileSync(
    'mise', ['-C', sourceComponent, 'which', 'maka'], { encoding: 'utf8' },
  ).trim()), '..', 'maka-agent');
  const undiciPath = 'node_modules/@maka/runtime/node_modules/undici/lib/dispatcher/client-h2.js';
  const originalH2 = await readFile(join(packageRoot, undiciPath), 'utf8');
  assert.equal(createHash('sha256').update(originalH2).digest('hex'),
    '611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7');
  const stagedPackage = join(root, 'staged-package');
  const stagedH2 = join(stagedPackage, undiciPath);
  await mkdir(dirname(stagedH2), {recursive: true});
  await writeFile(stagedH2, originalH2);
  execFileSync('python', ['-B', join(sourceComponent, 'controller/apply_undici_h2_close_compat.py'),
    stagedPackage], {timeout: 5000});
  const patchedH2 = await readFile(stagedH2, 'utf8');
  assert.equal(createHash('sha256').update(patchedH2).digest('hex'),
    '32c2b747c1c1c9c7405e60dc70145f67f9b8f96db0b6264f55e1f0400a375dc4');
  const sourceFiles = new Map([
    ['controller/harbor_judge.mjs', 'runner'],
    ['controller/egress-overlay/egress_filter.py', 'relay'],
    ['mise.toml', 'node = "24.16.0"'],
    ['mise.lock', 'locked'],
  ]);
  sourceFiles.set('install-files.txt', `${[...sourceFiles.keys(), 'install-files.txt'].join('\n')}\n`);
  const sourceHash = createHash('sha256');
  for (const [name, content] of sourceFiles) {
    const target = join(component, name);
    await mkdir(join(target, '..'), { recursive: true });
    await writeFile(target, content);
    sourceHash.update(name).update('\0').update(content).update('\0');
  }
  const runtimeFiles = new Map([
    ['maka-agent/package.json', '{"version":"0.2.0-dev.62.20260928"}'],
    ['maka-agent/dist/cli.js', 'cli'],
    ['maka-agent/node_modules/@maka/runtime-host/dist/client/hosted-execution.js', 'host'],
    ['node/bin/node', 'node'],
    [`maka-agent/${undiciPath}`, patchedH2],
    ['.complete', `maka=0.2.0-dev.62.20260928
node=24.16.0
compatibility-overlay-bundle=relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1-undici-h2-close-v2
compatibility-overlays=relay-root-virtiofs-v2,eval-openai-onboarding-v2,harbor-judge-sol61-v1,undici-h2-close-v2
relay-agent-sha256=6214ed402d2d8345b20b625739b9a08905116bfdd944775938d5d78054033621
model-fetcher-sha256=7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5
maka-subject-sha256=f47c1278ed25434f2a1a012d26ed285eb930e737acbc182cbdcda6b914887775
model-metadata-sha256=9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7
undici-h2-base-sha256=611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7
undici-h2-sha256=32c2b747c1c1c9c7405e60dc70145f67f9b8f96db0b6264f55e1f0400a375dc4
`],
  ]);
  for (let index = 0; index < 512; index++) {
    runtimeFiles.set(`maka-agent/bulk/file-${index}.txt`, `file-${index}`);
  }
  let manifest = '';
  for (const [name, content] of runtimeFiles) {
    const target = join(runtime, name);
    await mkdir(join(target, '..'), { recursive: true });
    await writeFile(target, content);
    await chmod(target, 0o444);
    manifest += `${createHash('sha256').update(content).digest('hex')}  ./${name}\n`;
  }
  await mkdir(join(runtime, 'empty/nested'), { recursive: true });
  const entrypoint = await readFile(join(sourceComponent, 'controller/entrypoint.sh'), 'utf8');
  const stagingFragment = entrypoint.slice(entrypoint.indexOf('  printf \'%s\\n\' "$expected_complete"'),
    entrypoint.indexOf('  (\n    cd "$staging"'));
  await chmod(join(runtime, '.complete'), 0o644);
  execFileSync('/bin/bash', ['-c', `set -eu\n${stagingFragment}`], {
    env: { ...process.env, staging: runtime, expected_complete: runtimeFiles.get('.complete').trimEnd() },
  });
  await chmod(join(runtime, '.complete'), 0o444);
  await assert.rejects(readFile(join(runtime, 'empty/nested')), { code: 'ENOENT' });
  manifest = execFileSync('python', [join(sourceComponent, 'controller/runtime_manifest_verify.py'),
    runtime, execFileSync('which', ['sha256sum'], { encoding: 'utf8' }).trim()], { encoding: 'utf8' });
  await writeFile(join(runtime, 'RUNTIME_SHA256SUMS'), manifest, { mode: 0o444 });
  await writeFile(join(runtime, 'RUNTIME_SYMLINKS'), '', { mode: 0o444 });
  const receiptPath = join(root, 'install-receipt.json');
  const receipt = {
    schema: 'khenrix-maka-install-v2', component,
    source_digest: sourceHash.digest('hex'),
    runtime_package_digest: 'a'.repeat(64),
  };
  await writeFile(receiptPath, JSON.stringify(receipt), { mode: 0o600 });
  const started = performance.now();
  const boundary = await runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` });
  if (process.env.MAKA_BOUNDARY_BENCHMARK === '1') {
    t.diagnostic(`${runtimeFiles.size}-file boundary: ${(performance.now() - started).toFixed(1)} ms`);
  }
  assert.match(boundary, /^[0-9a-f]{64}$/u);
  const oldJudgeRuntime = runtime.replace('-undici-h2-close-v2', '');
  await assert.rejects(runtimeBoundaryDigest(oldJudgeRuntime, join(oldJudgeRuntime, 'maka-agent'),
    component, receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }),
  /runtime path/u);
  const h2RuntimeFile = join(runtime, `maka-agent/${undiciPath}`);
  await chmod(h2RuntimeFile, 0o644);
  await writeFile(h2RuntimeFile, originalH2);
  await chmod(h2RuntimeFile, 0o444);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SHA256SUMS'), manifest.replace(
    '32c2b747c1c1c9c7405e60dc70145f67f9b8f96db0b6264f55e1f0400a375dc4',
    '611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7'));
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o444);
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }),
  /runtime manifest/u);
  await chmod(h2RuntimeFile, 0o644);
  await writeFile(h2RuntimeFile, patchedH2);
  await chmod(h2RuntimeFile, 0o444);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SHA256SUMS'), manifest);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o444);
  const oldCompletion = runtimeFiles.get('.complete')
    .replace('-harbor-judge-sol61-v1', '').replace(',harbor-judge-sol61-v1', '')
    .replace('7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5',
      '427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7')
    .replace('model-metadata-sha256=9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7\n', '');
  await chmod(join(runtime, '.complete'), 0o644);
  await writeFile(join(runtime, '.complete'), oldCompletion);
  await chmod(join(runtime, '.complete'), 0o444);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SHA256SUMS'), manifest.replace(
    createHash('sha256').update(runtimeFiles.get('.complete')).digest('hex') + '  ./.complete',
    createHash('sha256').update(oldCompletion).digest('hex') + '  ./.complete'));
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o444);
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }),
  /completion marker/u);
  await chmod(join(runtime, '.complete'), 0o644);
  await writeFile(join(runtime, '.complete'), runtimeFiles.get('.complete'));
  await chmod(join(runtime, '.complete'), 0o444);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SHA256SUMS'), manifest);
  await chmod(join(runtime, 'RUNTIME_SHA256SUMS'), 0o444);
  assert.equal(await runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), boundary);
  const swapped = await runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'c'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` });
  assert.notEqual(swapped, boundary);
  const swappedEgress = await runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'d'.repeat(64)}` });
  assert.notEqual(swappedEgress, boundary);
  await writeFile(receiptPath, JSON.stringify({ ...receipt,
    runtime_package_digest: 'c'.repeat(64) }), { mode: 0o600 });
  const swappedReceipt = await runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` });
  assert.notEqual(swappedReceipt, boundary);
  await writeFile(receiptPath, JSON.stringify(receipt), { mode: 0o600 });
  await writeFile(join(runtime, 'unmanifested.txt'), 'injected');
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /runtime tree/i);
  const { rm } = await import('node:fs/promises');
  await rm(join(runtime, 'unmanifested.txt'));
  await mkdir(join(runtime, 'unmanifested-directory'));
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /runtime tree/i);
  await rm(join(runtime, 'unmanifested-directory'), { recursive: true });
  await symlink('/etc/passwd', join(runtime, 'escape'));
  await chmod(join(runtime, 'RUNTIME_SYMLINKS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SYMLINKS'), './escape -> /etc/passwd\n');
  await chmod(join(runtime, 'RUNTIME_SYMLINKS'), 0o444);
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /symlink escapes/i);
  await rm(join(runtime, 'escape'));
  await chmod(join(runtime, 'RUNTIME_SYMLINKS'), 0o644);
  await writeFile(join(runtime, 'RUNTIME_SYMLINKS'), '');
  await chmod(join(runtime, 'RUNTIME_SYMLINKS'), 0o444);
  await chmod(join(runtime, 'maka-agent/dist/cli.js'), 0o644);
  await writeFile(join(runtime, 'maka-agent/dist/cli.js'), 'changed');
  await chmod(join(runtime, 'maka-agent/dist/cli.js'), 0o444);
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /runtime differs/i);
  await chmod(join(runtime, 'maka-agent/dist/cli.js'), 0o644);
  await writeFile(join(runtime, 'maka-agent/dist/cli.js'), 'cli');
  await chmod(join(runtime, 'maka-agent/dist/cli.js'), 0o444);
  const outside = join(root, 'outside-cli');
  await writeFile(outside, 'cli');
  await rm(join(runtime, 'maka-agent/dist/cli.js'));
  await symlink(outside, join(runtime, 'maka-agent/dist/cli.js'));
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /runtime file is unsafe/i);
  await rm(join(runtime, 'maka-agent/dist/cli.js'));
  await writeFile(join(runtime, 'maka-agent/dist/cli.js'), 'cli');
  await chmod(join(runtime, 'maka-agent/dist/cli.js'), 0o444);
  await writeFile(join(component, 'mise.lock'), 'changed');
  await assert.rejects(runtimeBoundaryDigest(runtime, join(runtime, 'maka-agent'), component,
    receiptPath, { controller: `sha256:${'a'.repeat(64)}`, egress: `sha256:${'b'.repeat(64)}` }), /installed source/i);
});
