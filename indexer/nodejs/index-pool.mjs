// Stage 2 parent: worker pool over repos — one child Node process per repo,
// default concurrency min(4, CPUs) (design.md §7). Usage:
//   node index-pool.mjs <configJsonPath>
// config: {"repos": [{"name","path"}], "concurrency"?: number}
// Prints one JSON result line per repo to stdout; exit 1 if any repo failed.
import { cpus } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

const here = path.dirname(fileURLToPath(import.meta.url));
const config = JSON.parse((await import('node:fs'))
  .readFileSync(process.argv[2], 'utf-8'));
const concurrency = Math.max(1, config.concurrency ?? Math.min(4, cpus().length));

const queue = [...config.repos];
const results = [];
let failed = 0;

function runOne(repo) {
  return new Promise((resolve) => {
    // 4 GB heap: the SDK shim's WASM Zone-OOM guard note for large indexing
    // (spike findings, verification-bulk-api.md).
    const child = spawn(process.execPath, [
      '--max-old-space-size=4096',
      path.join(here, 'index-worker.mjs'),
      repo.path,
    ], { cwd: here, stdio: ['ignore', 'pipe', 'pipe'] });

    let out = '';
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => process.stderr.write(d)); // progress relay
    child.on('close', (code) => {
      let res;
      try {
        res = JSON.parse(out.trim().split('\n').pop());
      } catch {
        res = { ok: false, repoPath: repo.path, error: `worker exited code=${code} output=${out.slice(0, 400)}` };
      }
      res.name = repo.name;
      if (!res.ok) failed += 1;
      process.stdout.write(JSON.stringify(res) + "\n");
      results.push(res);
      resolve();
    });
  });
}

const workers = Array.from({ length: Math.min(concurrency, queue.length) }, async () => {
  while (queue.length) {
    const repo = queue.shift();
    await runOne(repo);
  }
});
await Promise.all(workers);
process.stdout.write(JSON.stringify({
  summary: true, total: results.length, failed,
  concurrency,
}) + "\n");
process.exit(failed ? 1 : 0);
