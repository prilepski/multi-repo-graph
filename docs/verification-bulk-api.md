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

## Findings (2026-11-08, spike executed)

**Environment:** Node v22.22.3, `@colbymchenry/codegraph@1.6.2` (pin carried in
`spike/package-lock.json`; npm-sdk entry is CommonJS — `import pkg from
'@colbymchenry/codegraph'` then destructure). Scratch clone:
`spike/repos/dolphinscheduler` @ `0250417c` (fresh `--depth 1`, not the
java-graph checkout). Spike scripts: `spike/run-spike.mjs`,
`spike/export-surface.mjs`.

### Decision: bulk API it is (D1 preferred path confirmed)

The package's `.d.ts` + runtime enumeration show a complete bulk surface on
`CodeGraph`/`QueryBuilder`; full enumeration of the real dolphinscheduler
index matched `getStats()` exactly with bounded memory:

| Need | API used | Result on dolphinscheduler |
|---|---|---|
| Files | `cg.getFiles(): FileRecord[]` | 3,657 files, 7 ms |
| Symbols | `qb.iterateNodesByKind(kind)` (streaming iterator, one pass per kind) | 69,295 nodes, 286 ms, RSS flat |
| Edges | `qb.getOutgoingEdgesFrom(idBatch, kinds?)` (5k-id batches) | 143,654 edges, 238 ms, RSS flat |
| Unresolved calls | `cg.getUnresolvedReferencesFrom(nodeId)` / `...InFile` | for `external:true` CallTarget synthesis |
| WAL fold | `conn.checkpointWalTruncate()` (on `DatabaseConnection`) | before artifact compression |
| Raw fallback | `DatabaseConnection.open(path,{readOnly:true}).getDb()` | plain SQLite; not needed |

Node kinds present (dolphinscheduler): file 3,432 · class 2,480 · interface
621 · function 1,963 · method 18,436 · property 1,618 · field 6,039 ·
constant 3,052 · enum 129 · enum_member 1,102 · namespace 2,578 · import
27,499 · route 239 · variable 69 · type_alias 37 · module 1.
Edge kinds: contains 64,569 · calls 40,496 · references 19,505 · imports
13,523 · instantiates 4,064 · implements 429 · extends 679 · decorates 389.

### Schema fingerprint (O2 guard, verified at every run start)

`@colbymchenry/codegraph@1.6.2`, schema version 11. sha256 over sorted
table+column names (plus index/trigger names) of `sqlite_master`:

```
6f73261f40603bc3d0df494814c2d57616435e3750af89d06b9a01c54e1c78d0
```

Core tables (full column lists in the run manifest's fingerprint check):
`nodes(id, kind, name, qualified_name, file_path, language, start_line, …,
signature, visibility, is_exported, is_async, is_static, is_abstract,
decorators, type_parameters, return_type, updated_at)`,
`edges(id, source, target, kind, metadata, line, col, provenance)`,
`files(path, content_hash, language, size, modified_at, indexed_at,
node_count, errors, generated)`, `unresolved_refs(id, from_node_id,
reference_name, reference_kind, line, col, candidates, file_path, language,
status, name_tail)` + FTS5 (`nodes_fts*`), `name_segment_vocab`,
`project_metadata`, `schema_versions`, `synthesis_inputs`.

### Index timings / sizes (first O3 data point)

- `indexAll`: **11.1 s** wall for 3,657 files indexed (1,154 skipped as
  unsupported: .png/.md/.txt/.sql/.sh), 69,295 nodes / 143,654 edges,
  `indexState: complete`, 0 pending references, frameworks detected
  (spring, express, react, vue-router, terraform).
- `.codegraph/codegraph.db`: **220 MB** (WAL folded at close); zstd -3:
  **32.3 MB (15.3%)**. Extrapolation to 100M LOC stays open (O3).
- `FileRecord.contentHash` is a 64-hex content hash, but codegraph skips
  >1 MB files — we still compute `:File.sha256` ourselves (design §5).

### Mapping notes that shape the exporter (stage 3)

- `Node.qualifiedName` for Java is `package::Type::member` — portfolio FQN
  = `package.Type.member` (join with `.`); TS/JS names are module-relative.
- `calls` edges carry `metadata.refName` (callee name as written) — group
  by (caller, refName) into `CALLS {count}` → `:CallTarget`, preserving
  java-graph's name-level semantics; unresolved call refs synthesize
  `CallTarget {external: true}`.
- `extends`/`implements` edges are resolved within the repo; `contains`
  edges give Type→Method/Field; `DEFINES` derives from `node.filePath`.
- `import` NODES (not the `imports` edges) carry the statement in
  `signature` — parse for Java FQN imports → `(File)-[:IMPORTS]->(Package |
  Type)`; non-Java imports map to a Package named by the module specifier.
- codegraph writes `.codegraph/.gitignore` (`*` + `!.gitignore`) — after
  indexing, `git status` shows only `?? .codegraph/`; the clean-tree guard
  excludes `.codegraph/` explicitly.
- Heavy extraction hint from the SDK shim: large-repo indexing wants a
  generous `--max-old-space-size` (WASM Zone-OOM guard, issues #293/#298) —
  the worker pool launches Node children with 4 GB.
