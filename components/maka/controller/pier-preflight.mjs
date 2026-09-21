import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const specPath = join(process.cwd(), 'config', 'pier-preflight.json');
const spec = JSON.parse(await readFile(specPath, 'utf8'));
const evalDist = join(process.env.MAKA_EVAL_MAKA_BUNDLE_PATH, 'node_modules', '@maka', 'eval', 'dist');
const { parseExperimentSpec } = await import(pathToFileURL(join(evalDist, 'spec.js')).href);
const { createPierExecutor } = await import(pathToFileURL(join(evalDist, 'harness-executor.js')).href);
const parsed = parseExperimentSpec(spec);
const executor = createPierExecutor(parsed.executor.config, specPath);
await executor.preflight({ subjectCredentialNames: [] });
console.log(JSON.stringify({ ok: true, framework: 'pier', version: '0.3.0', trialStarted: false }));
