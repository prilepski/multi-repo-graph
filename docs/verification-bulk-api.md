# Spike: codegraph bulk export API (open item O1)

**Question:** does the `@colbymchenry/codegraph` npm package expose a bulk
enumeration API (all symbols / all edges / all files, ideally paged), or do
we read SQLite directly?

**Why it gates implementation:** design.md D1 prefers the bulk API; the
exporter (stage 3) is written against whichever surface this spike confirms.

## Known surface (from codegraph README, 2026-11-08)

High-level `CodeGraph` class:

```typescript
const cg = await CodeGraph.init('/path/to/project');   // or .open(path)
await cg.indexAll({ onProgress });                     // full index
cg.searchNodes('UserService');                         // point query
cg.getCallers(nodeId); cg.getImpactRadius(nodeId, 2);  // point queries
await cg.buildContext('…', {maxNodes, includeCode, format});
cg.watch(); cg.unwatch(); cg.close();
```

Lower-level exports from the same entry point: `DatabaseConnection`,
`QueryBuilder`, `getDatabasePath`, `initGrammars` / `loadGrammarsForLanguages`,
`FileLock`. Storage: per-project SQLite (`.codegraph/codegraph.db`), WAL
mode, tables described only as "symbols · edges · files · FTS5" — **schema is
internal/undocumented**.

Bulk enumeration is **not documented** either way — hence this spike.

## Steps

1. **Environment**: Node 22.5+ (required for `node:sqlite`); scratch dir
   outside any repo we care about; `npm init -y && npm i
   @colbymchenry/codegraph`.
2. **Index a small real repo** — dolphinscheduler (≈6.6k files) is the
   current smaller target; time `indexAll`, record `.codegraph/` sizes
   (baseline data for O3).
3. **Enumerate the API surface**:
   - read the shipped `.d.ts` (`node_modules/@colbymchenry/codegraph/dist/*.d.ts`)
     — list every public method of `CodeGraph`, `DatabaseConnection`,
     `QueryBuilder`; look for `all*`, `list*`, `dump*`, `each*`, `stream*`,
     pagination params;
   - runtime dump: `console.log(Object.getOwnPropertyNames(Object.getPrototypeOf(cg)))`
     and the same for the lower-level classes.
4. **Decision gate**:
   - Bulk API exists and covers symbols+edges+files → exporter uses it;
     record paging pattern, ordering guarantees, memory behavior on a full
     enumeration of dolphinscheduler.
   - No bulk API → SQLite fallback: open via `DatabaseConnection`, dump
     `sqlite_master`, enumerate tables/columns of symbols/edges/files/FTS5;
     capture the **schema fingerprint** (hash of table+column names) the
     runner will verify at every start (design.md §6).
5. **Record results** in this file (append a "Findings" section): API
   decision, exact method signatures used, schema fingerprint, index
   timings/sizes, and the pinned package version (`npm ls
   @colbymchenry/codegraph`).

## Guardrails

- **Pin the version** from day one (`package-lock.json` committed in this
  spike → carried into the real project). `codegraph upgrade` exists and the
  schema can shift; the runner must hard-fail on fingerprint mismatch
  (O2).
- No writes to the repos beyond `.codegraph/` itself; spike works on a
  scratch clone, not the java-graph `repos/` checkouts.
- Timebox: if neither surface yields full enumeration within the spike,
  stop and reconsider (worst case: derive edges by walking point queries —
  last resort, do not silently accept).
