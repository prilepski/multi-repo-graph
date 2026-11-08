# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Workspace end-to-end (MRG_E2E=1 gated — needs docker, node, git):

  fresh workspace -> pipeline --workspace (stages 0-8) -> runtime/scripts/up.py
  -> neo4j + viewer + MFLens + codegraph fan-out all served FROM THE WORKSPACE
  -> down.py cleans up.

This is the acceptance test for workspace-restructure.md goal 2: everything
consumable comes from the workspace; the tool repo is never needed at
consumption time.
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.environ.get("MRG_E2E") != "1",
    reason="MRG_E2E=1 not set (needs docker + node)")

FIXTURE_JAVA = """package org.e2e;

public class Widget {
    public String name() { return "w"; }
}
"""
# distinct ports/container so the e2e never collides with a live workspace
E2E_PORTS = {"bolt": 7797, "http": 7798, "viewer": 7799}
E2E_CONTAINER = "mrg-e2e-neo4j"


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e-ws")
    origin = root / "origin"
    (origin / "src/org/e2e").mkdir(parents=True)
    (origin / "src/org/e2e/Widget.java").write_text(FIXTURE_JAVA)
    for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "x"]):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-C", str(origin), *args], check=True, capture_output=True)

    ws = root / "ws"
    ws.mkdir()
    (ws / "workspace.json").write_text(json.dumps({
        "repos": [{"name": "fixture", "cloneUrl": str(origin),
                   "path": "repos/fixture", "shallow": True}],
        "settings": {"neo4j": {"boltPort": E2E_PORTS["bolt"],
                               "httpPort": E2E_PORTS["http"],
                               "container": E2E_CONTAINER},
                     "viewerPort": E2E_PORTS["viewer"]},
    }))
    return ws


@pytest.fixture(scope="module")
def pipeline_run(workspace):
    """The full pipeline against the workspace (tool-repo venv python)."""
    venv_py = PROJECT_ROOT / "indexer/.venv/bin/python"
    if not venv_py.exists():
        venv_py = Path(sys.executable)
    proc = subprocess.run(
        [str(venv_py), "-m", "indexer.pipeline", "--workspace", str(workspace)],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    return workspace


def test_pipeline_published_run_and_runtime(pipeline_run):
    ws = pipeline_run
    run_id = json.loads((ws / "latest.json").read_text())["run"]
    run_dir = ws / "runs" / run_id
    assert (run_dir / "run-manifest.json").exists()
    assert (run_dir / "neo4j/neo4j.dump.zst").exists()
    assert (run_dir / "codegraphs/fixture/codegraph.db.zst").exists()
    rm = json.loads((run_dir / "run-manifest.json").read_text())
    assert rm["verification"]["status"] == "passed"
    assert rm["repos"][0]["commitSha"]
    assert (ws / "runtime/scripts/up.py").exists()
    assert (ws / "runtime/VERSION").exists()
    log = subprocess.run(["git", "-C", str(ws), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert f"index-run: {run_id}" in log


@pytest.fixture(scope="module")
def up(pipeline_run):
    proc = subprocess.run([sys.executable, "up.py"],
                          cwd=str(pipeline_run / "runtime/scripts"),
                          capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    yield pipeline_run
    subprocess.run([sys.executable, "down.py", "--purge"],
                   cwd=str(pipeline_run / "runtime/scripts"),
                   capture_output=True, timeout=120)


def test_neo4j_restored_from_workspace(up):
    from neo4j import GraphDatabase
    with GraphDatabase.driver(
            f"bolt://localhost:{E2E_PORTS['bolt']}",
            auth=("neo4j", "graphpassword123")) as d:
        with d.session() as s:
            repos = sorted(r["name"] for r in s.run(
                "MATCH (r:Repository) RETURN r.name AS name"))
            types = [r["fqn"] for r in s.run(
                "MATCH (t:Type) RETURN t.fqn AS fqn")]
    assert repos == ["fixture"]
    assert "org.e2e.Widget" in types


def _get(path: str):
    """127.0.0.1 (viewer binds IPv4; localhost can resolve ::1 first) with
    a short retry for the viewer's slow first-bind."""
    import time
    url = f"http://127.0.0.1:{E2E_PORTS['viewer']}{path}"
    for _ in range(10):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return json.load(r)
        except (ConnectionError, urllib.error.URLError):
            time.sleep(1)
    raise AssertionError(f"viewer not reachable at {url}")


def test_viewer_served_from_workspace(up):
    root = _get("/api/root")
    assert [x["name"] for x in root] == ["fixture"]


def test_mflens_runtime_reads_sources(up):
    ws = up
    sys.path.insert(0, str(ws / "runtime/mcp-server"))
    try:
        import importlib
        backend = importlib.import_module("backend")
        b = backend.Neo4jBackend(
            f"bolt://localhost:{E2E_PORTS['bolt']}", "graphpassword123",
            str(ws / "runtime/config/mflens-repos.json"))
        try:
            state = b.source_state("org.e2e.Widget")
            assert state and state["hashMatch"] is True
            assert "class Widget" in "\n".join(state["lines"])
        finally:
            b.close()
    finally:
        sys.path.remove(str(ws / "runtime/mcp-server"))


def test_codegraph_fanout_from_snapshot(up):
    ws = up
    proc = subprocess.run(
        ["node", "fanout.mjs", str(ws / "runtime/config/codegraph-repos.json"),
         "search", json.dumps({"query": "Widget"})],
        cwd=str(ws / "runtime/codegraph-mcp"),
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-800:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["repos"] == ["fixture"]
    hits = [h["name"] for r in out["results"] for h in r.get("hits", [])]
    assert "Widget" in hits


def test_statistics_pane_populated(up):
    """The viewer's Statistics surface reads the inventory layer: taxonomy
    categories, LOC breakdown, coverage snapshot — no nulls."""
    import urllib.parse
    base = f"http://127.0.0.1:{E2E_PORTS['viewer']}/api/stats"
    rows = _get(f"/api/stats/categories?{urllib.parse.urlencode({'repo': 'fixture'})}")
    assert rows, "category stats must not be empty"
    by_cat = {row["category"]: row for row in rows}
    assert "java_backend" in by_cat
    jb = by_cat["java_backend"]
    assert jb["kind"] == "source" and jb["files"] >= 1
    assert jb["codeLines"] >= 1
    assert jb["totalLines"] == (jb["codeLines"] + jb["commentLines"]
                                + jb["blankLines"])
    kpis = _get(f"/api/stats/overview?{urllib.parse.urlencode({'repo': 'fixture'})}")["kpis"]
    assert kpis["tracked"] >= 1 and kpis["deep"] >= 1
    assert kpis["totalLines"] >= 1 and kpis["codeLines"] >= 1
    # coverage snapshot (StatsSnapshot) is present in the unscoped overview
    repos = _get("/api/stats/overview")["repos"]
    entry = next(r for r in repos if r["repository"] == "fixture")
    assert entry["coverage"], "StatsSnapshot coverage must exist"
    assert entry["coverage"]["filesTracked"] == kpis["tracked"]
