# AGENTS.md — multi-repo-graph

Batch indexer: codegraph extraction → java-graph Neo4j schema →
timestamp-versioned snapshots in a per-company **codegraph-workspace**.

**This repo is the TOOL: source code only — never write run data here.**
All runs target a workspace (`--workspace`): repos/, runs/, runtime/, logs/
live there (docs/workspace-restructure.md, approved + implemented).

## Read first

1. `docs/architecture.md` — the specification (components, stages, data
   model, verification, MCP surfaces).
2. `docs/workspace-reference.md` — the workspace: workspace.json, runtime,
   up/down lifecycle.
3. `docs/decisions.md` — settled decision log (D1–D7, A–G, and the
   implementation-forced ones) — do not relitigate without new evidence.
4. `docs/verification-bulk-api.md` — O1 spike: codegraph bulk API + the
   pinned schema fingerprint.

## Ground rules

- **java-graph is read-only** — original in its ZCode session workspace;
  frozen copy at `reference/java-indexer/`.
- **The portfolio Neo4j schema (design.md §5) is a contract** — MFLens MCP
  (60+4 tools) queries it unchanged (D7).
- **Batch, no watchers** (D2). **Scale target 100M LOC** (D5): streaming
  over loading, worker pools over threads, sampling over full re-checks.
- codegraph version **pinned**; schema fingerprint verified at run start
  (D1/O2) — mismatch is a hard fail, never a silent wrong export.
- **Workspace is a git repo, always** (decision C): publish = commit
  (`runs/`, `runtime/`, `latest.json`), keepLast prunes, remote push
  optional. Pruned runs stay reachable in history.
- The pipeline **stops its ingest container after dumping** (batch
  cleanliness; `up.py` needs the workspace ports on the same machine).
- Runtime bootstrap: **thin sh/ps1 shims locate a Python; all logic is
  Python** (`runtime_templates/up.py` / `down.py`) (decision G).
- Exit code 0 only if every repo indexed, exported, ingested, verified,
  and published cleanly.
- **License: MIT, Dmitri Prilepski <dmitri@vidukate.ai>** — keep the SPDX
  headers on first-party sources; new files get one.

## Tests

offline (`pytest`), Neo4j integration (`NEO4J_TEST_URI`), workspace e2e
(`MRG_E2E=1` — needs docker+node). MCP suites: `mcp-server/.venv`,
`codegraph-mcp/.venv` (integration tests also need `NEO4J_TEST_URI`).
