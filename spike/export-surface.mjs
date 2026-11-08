// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
// O1 spike: full enumeration via the bulk API (QueryBuilder) on a real index.
// Measures wall time + RSS for: nodes (streaming iterator, per kind),
// edges (getOutgoingEdgesFrom over id batches), files. Prints counts to
// compare against getStats(). Usage: node export-surface.mjs <projectRoot>
import pkg from '@colbymchenry/codegraph';
const { CodeGraph, DatabaseConnection, QueryBuilder, NODE_KINDS } = pkg;

const root = process.argv[2];
const t0 = Date.now();
const rss = () => Math.round(process.memoryUsage().rss / 1e6);
const cg = await CodeGraph.open(root, { readOnly: true });
const stats = cg.getStats();

// lower-level: same db via DatabaseConnection + QueryBuilder (the exported
// fallback surface) to prove enumeration without the CodeGraph wrapper
const { getDatabasePath } = pkg;
const dbPath = getDatabasePath(root);
const conn = DatabaseConnection.open(dbPath, { readOnly: true });
const qb = new QueryBuilder(conn.getDb());

console.log(JSON.stringify({
  event: 'open', dbPath, schemaVersion: conn.getSchemaVersion(),
  journalMode: conn.getJournalMode(), rssMb: rss(), stats: {
    nodeCount: stats.nodeCount, edgeCount: stats.edgeCount, fileCount: stats.fileCount,
  },
}));

// 1) nodes via streaming iterators (bounded memory)
const tN = Date.now();
let nodeTotal = 0;
const ids = [];
for (const kind of NODE_KINDS) {
  let n = 0;
  for (const node of qb.iterateNodesByKind(kind)) {
    n++;
    if (n <= 2) ids.push(node.id); // sample ids for the edge test below
  }
  nodeTotal += n;
  if (n) console.log(JSON.stringify({ event: 'kindCount', kind, n }));
}
console.log(JSON.stringify({ event: 'nodes', nodeTotal, ms: Date.now() - tN, rssMb: rss() }));

// 2) all node ids in memory (what edge batching would need at minimum)
const tI = Date.now();
const allIds = [];
for (const kind of NODE_KINDS) {
  for (const node of qb.iterateNodesByKind(kind)) allIds.push(node.id);
}
console.log(JSON.stringify({ event: 'allIds', count: allIds.length, ms: Date.now() - tI, rssMb: rss() }));

// 3) edges via batched outgoing lookup
const tE = Date.now();
let edgeTotal = 0;
const kindsSeen = {};
const B = 5000;
for (let i = 0; i < allIds.length; i += B) {
  const edges = qb.getOutgoingEdgesFrom(allIds.slice(i, i + B));
  edgeTotal += edges.length;
  for (const e of edges) kindsSeen[e.kind] = (kindsSeen[e.kind] || 0) + 1;
}
console.log(JSON.stringify({ event: 'edges', edgeTotal, kindsSeen, ms: Date.now() - tE, rssMb: rss() }));

// 4) files
const tF = Date.now();
const files = qb.getAllFiles ? qb.getAllFiles() : null;
console.log(JSON.stringify({ event: 'files', viaGetAllFiles: files !== null,
  count: files ? files.length : 'n/a', ms: Date.now() - tF, rssMb: rss() }));

// 5) sample edge shapes (first few, for the mapping notes)
const sample = qb.getOutgoingEdgesFrom(ids);
console.log(JSON.stringify({ event: 'sampleEdges', sample: sample.slice(0, 4) }, null, 1));

conn.close();
cg.close();
console.log(JSON.stringify({ event: 'done', totalMs: Date.now() - t0, rssMb: rss() }));
