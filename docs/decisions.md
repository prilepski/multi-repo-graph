# Decision log

Consolidated record of settled decisions. Originals: D1–D7 from the
seeded design (docs/history/design.md), A–G from the workspace
restructure (docs/history/workspace-restructure.md); later entries were
made during implementation when evidence forced a choice. Do not
relitigate without new evidence.

## D1–D7 (design seed, 2026-11-08)

| # | Topic | Decision |
|---|---|---|
| D1 | Export source | codegraph **bulk API** (`iterateNodesByKind`, `getOutgoingEdgesFrom`, `getFiles`); SQLite fallback available but unused; version-pinned + schema fingerprint either way |
| D2 | Live updates | **None** — batch over cloned repos; watchers/incremental deferred |
| D3 | Artifact store | superseded by the workspace model (decision C) — the workspace IS the store |
| D4 | Snapshot contents | Neo4j dumps (graph + system) + per-repo codegraph DBs + run manifest recording where everything lives |
| D5 | Scale target | up to **100M LOC** — streaming, worker pools, sampled verification |
| D6 | codegraph exclusions | accepted as-is (node_modules/target/dist/.gitignore/>1 MB); clean-clone enforcement added on our side |
| D7 | Schema | java-graph portfolio schema retained verbatim; MFLens 60+4 contract unchanged |

## A–G (workspace restructure, 2026-11-08 — approved in review)

| # | Topic | Decision |
|---|---|---|
| A | Tool repo name | keep `multi-repo-graph` (source only; runs never write to it) |
| B | Workspace name | `codegraph-workspace` (instances: e.g. `demo-graph`) |
| C | Workspace is a git repo | **always** — one publish path, free history/rollback; `remote.push` optional |
| D | Runtime delivery | source copies pinned by `runtime/VERSION` (packaging later if needed) |
| E | GitHub `multi-repo-index-store` | superseded; first workspaces created fresh |
| F | Repo clones | inside the workspace (`repos/`, gitignored) |
| G | Bootstrap | thin sh/ps1 shims locate a Python; ALL logic in Python (`up.py`/`down.py`) — one cross-platform implementation |

## Implementation-forced decisions (2026-11-08, with evidence)

| Topic | Decision | Evidence |
|---|---|---|
| Repo attribution in MFLens | derive from the **DEFINES file**, never from Package CONTAINS | packages merge across repos; java-graph's package-derived attribution pointed at repos whose tree lacked the file (FileNotFoundError) — caught by the multi-repo MFLens tests |
| Dual-database dumps | dump `system` **and** `neo4j` | credentials live in the system DB; a graph-only dump restores a database nobody can log into — caught by the workspace e2e |
| Dump mechanics | docker stop → helper container (`--volumes-from` + bind mount) → docker start | `docker exec neo4j stop` kills PID 1 (exit 137) |
| Pipeline container lifecycle | ingest container **stopped after the dump** | workspace ports must be free for `up.py` on the same machine |
| Docs-only repos | zero codegraph files = warning, inventory-only | wikis legitimately have nothing indexable; failing the run was wrong |
| Enum-constant bodies | methods attach to the enum Type (walk-up owner resolution) | codegraph kinds the constant `enum_member` (a Field); java-graph semantics attached such methods to the enum |
| `:File.sha256` variant | hash of utf-8-decoded/re-encoded text | matches MFLens `drift_check` exactly (the contract); identical to raw bytes for valid UTF-8 |
| Snapshot decompression | into `snapshot/`, never `runs/` | up.py once wrote raw dumps next to the .zst files and the next publish committed them |
| Test DB bootstraps | integration suites self-provision (create if missing, docker start if stopped) | pipeline stops the container; tests must not assume it runs |
| Replicated-file drift | read as **installer conformance**, not duplication smell | infra's Makefile stamps `templates/ci-agent-templates` into per-repo GitHub actions (actions are per-repo by design); graph = independent verification that installed artifacts match the template |
