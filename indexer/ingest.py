# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Stage 4: Neo4j ingest — batched UNWIND Cypher with idempotent MERGE.

Ported from java-graph's index_to_neo4j.py (frozen at
../reference/java-indexer/): same constants (INGEST_CHUNK_SIZE = 5000), same
constraints, same MERGE patterns — the portfolio schema is a contract (D7).
Differences, all additive:
  - rows arrive as a (table, row) STREAM (exporter.RepoExport.rows()), not a
    parse result in RAM; tables are flushed in ingest order as they complete
  - :File rows carry totalLines/bytes (java-graph's inventory phase did the
    same on the same nodes)
  - :CallTarget gets external (true for unresolved callees the exporter
    synthesized, design.md §5); nothing downstream reads it
"""

import logging
from typing import Any, Dict, Iterable, Iterator, List, Tuple

log = logging.getLogger("indexer.ingest")

INGEST_CHUNK_SIZE = 5000  # rows per UNWIND batch (java-graph constant)

# ingest order = dependencies (files before types, types before members, …)
TABLE_ORDER = ("files", "packages", "types", "methods", "fields", "extends",
               "implements", "package_imports", "type_imports", "calls")

CONSTRAINTS = [
    "CREATE CONSTRAINT repo_name_uniq IF NOT EXISTS FOR (r:Repository) REQUIRE r.name IS UNIQUE;",
    "CREATE CONSTRAINT file_id_uniq IF NOT EXISTS FOR (f:File) REQUIRE f.id IS UNIQUE;",
    "CREATE CONSTRAINT package_fqn_uniq IF NOT EXISTS FOR (p:Package) REQUIRE p.fqn IS UNIQUE;",
    "CREATE CONSTRAINT type_fqn_uniq IF NOT EXISTS FOR (t:Type) REQUIRE t.fqn IS UNIQUE;",
    "CREATE CONSTRAINT method_id_uniq IF NOT EXISTS FOR (m:Method) REQUIRE m.id IS UNIQUE;",
    "CREATE CONSTRAINT field_fqn_uniq IF NOT EXISTS FOR (fl:Field) REQUIRE fl.fqn IS UNIQUE;",
    "CREATE CONSTRAINT call_target_uniq IF NOT EXISTS FOR (c:CallTarget) REQUIRE c.name IS UNIQUE;",
]

# Cypher per table — java-graph's statements, verbatim except the additive
# SETs noted in the module docstring.
CYPHER = {
    "files": """
        UNWIND $batch AS row
        MATCH (r:Repository {name: row.repo})
        MERGE (f:File {id: row.id})
        SET f.path = row.path, f.sha256 = row.sha256,
            f.totalLines = row.totalLines, f.bytes = row.bytes,
            f.extension = row.extension, f.kind = row.kind,
            f.category = row.category, f.role = row.role,
            f.codeLines = row.codeLines, f.commentLines = row.commentLines,
            f.blankLines = row.blankLines
        MERGE (r)-[:CONTAINS]->(f)
    """,
    "packages": """
        UNWIND $batch AS row
        MATCH (r:Repository {name: $repo})
        MERGE (p:Package {fqn: row.fqn})
        MERGE (r)-[:CONTAINS]->(p)
    """,
    "types": """
        UNWIND $batch AS row
        MERGE (p:Package {fqn: row.package})
        MERGE (t:Type {fqn: row.fqn})
        SET t.name = row.name, t.kind = row.kind, t.line = row.line,
            t.modifiers = row.modifiers, t.external = row.external
        MERGE (p)-[:CONTAINS]->(t)
        WITH t, row
        MATCH (f:File {id: row.file_id})
        MERGE (f)-[:DEFINES]->(t)
    """,
    "methods": """
        UNWIND $batch AS row
        MATCH (t:Type {fqn: row.type_fqn})
        MERGE (m:Method {id: row.id})
        SET m.name = row.name, m.arity = row.arity,
            m.return_type = row.return_type, m.modifiers = row.modifiers,
            m.line = row.line
        MERGE (t)-[:CONTAINS]->(m)
    """,
    "fields": """
        UNWIND $batch AS row
        MATCH (t:Type {fqn: row.type_fqn})
        MERGE (fl:Field {fqn: row.fqn})
        SET fl.name = row.name, fl.type = row.type,
            fl.modifiers = row.modifiers, fl.line = row.line
        MERGE (t)-[:CONTAINS]->(fl)
    """,
    "extends": """
        UNWIND $batch AS row
        MATCH (t:Type {fqn: row.fqn})
        MERGE (tt:Type {fqn: row.target})
        SET tt.external = coalesce(tt.external, true)
        MERGE (t)-[:EXTENDS]->(tt)
    """,
    "implements": """
        UNWIND $batch AS row
        MATCH (t:Type {fqn: row.fqn})
        MERGE (tt:Type {fqn: row.target})
        SET tt.external = coalesce(tt.external, true)
        MERGE (t)-[:IMPLEMENTS]->(tt)
    """,
    "package_imports": """
        UNWIND $batch AS row
        MATCH (f:File {id: row.file_id})
        MERGE (p:Package {fqn: row.target})
        MERGE (f)-[:IMPORTS]->(p)
    """,
    "type_imports": """
        UNWIND $batch AS row
        MATCH (f:File {id: row.file_id})
        MERGE (t:Type {fqn: row.target})
        SET t.external = coalesce(t.external, row.external)
        MERGE (f)-[:IMPORTS]->(t)
    """,
    "calls": """
        UNWIND $batch AS row
        MATCH (m:Method {id: row.method_id})
        MERGE (ct:CallTarget {name: row.callee})
        SET ct.external = coalesce(ct.external, row.external)
        MERGE (m)-[c:CALLS]->(ct)
        SET c.count = row.count
    """,
}


def init_neo4j_schema(driver: Any) -> None:
    with driver.session() as session:
        for q in CONSTRAINTS:
            session.run(q)


def chunks(rows: List[dict], size: int = INGEST_CHUNK_SIZE) -> Iterator[List[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


class _Counts:
    """Unique-entity counters compared against the graph in stage 5 (the
    graph's uniqueness constraints collapse duplicate declarations — same
    FQN in main and test sources — so expected counts must be uniques too)."""

    def __init__(self) -> None:
        self.files: set = set()
        self.types: set = set()
        self.methods: set = set()
        self.fields: set = set()
        self.extends: set = set()
        self.implements: set = set()
        self.imports: set = set()
        self.calls: set = set()
        self.raw: Dict[str, int] = {t: 0 for t in TABLE_ORDER}

    def observe(self, table: str, row: Dict[str, Any]) -> None:
        self.raw[table] += 1
        if table == "files":
            self.files.add(row["id"])
        elif table == "types":
            self.types.add(row["fqn"])
        elif table == "methods":
            self.methods.add(row["id"])
        elif table == "fields":
            self.fields.add(row["fqn"])
        elif table == "extends":
            self.extends.add((row["fqn"], row["target"]))
        elif table == "implements":
            self.implements.add((row["fqn"], row["target"]))
        elif table in ("package_imports", "type_imports"):
            self.imports.add((row["file_id"], row["target"]))
        elif table == "calls":
            self.calls.add((row["method_id"], row["callee"]))

    def expected(self) -> Dict[str, int]:
        exp = {
            "files": len(self.files), "types": len(self.types),
            "methods": len(self.methods), "fields": len(self.fields),
            "extends": len(self.extends), "implements": len(self.implements),
            "imports": len(self.imports), "calls": len(self.calls),
        }
        raw_by_metric: Dict[str, int] = {}
        for table in TABLE_ORDER:
            metric = _METRIC.get(table)
            if metric:
                raw_by_metric[metric] = raw_by_metric.get(metric, 0) + self.raw[table]
        duplicates = {m: raw_by_metric[m] - exp[m]
                      for m in raw_by_metric if raw_by_metric[m] > exp[m]}
        exp["duplicates_collapsed"] = sum(duplicates.values())
        exp["duplicates_by_metric"] = duplicates
        return exp


_METRIC = {"files": "files", "types": "types", "methods": "methods",
           "fields": "fields", "extends": "extends", "implements": "implements",
           "package_imports": "imports", "type_imports": "imports", "calls": "calls"}


def _metric_of(table: str) -> str:
    return _METRIC[table]


def ingest_repo(driver: Any, repo_name: str,
                row_stream: Iterable[Tuple[str, Dict[str, Any]]]) -> Dict[str, int]:
    """Ingest one repo's (table, row) stream; returns expected unique counts."""
    log.info("[*] Ingesting graph into Neo4j: %s...", repo_name)
    counts = _Counts()
    current: str | None = None
    pending: List[dict] = []

    def flush(session: Any, table: str, rows: List[dict]) -> None:
        query = CYPHER[table]
        extra = {"repo": repo_name} if table == "packages" else {}
        for chunk in chunks(rows):
            session.run(query, batch=chunk, **extra)
        log.info("  [*] %-16s %d rows", table, len(rows))

    with driver.session() as session:
        session.run("MERGE (r:Repository {name: $name})", name=repo_name)
        for table, row in row_stream:
            counts.observe(table, row)
            if table not in CYPHER:
                raise ValueError(f"unknown ingest table {table!r}")
            if table != current:
                if current is not None:
                    flush(session, current, pending)
                current, pending = table, []
            pending.append(row)
        if current is not None:
            flush(session, current, pending)

    exp = counts.expected()
    if exp["duplicates_collapsed"]:
        log.warning("  [!] %d duplicate declarations collapse under graph "
                    "uniqueness constraints: %s", exp["duplicates_collapsed"],
                    exp["duplicates_by_metric"])
    log.info("  [+] Ingested %d files, %d types, %d methods, %d fields, "
             "%d extends, %d implements, %d imports, %d call edges.",
             exp["files"], exp["types"], exp["methods"], exp["fields"],
             exp["extends"], exp["implements"], exp["imports"], exp["calls"])
    return exp


# ---------------------------------------------------------------------------
# inventory snapshots (java-graph's StatsSnapshot pattern, retention: 2)
# ---------------------------------------------------------------------------

def ingest_stats_snapshot(driver: Any, repo_name: str, run_id: str,
                          inventory: Dict[str, Any]) -> None:
    """Materialize (:StatsSnapshot)-[:COVERS]->(Repository) with coverage
    props and (:CategoryStat) rollups; prune beyond the last 2 snapshots."""
    from datetime import datetime, timezone
    coverage = inventory["coverage"]
    sid = f"{run_id}:{repo_name}"
    cat_rows = inventory["categories"]
    with driver.session() as session:
        session.run("""
            MATCH (r:Repository {name: $name})
            CREATE (s:StatsSnapshot {sid: $sid, runId: $runId, at: $at})
            CREATE (s)-[:COVERS $cov]->(r)
        """, name=repo_name, sid=sid, runId=run_id,
            at=datetime.now(timezone.utc).isoformat(), cov=coverage)
        for chunk in chunks(cat_rows, 200):
            session.run("""
                UNWIND $batch AS row
                MATCH (s:StatsSnapshot {sid: $sid})
                CREATE (cs:CategoryStat {
                    kind: row.kind, category: row.category, files: row.files,
                    bytes: row.bytes, totalLines: row.totalLines,
                    codeLines: row.codeLines, commentLines: row.commentLines,
                    blankLines: row.blankLines})
                CREATE (s)-[:HAS_STAT]->(cs)
            """, batch=chunk, sid=sid)
        session.run("""
            MATCH (s:StatsSnapshot)-[:COVERS]->(:Repository {name: $repo})
            WITH s ORDER BY s.at DESC SKIP 2
            OPTIONAL MATCH (s)-[:HAS_STAT]->(cs)
            DETACH DELETE cs, s
        """, repo=repo_name)
    log.info("  [+] StatsSnapshot %s: %d categories; coverage: %s", sid,
             len(cat_rows),
             ", ".join(f"{k}={v}" for k, v in coverage.items()))


def verify_inventory(driver: Any, repo_name: str,
                     inventory: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot verification (java-graph rule): file counts match the graph,
    category sums match the tracked total."""
    coverage = inventory["coverage"]
    with driver.session() as session:
        files_in_graph = session.run(
            "MATCH (:Repository {name: $n})-[:CONTAINS]->(f:File) "
            "RETURN count(f) AS c", n=repo_name).single()["c"]
        cat_files_sum = session.run("""
            MATCH (s:StatsSnapshot)-[:COVERS]->(:Repository {name: $n})
            WITH s ORDER BY s.at DESC LIMIT 1
            MATCH (s)-[:HAS_STAT]->(cs)
            RETURN sum(cs.files) AS c
        """, n=repo_name).single()["c"]
    result = {
        "filesTracked": coverage["filesTracked"],
        "filesInGraph": files_in_graph,
        "filesMatch": files_in_graph == coverage["filesTracked"],
        "categorySumsMatch": cat_files_sum == coverage["filesTracked"],
    }
    ok = result["filesMatch"] and result["categorySumsMatch"]
    log.info("    %s inventory files=%d/%d category-sums=%s",
             "OK " if ok else "MISMATCH", files_in_graph,
             coverage["filesTracked"], result["categorySumsMatch"])
    return result


# ---------------------------------------------------------------------------
# cross-repo integrations (ADDITIVE to the D7 schema; evidence-derived)
# ---------------------------------------------------------------------------

INTEGRATES_CYPHER = """
    UNWIND $batch AS row
    MATCH (a:Repository {name: $src})
    MATCH (b:Repository {name: row.target})
    MERGE (a)-[r:INTEGRATES]->(b)
    SET r.refs = row.refs, r.files = row.files, r.samples = row.samples
"""


def ingest_integrations(driver: Any, repo_name: str,
                        integrations: List[Dict[str, Any]]) -> int:
    """(:Repository)-[:INTEGRATES {refs, files, samples}]->(:Repository),
    derived from cross-repo name references (integrations.py). Returns
    the number of edges written."""
    if not integrations:
        return 0
    import json as _json
    rows = [{**i, "samples": _json.dumps(i["samples"])}  # neo4j props are primitives
            for i in integrations]
    with driver.session() as session:
        session.run(INTEGRATES_CYPHER, batch=rows, src=repo_name)
    log.info("  [+] INTEGRATES from %s: %s", repo_name,
             ", ".join(f"{i['target']} ({i['refs']} refs)"
                       for i in integrations))
    return len(integrations)
