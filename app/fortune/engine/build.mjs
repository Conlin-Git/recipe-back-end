import { build } from 'esbuild';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
const root = dirname(fileURLToPath(import.meta.url));
await mkdir(join(root, 'dist'), { recursive: true });
const result = await build({ absWorkingDir: root, entryPoints: ['runner.ts'], outfile: 'dist/runner.cjs', platform: 'node', target: 'node18', bundle: true, minify: true, legalComments: 'eof', metafile: true, alias: { 'server-only': './server-only.ts' } });
// A committed, portable bundle lets the Python package deploy without npm or the cloned repo.
const licenses = [];
for (const pkg of ['iztro', 'lunar-javascript', 'lunar-typescript', 'lunar-lite', 'dayjs', 'i18next', '@babel/runtime']) {
  try {
    const dir = join(root, 'node_modules', pkg);
    const info = JSON.parse(await readFile(join(dir, 'package.json'), 'utf8'));
    let license;
    for (const name of ['LICENSE', 'LICENSE.md', 'LICENSE.txt']) {
      try { license = await readFile(join(dir, name), 'utf8'); break; } catch {}
    }
    if (!license) throw new Error(`License missing: ${pkg}`);
    licenses.push(`${pkg} ${info.version}\n${license}`);
  } catch (error) { throw error; }
}
await writeFile(join(root, 'dist/THIRD-PARTY-NOTICES.txt'), licenses.join('\n\n---\n\n'));
const files = [...Object.keys(result.metafile.inputs), 'package-lock.json', 'build.mjs'].sort();
const hashes = {};
for (const file of files) hashes[file] = createHash('sha256').update(await readFile(join(root, file))).digest('hex');
await writeFile(join(root, 'dist/manifest.json'), JSON.stringify({ upstream: 'https://github.com/Renhuai123/ziwei-doushu', commit: 'a600484a49255039d0725f1ba9e11ca81a948f97', bundle_sha256: createHash('sha256').update(await readFile(join(root, 'dist/runner.cjs'))).digest('hex'), sources: hashes }, null, 2) + '\n');
