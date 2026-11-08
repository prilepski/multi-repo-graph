# Design: multi-repo-indexer

Status: **implemented — first verified end-to-end run 2026-11-08** (both
baseline repos: 2.55M LOC, all count metrics OK, 2,000/2,000 sampled edges,
artifacts published to the local index store; GitHub mode exercised via
`--publish-dry-run`). Rev 1 seeded 2026-11-08 from the java-graph → codegraph
evaluation conversation; implementation lives in `indexer/` of this repo.
Decisions D1–D7 are settled; open items are tracked in §9.

## 1. Purpose

A **batch** indexer that turns a manifest-listed set of cloned repositories
into three artifacts:

1. **Per-repo codegraph indexes** — `.codegraph/` SQLite graphs produced by
   [codegraph](https://github.com/colbymchenry/codegraph) (34 languages,
   tree-sitter based, framework-aware routes, cross-file resolution).
2. **A unified Neo4j portfolio graph** — the java-graph schema
   (`:Repository :Package :File :Type :Method :Field :CallTarget` + edges),
   populated by an exporter that reads the codegraph indexes.
3. **Timestamp-versioned artifacts** published to an index-store repo
   (GitHub private repo) or a local fallback folder — including a Neo4j
   database dump and a run manifest that records where every codegraph
   index lives.

No live updates, no watchers (decision D2). The pipeline operates on cloned,
quiescent versions of the repos. Future work (§10) adds a snapshot-to-local
process so MCPs and the graph-viewer run from published runs.

### Relationship to java-graph

This project is **completely separate** from java-graph but copies its
foundations (see `java-graph-references.md` for the file-by-file copy map):

| java-graph component | Fate here |
|---|---|
| `java-indexer/index_to_neo4j.py` parse phase (tree-sitter-java, FQN resolution) | **Replaced** by codegraph extraction (Java-only → 34 languages) |
| Container lifecycle, chunked UNWIND ingest, idempotent MERGE, verification, logging/summary | **Copied** (same patterns, same constants) |
| Portfolio Neo4j schema | **Retained verbatim** — the MFLens MCP contract must not change |
| `mcp-server` (60+4 tools), `graph-viewer` | **Not ported** — future snapshot consumers, unchanged |
| `inventory.py` (file taxonomy / LOC) | **Deferred** — natural port later (§10) |

## 2. Architecture

```
manifest.json ──► stage 0: validate ──► stage 1: ensure clones
                                              │  (cloneUrl if path missing; shallow default)
                                              ▼
                              stage 2: codegraph index (per repo)
                                 Node 22.5+ runner: CodeGraph.init/indexAll
                                 worker pool, one child process per repo
                                              │
                                              ▼
                              stage 3: export (per repo)
                                 bulk API preferred (D1), SQLite fallback
                                 streaming reads → portfolio-schema rows
                                              │
                                              ▼
                              stage 4: Neo4j ingest (single writer)
                                 batched UNWIND 5k rows, idempotent MERGE,
                                 uniqueness constraints (java-graph pattern)
                                              │
                              ┌───────────────┴───────────────┐
                              ▼                               ▼
                    stage 5: verify                stage 6: artifacts
                    extracted vs ingested          neo4j dump (zstd) +
                    counts per repo;               codegraph DBs (zstd) +
                    sampled edge checks            run-manifest.json +
                    at scale (§7)                  summary.json
                              │                               │
                              └───────────────┬───────────────┘
                                              ▼
                              stage 7: publish
                                indexStore given → clone/pull store repo,
                                write runs/<UTC-timestamp>/, commit, push
                                otherwise     → local folder next to repos
```

Failure semantics (java-graph rule, kept): exit code 0 only if every repo
indexed, exported, ingested, verified, and published cleanly; partial runs
write artifacts + summary with explicit failure markers but fail the run.

## 3. Input manifest (`manifest.json`)

```json
{
  "repos": [
    {
      "name": "thingsboard",
      "cloneUrl": "https://github.com/thingsboard/thingsboard.git",
      "path": "../repos/thingsboard",
      "shallow": true
    },
    {
      "name": "dolphinscheduler",
      "cloneUrl": "https://github.com/apache/dolphinscheduler.git",
      "path": "../repos/dolphinscheduler",
      "shallow": true
    }
  ],
  "indexStore": {
    "provider": "github",
    "repo": "prilepski/multi-repo-index-store",
    "branch": "main",
    "auth": "gh"
  }
}
```

Rules:

- `repos[]` — required, non-empty. `name` unique. `path` used if it exists
  (a git checkout — codegraph honors `.gitignore`); otherwise cloned from
  `cloneUrl` into `path`'s parent. `shallow` defaults `true` (`--depth 1` is
  sufficient: codegraph reads the tree, not history; HEAD SHA still
  recorded in the run manifest).
- `indexStore` — **optional** (D3). Present → publish to the GitHub repo
  (created 2026-11-08: `prilepski/multi-repo-index-store`, private).
  Absent → publish to `./index-store/` created next to the repos directory.
- Working trees must be **clean of build residue** — java-graph's lesson
  (1,667 stray `target/**/*.class` files polluted its first inventory).
  Enforce: refuse `path`s with untracked build artifacts, or clean them
  behind a `--force-clean` flag.

## 4. Index store layout

```
runs/2026-11-08T12-00-00Z/          # UTC ISO-8601, second precision
  run-manifest.json
  summary.json                       # counts, durations, verification, failures
  neo4j/neo4j.dump.zst               # neo4j-admin database dump, zstd
  codegraphs/
    thingsboard/codegraph.db.zst     # WAL folded before compress
    dolphinscheduler/codegraph.db.zst
latest.json                          # pointer: {"run": "2026-11-08T12-00-00Z"}
```

**`run-manifest.json`** — the answer to "where is codegraph" for any run:

```json
{
  "runId": "2026-11-08T12-00-00Z",
  "startedAt": "…", "finishedAt": "…",
  "tools": {
    "multiRepoIndexer": "0.1.0",
    "codegraph": "<npm package version>",
    "neo4jImage": "neo4j:5-community"
  },
  "schemaFingerprint": "<hash of codegraph SQLite table/column names>",
  "repos": [
    {
      "name": "thingsboard",
      "cloneUrl": "https://github.com/thingsboard/thingsboard.git",
      "commitSha": "<HEAD at index time>",
      "loc": 786000,
      "codegraphDb": "codegraphs/thingsboard/codegraph.db.zst",
      "codegraphDbSha256": "…",
      "nodesExported": 123456,
      "edgesExported": 456789
    }
  ],
  "neo4j": {"dump": "neo4j/neo4j.dump.zst", "dumpSha256": "…", "database": "neo4j"},
  "verification": {"status": "passed", "reportRef": "summary.json"}
}
```

**Versioning & retention:** every run is a new timestamped directory; commits
are `index-run: <timestamp> (<repo names>, <total LOC>)`. `keepLast` config
(default **3**): after a successful publish, prune older run dirs and push
the prune. `latest.json` lets snapshot tooling resolve the current run
without listing.

**Git constraints (binding):**

- Files >100 MB are rejected by GitHub → `.gitattributes` marks
  `*.dump.zst`, `*.db.zst` as **LFS**. LFS storage/bandwidth quotas apply —
  **open item O3**: measure real dump sizes on the current two-repo
  portfolio and extrapolate to 100M LOC before committing to retention=3.
- Single pushes >2 GB are rejected → publish pushes at most one run at a
  time; if one run exceeds this, artifacts must be chunked (split archives)
  or the dump moves out of git (decision then).

## 5. Portfolio graph schema (target — unchanged from java-graph)

```
Nodes: (:Repository), (:Package), (:File {path, sha256}),
       (:Type {kind: class|interface|enum|record|annotation}),
       (:Method), (:Field), (:CallTarget)
Edges: (Repository)-[:CONTAINS]->(File | Package)
       (Package)-[:CONTAINS]->(Type)
       (File)-[:DEFINES]->(Type)
       (Type)-[:CONTAINS]->(Method | Field)
       (Type)-[:EXTENDS | :IMPLEMENTS]->(Type)
       (File)-[:IMPORTS]->(Package | Type)
       (Method)-[:CALLS {count}]->(CallTarget)
```

`:File.sha256` is computed by **this** pipeline (codegraph skips >1 MB files
and does not store our hash) — drift detection semantics for downstream MCPs
are preserved. `:CallTarget` nodes for unresolved targets are **synthesized
by the exporter** with `external: true` (codegraph leaves unresolved
references implicit).

### Mapping from codegraph's model

| codegraph | portfolio schema | Notes |
|---|---|---|
| symbol kind class/interface/… | `:Type {kind}` | direct |
| method/function symbols | `:Method` | arity from signature if exposed, else `null`; `id` keeps java-graph's `FQN#method` convention |
| field/property symbols | `:Field` | direct |
| `extends` / `implements` edges | `:EXTENDS` / `:IMPLEMENTS` | direct |
| call edges | `:CALLS {count}` | exporter **groups** by (caller, target); count stored |
| file entries | `:File {path, sha256}` | hash ours; >1 MB files that codegraph skipped still get `:File` rows (inventory-grade, no `DEFINES`) |
| import edges | `:IMPORTS` | best-effort FQN resolution (java-graph rule) |
| repo (init directory) | `:Repository {name}` | from manifest, not from codegraph |
| non-OO languages (TS functions, …) | `:Method` under synthetic `:Type {kind:'module'}` per file | **V1 simplification** — revisit when frontend MCP queries exist |
| `route` nodes, dynamic-dispatch edges | *not mapped* | remain in codegraph DBs; consumed later by the codegraph-side MCP (§10) |

## 6. Export strategy (decision D1)

1. **Preferred:** codegraph library bulk API (enumerate all symbols/edges).
   Precondition — the **bulk-API spike** (`verification-bulk-api.md`) must
   confirm it exists and characterize it.
2. **Fallback:** read the per-repo SQLite directly via the library's
   exported `DatabaseConnection` / `QueryBuilder`. Requires:
   - **version pinning** — `package-lock.json` committed; the run refuses to
     start if the installed codegraph version ≠ pinned;
   - **schema fingerprint** — hash of table+column names captured at design
     time, verified at run start; mismatch → hard fail with a "re-spike
     schema" error, never a silent wrong export.

Either way the exporter **streams** (paged queries → UNWIND batches); the
full graph is never held in memory (§7).

## 7. Scale plan — target: 100M LOC across multiple repos (D5)

Baseline measurement first: the current portfolio (thingsboard +
dolphinscheduler ≈ 1.05M Java LOC + ~0.5M TS/other) parses + ingests in
java-graph's pipeline in ~15 s. Codegraph's cost is measured in the first
real run; everything below is then extrapolated ×~100.

| Stage | Approach at 100M LOC |
|---|---|
| Clone | parallel shallow clones; disk budget ≈ repos' own size (~2–5 GB) |
| codegraph index | per-repo **child process** worker pool (default `min(4, CPUs)`); bounded memory per worker; per-repo SQLite keeps failure blast radius = one repo |
| Export | paged/streaming reads; transform is O(rows); no full-graph materialization |
| Ingest | single Neo4j writer, 5,000-row UNWIND batches (java-graph constant), periodic commits; constraints created once up-front; idempotent MERGE → re-runs safe |
| Neo4j | community single instance; heap 8–16 GB, page cache ≥ hot graph size; est. 20–60M nodes+rels at target — validate on baseline ×10 before ×100 (O4) |
| Artifacts | dumps compressed zstd; **expected multi-GB** → LFS + retention; O3 measures and decides |
| Verification | exact node/edge counts per repo always; **edge-level checks sampled** (e.g. 200 random edges re-queried) above 10M LOC so verification stays O(sample), not O(graph) |

## 8. Testing strategy (java-graph pattern, ported)

- **Offline unit tests**: mapping functions over synthetic codegraph fixture
  DBs (small SQLite files checked in); manifest validation; run-manifest
  writer; retention/prune logic against a fake store dir.
- **Neo4j integration test** (`NEO4J_TEST_URI`): export fixtures → ingest
  **twice** (idempotency) → assert graph shape, counts, `CALLS {count}`
  grouping, `external: true` synthesis.
- **Publish test**: local-folder mode end-to-end; GitHub mode exercised via
  `--dry-run` (builds the commit, prints it, pushes nothing).

## 9. Open items

| # | Item | Blocked by |
|---|---|---|
| O1 | ~~Bulk API existence/shape~~ **resolved** | bulk API confirmed + used (see `verification-bulk-api.md` Findings) |
| O2 | codegraph schema stability across `upgrade` | spike + first pinned version |
| O3 | Artifact sizes at 100M LOC → LFS quota, retention, chunking | first full run measurements |
| O4 | Neo4j at 20–60M nodes on community edition | baseline ×10 run |
| O5 | ~~java-graph relocation~~ **resolved** | code copied and enhanced into `~/projects/multi-repo-graph` (frozen base: `reference/java-indexer/`); original stays read-only in the session workspace |

## 10. Deferred / future work

- **Change events → incremental re-export** (explicitly out of scope now, D2).
- **Snapshot-to-local runner**: fetch a run from the store, restore the Neo4j
  dump into a local container, decompress codegraph DBs, start the MFLens
  MCP and graph-viewer against them.
- **Codegraph-compliant portfolio MCP**: fan-out wrapper over the per-repo
  codegraph DBs from a snapshot (search/callers/impact/explore across all
  repos); complements — never replaces — the MFLens 60+4 contract.
- **Inventory/stats layer** port (java-graph `inventory.py` + its design doc).

## Decisions log

| # | Topic | Decision |
|---|---|---|
| D1 | Export source | codegraph **bulk API preferred**; SQLite via `DatabaseConnection` as fallback; version-pinned + schema fingerprint either way |
| D2 | Live updates | **None** — batch over cloned repos; change events deferred |
| D3 | Artifact store | `indexStore` in manifest → GitHub private repo `prilepski/multi-repo-index-store` (created 2026-11-08), timestamp-versioned commits; absent → local folder next to repos |
| D4 | Dump contents | Neo4j database dump + per-repo codegraph DBs + run manifest recording where every codegraph index lives |
| D5 | Scale target | Up to **100M LOC** across repos — shapes parallelism, streaming, retention |
| D6 | codegraph exclusions | Accepted as-is (`node_modules`/`target`/`dist`/`.gitignore`/>1 MB); clean-clone enforcement added |
| D7 | Schema | java-graph portfolio schema retained verbatim; MFLens MCP contract unchanged |
