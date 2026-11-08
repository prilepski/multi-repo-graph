# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Multi-repo MFLens integration tests (NEO4J_TEST_URI gated).

The java-graph suite (test_mcp_tools.py, offline FakeBackend) validates the
60+4 tool contract for a SINGLE repo. These tests exercise the semantics that
only exist once several repos share one portfolio graph (design.md §1/§5,
D7):

  - same-FQN type declared in TWO repos -> one Type node (portfolio MERGE),
    DEFINES edges from both repos' files, method set is the UNION
  - cross-repo extends: repo B extends a class only repo A declares ->
    target external:false (the portfolio declares it, coalesce keeps it)
  - source_state / fileHashMatch reads the RIGHT repo's working tree
  - drift detection: edit a file after ingest -> hashMatch flips false
  - deep_call_chain resolves name-level callees to the shared Method id

Fixtures flow through the REAL pipeline path (codegraph index worker ->
exporter -> ingest), not a synthetic graph.

  NEO4J_TEST_URI=bolt://localhost:7688 NEO4J_TEST_PASSWORD=graphpassword123 \
  mcp-server/.venv/bin/python -m pytest mcp-server/tests/test_mcp_multi_repo.py
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

MCP_SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = MCP_SERVER_DIR.parent
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

from indexer import exporter, ingest  # noqa: E402
from backend import Neo4jBackend      # noqa: E402

NEO4J_TEST_URI = os.environ.get("NEO4J_TEST_URI")
NEO4J_TEST_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD", "graphpassword123")
NODEJS_DIR = PROJECT_ROOT / "indexer" / "nodejs"

def _ensure_container_up():
    """docker-start bootstrap (pipeline stops its container after dumping)."""
    import subprocess
    subprocess.run(["docker", "start", "mrg-neo4j-instance"], capture_output=True)


pytestmark = pytest.mark.skipif(
    not NEO4J_TEST_URI, reason="NEO4J_TEST_URI not set (integration test)")

# two repos sharing package org.acme — the collision is the point
CODES_A = """package org.acme.util;

public class Codes {
    public String encode(String s) { return s; }
}
"""

CODES_B = """package org.acme.util;

public class Codes {
    public String encode(String s) { return s; }
    public int decode(String s) { return 1; }
}
"""

BASE_A = """package org.acme.core;

public abstract class Base {
    protected void before() {}
}
"""

APP_A = """package org.acme;

import org.acme.util.Codes;

public class AppA extends org.acme.core.Base {
    public void run() {
        Codes c = new Codes();
        c.encode("x");
        before();
    }
}
"""

APP_B = """package org.acme;

import org.acme.core.Base;
import org.acme.util.Codes;

public class AppB extends Base {
    public void run() {
        new Codes().encode("y");
    }
}
"""


def _git_repo(path: Path, files: dict) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "x"]):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-C", str(path), *args], check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def two_repos(tmp_path_factory):
    """Repo A declares Codes+Base+AppA; repo B re-declares Codes (same FQN,
    extra method) and extends A's Base."""
    root = tmp_path_factory.mktemp("multirepo")
    a = _git_repo(root / "repoa", {
        "src/org/acme/util/Codes.java": CODES_A,
        "src/org/acme/core/Base.java": BASE_A,
        "src/org/acme/AppA.java": APP_A,
    })
    b = _git_repo(root / "repob", {
        "src/org/acme/util/Codes.java": CODES_B,
        "src/org/acme/AppB.java": APP_B,
    })
    return a, b


@pytest.fixture(scope="module")
def names():
    suffix = uuid.uuid4().hex[:8]
    return f"fixture{suffix}a", f"fixture{suffix}b"


@pytest.fixture(scope="module")
def portfolio(two_repos, names):
    """Index + export + ingest BOTH repos through the real pipeline path,
    then yield (driver, backend). Cleanup deletes every fixture shape."""
    import neo4j as neo4j_mod
    (repo_a, repo_b) = two_repos
    name_a, name_b = names
    _ensure_container_up()
    driver = neo4j_mod.GraphDatabase.driver(
        NEO4J_TEST_URI, auth=("neo4j", NEO4J_TEST_PASSWORD))
    driver.verify_connectivity()

    ingest.init_neo4j_schema(driver)
    for name, path in ((name_a, repo_a), (name_b, repo_b)):
        proc = subprocess.run(
            ["node", str(NODEJS_DIR / "index-worker.mjs"), str(path)],
            cwd=str(NODEJS_DIR), capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr[-1500:]
        exp = exporter.RepoExport(name, path)
        ingest.ingest_repo(driver, name, exp.rows())

    repos_json = root_repos_json(two_repos, names)
    backend = Neo4jBackend(NEO4J_TEST_URI, NEO4J_TEST_PASSWORD, str(repos_json))
    yield driver, backend
    backend.close()
    with driver.session() as s:
        for name in names:
            s.run("MATCH (r:Repository {name: $r}) DETACH DELETE r", r=name)
            p = f"{name}:"
            s.run("MATCH (f:File) WHERE f.id STARTS WITH $p DETACH DELETE f", p=p)
            s.run("""MATCH (n) WHERE n.fqn IS NOT NULL AND
                     (n.fqn STARTS WITH 'org.acme.' OR n.fqn STARTS WITH $p)
                     DETACH DELETE n""", p=p)
            s.run("""MATCH (m:Method) WHERE m.id STARTS WITH 'org.acme.'
                     OR m.id STARTS WITH $p DETACH DELETE m""", p=p)
    driver.close()


def root_repos_json(two_repos, names):
    (repo_a, repo_b) = two_repos
    import json
    path = repo_a.parent / "repos.json"
    path.write_text(json.dumps([
        {"name": names[0], "path": str(repo_a)},
        {"name": names[1], "path": str(repo_b)},
    ]))
    return path


# ---------------------------------------------------------------------------
# portfolio semantics: shared types across repos
# ---------------------------------------------------------------------------

class TestSharedTypes:
    def test_same_fqn_is_one_type_with_two_defines(self, portfolio, names):
        driver, _ = portfolio
        with driver.session() as s:
            rec = s.run("""
                MATCH (t:Type {fqn: 'org.acme.util.Codes'})
                OPTIONAL MATCH (f:File)-[:DEFINES]->(t)
                RETURN count(DISTINCT t) AS types,
                       count(DISTINCT f) AS defines,
                       collect(DISTINCT split(f.id, ':')[0]) AS repos
            """).single()
        assert rec["types"] == 1, "same FQN in two repos must MERGE to one Type"
        assert rec["defines"] == 2
        assert set(rec["repos"]) == set(names)

    def test_method_set_is_the_union(self, portfolio):
        driver, _ = portfolio
        with driver.session() as s:
            ids = [r["id"] for r in s.run(
                "MATCH (:Type {fqn: 'org.acme.util.Codes'})-[:CONTAINS]->(m:Method) "
                "RETURN m.id AS id ORDER BY id")]
        # encode is declared by BOTH repos -> one shared id (uniqueness collapse)
        assert ids.count("org.acme.util.Codes#encode/1") == 1
        assert "org.acme.util.Codes#decode/1" in ids  # only repo B declares it

    def test_cross_repo_extends_is_not_external(self, portfolio):
        driver, _ = portfolio
        with driver.session() as s:
            rec = s.run("""
                MATCH (t:Type {fqn: 'org.acme.AppB'})-[:EXTENDS]->(b:Type {fqn: 'org.acme.core.Base'})
                RETURN b.external AS external
            """).single()
        assert rec is not None, "cross-repo EXTENDS edge must exist"
        # repo B's export row says external (Base not declared IN B), but the
        # portfolio declares it via repo A — coalesce keeps the earlier false
        assert rec["external"] is False

    def test_mflens_export_unions_methods(self, portfolio):
        _, backend = portfolio
        result = backend.export("org.acme.util.Codes")
        assert result is not None
        method_names = {m["name"] for m in result["methods"]}
        assert method_names == {"encode", "decode"}


# ---------------------------------------------------------------------------
# MFLens reads across repos
# ---------------------------------------------------------------------------

class TestBackendReads:
    def test_search_finds_both_repos_types(self, portfolio, names):
        _, backend = portfolio
        # the portfolio is LIVE (thingsboard/dolphinscheduler included) —
        # scope assertions to the fixture's package
        hits = {h["fqn"] for h in backend.search_types("App")
                if h["fqn"].startswith("org.acme.")}
        assert {"org.acme.AppA", "org.acme.AppB"} <= hits
        repos = {h["repo"] for h in backend.search_types("App")
                 if h["fqn"].startswith("org.acme.")}
        assert repos == set(names)

    def test_source_state_reads_own_repo_tree(self, portfolio):
        _, backend = portfolio
        state = backend.source_state("org.acme.AppA")
        assert state is not None
        assert "class AppA" in "\n".join(state["lines"])
        state_b = backend.source_state("org.acme.AppB")
        assert "class AppB" in "\n".join(state_b["lines"])

    def test_drift_detection_flips_with_working_tree(self, portfolio, two_repos):
        _, backend = portfolio
        repo_a, _ = two_repos
        app_a = repo_a / "src/org/acme/AppA.java"
        original = app_a.read_text()
        assert backend.source_state("org.acme.AppA")["hashMatch"] is True
        try:
            app_a.write_text(original + "\n// drifted\n")
            assert backend.source_state("org.acme.AppA")["hashMatch"] is False
        finally:
            app_a.write_text(original)
        assert backend.source_state("org.acme.AppA")["hashMatch"] is True

    def test_call_chain_resolves_shared_callee(self, portfolio):
        _, backend = portfolio
        chain = backend.deep_call_chain("org.acme.AppB")
        assert chain is not None
        run_nodes = [p for path in chain["paths"]
                     for p in [path] + _descendants(path)
                     if p.get("name") == "run"]
        assert run_nodes, "AppB.run must appear in the call chain"
        callees = [c for n in run_nodes for c in n.get("children", [])]
        resolved = [c["resolvedTo"] for c in callees if c.get("resolvedTo")]
        # 'encode' resolves by name to the shared Method id
        assert any(r.startswith("org.acme.util.Codes#encode") for r in resolved), resolved


def _descendants(node):
    out = []
    for child in node.get("children", []):
        out.append(child)
        out.extend(_descendants(child))
    return out
