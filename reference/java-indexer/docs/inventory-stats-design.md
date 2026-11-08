# Design: File Inventory & Code Statistics in Neo4j

Status: **design only — not implemented.** Rev 2 (review feedback applied;
decisions 1–4 from rev 1 are now settled, viewer statistics mode added).

Answers three questions with data stored in the graph:

1. Total files in the source vs files actually processed
2. All files categorized by type (taxonomy below)
3. Lines-of-code statistics grouped by category

…and exposes the results through three surfaces: **Neo4j (facts + snapshot
nodes)**, **MCP tools**, and a **Statistics mode in the HTML viewer**.

---

## 0. Reset runbook (decided: clean slate)

The working trees are polluted by the abandoned compile-based attempt
(1,667 `target/**/*.class` files, stray `requirements.txt` in the clone).
Cleanest fix — redo from clones:

1. `rm -rf repos/thingsboard repos/dolphinscheduler`
2. Re-clone: `git clone https://github.com/thingsboard/thingsboard.git`
   and `git clone --depth 1 https://github.com/apache/dolphinscheduler.git`
   (shallow is sufficient — `git ls-files` works in shallow clones)
3. Re-run the pipeline (parse + ingest + verify)

After reset: working tree == tracked files, `untrackedFiles` coverage metric
reads **0** and stays in the design as a pollution/drift guard, not a
constant. Numbers below marked *(scan of polluted tree)* are pre-reset
baselines; post-reset totals shift down by the residue (~1.7k files, all
`binary_blob`/none-categories — category ratios of *source* files barely
move).

## 1. Gap analysis — what exists today

| Fact | Value |
|---|---|
| Git-tracked files (canonical source) | TB 13,933 + DS ~4.8k ≈ **18.7k** |
| Files on disk *(scan of polluted tree)* | 20,494 (1,669 untracked residue) |
| Files in Neo4j today (`:File` nodes) | 9,516 — **Java only** |
| `:File` properties today | `id`, `path`, `sha256` — **no category, no LOC** |
| LOC reality (TB Java sample) | ~786k lines ≈ 82% code / 4% comment / 14% blank |
| Invisible to graph today | TB front-end: ~389k TS + 108k HTML + 52k SCSS |

## 2. File taxonomy

### `kind` — broad class (8 values)

`source` · `build` · `config` · `docs` · `data` · `assets` · `artifact` · `other`

### `category` — specific (rule table, ordered — first match wins)

| Category | kind | Rules (extension / filename / path) |
|---|---|---|
| `java_backend` | source | `*.java` outside `src/test` |
| `java_test` | source | `*.java` under `src/test/` (**decided: test/main folded into category**) |
| `frontend_logic` | source | `*.ts`, `*.tsx`, `*.js` outside `node_modules` |
| `frontend_style` | source | `*.scss`, `*.css`, `*.less` |
| `frontend_markup` | source | `*.html`, `*.vue` |
| `shell_script` | source | `*.sh`, `*.bat`; extensionless `mvnw`, `gradlew` |
| `python_script` | source | `*.py` |
| `sql` | source | `*.sql` |
| `protocol_schema` | source | `*.proto`, `*.thrift`, `*.avsc` |
| `build_script` | build | `pom.xml`, `build.gradle*`, `settings.gradle*`, `package.json`, `tsconfig.json`, `angular.json`, `vite.config.*`, `webpack.config.*`, `Makefile` |
| `ci_cd` | build | `.github/workflows/*`, `Jenkinsfile`, `.gitlab-ci.yml`, `.circleci/*` |
| `container` | build | `Dockerfile*`, `docker-compose*.yml`, `.dockerignore` |
| `iac` | build | `*.tf`, `*.tfvars` |
| `runtime_config` | config | `*.yml`, `*.yaml`, `*.properties`, `*.conf`, `*.env`, `*.cfg` |
| `xml_config` | config | `*.xml` (not matched above) |
| `documentation` | docs | `*.md`, `*.rst`, `*.txt`, `LICENSE*`, `NOTICE*` |
| `data_fixture` | data | `*.json`, `*.csv`, `*.factories` |
| `image` | assets | `*.png`, `*.jpg`, `*.svg`, `*.gif`, `*.ico` |
| `font` | assets | `*.ttf`, `*.woff*`, `*.eot` |
| `binary_blob` | artifact | `*.class`, `*.jar`, `*.raw`, `*.bin`, `*.pem`, `*.key`, `*.keystore` |
| `other` | other | anything unmatched (reported; rule table reviewed if >1% of files) |

A separate `role` property (`main` | `test` | `generated`) is still stored on
Java files for graph queries, but **category is the statistics dimension**
(decided).

## 3. LOC taxonomy

Per file: `totalLines`, `codeLines`, `commentLines`, `blankLines`
(sum = total) with language-aware comment rules:

- `//` + `/* */` (java, ts, tsx, js, scss, css) — javadoc `*` continuations count as comments
- `<!-- -->` (html, xml); `#` (sh, py, yml, yaml, properties, env, conf); `--` (sql); `REM` (bat)
- Binary/asset categories and any file with a NUL byte in the first 8 KB:
  **no line counts**, `bytes` only

## 4. Graph model

### Facts: `:File` covers the whole tracked tree

```
(:Repository)-[:CONTAINS]->(:File {
    id, path, sha256,               // existing key + hash
    extension, kind, category, role,
    bytes,
    totalLines, codeLines, commentLines, blankLines   // null for binaries
})
```

- Java files keep `DEFINES`/`IMPORTS` edges; "deep-processed" =
  `exists((f)-[:DEFINES]->())`.
- Non-Java files are categorized leaf nodes. Est. +9k nodes → ~18.7k total.

### Aggregates: snapshots — **retention: last 2 runs** (decided)

```
(:StatsSnapshot {at, runId, durationMs})
    [:COVERS {filesTracked, filesOnDisk, untrackedFiles,
              filesIndexedDeep, filesInventoryOnly, binaries}]->(:Repository)
    [:HAS_STAT]->(:CategoryStat {kind, category, files, bytes,
                                 totalLines, codeLines, commentLines, blankLines})
```

On each run: create the new snapshot, then delete all but the 2 newest
`(:StatsSnapshot)` nodes (cascade to their `CategoryStat`/`COVERS`).

## 5. Pipeline changes

New `inventory` phase in `index_to_neo4j.py` per repository:

1. `git ls-files` (tracked) + `find` (disk) → tracked/untracked split
2. `classify(path)` — pure rule-table function (unit-tested)
3. Line counting per comment-rule family; NUL-sniff binaries
4. Batch ingest `:File` nodes (chunked UNWIND, idempotent MERGE)
5. Materialize snapshot; prune to last 2
6. Verification additions: `filesTracked == count(File)` per repo; category
   sums == totals; LOC spot-check on 20 random files
7. Summary JSON/log gains the `inventory` + coverage block

Cost estimate: a few seconds on top of the current ~15 s run.

## 6. MCP tools (expanded per review — was only a bullet in rev 1)

The 60-tool MFLens inventory stays **intact and unchanged** (tests and the
tier table reference it). Inventory statistics ship as a small,
clearly-separated **extension set**, prefixed like the rest:

| Tool | Arguments | Returns | Backing |
|---|---|---|---|
| `get_portfolio_file_statistics` | `repository?: str` | coverage block: filesTracked, filesOnDisk, untrackedFiles, filesIndexedDeep, filesInventoryOnly, binaries + per-`kind` file counts | snapshot `COVERS` (falls back to live aggregate) |
| `get_file_categories` | `repository?: str`, `pathPrefix?: str`, `kind?: str` | rows `{category, files, bytes}` ordered by files — scoped to a directory subtree when `pathPrefix` given | live Cypher over `:File` facts |
| `get_loc_statistics` | `repository?: str`, `category?: str`, `pathPrefix?: str` | rows `{category, files, totalLines, codeLines, commentLines, blankLines}` + totals row | live Cypher over facts |
| `get_file_details` | `path: str`, `repository: str` | one file's full record (category, role, bytes, LOC split, hashMatch-style drift vs `sha256`) | facts |

Design notes:

- **Scoping is first-class** (`repository` / `pathPrefix` / `category`) so
  agents can ask "LOC for `dolphinscheduler-ui/`" — mirrors the viewer's
  tree-scoped statistics (§7).
- Tier assignment: these are **Tier A** for counts (deterministic, derived
  from verified facts); LOC splits are Tier A for total lines, **Tier B for
  comment/blank** (heuristic comment rules).
- Implementation impact: `TOOL_INVENTORY` becomes 64; the count test changes
  from 60 → 60 + 4 extension tools listed separately;
  `scripts/call_all_tools.py` gains 4 calls; `.zcode` config unchanged.
- The `cobol_ibm_to_iso`-style refusal pattern is not needed — all four are
  real queries on real data.

## 7. Viewer: Statistics mode (new — per review)

### Mode switch

Header gains a top-level two-way switch: **Graph** (today's Packages/Files
navigator, unchanged) | **Statistics**. Inside Graph, the existing
Packages/Files toggle stays. Statistics replaces the canvas area, keeps the
left tree panel (different tree, below) and stays constellation-themed.

### Left tree in Statistics mode: directory hierarchy

- `Repository → directory → subdirectory → file`, built lazily from `:File`
  paths (same `/api/children?mode=…` pattern; a `mode=stats` returns
  directory nodes with rollup counts instead of packages/types)
- Click = scope selection; the whole statistics pane re-queries for the
  selected prefix; breadcrumb + "↰ Reset scope" control mirrors drill-up

### Main area layout (scoped to current selection)

```
┌ KPI row ────────────────────────────────────────────────────────┐
│ tracked files · deep-indexed · inventory-only · binaries ·      │
│ coverage % (deep/tracked, progress bar)                          │
├──────────────────────────┬──────────────────────────────────────┤
│ Donut: files by category │ Stacked bars: LOC by category         │
│ (legend = category)      │ (code/comment/blank segments, toggle  │
│                          │  files ⇄ LOC)                         │
├──────────────────────────┴──────────────────────────────────────┤
│ Table: category · files · bytes · total/code/comment/blank LOC   │
│ (sortable, footer totals row)                                    │
└──────────────────────────────────────────────────────────────────┘
```

- **Charts**: hand-rolled SVG (donut = arc paths; stacked horizontal bars) —
  keeps the viewer's zero-dependency rule; same palette as the graph legend,
  category → color mapping shared with the constellation theme
- **Tables**: plain HTML, sortable columns, totals footer
- Empty categories omitted; "other" row always last, visually muted

### New viewer API endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/stats/overview?repo=&prefix=` | KPI row + coverage |
| `GET /api/stats/categories?repo=&prefix=&kind=` | donut + table rows |
| `GET /api/stats/loc?repo=&prefix=` | stacked bars + LOC table |
| `GET /api/stats/tree/children?eid=` | directory-tree expansion (mode=stats) |

All read **live facts** (consistent with re-runs); the snapshot nodes serve
run history (future: a small "vs previous run" delta chip — out of scope
now).

## 8. Indicative results (pre-reset scan; post-reset shifts by residue only)

Files by category *(scan of polluted tree)*:

| Category | TB | DS |
|---|---|---|
| java_backend+test | 6,936 | 2,580 |
| frontend_logic | 2,327 | 445 |
| frontend_markup | 1,290 | — |
| frontend_style | 748 | 39 |
| xml (config+build) | 542 | 237 |
| documentation | 388 | 358 |
| image/font/binary | ~120 | ~470 |
| runtime_config | ~120 | ~230 |

LOC by category: java 786k/259k · frontend_logic 389k/68k · html 108k/— ·
xml 77k/21k · scss 52k/1.8k · sql 3.3k/13.8k · config ~10k/24k.

## 9. Decisions log

| # | Question | Decision |
|---|---|---|
| 1 | Inventory basis + residue | **Reset: delete clones, re-clone, re-run**; git-tracked basis; untracked metric stays as guard |
| 2 | test/main | **Folded into category** (java_test); `role` kept as query attribute |
| 3 | Snapshot retention | **Last 2 runs** |
| 4 | MCP | **4 extension tools** (§6); MFLens 60 set untouched |
| 5 | Visualization | **Statistics mode in the viewer** (§7): directory tree scoping, KPI row, donut + stacked bars + sortable table |
