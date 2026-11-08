# AGENTS.md — multi-repo-graph

Batch indexer: codegraph extraction → java-graph Neo4j schema →
timestamp-versioned artifacts in an index store. **Implementing — design in
`docs/design.md`, seeded from the multi-repo-indexer design docs.**

This project is the copy-and-enhance successor of **java-graph** (never
"relocated" as java-graph — it was copied here and is being enhanced under a
new name). The original java-graph stays read-only in its ZCode session
workspace; a pristine code copy lives in `reference/java-indexer/`.

## Read first

1. `docs/design.md` — architecture, schema, scale plan, **decisions D1–D7
   (settled — do not relitigate without the user)**, open items O1–O5.
2. `docs/java-graph-references.md` — what was copied from java-graph and
   where the original lives.
3. `docs/verification-bulk-api.md` — the O1 spike that gates the exporter
   (findings appended at the bottom).

## Ground rules

- **java-graph is read-only** — the original in the session workspace is
  never modified; `reference/java-indexer/` is the frozen copy we enhanced
  from. New pipeline code lives in `indexer/`.
- **The portfolio Neo4j schema (design.md §5) is a contract** — downstream
  MFLens MCP (60+4 tools, 111 tests) queries it unchanged (D7).
- **Batch, no watchers** (D2). Anything live/incremental is future work.
- codegraph version is **pinned**; schema fingerprint verified at run start
  (D1/O2).
- Scale decisions assume **100M LOC** (D5): streaming over loading, worker
  pools over threads, sampling over full re-checks at scale.
- **Neo4j**: dedicated container `mrg-neo4j-instance` (bolt 7688, http 7475,
  `neo4j:5-community` + APOC). Never touch java-graph's running
  `scip-neo4j-instance` (ports 7687/7474).
- Index store: `prilepski/multi-repo-index-store` (private) when
  `indexStore` is in the manifest, else local folder next to repos (D3).
  Runs are timestamp-versioned; retention default 3.
- Exit code 0 only if every repo indexed, exported, ingested, verified, and
  published cleanly; partial runs write artifacts + summary with explicit
  failure markers but fail the run.
