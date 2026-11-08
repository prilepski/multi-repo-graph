# java-graph references — what to copy, file by file

java-graph is the reference implementation this project is seeded from. It is
**read-only** for us: never modified, only copied/adapted.

> ✅ **O5 — resolved 2026-11-08.** java-graph was never relocated as such:
> per user decision its code was **copied and enhanced into this project**
> (`multi-repo-graph`) — the pipeline skeleton frozen at
> `reference/java-indexer/`, `mcp-server/` and `graph-viewer/` copied
> verbatim as future snapshot consumers. The original java-graph stays
> read-only in its ZCode session workspace (not durable — the frozen copy
> here is the working reference):
> `/Users/dmitriprilepski/.zcode/workspace/691cc3bf-5bed-4a05-9bb9-500c9bf3f3ba/`

## Copy map

| java-graph source | What it is | Action for multi-repo-indexer |
|---|---|---|
| `java-indexer/index_to_neo4j.py` | The pipeline: prereq validation, auto pip-install, podman/docker Neo4j container lifecycle (`scip-neo4j-instance`, `neo4j:5-community`, APOC, Bolt wait), tree-sitter-java parse, chunked UNWIND ingest (`INGEST_CHUNK_SIZE = 5000`), idempotent MERGE, uniqueness constraints, verification (extracted vs ingested counts, exit-code gating), `logs/run-*.log` + `summary-*.json` | **Copy the skeleton**: prereq/container/ingest/verify/logging stages; replace the parse phase with codegraph stages (§2 of design.md). Keep the container name/image/constants. |
| `java-indexer/index_to_neo4j.py` (schema section) | Portfolio schema + `:File.sha256` drift contract + `external: true` CallTargets | **Retain verbatim** — target schema (design.md §5) |
| `java-indexer/test_index_to_neo4j.py` | 30 offline unit tests + Neo4j integration test (ingest-twice idempotency, graph-shape asserts) | **Copy the test structure**; fixtures become synthetic codegraph DBs |
| `java-indexer/requirements.txt`, `repos.json` | Deps manifest; repo list format `[{"name","path"}]` | **Extend**: manifest gains `cloneUrl`, `shallow`, `indexStore` (design.md §3) |
| `java-indexer/inventory.py` + `docs/inventory-stats-design.md` | Whole-tree file taxonomy, LOC stats, snapshots | **Defer** (design.md §10) — port after core pipeline |
| `mcp-server/backend.py` | `Neo4jBackend` — the schema's consumer contract: FQN resolution, `FQN#method` id convention, `CALLS {count}` reads, `fileHashMatch` drift semantics | **Reference only** — the exporter must emit a graph this file can query unchanged. Method-id separator `#`, `resolvedTo` resolution by name+package, `:File.sha256` |
| `mcp-server/server.py` | FastMCP 60 MFLens tools + 4 extension tools; `TOOL_INVENTORY`/`EXTENSION_TOOLS` asserts (60/64) | **Reference only** — defines the contract D7 protects; not ported |
| `mcp-server/tests/test_mcp_tools.py` | 111 offline tests (inventory completeness, tiers, mutators, protocol) | **Reference only** — behavior spec for the contract |
| `graph-viewer/` (`viewer_server.py`, `graph-viewer.html`) | Constellation viewer over Neo4j (packages/files modes, stats mode) | **Not ported** — future snapshot consumer (design.md §10) |
| `java-indexer/README.md` | Documents the scip-java → tree-sitter decision history and the CALLS-is-syntactic trade-off | **Read first** — context for why the schema is name-level |

## Lessons encoded into this project's design

1. **Clean clones or bust** — java-graph's compile-based first attempt left
   1,667 `target/**/*.class` files polluting the tree; its reset runbook
   (delete clones, re-clone, re-run) is why design.md §3 enforces clean
   working trees (D6).
2. **Verify every run** — extracted-vs-ingested count comparison with
   exit-code gating catches silent export/ingest drift (design.md §2, §8).
3. **Idempotent by construction** — MERGE-on-unique-key + uniqueness
   constraints make re-runs safe; ingest-twice is an explicit test.
4. **`:File.sha256` is a contract** — downstream MCPs report
   `fileHashMatch`; we compute it ourselves since codegraph doesn't.
5. **Syntactic CALLS is a documented trade-off, not a bug** — codegraph is
   also tree-sitter based; accuracy parity is expected, breadth (languages,
   routes, resolution) is the gain.
