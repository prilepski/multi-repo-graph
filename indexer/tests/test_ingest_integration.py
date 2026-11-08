# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Neo4j integration test (NEO4J_TEST_URI gated, design.md §8): fixture
export -> ingest TWICE (idempotency) -> graph shape, counts, CALLS grouping,
external synthesis. Run against the pipeline's own container:

  NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_PASSWORD=graphpassword123 \
  python -m pytest indexer/tests/test_ingest_integration.py
"""

import os
import uuid

import pytest

from indexer import exporter, ingest, verify

NEO4J_TEST_URI = os.environ.get("NEO4J_TEST_URI")
NEO4J_TEST_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD", "graphpassword123")

pytestmark = pytest.mark.skipif(
    not NEO4J_TEST_URI, reason="NEO4J_TEST_URI not set (integration test)")


def _ensure_container_up(neo4j):
    """docker-start bootstrap: the pipeline stops its ingest container after
    dumping; integration tests bring it back (no-op when already running)."""
    import subprocess
    import time
    subprocess.run(["docker", "start", "mrg-neo4j-instance"], capture_output=True)
    for _ in range(30):
        try:
            d = neo4j.GraphDatabase.driver(
                NEO4J_TEST_URI, auth=("neo4j", NEO4J_TEST_PASSWORD))
            d.verify_connectivity()
            d.close()
            return
        except Exception:
            time.sleep(2)
    pytest.skip(f"Neo4j not reachable at {NEO4J_TEST_URI}")


@pytest.fixture()
def driver():
    neo4j = pytest.importorskip("neo4j")
    _ensure_container_up(neo4j)
    d = neo4j.GraphDatabase.driver(
        NEO4J_TEST_URI, auth=("neo4j", NEO4J_TEST_PASSWORD))
    try:
        d.verify_connectivity()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Neo4j not reachable at {NEO4J_TEST_URI}: {e}")
    yield d
    d.close()


@pytest.fixture()
def repo_name():
    return "fixture" + uuid.uuid4().hex[:8]


@pytest.fixture()
def ingested(driver, indexed_fixture, repo_name):
    """Ingest the fixture twice (idempotency); cleans up on teardown.

    Cleanup must catch EVERY node shape the fixture creates: Files and
    module-scoped symbols carry the repo prefix in id/fqn, but java symbols
    use org.demo./org.other. FQNs — the container DB is the live portfolio
    graph, so leaving them pollutes the viewer and the next dump. Shared
    nodes a real portfolio also creates (java.util.Map, common CallTarget
    names) are deliberately NOT deleted."""
    ingest.init_neo4j_schema(driver)
    expected = None
    for _ in range(2):
        exp = exporter.RepoExport(repo_name, indexed_fixture)
        expected = ingest.ingest_repo(driver, repo_name, exp.rows())
    yield expected
    prefix = f"{repo_name}:"
    with driver.session() as s:
        s.run("MATCH (r:Repository {name: $r}) DETACH DELETE r", r=repo_name)
        s.run("MATCH (f:File) WHERE f.id STARTS WITH $p DETACH DELETE f", p=prefix)
        s.run("""MATCH (n) WHERE n.fqn IS NOT NULL AND (
                     n.fqn STARTS WITH 'org.demo.' OR n.fqn STARTS WITH 'org.other.'
                     OR n.fqn STARTS WITH $p) DETACH DELETE n""", p=prefix)
        s.run("""MATCH (m:Method) WHERE m.id STARTS WITH 'org.demo.'
                     OR m.id STARTS WITH 'org.other.'
                     OR m.id STARTS WITH $p DETACH DELETE m""", p=prefix)
        s.run("MATCH (ct:CallTarget {name: 'unknownExternalFn'}) DETACH DELETE ct")


def test_ingest_twice_idempotent(driver, ingested, repo_name):
    expected = ingested
    actual = verify.verify_repo_in_graph(driver, repo_name)
    validation = verify.diff_counts(expected, actual)
    bad = {m: v for m, v in validation.items() if not v["ok"]}
    assert not bad, bad


def test_graph_shape(driver, ingested, repo_name):
    with driver.session() as s:
        # Repository -> File -> DEFINES -> Type (java)
        rec = s.run("""
            MATCH (r:Repository {name: $r})-[:CONTAINS]->(f:File)
                  -[:DEFINES]->(t:Type {fqn: 'org.demo.Service'})
            RETURN f.path AS p, t.kind AS k
        """, r=repo_name).single()
        assert rec is not None
        assert rec["p"].endswith("Service.java")
        assert rec["k"] == "class"

        # Type CONTAINS Method with java-graph id convention
        rec = s.run(
            "MATCH (:Type {fqn: 'org.demo.Service'})-[:CONTAINS]->(m:Method) "
            "RETURN collect(m.id) AS ids").single()
        assert "org.demo.Service#run/1" in rec["ids"]

        # EXTENDS to the internal base, IMPLEMENTS to external Runnable
        ext = s.run("MATCH (t:Type {fqn:'org.demo.Service'})-[:EXTENDS]->(b) "
                    "RETURN b.fqn AS f").single()
        assert ext and ext["f"] == "org.demo.Base"
        imp = s.run("MATCH (t:Type {fqn:'org.demo.Service'})-[:IMPLEMENTS]->(b) "
                    "RETURN b.fqn AS f, b.external AS e").single()
        assert imp and imp["f"].endswith("Runnable") and imp["e"] is True

        # CALLS {count} grouping, name-level; external CallTarget synthesized
        rec = s.run("""
            MATCH (m:Method {id: 'org.demo.Service#run/1'})-[c:CALLS]->(ct:CallTarget)
            RETURN ct.name AS n, c.count AS cnt, ct.external AS ext ORDER BY n
        """)
        calls = {r["n"]: (r["cnt"], r["ext"]) for r in rec}
        assert "helper" in calls
        assert calls["unknownExternalFn"][0] == 1
        assert calls["unknownExternalFn"][1] is True

        # IMPORTS: single-type import (external java.util.Map)
        rec = s.run("""
            MATCH (f:File {id: $fid})-[:IMPORTS]->(t:Type {fqn: 'java.util.Map'})
            RETURN count(*) AS c
        """, fid=f"{repo_name}:src/main/java/org/demo/Service.java").single()
        assert rec["c"] == 1

        # non-java: module type + file-scoped members
        rec = s.run("""
            MATCH (t:Type {fqn: $mod})-[:CONTAINS]->(m:Method)
            RETURN collect(m.id) AS ids
        """, mod=f"{repo_name}:ui/src/util.ts").single()
        assert f"{repo_name}:ui/src/util.ts#format/1" in rec["ids"]
        rec = s.run("""
            MATCH (t:Type {fqn: $g})-[:CONTAINS]->(fl:Field)
            RETURN count(fl) AS c
        """, g=f"{repo_name}:ui/src/util.ts::Greeter").single()
        assert rec["c"] >= 1

        # :File carries our sha256 + line count
        rec = s.run("""
            MATCH (f:File {id: $fid}) RETURN f.sha256 IS NOT NULL AS h,
                   f.totalLines AS l, f.bytes AS b
        """, fid=f"{repo_name}:src/main/java/org/demo/Service.java").single()
        assert rec["h"] is True and rec["l"] and rec["b"]


def test_sampled_verification(driver, ingested, repo_name, indexed_fixture):
    exp = exporter.RepoExport(repo_name, indexed_fixture)
    sampler = exporter.RowSampler()
    rows = list(exp.rows())
    for t, r in rows:
        sampler.observe(t, r)
    result = verify.verify_samples(driver, sampler.samples)
    assert result["ok"], result["failures"]
    assert result["checked"] > 0
