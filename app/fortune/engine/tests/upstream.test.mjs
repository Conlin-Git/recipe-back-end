import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { build } from 'esbuild';
const root = dirname(fileURLToPath(import.meta.url));
// Upstream tests are preserved except for the vendored import paths.
for (const name of ['pan-integrity', 'solar-date-exists', 'gender-validation', 'leap-policy-validation', 'patterns-sihua-bonus', 'natal-dual-sihua-same-palace']) {
  test(`upstream: ${name}`, async () => {
    const dir = await mkdtemp(join(tmpdir(), 'recipe-ziwei-'));
    try {
      const outfile = join(dir, 'test.cjs');
      await build({ entryPoints: [join(root, 'upstream', `${name}.test.ts`)], outfile, platform: 'node', target: 'node18', bundle: true, logLevel: 'silent', alias: { 'server-only': join(root, '../server-only.ts') } });
      const run = spawnSync(process.execPath, [outfile], { encoding: 'utf8', timeout: 60000 });
      assert.equal(run.status, 0, run.stdout + run.stderr);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
}
