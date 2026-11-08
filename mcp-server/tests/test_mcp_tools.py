"""Tests for the MFLens-compatible MCP server (offline: FakeBackend)."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["MCPLENS_FAKE"] = "1"
os.environ.setdefault("MCPLENS_FAKE_ROOT", str(Path(__file__).parent / "fixture-src"))

import server  # noqa: E402  (imports with the fake backend injected)

EXPECTED_TOOLS = server.ALL_TOOLS


@pytest.fixture(scope="module")
def tools():
    """All registered MCP tools by name."""
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


import json as _json


def _unwrap(result):
    """Normalize FastMCP call_tool results to a one-element list of plain dicts
    (tools serialize to TextContent JSON in this SDK version)."""
    first = result[0] if isinstance(result, (list, tuple)) else result
    text = getattr(first, "text", None)
    parsed = _json.loads(text) if text is not None else first
    return [parsed]


def call(tool):
    async def _inner(**kwargs):
        return _unwrap(await server.mcp.call_tool(tool, kwargs))
    return _inner


# ---------------------------------------------------------------------------
# Inventory completeness — all 60, exactly the documented set
# ---------------------------------------------------------------------------

class TestInventory:
    def test_all_64_tools_registered(self, tools):
        assert len(tools) == 64, f"expected 64 tools, got {len(tools)}"

    def test_mflens_60_set_intact(self, tools):
        assert sorted(set(tools) & set(server.TOOL_INVENTORY)) == \
            sorted(server.TOOL_INVENTORY)

    def test_registered_names_match_inventory_exactly(self, tools):
        assert sorted(tools) == sorted(EXPECTED_TOOLS)

    @pytest.mark.parametrize("name", EXPECTED_TOOLS)
    def test_tool_has_description(self, tools, name):
        assert tools[name].description, f"{name} lacks a description"


# ---------------------------------------------------------------------------
# Tier A — accurate and reliable
# ---------------------------------------------------------------------------

class TestTierA:
    def test_search_program_resolves(self):
        r = asyncio.run(call("search_program")(
            programName="OrderService"))
        assert r[0]["count"] >= 1
        m = r[0]["matches"][0]
        assert m["name"] == "OrderService"
        assert m["kind"] == "class" and m["repo"] == "demo"
        assert m["ok"] is True

    def test_search_program_fqn(self):
        r = asyncio.run(call("search_program")(
            programName="org.demo.OrderService"))
        assert r[0]["count"] >= 1

    def test_search_component_prefix(self):
        r = asyncio.run(call("search_component")(prefix="Order"))
        names = [m["name"] for m in r[0]["matches"]]
        assert "OrderService" in names and "OrderRepository" in names

    def test_get_program_source_code_exact_lines(self):
        r = asyncio.run(call("get_program_source_code")(
            programName="OrderService", startLine=10, endLine=10))
        line = r[0]["code"]
        assert "process" in line and line.lstrip().startswith("10|")

    def test_get_program_source_code_unknown_program(self):
        r = asyncio.run(call("get_program_source_code")(
            programName="Nope", startLine=1, endLine=2))
        assert "error" in r[0]

    def test_search_code_physical_lines(self):
        r = asyncio.run(call("search_code")(
            programName="OrderService", searchText="validate"))
        hits = r[0]["hits"]
        assert any(h["line"] == 11 and "validate(id);" in h["text"] for h in hits)
        assert all(h["paragraphName"] is None and h["deadCode"] is False for h in hits)

    def test_search_code_portfolio(self):
        r = asyncio.run(call("search_code_portfolio")(searchText="package org.demo"))
        assert r[0]["count"] == 3  # all three fixture files
        repos = {h["repo"] for h in r[0]["hits"]}
        assert repos == {"demo"}

    def test_deep_call_chain_resolves_internal_and_cross_type(self):
        r = asyncio.run(call("get_program_deep_call_chain")(
            programName="OrderService"))
        chain = r[0]
        entry = chain["paths"][0]
        assert entry["nodeType"] == "entry_point"
        process = next(c for c in entry["children"] if c["name"] == "process")
        callees = {c["name"]: c for c in process["children"]}
        assert callees["validate"]["nodeType"] == "method"      # internal
        assert "save" in callees                                 # cross-type
        assert "info" in callees                                 # external leaf
        assert callees["info"].get("nodeType") is None           # MD: external no nodeType


# ---------------------------------------------------------------------------
# Tier B — real data with documented shape
# ---------------------------------------------------------------------------

class TestTierB:
    def test_portfolio_overview_counts(self):
        r = asyncio.run(call("get_portfolio_overview")())
        ov = r[0]
        assert ov["repositories"] == 1 and ov["files"] == 3
        assert ov["programs"] == 3 and ov["methods"] == 6
        assert ov["parserErrors"] == 0
        assert ov["totalDependencies"] == 2  # the two imports of OrderService

    def test_portfolio_files(self):
        r = asyncio.run(call("get_portfolio_files")())
        files = r[0]["files"]
        assert len(files) == 3
        assert all(f["definedTypes"] >= 1 for f in files)

    def test_portfolio_assets(self):
        r = asyncio.run(call("get_portfolio_assets")())
        assert r[0]["programs"] == 3 and r[0]["jclJobs"] == 0

    def test_complex_programs(self):
        r = asyncio.run(call("get_complex_programs")())
        progs = r[0]["programs"]
        assert progs[0]["program"] == "OrderService"  # most methods + calls
        assert all("kind" in p for p in progs)

    def test_program_export_shape(self):
        r = asyncio.run(call("get_program_export")(programName="OrderService"))
        exp = r[0]
        assert exp["fqn"] == "org.demo.OrderService"
        assert len(exp["methods"]) == 4 and len(exp["fields"]) == 2
        assert {"rel": "EXTENDS", "target": "org.demo.AbstractService"} in exp["bases"]
        assert {"target": "org.demo.OrderRepository"} in exp["imports"]

    def test_paragraphs_have_accurate_spans(self):
        r = asyncio.run(call("get_program_paragraphs")(programName="OrderService"))
        spans = {p["name"]: p for p in r[0]["paragraphs"]}
        # validate() starts at line 16 per the fixture source
        assert spans["validate"]["startLine"] == 16
        assert spans["validate"]["endLine"] >= spans["validate"]["startLine"]

    def test_paragraph_details_returns_code(self):
        r = asyncio.run(call("get_program_paragraph_details")(
            programName="OrderService", paragraphName="validate"))
        d = r[0]
        assert d["name"] == "validate"
        assert "IllegalArgumentException" in d["code"]

    def test_control_flow_graph_internal_edges(self):
        r = asyncio.run(call("get_control_flow_graph")(
            programName="OrderService", packageName="org.demo"))
        cfg = r[0]
        assert {"from": "process", "to": "validate", "type": "CALL"} in cfg["edges"]
        # external calls (save/info) are not intra-program edges
        assert not any(e["to"] == "info" for e in cfg["edges"])

    def test_portfolio_call_chain_paged(self):
        r = asyncio.run(call("get_portfolio_call_chain")(page=0, pageSize=50))
        page = r[0]
        assert page["totalEdges"] == 1
        assert page["edges"][0]["from"] == "OrderService"
        assert page["hasNext"] is False
        types = {n["name"]: n["type"] for n in page["nodes"]}
        assert types["OrderService"] == "entry_point"

    def test_find_portfolio_call_paths(self):
        r = asyncio.run(call("find_portfolio_call_paths")(
            fromProgram="OrderService", toProgram="OrderRepository",
            minDepth=1, maxDepth=3))
        assert r[0]["paths"] == [["OrderService", "OrderRepository"]]

    def test_ibmi_usage_resolves_imports(self):
        r = asyncio.run(call("get_ibmi_usage")(programName="OrderService"))
        usage = {u["name"]: u for u in r[0]["usage"]}
        assert usage["org.demo.OrderRepository"]["resolved"] is True
        assert usage["java.util.List"]["resolved"] is False
        assert all(u["accessMode"] == "IMPORT" for u in usage.values())

    def test_lineage_sources(self):
        r = asyncio.run(call("list_lineage_sources")(
            programName="OrderService", includeInternal=True))
        srcs = r[0]["sources"]
        assert {"relation": "EXTENDS", "source": "org.demo.AbstractService",
                "internal": True} in srcs

    def test_lineage_targets_derived(self):
        r = asyncio.run(call("list_lineage_targets")(
            programName="AbstractService", includeInternal=True))
        assert {"relation": "DERIVED", "target": "org.demo.OrderService",
                "internal": True} in r[0]["targets"]

    def test_trace_target_lineage(self):
        r = asyncio.run(call("trace_target_lineage")(
            targetName="org.demo.OrderService", direction="backward", maxDepth=3))
        assert r[0]["edges"] == [{"from": "org.demo.OrderService",
                                  "to": "org.demo.AbstractService", "depth": 1}]
        assert r[0]["certainty"] == "DEFINITE"

    def test_data_structures_accurate_types(self):
        r = asyncio.run(call("list_data_structures")(programName="OrderService"))
        ds = {d["name"]: d for d in r[0]["structures"]}
        assert ds["repo"]["declaredType"] == "OrderRepository"
        assert ds["counter"]["declaredType"] == "int"

    def test_data_structure_details(self):
        r = asyncio.run(call("get_data_structure_details")(
            programName="OrderService", structureName="counter"))
        assert r[0]["declaredType"] == "int"

    def test_sql_family_returns_documented_empty_shapes(self):
        assert asyncio.run(call("get_portfolio_sql_tables")())[0]["tables"] == []
        assert asyncio.run(call("get_sql_table_usage")(tableName="ANY"))[0]["usage"] == []
        assert asyncio.run(call("get_sql_table_structure")(tableName="ANY"))[0]["columns"] == []
        assert asyncio.run(call("get_table_column_usage")(tableName="ANY"))[0]["columns"] == []
        assert asyncio.run(call("get_table_lineage")(tableName="ANY"))[0]["lineage"] == []
        assert asyncio.run(call("get_program_sql_statements")(
            programName="OrderService"))[0]["statements"] == []


# ---------------------------------------------------------------------------
# Tier C — computed or honestly-empty
# ---------------------------------------------------------------------------

class TestTierC:
    def test_dead_paragraphs_finds_uncalled_methods(self):
        r = asyncio.run(call("get_program_dead_paragraphs")(
            programName="OrderService"))
        names = [d["name"] for d in r[0]["deadParagraphs"]]
        assert "validate" not in names   # called by process()
        assert "cleanup" not in names    # called by close()
        assert "process" in names        # no caller anywhere in the portfolio

    def test_call_chain_statistics_not_broken(self):
        r = asyncio.run(call("get_portfolio_call_chain_statistics")())
        stats = r[0]
        assert stats["totalEdges"] >= 1
        assert all(m["name"] and m["callCount"] > 0
                   for m in stats["mostCalledModules"])  # no empty names (MD defect)

    def test_ibmi_usage_portfolio_aggregates(self):
        r = asyncio.run(call("get_ibmi_usage_portfolio")())
        assert r[0]["fileCount"] == 3  # not zero (MD defect)

    def test_outputs_and_legacy_lineage_empty(self):
        assert asyncio.run(call("get_program_outputs")(
            programName="OrderService"))[0]["outputs"] == {}
        assert asyncio.run(call("get_field_lineage")(
            fieldName="repo", programName="OrderService"))[0]["transformations"] == []
        assert asyncio.run(call("trace_field_lineage")(
            fieldName="repo", programName="OrderService"))[0]["lineage"] == []
        assert asyncio.run(call("trace_output_structure")(
            outputName="ANY"))[0]["structure"] == {}
        s = asyncio.run(call("get_lineage_summary")(programName="OrderService"))[0]
        assert "note" in s and s["sources"] == 2

    def test_capabilities_empty(self):
        assert asyncio.run(call("get_business_capabilities")())[0]["capabilities"] == []
        r = asyncio.run(call("get_programs_by_capabilities")(
            capabilities=["billing"]))
        assert r[0]["programs"] == []

    def test_modernization_tools_computed(self):
        recs = asyncio.run(call("recommend_modernization_start")(limit=2))[0]["recommendations"]
        assert 1 <= len(recs) <= 2
        assert all("score" in r and r["score"] >= 0 for r in recs)
        impact = asyncio.run(call("analyze_migration_impact")(
            programName="OrderService"))[0]
        assert impact["fileCount"] == 2          # not zero (MD defect)
        assert impact["unresolvedDependencies"] == 1  # java.util.List unresolved
        # rule: LOW = 0 unresolved, MEDIUM <= 20, HIGH > 20
        assert impact["risk"] == "MEDIUM"

    @pytest.mark.parametrize("tool,args,empty_key", [
        ("get_cics_usage", {"programName": "X"}, "transactions"),
        ("get_cics_usage_portfolio", {}, "programs"),
        ("get_cics_transactions", {}, "transactions"),
        ("get_ims_usage", {"programName": "X"}, "databases"),
        ("get_ims_databases", {}, "databases"),
        ("get_portfolio_ims_segments", {}, "segments"),
        ("get_mq_usage_program", {"programName": "X"}, "queues"),
        ("get_mq_usage_portfolio", {}, "programs"),
        ("list_portfolio_jcl_jobs", {}, "jobs"),
        ("search_jcl_source", {"searchText": "anything"}, "hits"),
    ])
    def test_platform_specific_tools_are_empty(self, tool, args, empty_key):
        r = asyncio.run(call(tool)(**args))
        assert r[0][empty_key] == []

    def test_jcl_details_not_found(self):
        for tool in ("get_jcl_job_execution_details", "get_jcl_job_io",
                     "get_jcl_bottom_up_file_analysis",
                     "map_jcl_job_files_to_cobol_data_structures"):
            r = asyncio.run(call(tool)(jobName="JOB1"))
            assert "error" in r[0]

    def test_cobol_ibm_to_iso_refuses(self):
        r = asyncio.run(call("cobol_ibm_to_iso")(programName="X"))
        assert "COBOL only" in r[0]["error"]


# ---------------------------------------------------------------------------
# Mutating tools — local annotation store
# ---------------------------------------------------------------------------

class TestMutating:
    def test_summary_roundtrip(self):
        asyncio.run(call("update_program_summary")(
            programName="OrderService", summary="Handles order processing."))
        r = asyncio.run(call("get_program_summary")(programName="OrderService"))
        assert r[0]["summary"] == "Handles order processing."
        assert r[0]["source"] == "stored"

    def test_prd_roundtrip(self):
        asyncio.run(call("update_program_prd_documentation")(
            programName="OrderService", documentation="PRD v2 content"))
        r = asyncio.run(call("get_program_prd")(programName="OrderService"))
        assert r[0]["documentation"] == "PRD v2 content"

    def test_classification_roundtrip(self):
        asyncio.run(call("update_program_classification")(
            programName="OrderRepository", classification="DOMAIN"))
        stored = server.B.get_annotation("OrderRepository", "classification")
        assert stored == "DOMAIN"

    def test_auto_summary_when_no_annotation(self):
        r = asyncio.run(call("get_program_summary")(programName="AbstractService"))
        assert r[0]["source"] == "auto-generated"
        assert "class" in r[0]["summary"]


# ---------------------------------------------------------------------------
# MCP protocol-level smoke test (list tools through the protocol)
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_list_tool_names_over_protocol(self):
        names = asyncio.run(server.list_tool_names())
        assert len(names) == 64
        assert set(names) == set(EXPECTED_TOOLS)

    def test_call_tool_through_protocol(self):
        async def run():
            return _unwrap(await server.mcp.call_tool(
                "search_program", {"programName": "OrderRepository"}))
        r = asyncio.run(run())
        assert r[0]["count"] >= 1


# ---------------------------------------------------------------------------
# Fast portfolio search (ripgrep path + parallel fallback)
# ---------------------------------------------------------------------------

class TestFastPortfolioSearch:
    def test_parallel_fallback_matches_shape(self, monkeypatch):
        monkeypatch.setattr(server.back, "find_rg", lambda: None)
        r = asyncio.run(call("search_code_portfolio")(searchText="package org.demo"))
        hits = r[0]["hits"]
        assert r[0]["count"] == 3  # one package line per fixture file
        assert all(set(h) == {"program", "repo", "file", "line", "text"} for h in hits)

    def test_per_program_cap_respected(self, monkeypatch):
        monkeypatch.setattr(server.back, "find_rg", lambda: None)
        r = asyncio.run(call("search_code_portfolio")(
            searchText="org.demo", maxResultsPerProgram=1))
        programs = [h["program"] for h in r[0]["hits"]]
        assert len(programs) == len(set(programs))  # at most one hit per program

    def test_rg_path_when_available(self):
        import backend as back
        rg = back.find_rg()
        if not rg:
            pytest.skip("ripgrep not available")
        hits = server.B.search_portfolio_text("OrderRepository", 0)
        assert hits and all("line" in h and "text" in h for h in hits)
        assert any(h["program"] == "OrderService" for h in hits)

    def test_find_rg_discovers_bundled_candidate(self):
        # find_rg must return a path (PATH or the known bundled location)
        assert server.back.find_rg() is not None or shutil.which("rg") is None


# ---------------------------------------------------------------------------
# Drift detection (sha256 per File)
# ---------------------------------------------------------------------------

class TestDriftDetection:
    def test_fresh_fixture_reports_match(self):
        r = asyncio.run(call("get_program_source_code")(
            programName="OrderService", startLine=1, endLine=3))
        assert r[0]["fileHashMatch"] is True
        r = asyncio.run(call("search_code")(
            programName="OrderService", searchText="process"))
        assert r[0]["fileHashMatch"] is True
        r = asyncio.run(call("get_program_paragraph_details")(
            programName="OrderService", paragraphName="validate"))
        assert r[0]["fileHashMatch"] is True

    def test_modified_file_reports_drift(self):
        fixture = Path(server.B.root) / "src" / "org" / "demo" / "OrderService.java"
        original = fixture.read_text()
        try:
            fixture.write_text("// local edit\n" + original)
            r = asyncio.run(call("get_program_source_code")(
                programName="OrderService", startLine=1, endLine=2))
            assert r[0]["fileHashMatch"] is False
            r = asyncio.run(call("search_code")(
                programName="OrderService", searchText="process"))
            assert r[0]["fileHashMatch"] is False
        finally:
            fixture.write_text(original)

    def test_stale_hash_covers_exact_read_text(self):
        # stored hash == sha256 of exactly the text the backend reads
        state = server.B.source_state("OrderService")
        text = (Path(server.B.root) / state["file"]).read_text(
            encoding="utf-8", errors="replace")
        assert server.back.sha256_text(text) == server.B.types[
            "org.demo.OrderService"]["sha256"]


# ---------------------------------------------------------------------------
# Extension tools — inventory & code statistics
# ---------------------------------------------------------------------------

class TestExtensionTools:
    def test_file_statistics_shape(self):
        r = asyncio.run(call("get_portfolio_file_statistics")())
        stats = r[0]["repositories"][0]
        assert stats["filesInGraph"] == 9
        assert stats["deep"] == 4
        assert stats["binaries"] == 1
        cov = stats["coverage"]
        assert cov["filesTracked"] == 9 and cov["untrackedFiles"] == 0

    def test_file_categories_and_scoping(self):
        r = asyncio.run(call("get_file_categories")())
        rows = {row["category"]: row for row in r[0]["rows"]}
        assert rows["java_backend"]["files"] == 3
        assert rows["java_test"]["files"] == 1
        assert r[0]["totalFiles"] == 9
        # path scoping
        r = asyncio.run(call("get_file_categories")(pathPrefix="src/org/demo"))
        assert r[0]["totalFiles"] == 4
        # kind scoping
        r = asyncio.run(call("get_file_categories")(kind="source"))
        assert all(row["kind"] == "source" for row in r[0]["rows"])

    def test_loc_statistics_rows_and_totals(self):
        r = asyncio.run(call("get_loc_statistics")())
        data = r[0]
        rows = {row["category"]: row for row in data["rows"]}
        assert rows["java_backend"]["totalLines"] == 36  # 25+7+4
        assert data["totals"]["files"] == 9
        assert data["totals"]["totalLines"] == sum(
            row["totalLines"] for row in data["rows"])
        # binaries excluded from LOC totals
        assert data["totals"]["totalLines"] == 100
        # category filter
        r = asyncio.run(call("get_loc_statistics")(category="java_test"))
        assert r[0]["totals"]["files"] == 1

    def test_file_details(self):
        r = asyncio.run(call("get_file_details")(path="pom.xml",
                                                 repository="demo"))
        d = r[0]
        assert d["category"] == "build_script" and d["deepIndexed"] is False
        assert d["totalLines"] == 30

    def test_file_details_not_found(self):
        r = asyncio.run(call("get_file_details")(path="nope.java",
                                                 repository="demo"))
        assert "error" in r[0]
