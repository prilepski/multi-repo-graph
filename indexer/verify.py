# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Stage 5: post-ingest verification (java-graph rule: verify every run).

- exact node/edge counts per repo, always (extracted uniques vs graph)
- sampled edge-level checks (design.md §7): N random rows re-queried in the
  graph so verification stays O(sample), not O(graph), at scale
"""

import logging
from typing import Any, Dict, List

log = logging.getLogger("indexer.verify")

GRAPH_METRICS = ["files", "types", "methods", "fields", "extends",
                 "implements", "imports", "calls"]

VERIFY_QUERIES = {
    "files": "MATCH (:Repository {name:$n})-[:CONTAINS]->(f:File) RETURN count(f) AS c",
    "types": """MATCH (:Repository {name:$n})-[:CONTAINS]->(:File)-[:DEFINES]->(t:Type)
                RETURN count(DISTINCT t) AS c""",
    "methods": """MATCH (:Repository {name:$n})-[:CONTAINS]->(:File)-[:DEFINES]->(:Type)-[:CONTAINS]->(m:Method)
                  RETURN count(DISTINCT m) AS c""",
    "fields": """MATCH (:Repository {name:$n})-[:CONTAINS]->(:File)-[:DEFINES]->(:Type)-[:CONTAINS]->(fl:Field)
                 RETURN count(DISTINCT fl) AS c""",
    "extends": """MATCH (:Repository {name:$n})-[:CONTAINS]->(:File)-[:DEFINES]->(t:Type)-[e:EXTENDS]->()
                  RETURN count(DISTINCT e) AS c""",
    "implements": """MATCH (:Repository {name:$n})-[:CONTAINS]->(:File)-[:DEFINES]->(t:Type)-[e:IMPLEMENTS]->()
                     RETURN count(DISTINCT e) AS c""",
    "imports": "MATCH (:Repository {name:$n})-[:CONTAINS]->(f:File)-[i:IMPORTS]->() RETURN count(i) AS c",
    "calls": """MATCH (:Repository {name:$n})-[:CONTAINS]->(:File)-[:DEFINES]->(:Type)-[:CONTAINS]->(:Method)-[c:CALLS]->()
                RETURN count(DISTINCT c) AS c""",
}


def verify_repo_in_graph(driver: Any, repo_name: str) -> Dict[str, int]:
    actual: Dict[str, int] = {}
    with driver.session() as session:
        for metric, query in VERIFY_QUERIES.items():
            actual[metric] = session.run(query, n=repo_name).single()["c"]
    return actual


def diff_counts(expected: Dict[str, int], actual: Dict[str, int]) -> Dict[str, dict]:
    return {
        m: {"expected": expected.get(m, 0), "actual": actual.get(m, 0),
            "ok": expected.get(m, 0) == actual.get(m, 0)}
        for m in GRAPH_METRICS
    }


def verify_samples(driver: Any, samples: Dict[str, List[dict]]) -> Dict[str, Any]:
    """Edge-level spot checks over the exporter's reservoir sample.

    Note the imports/extends counts query semantics: IMPORTS edges attach to
    files of the repo, but an IMPORTS target Type row may point at a type the
    file also DEFINES (internal import) — these queries count what actually
    landed, which is what the count diff compares against.
    """
    failures: List[str] = []
    checked = 0
    with driver.session() as session:
        for row in samples.get("types", []):
            checked += 1
            hit = session.run(
                "MATCH (f:File {id: $fid})-[:DEFINES]->(t:Type {fqn: $fqn}) "
                "RETURN count(t) AS c", fid=row["file_id"], fqn=row["fqn"]
            ).single()["c"]
            if hit != 1:
                failures.append(f"DEFINES missing: {row['file_id']} -> {row['fqn']}")
        for row in samples.get("methods", []):
            checked += 1
            hit = session.run(
                "MATCH (t:Type {fqn: $tf})-[:CONTAINS]->(m:Method {id: $mid}) "
                "RETURN count(m) AS c", tf=row["type_fqn"], mid=row["id"]).single()["c"]
            if hit != 1:
                failures.append(f"Method/CONTAINS missing: {row['id']}")
        for edge_kind in ("extends", "implements"):
            for row in samples.get(edge_kind, []):
                checked += 1
                hit = session.run(
                    f"MATCH (:Type {{fqn: $f}})-[:{edge_kind.upper()}]->(tt:Type {{fqn: $t}}) "
                    "RETURN count(*) AS c", f=row["fqn"], t=row["target"]).single()["c"]
                if hit != 1:
                    failures.append(f"{edge_kind.upper()} missing: {row['fqn']} -> {row['target']}")
        for row in samples.get("calls", []):
            checked += 1
            hit = session.run(
                "MATCH (m:Method {id: $mid})-[c:CALLS]->(ct:CallTarget {name: $n}) "
                "RETURN c.count AS cnt", mid=row["method_id"], n=row["callee"]).single()
            if hit is None or hit["cnt"] != row["count"]:
                failures.append(
                    f"CALLS mismatch: {row['method_id']} -> {row['callee']} "
                    f"(count {None if hit is None else hit['cnt']} != {row['count']})")
    log.info("  [+] Sampled edge checks: %d checked, %d failed", checked, len(failures))
    return {"checked": checked, "failures": failures, "ok": not failures}
