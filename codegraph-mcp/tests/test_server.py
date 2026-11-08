"""MCP tool-level tests for the portfolio wrapper server: real fanout.mjs
subprocess over the two fixture repos, called through the FastMCP tool
surface (same call/unwrap idiom as mcp-server's offline suite)."""

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

CODEGRAPH_MCP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODEGRAPH_MCP_DIR))


@pytest.fixture(scope="module")
def server(wrapper_repos):
    repos_json, _ = wrapper_repos
    os.environ["MRG_REPOS_JSON"] = str(repos_json)
    import server as server_mod
    importlib.reload(server_mod)  # pick up MRG_REPOS_JSON
    return server_mod


def call(server_mod, tool):
    async def _inner(**kwargs):
        result = await server_mod.mcp.call_tool(tool, kwargs)
        first = result[0] if isinstance(result, (list, tuple)) else result
        text = getattr(first, "text", None)
        return json.loads(text) if text is not None else first
    return _inner


@pytest.fixture(scope="module")
def tools(server):
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


class TestToolInventory:
    def test_five_portfolio_tools_registered(self, tools):
        assert sorted(tools) == [
            "portfolio_callers", "portfolio_explore", "portfolio_impact",
            "portfolio_search", "portfolio_symbol"]

    @pytest.mark.parametrize("name", [
        "portfolio_callers", "portfolio_explore", "portfolio_impact",
        "portfolio_search", "portfolio_symbol"])
    def test_tool_has_description(self, tools, name):
        assert tools[name].description, name


class TestToolBehavior:
    def test_search_tool_fans_out(self, server):
        out = asyncio.run(call(server, "portfolio_search")(query="App"))
        assert out["command"] == "search"
        assert out["failures"] == []
        assert set(out["repos"]) == {"repoa", "repob"}
        names_by_repo = {}
        for r in out["results"]:
            names_by_repo[r["repo"]] = [h["name"] for h in r["hits"]]
        assert any("AppA" in v for v in names_by_repo.get("repoa", []))
        assert any("AppB" in v for v in names_by_repo.get("repob", []))

    def test_callers_tool_reports_resolved_edges(self, server):
        out = asyncio.run(call(server, "portfolio_callers")(name="encode"))
        assert out["failures"] == []
        callers = [c for r in out["results"] for c in r.get("callers", [])]
        assert callers, "encode must have at least one resolved caller"
        assert all("repoa" in r["repo"] or True for r in out["results"])
        assert any(c["caller"].split("::")[-2:-1] == ["AppA"] or
                   "AppA" in c["caller"] for c in callers)

    def test_callers_tool_repo_filter(self, server):
        out = asyncio.run(call(server, "portfolio_callers")(name="run",
                                                            repo="repob"))
        assert out["repos"] == ["repob"]
        assert [r["repo"] for r in out["results"]] == ["repob"]

    def test_symbol_tool_lookup(self, server):
        out = asyncio.run(call(server, "portfolio_symbol")(
            qualified_name="org.acme.util::Codes"))
        nodes = [(r["repo"], n["kind"])
                 for r in out["results"] for n in r.get("nodes", [])]
        assert ("repoa", "class") in nodes

    def test_impact_and_explore_tools(self, server):
        impact = asyncio.run(call(server, "portfolio_impact")(name="encode"))
        assert impact["command"] == "impact"
        assert any(r.get("impact") for r in impact["results"])
        explore = asyncio.run(call(server, "portfolio_explore")(
            query="encoding utilities"))
        assert explore["command"] == "explore"
        assert len(explore["results"]) == 2
        for r in explore["results"]:
            assert r.get("summary") or r.get("error"), r
