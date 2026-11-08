// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
// O1 spike: index a repo with the pinned codegraph, time it, dump stats +
// runtime API surface. Usage: node run-spike.mjs <projectRoot>
import pkg from '@colbymchenry/codegraph';
const { CodeGraph } = pkg;

const root = process.argv[2];
if (!root) { console.error('usage: node run-spike.mjs <projectRoot>'); process.exit(2); }

const t0 = Date.now();
let lastLog = 0;
const cg = await CodeGraph.init(root);
console.log(JSON.stringify({ event: 'init', ms: Date.now() - t0 }));

const t1 = Date.now();
const result = await cg.indexAll({
  onProgress: (p) => {
    const now = Date.now();
    if (now - lastLog > 5000) {
      lastLog = now;
      console.error(JSON.stringify({ event: 'progress', ...p }));
    }
  },
});
console.log(JSON.stringify({ event: 'indexAll', ms: Date.now() - t1, result }));

const stats = cg.getStats();
console.log(JSON.stringify({ event: 'stats', stats }, null, 1));
console.log(JSON.stringify({
  event: 'meta',
  indexState: cg.getIndexState(),
  pendingReferences: cg.getPendingReferenceCount(),
  buildInfo: cg.getIndexBuildInfo(),
  journalMode: cg.getJournalMode(),
  backend: String(cg.getBackend()),
  detectedFrameworks: cg.getDetectedFrameworks(),
}));

// runtime API surface (prototype method names), for the record
const proto = Object.getOwnPropertyNames(Object.getPrototypeOf(cg)).sort();
console.log(JSON.stringify({ event: 'runtimeMethods', count: proto.length, methods: proto }));

cg.close();
console.log(JSON.stringify({ event: 'done', totalMs: Date.now() - t0 }));
