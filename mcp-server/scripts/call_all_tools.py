#!/usr/bin/env python3
"""Protocol-level smoke test: spawn the deployed MCP server (stdio) and call
ALL 60 tools through the MCP protocol, exactly as ZCode will.

Requires the live Neo4j graph. Exits non-zero if any tool fails.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).resolve().parent.parent / "server.py"
PYTHON = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
REPOS = Path(__file__).resolve().parents[2] / "java-indexer" / "repos.json"

PROGRAM = "DeviceController"
PROGRAM2 = "BaseController"

# one valid argument set per tool (Tier order as in the inventory)
CALLS = [
    # Tier A
    ("search_program", {"programName": PROGRAM}),
    ("search_component", {"prefix": "Device"}),
    ("get_program_source_code", {"programName": PROGRAM, "startLine": 70, "endLine": 72}),
    ("search_code", {"programName": PROGRAM, "searchText": "process"}),
    ("search_code_portfolio", {"searchText": "RTNMSGTEXT", "maxResultsPerProgram": 2}),
    ("get_program_deep_call_chain", {"programName": PROGRAM2, "maxDepth": 2}),
    # Tier B
    ("get_portfolio_overview", {}),
    ("get_portfolio_files", {}),
    ("get_portfolio_assets", {}),
    ("get_complex_programs", {}),
    ("get_program_summary", {"programName": PROGRAM}),
    ("get_program_prd", {"programName": PROGRAM}),
    ("get_program_export", {"programName": PROGRAM}),
    ("get_program_paragraphs", {"programName": PROGRAM}),
    ("get_program_paragraph_details", {"programName": PROGRAM, "paragraphName": "saveDevice"}),
    ("get_control_flow_graph", {"programName": PROGRAM, "packageName": "org.thingsboard.server.controller"}),
    ("get_portfolio_call_chain", {"page": 0, "pageSize": 10}),
    ("find_portfolio_call_paths", {"fromProgram": PROGRAM2, "toProgram": PROGRAM, "minDepth": 1, "maxDepth": 4}),
    ("get_portfolio_sql_tables", {}),
    ("get_sql_table_usage", {"tableName": "ANY"}),
    ("get_sql_table_structure", {"tableName": "ANY"}),
    ("get_table_column_usage", {"tableName": "ANY"}),
    ("get_table_lineage", {"tableName": "ANY"}),
    ("get_program_sql_statements", {"programName": PROGRAM}),
    ("get_ibmi_usage", {"programName": PROGRAM}),
    ("list_lineage_sources", {"programName": PROGRAM, "includeInternal": True}),
    ("list_lineage_targets", {"programName": PROGRAM2, "includeInternal": True}),
    ("trace_target_lineage", {"targetName": "org.thingsboard.server.controller.BaseController", "direction": "backward", "maxDepth": 2}),
    ("list_data_structures", {"programName": PROGRAM}),
    ("get_data_structure_details", {"programName": PROGRAM, "structureName": "DEVICE_NAME"}),
    # Tier C
    ("get_program_dead_paragraphs", {"programName": PROGRAM}),
    ("get_portfolio_call_chain_statistics", {}),
    ("get_ibmi_usage_portfolio", {}),
    ("get_program_outputs", {"programName": PROGRAM}),
    ("get_lineage_summary", {"programName": PROGRAM}),
    ("get_field_lineage", {"fieldName": "log", "programName": PROGRAM}),
    ("trace_field_lineage", {"fieldName": "log", "programName": PROGRAM}),
    ("trace_output_structure", {"outputName": "ANY"}),
    ("get_business_capabilities", {}),
    ("get_programs_by_capabilities", {"capabilities": ["orders"]}),
    ("recommend_modernization_start", {"limit": 3}),
    ("analyze_migration_impact", {"programName": PROGRAM}),
    ("get_cics_usage", {"programName": PROGRAM}),
    ("get_cics_usage_portfolio", {}),
    ("get_cics_transactions", {}),
    ("get_ims_usage", {"programName": PROGRAM}),
    ("get_ims_databases", {}),
    ("get_portfolio_ims_segments", {}),
    ("get_mq_usage_program", {"programName": PROGRAM}),
    ("get_mq_usage_portfolio", {}),
    ("list_portfolio_jcl_jobs", {}),
    ("get_jcl_job_execution_details", {"jobName": "JOB1"}),
    ("get_jcl_job_io", {"jobName": "JOB1"}),
    ("get_jcl_bottom_up_file_analysis", {"jobName": "JOB1"}),
    ("map_jcl_job_files_to_cobol_data_structures", {"jobName": "JOB1"}),
    ("search_jcl_source", {"searchText": "anything"}),
    ("cobol_ibm_to_iso", {"programName": PROGRAM}),
    # Mutating (scratch program name; verifies the local annotation store)
    ("update_program_summary", {"programName": "__smoke__", "summary": "protocol smoke"}),
    ("update_program_prd_documentation", {"programName": "__smoke__", "documentation": "smoke"}),
    ("update_program_classification", {"programName": "__smoke__", "classification": "SMOKE"}),
    # Extension: inventory & code statistics
    ("get_portfolio_file_statistics", {}),
    ("get_file_categories", {"pathPrefix": "application/src/main/java"}),
    ("get_loc_statistics", {"category": "java_backend"}),
    ("get_file_details", {"path": "pom.xml", "repository": "thingsboard"}),
]


def parse_result(result):
    """Return (ok, payload) from a CallToolResult."""
    if getattr(result, "isError", False):
        return False, {"protocolError": True}
    content = getattr(result, "content", [])
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return True, structured
    if content and getattr(content[0], "text", None) is not None:
        try:
            return True, json.loads(content[0].text)
        except json.JSONDecodeError:
            return True, {"raw": content[0].text[:100]}
    return False, {"empty": True}


async def main():
    params = StdioServerParameters(
        command=str(PYTHON), args=[str(SERVER)],
        env={
            "MCP_NEO4J_URI": "bolt://localhost:7687",
            "MCP_NEO4J_PASSWORD": "graphpassword123",
            "MCP_REPOS_JSON": str(REPOS),
        },
    )
    failures = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"connected: server={info.serverInfo.name} "
                  f"protocol={info.protocolVersion}")

            listed = await session.list_tools()
            names = sorted(t.name for t in listed.tools)
            print(f"tools/list: {len(names)} tools")
            expected = sorted(n for n, _ in CALLS)
            if names != expected:
                missing = set(expected) - set(names)
                extra = set(names) - set(expected)
                failures.append(("tools/list",
                                 {"missing": sorted(missing), "extra": sorted(extra)}))

            for i, (name, args) in enumerate(CALLS, 1):
                try:
                    result = await session.call_tool(name, args)
                    ok, payload = parse_result(result)
                except Exception as e:  # protocol/transport level failure
                    ok, payload = False, {"exception": str(e)[:120]}
                status = "PASS" if ok else "FAIL"
                note = ""
                if ok and isinstance(payload, dict):
                    # tiny per-tool sanity for a few key tools
                    if name == "get_portfolio_overview":
                        note = f"repos={payload.get('repositories')} files={payload.get('files')}"
                    elif name == "get_program_source_code":
                        note = f"hashMatch={payload.get('fileHashMatch')}"
                    elif name == "search_code_portfolio":
                        note = f"hits={payload.get('count')}"
                print(f"  [{i:>2}/{len(CALLS)}] {status} {name} {note}")
                if not ok:
                    failures.append((name, payload))

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        for name, payload in failures:
            print(f"  {name}: {payload}")
        sys.exit(1)
    print(f"ALL {len(CALLS)} TOOLS PASSED over the MCP protocol")


if __name__ == "__main__":
    asyncio.run(main())
