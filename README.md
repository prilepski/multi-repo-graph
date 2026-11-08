# multi-repo-graph

Batch pipeline that indexes a manifest-listed set of cloned repositories with
[codegraph](https://github.com/colbymchenry/codegraph) (34 languages,
tree-sitter based), exports the results into a unified **Neo4j portfolio
graph** (java-graph schema), and publishes **timestamp-versioned artifacts**
— per-repo codegraph DBs + a Neo4j dump + a run manifest — to a private
index-store repo or a local folder.

Copy-and-enhance successor of **java-graph** (pipeline skeleton, portfolio
schema, and MCP/viewer consumers originate there — see
`docs/java-graph-references.md`).

## Layout

```
AGENTS.md                       ground rules for agent sessions
manifest.example.json           input manifest format (repos + optional indexStore)
docs/
  design.md                     the design (architecture, schema, scale, D1–D7)
  java-graph-references.md      what was copied from java-graph, file by file
  verification-bulk-api.md      O1 spike plan + findings (gates the exporter)
indexer/                        the batch pipeline (stages 0–7)
reference/java-indexer/         frozen copy of java-graph's pipeline (enhance base)
mcp-server/                     MFLens MCP (60+4 tools) — future snapshot consumer
graph-viewer/                   constellation viewer — future snapshot consumer
```

## Pipeline

`manifest.json` lists repos (+ optional `indexStore`). Stages: validate →
ensure clones (shallow default) → codegraph index per repo (Node worker
pool) → export to the java-graph Neo4j schema (bulk API preferred, SQLite
fallback; streaming, never full-graph-in-RAM) → batched idempotent ingest →
verify counts per repo → artifacts (`neo4j.dump.zst`, per-repo
`codegraph.db.zst`, `run-manifest.json`, `summary.json`) → publish to
`runs/<UTC-timestamp>/` in the store (GitHub commit) or a local folder.

No live updates (D2); scale target 100M LOC (D5). See `docs/design.md` for
decisions D1–D7 and open items O1–O5.

## Related

| What | Where |
|---|---|
| Design seed (docs-only predecessor) | `~/projects/multi-repo-indexer` |
| java-graph original (read-only) | ZCode session workspace (see `docs/java-graph-references.md`) |
| Index store (private, artifacts) | [prilepski/multi-repo-index-store](https://github.com/prilepski/multi-repo-index-store) |
| Extraction engine | [codegraph](https://github.com/colbymchenry/codegraph) — npm `@colbymchenry/codegraph`, Node 22.5+ |
