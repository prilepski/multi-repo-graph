// Stage 2 worker: index ONE repo with codegraph, fold the WAL, report JSON.
// Child of index-pool.mjs. Usage: node index-worker.mjs <repoPath>
// stdout gets exactly one JSON line; progress goes to stderr (parent relays).
import { execFileSync } from 'node:child_process';
import pkg from '@colbymchenry/codegraph';
const { CodeGraph, DatabaseConnection, getDatabasePath } = pkg;

const repoPath = process.argv[2];
const emit = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");
const t0 = Date.now();
try {
  const cg = await CodeGraph.init(repoPath);
  const result = await cg.indexAll({
    onProgress: (p) => process.stderr.write(
      JSON.stringify({ repo: repoPath, ...p }) + "\n"),
  });
  const state = cg.getIndexState();
  const stats = cg.getStats();
  const pendingReferences = cg.getPendingReferenceCount();
  cg.close();

  // Fold the WAL so the artifact copy is a single self-contained file.
  const conn = DatabaseConnection.open(getDatabasePath(repoPath));
  await conn.checkpointWalTruncate();
  conn.close();

  emit({
    ok: result.success && state === 'complete',
    repoPath,
    ms: Date.now() - t0,
    indexState: state,
    pendingReferences,
    stats: { nodeCount: stats.nodeCount, edgeCount: stats.edgeCount,
             fileCount: stats.fileCount },
    filesIndexed: result.filesIndexed,
    filesSkippedUnsupported: result.filesSkippedUnsupported,
    filesErrored: result.filesErrored,
    nodesCreated: result.nodesCreated,
    edgesCreated: result.edgesCreated,
    errors: result.errors,
  });
} catch (err) {
  emit({ ok: false, repoPath, ms: Date.now() - t0, error: String(err?.stack || err) });
  process.exit(1);
}
