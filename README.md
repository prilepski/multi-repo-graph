# multi-repo-graph

Batch indexer that turns a manifest-listed set of cloned repositories into
[codegraph](https://github.com/colbymchenry/codegraph) indexes (34 languages,
tree-sitter based), a unified **Neo4j portfolio graph** (java-graph schema),
and **timestamp-versioned snapshots** published into a
**codegraph-workspace** — the single per-company repo users interact with.

**This repo is the TOOL: source code only.** Runs write nothing here; every
user (company) sets up a `codegraph-workspace` that owns the repo list, all
snapshots, and a deployed runtime (viewer + MCP servers). Users never need
this repo — see `docs/workspace-restructure.md`.

## Layout

```
indexer/                        the batch pipeline (stages 0–8)
  pipeline.py                   orchestrator + CLI (--workspace …)
  manifest.py / workspace.py    workspace.json + repo validation/clones
  exporter.py + nodejs/         codegraph bulk API -> portfolio rows
  ingest.py / verify.py         batched UNWIND ingest; count+sample checks
  artifacts.py / publish.py     run dir, zstd artifacts, workspace commit
  deploy.py + runtime_templates stage 8: runtime deploy; up.py/down.py
  tests/                        offline units + gated e2e (MRG_E2E=1)
mcp-server/                     MFLens MCP (60+4 tools; 128 offline + 8 multi-repo tests)
codegraph-mcp/                  portfolio fan-out wrapper MCP (5 tools; 21 tests)
graph-viewer/                   constellation viewer
reference/java-indexer/         frozen java-graph copy (enhance base)
docs/                           design.md, workspace-restructure.md, spike findings
workspace.example.json          workspace format (repos + settings + remote)
```

## Usage

```bash
# tool setup (once)
cd indexer && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
(cd nodejs && npm install)

# create a workspace (per company) — see workspace.example.json
mkdir my-codegraph-workspace  # …add workspace.json…

# run the pipeline: index -> graph -> snapshot -> runtime, all INTO the workspace
indexer/.venv/bin/python -m indexer.pipeline --workspace /path/to/my-codegraph-workspace

# consume (any user with a clone of the workspace — no tool repo needed)
runtime/scripts/up.sh          # or up.ps1 on Windows
#   -> neo4j (restored dump), codegraph DBs, viewer on :7600, MCP registration
runtime/scripts/down.sh
```

Exit code 0 only if every repo indexed, exported, ingested, verified, and
published cleanly (docs/design.md §2). Scale target 100M LOC (D5); batch
only, no watchers (D2); MFLens 60+4 contract unchanged (D7).

## Tests

```bash
indexer/.venv/bin/python -m pytest                      # offline
NEO4J_TEST_URI=bolt://localhost:7688 indexer/.venv/bin/python -m pytest \
  indexer/tests/test_ingest_integration.py              # + neo4j integration
MRG_E2E=1 indexer/.venv/bin/python -m pytest indexer/tests/test_workspace_e2e.py
```

## License

MIT © [Dmitri Prilepski](mailto:dmitri@vidukate.ai) — see [LICENSE](LICENSE).
