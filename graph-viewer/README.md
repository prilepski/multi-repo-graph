# Graph Viewer

Interactive viewer for the Java code graph stored in Neo4j. The graph itself
is produced by the [java-indexer](../java-indexer) pipeline (Tree-sitter parse
of Java repos → Neo4j); this project only reads it.

## Features

- **Constellation canvas** — glowing orbs, starfield with parallax depth, pan/zoom
- **Two browsing modes** — `Packages` (Repository → Package → Type → Members)
  and `Files` (Repository → File → Type → Members, with cross-file class links)
- **Collapsible tree** on the left, synced with canvas navigation
- **Click** a node → details popup (properties, relationship counts)
- **Double-click** → drill into a node's neighborhood; **↑ Level up** walks back
- **Search** files by path and classes by name; Enter/click jumps, drills,
  opens details, and highlights the node in the tree
- **Statistics mode** (`Graph | Statistics` switch): KPI row (tracked /
  deep-indexed / inventory-only / binaries / LOC), donut of files by
  category, stacked LOC bars (code/comment/blank), sortable category table —
  scoped live by clicking the directory tree (`Repository → folder → …`),
  with reset-scope

## Run

Requires a reachable Neo4j (default `bolt://localhost:7687`, password
`graphpassword123` — edit `viewer_server.py` constants to change) and the
graph populated by the java-indexer pipeline.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python viewer_server.py        # serves at http://127.0.0.1:7600
```

## Layout

| File | Purpose |
|---|---|
| `viewer_server.py` | stdlib HTTP server + Neo4j JSON API (`/api/root`, `/api/children`, `/api/neighborhood`, `/api/details`, `/api/ancestors`, `/api/search`) and static page serving |
| `graph-viewer.html` | the viewer UI (zero external dependencies) |

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/root?mode=packages\|files` | topmost nodes with child counts |
| `GET /api/children?eid=&mode=` | hierarchy children (tree expansion, capped at 500) |
| `GET /api/neighborhood?eid=&mode=&depth=1\|2` | node + 1-hop neighbors; depth 2 adds cross-file EXTENDS/IMPLEMENTS links |
| `GET /api/ancestors?eid=&mode=` | hierarchy chain up to the repository |
| `GET /api/details?eid=` | node properties + relationship counts |
| `GET /api/search?q=` | classes by name / files by path (20 each) |
| `GET /api/stats/overview?repo=&prefix=` | KPIs + per-repo coverage (latest snapshot + live) |
| `GET /api/stats/categories?repo=&prefix=&kind=` | files/bytes/LOC by category |
| `GET /api/stats/loc?repo=&prefix=` | LOC rows + totals |
| `GET /api/stats/tree/children?eid=` | directory-tree expansion |
