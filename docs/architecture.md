# Architecture & Specification

multi-repo-graph turns a list of cloned repositories into a queryable
portfolio code graph and publishes versioned snapshots into a
per-company **codegraph-workspace**. This document specifies what the
system is and how each part works. Configuration and day-to-day usage
live in the [README](../README.md) and
[workspace-reference.md](workspace-reference.md); the decision log is
[decisions.md](decisions.md).

## 1. System model

Two repo types, strictly separated:

| | Tool repo (`multi-repo-graph`) | Workspace repo (`<org>/codegraph-workspace`) |
|---|---|---|
| Contains | pipeline, MCP servers, viewer, tests, docs | repo list, snapshots, deployed runtime |
| Who uses it | the operator running indexing | **everyone** consuming the index |
| Runs write to it | never | always (git commits) |

The pipeline executes out of a tool-repo checkout, targets a workspace
(`--workspace <path-or-git-url>`), and the workspace becomes the single
user-facing artifact: `git clone` it, run `runtime/scripts/up.sh`, get
Neo4j + viewer + both MCP servers at the exact indexed state.

## 2. Components

```
multi-repo-graph/
  indexer/                batch pipeline (stages 0–8, below)
    nodejs/               Node side: codegraph index pool + bulk exporter
      index-pool.mjs        worker pool parent (one child per repo)
      index-worker.mjs      codegraph indexAll + WAL fold for one repo
      export.mjs            codegraph bulk API -> portfolio rows (streamed)
    manifest.py           repo specs, validation, clean-tree guard, clones
    workspace.py          workspace.json, workspace skeleton/git ensure
    exporter.py           file scan (sha256/inventory) + Node stream merge
    inventory.py          taxonomy + LOC rules (ported from java-graph)
    ingest.py             batched UNWIND Cypher, StatsSnapshot ingestion
    verify.py             per-repo count checks + sampled edge checks
    container.py          Neo4j ingest container lifecycle + dual-db dump
    artifacts.py          run dir, zstd compression, run-manifest/summary
    publish.py            publish-into-workspace (commit/prune/push)
    deploy.py             stage 8: runtime deployment + workspace README
    runtime_templates/    up.py / down.py + sh/ps1 bootstrap shims
  mcp-server/             MFLens MCP — 60+4 tool contract (D7, unchanged)
  codegraph-mcp/          portfolio fan-out MCP (5 tools over codegraph)
  graph-viewer/           constellation viewer (packages/files/stats)
  reference/java-indexer/ frozen java-graph copy (provenance; read-only)
```

## 3. Pipeline stages

```
0  workspace     load + validate workspace.json; ensure git repo skeleton
1  clones        path exists -> clean-tree guard (refuse or --force-clean);
                 missing -> shallow clone from cloneUrl; record HEAD sha
2  index         fingerprint check (existing indexes); Node worker pool
                 (min(4, CPUs), 4 GB heap per child) codegraph indexAll;
                 WAL folded at close; fingerprint check again
3  export        per repo, streamed in ingest order:
                 files (our scan) | packages | types | methods | fields |
                 extends | implements | imports | calls
4  ingest        single Neo4j writer, 5,000-row UNWIND batches,
                 idempotent MERGE on unique keys, constraints up-front
5  verify        extracted uniques vs graph counts (8 metrics/repo) +
                 200-row reservoir sample per table re-queried;
                 inventory snapshot verified (files match, category sums)
6  artifacts     run dir under logs/: run-manifest.json, summary.json,
                 neo4j/{neo4j,system}.dump.zst, codegraphs/<repo>/*.db.zst
7  publish       copy into runs/<ts>/, latest.json, keepLast prune,
                 git commit (optional push); ingest container STOPPED
8  deploy        runtime sources + VERSION (tool commit) + workspace README
```

**Failure semantics** (java-graph rule): exit 0 only if every repo
indexed, exported, ingested, verified, and published cleanly. Partial
failures still write artifacts + summary with explicit markers, but
nothing is published and the exit code is 1.

**Batch cleanliness**: the ingest container is stopped after the dump —
the workspace ports stay free for `up.py` to restore the snapshot on the
same machine.

## 4. Data model

### 4.1 Portfolio graph (Neo4j) — the D7 contract

```
Nodes: (:Repository), (:Package), (:File {path, sha256, totalLines, …}),
       (:Type {kind}), (:Method), (:Field), (:CallTarget),
       (:StatsSnapshot), (:CategoryStat)
Edges: (Repository)-[:CONTAINS]->(File | Package)
       (Package)-[:CONTAINS]->(Type)
       (File)-[:DEFINES]->(Type)
       (Type)-[:CONTAINS]->(Method | Field)
       (Type)-[:EXTENDS | :IMPLEMENTS]->(Type)
       (File)-[:IMPORTS]->(Package | Type)
       (Method)-[:CALLS {count}]->(CallTarget)
       (StatsSnapshot)-[:COVERS]->(Repository), -[:HAS_STAT]->(CategoryStat)
```

Uniqueness keys: `Repository.name`, `File.id` (`repo:path`),
`Package.fqn`, `Type.fqn`, `Method.id` (`TypeFQN#name[/arity]`),
`Field.fqn` (`TypeFqn.name`), `CallTarget.name`. MERGE on these keys
makes re-runs and cross-repo sharing idempotent: the same FQN declared
in two repos is ONE Type with DEFINES edges from both files and the
union of members.

`:File` carries our own `sha256` (of utf-8-decoded/re-encoded text —
the MFLens `fileHashMatch` drift contract), byte size, and the full
inventory breakdown (§4.3). Files codegraph skips (>1 MB, unsupported)
still get `:File` rows — inventory-grade, no DEFINES.

### 4.2 Identity and mapping rules (codegraph → portfolio)

| codegraph | portfolio | rule |
|---|---|---|
| class/struct/trait, interface/protocol, enum | `:Type {kind}` | java/kotlin/scala: qualifiedName `pkg::Type[::member…]` → portfolio-global `pkg.Type.member`; every other language is file-scoped: `<repo>:<path>::<Name>` |
| method/function | `:Method` | id `ownerTypeFqn#name/arity`; arity parsed from signature's final parenthesized group (top-level commas); owner resolved by walking the qname path — enum-constant bodies (`enum X { C { m() } }`) attach to the enum Type (java-graph semantics) |
| field/property/enum_member | `:Field` | fqn `ownerTypeFqn.name` |
| ownerless members (top-level fns) | under synthetic `:Type {kind:'module'}` per file | fqn `<repo>:<path>` |
| extends/implements edges (resolved) | `:EXTENDS`/`:IMPLEMENTS` | external:true synthesized for unresolved refs resolved best-effort via the file's single-type imports |
| calls edges + unresolved call refs | `:CALLS {count}` | grouped by (caller, callee name as written minus receiver); unresolved refs synthesize `:CallTarget {external:true}` |
| import nodes (statement text) | `:IMPORTS` | java: FQN → Package (wildcard) or Type; other languages: module specifier → Package |
| route nodes, dynamic dispatch | not mapped | stay in per-repo codegraph DBs; served by codegraph-mcp |

### 4.3 Inventory layer (viewer Statistics)

Ported from java-graph's rule table: `kind` (8 values) → `category`
(~23 ordered rules; filename overrides, prefixes, extensions, CI paths)
+ `role` (main/test/generated), and language-aware LOC counting
(code/comment/blank per file). Coverage per repo (tracked / onDisk /
untracked / deep-indexed / inventory-only / binaries) lands on
`:StatsSnapshot` (retention: last 2 per repo) with `:CategoryStat`
rollups. Docs-only repos (wikis) legitimately have zero codegraph files
— they stay inventory-only; that is a warning, not a failure.

## 5. Verification model

Per repo, always: 8 exact count metrics (files/types/methods/fields/
extends/implements/imports/calls) — expected = unique keys seen during
export, actual = graph queries; duplicates collapsed by uniqueness
constraints are accounted for explicitly. Plus a deterministic
reservoir sample (seed 42, 200 rows/table) re-queried edge-by-edge so
verification stays O(sample) at scale (D5). Inventory: file counts vs
graph and category sums vs tracked total. All of it gates the exit code
and lands in `summary.json`.

## 6. Schema pinning (D1/O2)

codegraph is pinned (`@colbymchenry/codegraph` 1.6.2, schema v11). At
run start — and again after stage 2 — every index DB's
`sqlite_master` is hashed (tables as `name(cols)`, other objects as
`type:name`; the exact algorithm is recorded with the measured value in
[verification-bulk-api.md](verification-bulk-api.md)) and compared to
the pinned fingerprint. Mismatch is a hard fail: never export from an
unknown schema.

## 7. Workspace & runtime

Specified in [workspace-reference.md](workspace-reference.md). In
brief: the workspace is always a git repo; runs are append-only
snapshot dirs (keepLast prunes the tree, history keeps rollback); the
runtime (viewer + both MCP servers) is deployed as source copies
pinned by `runtime/VERSION`; `up.py` restores any run into live
services — Neo4j from the dual dumps (graph + system: credentials live
in the system DB), codegraph DBs into codegraph-openable roots, repo
checkouts at the SHAs recorded in run-manifest — and generates the MCP
registration config. Thin `sh`/`ps1` shims only locate a Python; all
logic is Python (cross-platform by construction).

## 8. MCP surfaces

| Server | Backing | Answers |
|---|---|---|
| **MFLens** (`mcp-server/`, 60+4 tools — unchanged contract) | Neo4j portfolio graph | types, members, inheritance, name-level CALLS, source reads + `fileHashMatch` drift, call chains (`resolvedTo` by name+package) |
| **codegraph-portfolio** (`codegraph-mcp/`, 5 tools) | per-repo codegraph DBs | FTS search, exact identity lookup, resolved callers, impact radius, task-context — fanned out over all repos, results repo-tagged, per-repo failures isolated |

They are complements: MFLens = portfolio-graph questions; wrapper =
codegraph-native questions across repos (routes, resolved edges,
per-file context). Portfolio semantics worth knowing: repo attribution
derives from the DEFINES file (packages merge across repos — the
java-graph package-derived attribution was a latent bug our multi-repo
tests caught); a name-level CALLS "bridge" between repos means *same
name defined in both*, which for template-distributed tooling
(`.github/scripts/*` stamped from demo-infra) is distribution, not
integration — cross-repo replicated-path/sha comparison is the
conformance signal.

## 9. Scale posture (D5: 100M LOC)

Streaming everywhere (paged export → UNWIND batches; never the whole
graph in RAM); per-repo worker processes bound blast radius and memory;
verification O(sample) above counts. Known V1 bounds, revisited at the
O3/O4 gates: exporter holds per-repo node maps (fine to ~10M-LOC
repos), Neo4j community single writer, artifact sizes measured per run
in run-manifest (LFS + keepLast manage the store).

## 10. Testing

`pytest` offline (units: manifest/workspace/inventory/mapping/
integrations/publish/deploy/fingerprint — 66) · `NEO4J_TEST_URI`
integration (ingest-twice idempotency, graph shape, sampled verification
— suites self-provision their container) · `MRG_E2E=1` workspace
end-to-end, 6 tests (fresh workspace → pipeline → up.py → viewer +
MFLens + fan-out served from the workspace alone) · MCP suites in
`mcp-server/` (128 offline contract + 8 live multi-repo) and
`codegraph-mcp/` (21). Total 229.
