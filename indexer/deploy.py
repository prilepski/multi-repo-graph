# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Stage 8: deploy the runtime into the workspace (workspace-restructure.md
§5). Copies the MCP servers + viewer sources, generates the workspace-scoped
codegraph package.json and the bootstrap scripts (thin sh/ps1 wrappers +
Python up/down), and stamps runtime/VERSION. Idempotent; the publish step
commits it only when something changed.
"""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

TOOL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / "runtime_templates"

# what ships per component (source only — deps install at `up` time)
COMPONENTS: Dict[str, List[str]] = {
    "mcp-server": ["backend.py", "server.py", "requirements.txt", "README.md"],
    "codegraph-mcp": ["server.py", "fanout.mjs", "requirements.txt", "README.md"],
    "graph-viewer": ["viewer_server.py", "graph-viewer.html",
                     "force-graph.min.js", "requirements.txt", "README.md"],
}

CODEGRAPH_PKG = {
    "name": "codegraph-workspace-runtime",
    "private": True,
    "type": "module",
    "description": ("codegraph SDK for the portfolio fan-out wrapper "
                    "(installed by runtime/scripts/up.py)"),
    "dependencies": {"@colbymchenry/codegraph": "1.6.2"},
}


def deploy_runtime(ws_root: Path) -> Dict[str, Any]:
    runtime = ws_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)

    copied: List[str] = []
    for comp, files in COMPONENTS.items():
        dest_dir = runtime / comp
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            src = TOOL_ROOT / comp / name
            if not src.exists():
                raise FileNotFoundError(f"runtime source missing: {src}")
            dest = dest_dir / name
            if not dest.exists() or dest.read_text(encoding="utf-8",
                                                   errors="replace") != \
                    src.read_text(encoding="utf-8", errors="replace"):
                shutil.copy2(src, dest)
                copied.append(f"{comp}/{name}")

    # workspace-scoped codegraph install (fanout.mjs resolves ./node_modules)
    pkg = runtime / "codegraph-mcp" / "package.json"
    pkg.write_text(json.dumps(CODEGRAPH_PKG, indent=2) + "\n", encoding="utf-8")

    # bootstrap scripts: Python does the work; sh/ps1 only find a python
    scripts = runtime / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for name in os_listdir(TEMPLATES):
        src = TEMPLATES / name
        dest = scripts / name
        if not dest.exists() or dest.read_text(encoding="utf-8",
                                               errors="replace") != \
                src.read_text(encoding="utf-8", errors="replace"):
            shutil.copy2(src, dest)
            copied.append(f"scripts/{name}")
        if name.endswith(".sh"):
            dest.chmod(dest.stat().st_mode | 0o111)

    write_workspace_readme(ws_root)

    try:
        tool_commit = subprocess.run(
            ["git", "-C", str(TOOL_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True).stdout.strip() or "unknown"
    except Exception:
        tool_commit = "unknown"

    version = {
        "toolCommit": tool_commit,
        "deployedAt": datetime.now(timezone.utc).isoformat(),
        "codegraph": CODEGRAPH_PKG["dependencies"]["@colbymchenry/codegraph"],
    }
    (runtime / "VERSION").write_text(json.dumps(version, indent=2) + "\n",
                                     encoding="utf-8")
    return {"copied": copied, "version": version}


def os_listdir(path: Path) -> List[str]:
    return sorted(p.name for p in path.iterdir() if p.is_file())


def runtime_changed(ws_root: Path) -> bool:
    res = subprocess.run(["git", "-C", str(ws_root), "status", "--porcelain",
                          "runtime"], capture_output=True, text=True)
    return bool(res.stdout.strip())


WORKSPACE_README = """# codegraph-workspace

This repo is a **multi-repo-graph workspace**: it owns the list of indexed
repositories, every indexed snapshot, and a self-contained runtime. You do
NOT need the multi-repo-graph tool repo to use it.

## Quick start

    runtime/scripts/up.sh        # Windows: runtime/scripts/up.ps1
                                  # (the shim only needs Python 3.11+)

That restores the latest run and starts:

  * graph viewer  — http://localhost:{viewer_port} — "Multi Repo Code
    Graph Viewer": cross-repo integration constellation (evidence-class
    colored edges with flowing particles), unified Nodes/Edges filter bar
    (bottom-right), repository multi-select (header), animation toggle,
    and a Statistics surface (file taxonomy, LOC, coverage)
  * Neo4j browser — http://localhost:{http_port}  (bolt :{bolt_port},
    password in workspace.json settings.neo4j.password)
  * MCP servers   — paste `runtime/config/mcp.json` into your
    ZCode/Claude config (MFLens portfolio graph + codegraph fan-out)

Also: `up.sh --run <id>` pins a snapshot, `--mode live` reads codegraph
from working trees, `runtime/scripts/down.sh [--purge]` stops everything.
Repo sources are checked out at the exact SHAs recorded in
`runs/<id>/run-manifest.json`.

## Contents

    workspace.json   repos + settings (ports, keepLast, remote.push)
    runs/<ts>/       snapshots (run-manifest, summary, neo4j + codegraph
                     dumps, zstd, LFS); latest.json points at the newest
    runtime/         viewer + MCP servers, pinned by runtime/VERSION
    repos/           gitignored repo clones · snapshot/, logs/ gitignored

Re-indexing is done by an operator with the multi-repo-graph tool:
`python -m indexer.pipeline --workspace <this repo>`.
"""


def write_workspace_readme(ws_root: Path) -> None:
    """Generate the consumption README from workspace.json settings.
    A hand-written README containing the keep-marker is PRESERVED (e.g. the
    multi-repo-graph-example sample repo ships a curated README)."""
    readme = ws_root / "README.md"
    if readme.exists() and "mrg:keep-readme" in readme.read_text(
            encoding="utf-8", errors="replace")[:2000]:
        return
    import json
    settings = json.loads((ws_root / "workspace.json").read_text()) \
        .get("settings", {})
    neo4j = settings.get("neo4j", {})
    text = WORKSPACE_README.format(
        viewer_port=settings.get("viewerPort", 7600),
        http_port=neo4j.get("httpPort", 7475),
        bolt_port=neo4j.get("boltPort", 7688),
    )
    (ws_root / "README.md").write_text(text, encoding="utf-8")
