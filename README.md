# multi-repo-graph

Batch indexer that turns a manifest-listed set of repositories into
[codegraph](https://github.com/colbymchenry/codegraph) indexes (34
languages, tree-sitter based), a unified **Neo4j portfolio code graph**
(java-graph schema), and **timestamp-versioned snapshots** published into
a per-company **codegraph-workspace** — the single repo everyone else
consumes (viewer + two MCP servers included, no tool checkout needed).

**This repo is the TOOL: source code only — runs never write here.**
Specifications: [docs/architecture.md](docs/architecture.md) ·
workspace guide: [docs/workspace-reference.md](docs/workspace-reference.md) ·
decisions: [docs/decisions.md](docs/decisions.md).

## What you get

- **Portfolio graph** (Neo4j): Repositories → Packages/Files → Types →
  Methods/Fields, inheritance, imports, name-level CALLS with counts —
  one graph across all repos, same-FQN types merge.
- **Per-repo codegraph DBs**: routes, resolved callers, impact radius,
  task context — per repo, kept next to the graph snapshot.
- **Viewer** ("Multi Repo Code Graph Viewer", WebGL): integration
  constellation with evidence classes, unified Nodes/Edges filter bar,
  repo multi-select, animation toggle + Statistics (taxonomy, LOC,
  coverage).
- **Cross-repo integrations**: `INTEGRATES` edges derived from name
  references, evidence-classified (submodule/code/config/comment/docs)
  with per-class breakdowns and sample evidence lines.
- **Two MCP servers**: MFLens (60+4-tool contract over the portfolio
  graph, incl. `fileHashMatch` drift) and codegraph-portfolio (5 fan-out
  tools over the codegraph DBs).
- **Verified runs**: exact counts + sampled edge checks gate every run;
  artifacts (dual Neo4j dumps + codegraph DBs, zstd, sha256'd) are
  committed into the workspace with LFS.

## Setup (tool, once per machine)

```bash
git clone <this repo> && cd multi-repo-graph
cd indexer && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
(cd nodejs && npm install)                 # pinned @colbymchenry/codegraph
# needs: docker, node >= 22.5, git, zstd; git-lfs where you push from
```

## Configuration — the workspace

Everything run-specific lives in `<workspace>/workspace.json` (full
reference: [docs/workspace-reference.md](docs/workspace-reference.md)):

```json
{
  "repos": [
    {"name": "my-service", "cloneUrl": "https://github.com/org/my-service.git",
     "path": "repos/my-service", "shallow": true}
  ],
  "settings": {
    "keepLast": 3,
    "neo4j": {"boltPort": 7688, "httpPort": 7475,
              "container": "mrg-neo4j-instance",
              "password": "graphpassword123"},
    "viewerPort": 7600
  },
  "remote": {"push": true}
}
```

- `repos[]` — non-empty, unique names; existing `path` must be a **clean**
  git tree (build residue fails the run unless `--force-clean`); missing
  path + `cloneUrl` → shallow clone. Docs-only repos are fine
  (inventory-only in the graph).
- Ports + `container` must be unique **per workspace** on a shared host.
- `remote.push: true` pushes each run commit to the workspace origin
  (install git-lfs first; artifacts are LFS-tracked).

## Usage

**Index (operator):**

```bash
indexer/.venv/bin/python -m indexer.pipeline \
  --workspace /path/to/codegraph-workspace        # or a git URL
# options: --force-clean  --publish-dry-run  --concurrency N
```

Stages 0–8: validate → clones → codegraph index (worker pool) → export
→ Neo4j ingest → verify → artifacts → publish+deploy. Exit 0 only if
every repo indexed, ingested, verified, and published cleanly.

**Consume (everyone else — only the workspace clone):**

```bash
runtime/scripts/up.sh          # Windows: up.ps1  (just needs Python 3.11+)
#   → Neo4j restored, codegraph DBs + SHA-pinned repo checkouts, viewer up,
#     runtime/config/mcp.json = ready-to-paste MCP registration
runtime/scripts/down.sh [--purge]
```

- Viewer: `http://localhost:<viewerPort>` · Neo4j browser:
  `http://localhost:<httpPort>` (password from settings)
- MCPs: paste `runtime/config/mcp.json` into your ZCode/Claude config
- Pin a snapshot: `up.sh --run 2026-11-08T22-13-41Z`; read codegraph from
  working trees: `up.sh --mode live`

## Tests

```bash
indexer/.venv/bin/python -m pytest                 # offline (66)
NEO4J_TEST_URI=bolt://localhost:7688 \
  indexer/.venv/bin/python -m pytest indexer/tests/test_ingest_integration.py
MRG_E2E=1 indexer/.venv/bin/python -m pytest indexer/tests/test_workspace_e2e.py
(cd mcp-server && .venv/bin/python -m pytest tests/)     # + NEO4J_TEST_URI
(cd codegraph-mcp && .venv/bin/python -m pytest tests/)
```

## Layout

```
indexer/        pipeline stages 0–8 (Python + Node worker pool/exporter)
mcp-server/     MFLens MCP (60+4 tools, unchanged java-graph contract)
codegraph-mcp/  portfolio fan-out MCP (5 tools)
graph-viewer/   constellation viewer + statistics
docs/           spec, workspace reference, decisions, spike record, history
reference/      frozen java-graph copy (provenance, read-only)
```

## License

MIT © [Dmitri Prilepski](mailto:dmitri@vidukate.ai) — [LICENSE](LICENSE).
