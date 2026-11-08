"""Tests for the Tree-sitter Java-to-Neo4j parsing pipeline.

Unit tests run fully offline against synthetic Java sources. The Neo4j
ingestion is covered by an integration test skipped unless NEO4J_TEST_URI
is set.
"""

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import index_to_neo4j as pipeline  # noqa: E402


SAMPLE_JAVA = """package com.example.app;

import java.util.List;
import java.util.Map;
import com.example.common.BaseService;
import com.example.common.*;

public class UserService extends BaseService implements Runnable, AutoCloseable {
    private final List<String> names;
    protected static int counter;
    public Map<String, String> cache;

    public UserService(List<String> names) {
        this.names = names;
        init();
    }

    @Override
    public void run() {
        for (String name : names) {
            validate(name);
            log.info(name);
        }
    }

    private String validate(String input) {
        return input.trim().toLowerCase();
    }

    public static class InnerTask implements Runnable {
        public void execute() { run(); }
    }
}

interface Repository<T> {
    T find(String id);
    void save(T entity);
}

enum Status { ACTIVE, SUSPENDED }

record Point(int x, int y) {}
"""


@pytest.fixture(scope="module")
def parsed_sample():
    return pipeline.parse_java_source(SAMPLE_JAVA)


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------

class TestLoadManifest:
    def test_loads_valid_manifest(self, tmp_path):
        f = tmp_path / "repos.json"
        f.write_text('[{"name": "a", "path": "/tmp/a"}]')
        repos = pipeline.load_manifest(f)
        assert repos == [{"name": "a", "path": "/tmp/a"}]

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            pipeline.load_manifest(tmp_path / "nope.json")

    def test_non_array_manifest_exits(self, tmp_path):
        f = tmp_path / "repos.json"
        f.write_text('{"repos": []}')
        with pytest.raises(SystemExit):
            pipeline.load_manifest(f)

    def test_entry_missing_name_exits(self, tmp_path):
        f = tmp_path / "repos.json"
        f.write_text('[{"path": "/tmp/a"}]')
        with pytest.raises(SystemExit):
            pipeline.load_manifest(f)


# ---------------------------------------------------------------------------
# Parsing: package / imports
# ---------------------------------------------------------------------------

class TestParsePackageAndImports:
    def test_package(self, parsed_sample):
        assert parsed_sample["package"] == "com.example.app"

    def test_single_type_imports(self, parsed_sample):
        targets = [(i["target"], i["wildcard"]) for i in parsed_sample["imports"]]
        assert ("java.util.List", False) in targets
        assert ("com.example.common.BaseService", False) in targets

    def test_wildcard_import(self, parsed_sample):
        assert any(i["target"] == "com.example.common" and i["wildcard"]
                   for i in parsed_sample["imports"])


# ---------------------------------------------------------------------------
# Parsing: types and structure
# ---------------------------------------------------------------------------

class TestParseTypes:
    def test_top_level_types(self, parsed_sample):
        names = {t["name"]: t for t in parsed_sample["types"]}
        assert set(names) == {"UserService", "Repository", "Status", "Point"}

    def test_kinds(self, parsed_sample):
        kinds = {t["name"]: t["kind"] for t in parsed_sample["types"]}
        assert kinds["UserService"] == "class"
        assert kinds["Repository"] == "interface"
        assert kinds["Status"] == "enum"
        assert kinds["Point"] == "record"

    def test_fqn_includes_package(self, parsed_sample):
        svc = next(t for t in parsed_sample["types"] if t["name"] == "UserService")
        assert svc["fqn"] == "com.example.app.UserService"

    def test_extends(self, parsed_sample):
        svc = next(t for t in parsed_sample["types"] if t["name"] == "UserService")
        assert svc["extends"] == ["BaseService"]

    def test_implements(self, parsed_sample):
        svc = next(t for t in parsed_sample["types"] if t["name"] == "UserService")
        assert set(svc["implements"]) == {"Runnable", "AutoCloseable"}

    def test_nested_class(self, parsed_sample):
        svc = next(t for t in parsed_sample["types"] if t["name"] == "UserService")
        nested = svc["nested"][0]
        assert nested["name"] == "InnerTask"
        assert nested["fqn"] == "com.example.app.UserService.InnerTask"
        assert "Runnable" in nested["implements"]
        assert nested["methods"][0]["calls"] == {"run": 1}


class TestParseMembers:
    def test_fields(self, parsed_sample):
        svc = next(t for t in parsed_sample["types"] if t["name"] == "UserService")
        fields = {f["name"]: f for f in svc["fields"]}
        assert set(fields) == {"names", "counter", "cache"}
        assert fields["names"]["type"] == "List<String>"
        assert fields["counter"]["modifiers"] == "protected static"

    def test_methods_with_arity(self, parsed_sample):
        svc = next(t for t in parsed_sample["types"] if t["name"] == "UserService")
        methods = {m["id"]: m for m in svc["methods"]}
        assert "com.example.app.UserService#run/0" in methods
        assert "com.example.app.UserService#validate/1" in methods
        assert "com.example.app.UserService#UserService/1" in methods  # constructor

    def test_method_return_type_and_modifiers(self, parsed_sample):
        svc = next(t for t in parsed_sample["types"] if t["name"] == "UserService")
        validate = next(m for m in svc["methods"] if m["name"] == "validate")
        assert validate["return_type"] == "String"
        assert "private" in validate["modifiers"]

    def test_interface_methods(self, parsed_sample):
        repo = next(t for t in parsed_sample["types"] if t["name"] == "Repository")
        assert {m["name"] for m in repo["methods"]} == {"find", "save"}

    def test_calls_collected_per_method(self, parsed_sample):
        svc = next(t for t in parsed_sample["types"] if t["name"] == "UserService")
        run = next(m for m in svc["methods"] if m["name"] == "run")
        assert run["calls"]["validate"] == 1
        assert run["calls"]["info"] == 1          # log.info(name) -> callee 'info'
        ctor = next(m for m in svc["methods"] if m["name"] == "UserService")
        assert ctor["calls"] == {"init": 1}


# ---------------------------------------------------------------------------
# Parsing: resilience
# ---------------------------------------------------------------------------

class TestParseResilience:
    def test_incomplete_source_does_not_crash(self):
        partial = "package x; class Broken { void m() { undeclaredCall("
        result = pipeline.parse_java_source(partial)
        # package parses; the truncated class may land in an ERROR node — no crash
        assert result["package"] == "x"

    def test_empty_source(self):
        result = pipeline.parse_java_source("")
        assert result == {"package": "", "imports": [], "types": []}

    def test_parse_repo_walks_tree_resiliently(self, tmp_path):
        (tmp_path / "A.java").write_text("package p; class A { int f; }")
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "B.java").write_text("package p; class B { void g() { a(); } }")
        (tmp_path / "broken.java").write_bytes(b"\xff\xfe invalid \x00")

        results, errors = pipeline.parse_repo(tmp_path)
        # invalid bytes still parse (Tree-sitter is resilient): no error recorded
        assert sorted(r["path"] for r in results) == ["A.java", "broken.java", "sub/B.java"]
        assert errors == []
        # every result carries a sha256 of its parsed text (drift detection)
        for r in results:
            assert len(r["sha256"]) == 64 and int(r["sha256"], 16) >= 0
        by_path = {r["path"]: r for r in results}
        assert by_path["A.java"]["types"][0]["name"] == "A"
        assert by_path["sub/B.java"]["types"][0]["methods"][0]["calls"] == {"a": 1}

    def test_parse_repo_records_read_errors(self, tmp_path, monkeypatch):
        (tmp_path / "A.java").write_text("package p; class A { }")
        (tmp_path / "boom.java").write_text("package p; class B { }")

        real_read = Path.read_text

        def flaky_read(self, *a, **kw):
            if self.name == "boom.java":
                raise OSError("disk error")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", flaky_read)
        results, errors = pipeline.parse_repo(tmp_path)
        assert [r["path"] for r in results] == ["A.java"]
        assert [e["path"] for e in errors] == ["boom.java"]
        assert "disk error" in errors[0]["error"]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

class TestResolve:
    def test_fq_name_passthrough(self):
        assert pipeline.resolve_type_name("java.lang.String", "p", {}) == "java.lang.String"

    def test_resolved_via_import(self):
        imports = {"BaseService": "com.example.common.BaseService"}
        assert pipeline.resolve_type_name("BaseService", "com.app", imports) \
            == "com.example.common.BaseService"

    def test_defaults_to_same_package(self):
        assert pipeline.resolve_type_name("Neighbor", "com.app", {}) == "com.app.Neighbor"

    def test_no_package_defaults_to_bare_name(self):
        assert pipeline.resolve_type_name("Neighbor", "", {}) == "Neighbor"

    def test_declared_set_flattens_nested(self):
        fr = pipeline.parse_java_source(SAMPLE_JAVA)
        declared = pipeline.build_declared_set([fr])
        assert "com.example.app.UserService" in declared
        assert "com.example.app.UserService.InnerTask" in declared
        assert "com.example.app.Status" in declared


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

class TestChunks:
    def test_splits_into_chunks(self):
        rows = [{"i": i} for i in range(12001)]
        assert [len(c) for c in pipeline.chunks(rows, size=5000)] == [5000, 5000, 2001]

    def test_empty_list_yields_nothing(self):
        assert list(pipeline.chunks([])) == []


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

class TestCheckPrerequisites:
    def test_engine_present_passes(self, monkeypatch, caplog):
        monkeypatch.setattr(pipeline.shutil, "which",
                            lambda c: {"docker": "/usr/bin/docker"}.get(c))
        with caplog.at_level(logging.INFO, logger="index_to_neo4j"):
            pipeline.check_prerequisites("docker")
        assert "Container engine found" in caplog.text

    def test_missing_engine_exits(self, monkeypatch):
        monkeypatch.setattr(pipeline.shutil, "which", lambda c: None)
        with pytest.raises(SystemExit):
            pipeline.check_prerequisites("docker")


# ---------------------------------------------------------------------------
# Post-import validation helpers
# ---------------------------------------------------------------------------

class TestDiffCounts:
    def test_matching_counts_are_ok(self):
        expected = {m: 10 for m in pipeline.GRAPH_METRICS}
        actual = {m: 10 for m in pipeline.GRAPH_METRICS}
        diff = pipeline.diff_counts(expected, actual)
        assert set(diff) == set(pipeline.GRAPH_METRICS)
        assert all(v["ok"] for v in diff.values())

    def test_mismatch_is_flagged(self):
        expected = {"files": 100, "types": 50}
        actual = {"files": 99, "types": 50}
        diff = pipeline.diff_counts(expected, actual)
        assert diff["files"] == {"expected": 100, "actual": 99, "ok": False}
        assert diff["types"]["ok"] is True

    def test_missing_metric_counts_as_zero(self):
        diff = pipeline.diff_counts({}, {"files": 5})
        assert diff["files"] == {"expected": 0, "actual": 5, "ok": False}


# ---------------------------------------------------------------------------
# Integration test (requires live Neo4j; skipped by default)
# ---------------------------------------------------------------------------

NEO4J_TEST_URI = os.environ.get("NEO4J_TEST_URI")  # e.g. bolt://localhost:7687
NEO4J_TEST_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD", "graphpassword123")
TEST_REPO = "parse-integration-test"


@pytest.mark.skipif(not NEO4J_TEST_URI, reason="NEO4J_TEST_URI not set")
class TestIngestIntegration:
    def test_ingest_parsed_source_and_query_graph(self):
        from neo4j import GraphDatabase

        import hashlib as _hl
        sample_sha = _hl.sha256(SAMPLE_JAVA.encode("utf-8", "replace")).hexdigest()
        file_results = [{"path": "UserService.java", "sha256": sample_sha,
                         **pipeline.parse_java_source(SAMPLE_JAVA)}]
        with GraphDatabase.driver(NEO4J_TEST_URI,
                                  auth=("neo4j", NEO4J_TEST_PASSWORD)) as driver:
            pipeline.init_neo4j_schema(driver)
            expected = pipeline.ingest_repo_to_neo4j(driver, TEST_REPO, file_results)
            pipeline.ingest_repo_to_neo4j(driver, TEST_REPO, file_results)  # idempotent

            # expected counts are returned by the ingest itself
            assert expected["files"] == 1
            assert expected["types"] == 5      # 4 top-level + 1 nested
            assert expected["methods"] == 6    # ctor/run/validate + execute + find/save
            assert expected["fields"] == 3     # names, counter, cache
            assert expected["extends"] == 1
            assert expected["implements"] == 3  # Runnable + AutoCloseable, InnerTask: Runnable
            assert expected["imports"] == 4
            assert expected["calls"] == 6      # init; validate,info; trim,toLowerCase; run

            # File nodes carry the content hash
            sha = session_run_scalar = None
            sha = driver.session().run(
                "MATCH (f:File) WHERE f.id ENDS WITH 'UserService.java' "
                "RETURN f.sha256 AS s").single()["s"]
            assert sha == sample_sha

            # post-import verification: graph counts match extraction
            actual = pipeline.verify_repo_in_graph(driver, TEST_REPO)
            diff = pipeline.diff_counts(expected, actual)
            bad = {m: v for m, v in diff.items() if not v["ok"]}
            assert not bad, f"graph validation mismatches: {bad}"

            with driver.session() as session:
                counts = session.run(f"""
                    MATCH (r:Repository {{name: $name}})
                    OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)-[:DEFINES]->(t:Type)
                    OPTIONAL MATCH (t)-[:CONTAINS]->(m:Method)
                    RETURN count(DISTINCT f) AS files,
                           count(DISTINCT t) AS types,
                           count(DISTINCT m) AS methods
                """, name=TEST_REPO).single().data()
                assert counts["files"] == 1
                assert counts["types"] >= 5  # 4 top-level + 1 nested
                assert counts["methods"] >= 6

                # inheritance resolved to the imported FQN, marked external
                ext = session.run(f"""
                    MATCH (t:Type {{fqn: 'com.example.app.UserService'}})-[:EXTENDS]->(base)
                    RETURN base.fqn AS base
                """).single().data()
                assert ext["base"] == "com.example.common.BaseService"

                # method -> call target edge with count
                call = session.run(f"""
                    MATCH (m:Method {{id: 'com.example.app.UserService#run/0'}})
                          -[c:CALLS]->(ct:CallTarget)
                    RETURN ct.name AS name, c.count AS count ORDER BY name
                """).data()
                assert {r["name"]: r["count"] for r in call} == {"info": 1, "validate": 1}

                session.run(f"""
                    MATCH (r:Repository {{name: $name}})
                    OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
                    OPTIONAL MATCH (r)-[:CONTAINS]->(p:Package)
                    DETACH DELETE f, p, r
                """, name=TEST_REPO)
