# Design: tool/workspace repo split

Status: implemented — superseded by ../architecture.md + ../workspace-reference.md (kept for provenance). Decisions from review:
A keep `multi-repo-graph`; B `codegraph-workspace`; C workspace always a git
repo; D runtime deployed as source copies (`runtime/VERSION` pins the tool
commit); F `repos/` clones live inside the workspace (gitignored). E (seed
`multi-repo-index-store` as the first workspace) remains open — the first
workspace was created locally at `~/projects/codegraph-workspace`. Bootstrap
scripts are thin `sh`/`ps1` shims that only locate a Python; all logic is
Python (`runtime/scripts/up.py`, `down.py`).

## 0. Goals

1. **Tool repo = source code only.** Pipeline, MCP servers, viewer, tests,
   docs. No repo clones, no runs, no logs, no store — nothing run-specific.
   The indexing process *runs out of* a checkout of this repo but writes
   nothing into it.
2. **Workspace repo = the only thing a user (company) needs** to interact
   with indexed code. It owns the repo list, every indexed snapshot (and
   future incremental updates), and a self-contained runtime (viewer + both
   MCP servers) deployed there by the indexer. Users never touch the tool
   repo.
3. Complete isolation: indexer code ↔ index results.

Non-goals (still true): batch-first (D2), no watchers; scale target 100M LOC
(D5); MFLens 60+4 contract unchanged (D7).

## 1. Naming

| Thing | Name | Notes |
|---|---|---|
| Tool repo | **`multi-repo-graph`** (keep) or `multi-repo-indexer` | already the implementation repo's name; renaming costs git remote + muscle memory. Decision A below. |
| Workspace repo | **`codegraph-workspace`** — primary suggestion | it is more than an index: manifest + versioned snapshots + served tools + bootstrap. "workspace" says *the place you work against the index*; "index"/"store" undersells the runtime. |
| Alternatives | `graph-index`, `index-hub`, `portfolio-graph`, `codegraph-instance` | pick per taste; instances would be `acme/codegraph-workspace`. |

Rationale: the repo answers "where is the code knowledge and how do I use
it" — a workspace, not a store. The current GitHub `multi-repo-index-store`
becomes either the first workspace instance or is superseded (Decision E).

## 2. Responsibility split

### Tool repo (`multi-repo-graph`) — committed

```
indexer/            pipeline stages 0–8 (stage 8 = runtime deploy, new)
mcp-server/         MFLens MCP source
codegraph-mcp/      portfolio wrapper MCP source
graph-viewer/       viewer source
docs/ tests/ reference/ ...
manifest.example.json   FORMAT only — real manifests live in workspaces
```

Removed after restructure: `repos/`, `logs/`, `index-store/`, `manifest.json`
(user data that leaked into the tool repo during development).

### Workspace repo (`<org>/codegraph-workspace`) — per company

```
workspace.json          repos list + settings (was manifest.json + indexStore)
runs/<UTC-ts>/          snapshots, exactly today's layout (design.md §4)
  run-manifest.json     …now also records toolCommit (see §5)
  summary.json
  neo4j/neo4j.dump.zst
  codegraphs/<repo>/codegraph.db.zst
latest.json             {"run": <ts>}
runtime/                DEPLOYED BY THE INDEXER each run (idempotent):
  VERSION               tool repo commit that produced this runtime
  mcp-server/  codegraph-mcp/  graph-viewer/     (source copies)
  scripts/up.sh|down.sh restore + start everything (§6)
  config/mcp.json       ZCode/Claude MCP registration snippet
repos/                  (gitignored) repo clones — cache + input
snapshot/               (gitignored) materialized current state for services
logs/                   (gitignored) indexer run logs
.gitattributes          *.dump.zst / *.db.zst → LFS (as today)
```

Committed: `workspace.json`, `runs/`, `latest.json`, `runtime/` (small —
source only; venvs/node_modules ignored). Everything machine-local is
ignored. A fresh `git clone` of the workspace gives any employee the full
history of snapshots + a working runtime.

## 3. `workspace.json` (supersedes manifest.json + indexStore block)

```json
{
  "repos": [
    {"name": "thingsboard", "cloneUrl": "…", "path": "repos/thingsboard",
     "shallow": true}
  ],
  "settings": {
    "keepLast": 3,
    "concurrency": null,
    "neo4j": {"boltPort": 7688, "httpPort": 7475}
  },
  "remote": {"push": true}     // optional: push runs to the workspace origin
}
```

Rules from design.md §3 carry over unchanged (names unique, shallow default,
clean-tree guard). `path` is now **workspace-relative** — clones land inside
the workspace regardless of where the tool checkout is.

## 4. Process flow (indexer runs out of the tool repo)

```
~/tools/multi-repo-graph (checkout)          ~/acme/codegraph-workspace
┌────────────────────────────┐              ┌───────────────────────────┐
│ python -m indexer.pipeline │──targets────►│ workspace.json (read)     │
│  --workspace ../acme/cgw   │              │ repos/ (clones, ignored)  │
│                            │──writes─────►│ logs/ (ignored)           │
│ stages 0–7 (unchanged)     │              │ runs/<ts>/ (committed)    │
│ stage 8 deploy (new)       │──deploys────►│ runtime/ (committed)      │
│ publish = workspace commit │──commits────►│ git commit [+ push]       │
└────────────────────────────┘              └───────────────────────────┘
```

- `--workspace` is required (path or git URL; URL → clone/pull first).
- The workspace is itself the index store: publish = `git add runs/ runtime/
  latest.json` + commit `index-run: <ts> (…)` (+ push when `remote.push`),
  then keepLast prune — this generalizes today's local/GitHub publish into
  one code path (the workspace dir is always a git repo; `git init` if the
  user's path isn't one yet).
- Codegraph SDK (indexer/nodejs) stays a tool-repo dependency — never
  deployed; only its *outputs* (dbs) ship.

## 5. Stage 8 — runtime deploy (new, idempotent)

Each run rsyncs `mcp-server/`, `codegraph-mcp/`, `graph-viewer/` sources +
generates `scripts/` and configs into `runtime/`, stamps `runtime/VERSION`
with the tool commit, and commits only if something changed. Consumers get
pinned, reproducible tooling per snapshot; the indexer upgrades propagate
with the next run.

## 6. Consumption — `runtime/scripts/up.sh` (the §10 runner, in scope now)

From a plain clone of the workspace, one command:

1. `neo4j dump → container`: `zstd -d` latest run's dump → load into
   `codegraph-neo4j` container (ports from workspace.json).
2. `codegraphs`: `zstd -d` each `codegraphs/<repo>/codegraph.db.zst` into
   `snapshot/codegraphs/<repo>/.codegraph/codegraph.db` (codegraph-openable
   project roots) → `codegraph-mcp/repos.json` points here.
3. `sources for MFLens`: clone/checkout each repo at the **commitSha recorded
   in run-manifest.json** into `repos/` (MFLens source reads + fileHashMatch
   drift semantics keep working against the exact indexed state).
4. Start MFLens MCP + codegraph-mcp + viewer; print the MCP registration
   snippet (`runtime/config/mcp.json`).

`down.sh` stops/removes containers and services. Nothing in this flow needs
the tool repo — that is the acceptance test for goal 2.

## 7. Code-change inventory (small, mostly plumbing)

| Module | Change |
|---|---|
| `manifest.py` | load `workspace.json`; workspace-relative `path` resolution; new `Workspace` dataclass (root, manifest, settings) |
| `pipeline.py` | `--workspace` required; log-dir/artifacts rooted in workspace; publish → workspace-commit path |
| `publish.py` | `publish_workspace()` = today's local copy + git commit/push + prune (GitHub-dry-run logic folds into "no remote / push:false") |
| `deploy.py` (new) | stage 8 runtime deploy + VERSION stamp |
| `runtime/scripts/*` (new, templates) | up.sh / down.sh / mcp.json generation |
| `codegraph-mcp/repos.json`, `mcp-server` repos config | generated into `runtime/config/` by deploy, workspace-relative |
| tests | manifest/workspace tests extend; publish tests against a temp git workspace; one e2e: fresh dir → run → up.sh → MFLens+viewer queries |

Stages 0–7 internals (fingerprint guard, exporter, ingest, verify, dump)
are untouched.

## 8. Migration from current state (one-time)

1. Tool repo: `git mv` nothing needed structurally; delete `repos/`,
   `logs/`, `index-store/`, `manifest.json` from the working tree (they are
   gitignored already except manifest.json) — keep `manifest.example.json`.
2. Create the first workspace (e.g. `~/workspaces/codegraph-workspace`,
   optionally push origin; the existing GitHub `multi-repo-index-store`
   either becomes it or is retired after its runs are re-published).
3. Move current `repos/`, last run's artifacts, write `workspace.json`.
4. Re-run the pipeline against the workspace → produces run #1 + runtime in
   the new layout; verify with `up.sh` + MFLens/viewer smoke.

## 9. Decisions (resolved in review)

| # | Question | Decision |
|---|---|---|
| A | Tool repo name | **keep `multi-repo-graph`** |
| B | Workspace repo name | **`codegraph-workspace`** |
| C | Workspace always a git repo? | **yes** — one publish path, free history/rollback |
| D | Runtime as source copies vs packaged artifacts | **source copies**, `runtime/VERSION` pins tool commit |
| E | Fate of GitHub `multi-repo-index-store` | open — first workspace created locally; store can be seeded later |
| F | `repos/` clones location | **inside the workspace** (gitignored) |
| G | Bootstrap scripts | **thin sh/ps1 shims locate a Python; all logic in Python** (up.py/down.py) — one cross-platform implementation |

## 10. Forward compatibility (incremental updates, D2 future)

Runs stay append-only snapshots. When incremental codegraph updates arrive
(later), they land as either (a) new run dirs (full snapshots — simplest,
current semantics of keepLast), or (b) `runs/<ts>/deltas/` consumed by
`up.sh` before decompress — the layout has room; no decision needed now.
