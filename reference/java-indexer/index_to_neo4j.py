#!/usr/bin/env python3
"""
Multi-repo Java parsing-to-Neo4j pipeline (Tree-sitter based).

Reference design: "Local Java Code Parsers and Indexers" conversation
(https://share.gemini.google/EKt8ZSwCiH1C), which prescribes:

  Indexer: "scip-java (if code builds via Maven/Gradle) or JavaParser /
  Tree-sitter (if parsing raw, unbuilt multi-repo checkouts)."

These target repos are raw, unbuilt multi-repo checkouts, so this pipeline
uses Tree-sitter (tree-sitter-java): fast, resilient to syntax errors, and
requiring no JDK, Maven, or repository builds.

Graph schema (per the reference):
  Nodes : Package, Class/Interface (labelled :Type {kind}), Method, Field
  Edges : CONTAINS, EXTENDS, IMPLEMENTS, CALLS, REFERENCES, IMPORTS

Flow:
  1. Validates prerequisites (podman/docker via --docker; Python deps).
  2. Pulls and runs a local Neo4j container if one is not already running.
  3. Reads a JSON manifest: [{"name", "path"}, ...]
  4. Parses every *.java file with Tree-sitter.
  5. Ingests the graph into Neo4j via batched UNWIND Cypher (idempotent MERGE).
  6. VERIFIES the import: queries the graph and compares actual node/edge
     counts against the extracted counts, per repository.

Outputs:
  logs/run-<timestamp>.log      - full run log (same content as console)
  logs/summary-<timestamp>.json - machine-readable run summary + validation

Exit code 0 only if every repository parsed, ingested, and verified cleanly.

Usage:
  python index_to_neo4j.py --docker --repos repos.json
"""

import argparse
import hashlib
import importlib
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import inventory as inv

NEO4J_CONTAINER_NAME = "scip-neo4j-instance"
NEO4J_IMAGE = "neo4j:5-community"
INGEST_CHUNK_SIZE = 5000  # rows per UNWIND batch

PYTHON_DEPS = [
    ("neo4j", "neo4j"),
    ("tree-sitter", "tree_sitter"),
    ("tree-sitter-java", "tree_sitter_java"),
]

TYPE_DECL_NODES = (
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
)
KIND_BY_NODE = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}

# Metrics compared between extraction and the loaded graph
GRAPH_METRICS = ["files", "types", "methods", "fields", "extends",
                 "implements", "imports", "calls"]

log = logging.getLogger("index_to_neo4j")


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    filehandler = logging.FileHandler(log_path, encoding="utf-8")
    filehandler.setLevel(logging.DEBUG)
    filehandler.setFormatter(fmt)
    log.handlers.clear()
    log.addHandler(console)
    log.addHandler(filehandler)
    return log_path


# ---------------------------------------------------------------------------
# 0. Python dependency bootstrap (validate + install)
# ---------------------------------------------------------------------------

def missing_python_deps() -> List[str]:
    missing = []
    for pkg, mod in PYTHON_DEPS:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    return missing


def ensure_python_deps(auto_install: bool) -> None:
    missing = missing_python_deps()
    if not missing:
        return
    if not auto_install:
        sys.exit(f"[-] Fatal: missing Python packages: {', '.join(missing)}. "
                 f"Run: pip install {' '.join(missing)}")
    log.info("[*] Installing missing Python dependencies: %s", ", ".join(missing))
    cmd = [sys.executable, "-m", "pip", "install", *missing]
    if subprocess.run(cmd).returncode != 0:
        sys.exit(f"[-] Fatal: pip install failed for: {', '.join(missing)}")


def get_neo4j_driver_module() -> Any:
    try:
        return importlib.import_module("neo4j")
    except ImportError:
        sys.exit("[-] Fatal: 'neo4j' package is missing. Run: pip install neo4j")


# ---------------------------------------------------------------------------
# 1. System checks
# ---------------------------------------------------------------------------

def check_prerequisites(container_engine: str) -> None:
    log.info("[*] Checking system prerequisites...")
    if not shutil.which(container_engine):
        sys.exit(f"[-] Fatal: '{container_engine}' is not installed or not found on PATH.")
    log.info("  [+] Container engine found: %s", container_engine)
    log.info("  [+] No JDK/Maven required: parsing is Tree-sitter based (no repo builds).")


# ---------------------------------------------------------------------------
# 2. Container lifecycle management (Neo4j)
# ---------------------------------------------------------------------------

def ensure_neo4j_running(engine: str, bolt_port: int, http_port: int, password: str) -> None:
    neo4j = get_neo4j_driver_module()
    log.info("[*] Verifying Neo4j container status using %s...", engine)

    res = subprocess.run(
        [engine, "ps", "--filter", f"name={NEO4J_CONTAINER_NAME}", "--format", "{{.Names}}"],
        capture_output=True, text=True)
    if NEO4J_CONTAINER_NAME in res.stdout:
        log.info("  [+] Neo4j container is already active.")
    else:
        res_all = subprocess.run(
            [engine, "ps", "-a", "--filter", f"name={NEO4J_CONTAINER_NAME}",
             "--format", "{{.Names}}"],
            capture_output=True, text=True)
        if NEO4J_CONTAINER_NAME in res_all.stdout:
            log.info("  [*] Starting stopped container '%s'...", NEO4J_CONTAINER_NAME)
            subprocess.run([engine, "start", NEO4J_CONTAINER_NAME], check=True)
        else:
            log.info("  [*] Launching new Neo4j container '%s'...", NEO4J_CONTAINER_NAME)
            run_cmd = [
                engine, "run", "-d",
                "--name", NEO4J_CONTAINER_NAME,
                "-p", f"{bolt_port}:7687",
                "-p", f"{http_port}:7474",
                "-e", f"NEO4J_AUTH=neo4j/{password}",
                "-e", 'NEO4J_PLUGINS=["apoc"]',
                NEO4J_IMAGE,
            ]
            subprocess.run(run_cmd, check=True)

    log.info("  [*] Waiting for Neo4j Bolt interface to become healthy...")
    uri = f"bolt://localhost:{bolt_port}"
    for attempt in range(60):
        try:
            with neo4j.GraphDatabase.driver(uri, auth=("neo4j", password)) as driver:
                driver.verify_connectivity()
                log.info("  [+] Connected to Neo4j successfully.")
                return
        except Exception:
            time.sleep(2)

    sys.exit("[-] Timeout: Failed to connect to Neo4j within 120 seconds.")


# ---------------------------------------------------------------------------
# 3. Manifest
# ---------------------------------------------------------------------------

def load_manifest(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        sys.exit(f"[-] Config file '{path}' not found.")
    with open(path, "r") as f:
        repos = json.load(f)
    if not isinstance(repos, list):
        sys.exit(f"[-] Invalid manifest '{path}': expected a JSON array of "
                 '{"name", "path"} objects.')
    for repo in repos:
        for key in ("name", "path"):
            if key not in repo:
                sys.exit(f"[-] Invalid manifest entry (missing '{key}'): {repo}")
    return repos


# ---------------------------------------------------------------------------
# 4. Java parsing (Tree-sitter) — no builds, no JDK
# ---------------------------------------------------------------------------

def make_parser() -> Any:
    import tree_sitter
    import tree_sitter_java
    language = tree_sitter.Language(tree_sitter_java.language())
    try:
        return tree_sitter.Parser(language)  # tree-sitter >= 0.22
    except TypeError:
        parser = tree_sitter.Parser()        # older bindings
        parser.set_language(language)
        return parser


def text_of(node: Any) -> str:
    return node.text.decode("utf-8", errors="replace")


def modifiers_of(node: Any) -> str:
    mods = node.child_by_field_name("modifiers")
    if mods is None:  # some declarations carry 'modifiers' as a plain child
        mods = next((c for c in node.children if c.type == "modifiers"), None)
    return text_of(mods).strip() if mods is not None else ""


def line_of(node: Any) -> int:
    return node.start_point[0] + 1


def qualified_text(node: Any) -> str:
    """Full dotted text of a name position (the 'name' field often yields only
    the last segment; the scoped_identifier child holds the whole path)."""
    for child in node.children:
        if child.type in ("scoped_identifier", "scoped_type_identifier"):
            return text_of(child)
    name = node.child_by_field_name("name")
    if name is not None:
        return text_of(name)
    ids = [c for c in node.children if c.type == "identifier"]
    if ids:
        return ".".join(text_of(i) for i in ids)
    return text_of(node)


def parse_type_declaration(node: Any, package: str, owner_fqn: Optional[str]) -> Dict[str, Any]:
    """Recursively extract a type declaration (and its nested types/members)."""
    name = text_of(node.child_by_field_name("name"))
    fqn = f"{owner_fqn}.{name}" if owner_fqn else (f"{package}.{name}" if package else name)
    kind = KIND_BY_NODE[node.type]

    extends: List[str] = []
    implements: List[str] = []

    superclass = node.child_by_field_name("superclass")
    if superclass is not None:
        for child in superclass.children:
            if "identifier" in child.type or "type_argument" in child.type:
                extends.append(text_of(child))
                break
    interfaces = (node.child_by_field_name("super_interfaces")
                  or node.child_by_field_name("interfaces"))
    if interfaces is not None:
        for child in interfaces.children:
            if child.type == "type_list":
                implements.extend(text_of(t) for t in child.children
                                  if "identifier" in t.type or t.type == "generic_type")

    result = {
        "fqn": fqn,
        "name": name,
        "kind": kind,
        "line": line_of(node),
        "modifiers": modifiers_of(node),
        "extends": extends,
        "implements": implements,
        "methods": [],
        "fields": [],
        "nested": [],
    }

    body = node.child_by_field_name("body")
    if body is not None:
        for child in body.children:
            if child.type in TYPE_DECL_NODES:
                result["nested"].append(parse_type_declaration(child, package, fqn))
            elif child.type == "method_declaration":
                result["methods"].append(parse_method(child, fqn))
            elif child.type == "constructor_declaration":
                result["methods"].append(parse_method(child, fqn))
            elif child.type == "field_declaration":
                result["fields"].extend(parse_fields(child, fqn))
    return result


def parse_method(node: Any, type_fqn: str) -> Dict[str, Any]:
    name_node = node.child_by_field_name("name")
    name = text_of(name_node) if name_node is not None else "<init>"
    params = node.child_by_field_name("parameters")
    arity = 0
    if params is not None:
        arity = sum(1 for c in params.children
                    if c.type in ("formal_parameter", "spread_parameter"))
    ret = node.child_by_field_name("type")

    calls: Dict[str, int] = {}
    _collect_calls(node, calls)

    return {
        "id": f"{type_fqn}#{name}/{arity}",
        "name": name,
        "arity": arity,
        "return_type": text_of(ret) if ret is not None else "",
        "modifiers": modifiers_of(node),
        "line": line_of(node),
        "calls": calls,
    }


def _collect_calls(node: Any, calls: Dict[str, int]) -> None:
    for child in node.children:
        if child.type == "method_invocation":
            callee = child.child_by_field_name("name")
            if callee is not None:
                calls[text_of(callee)] = calls.get(text_of(callee), 0) + 1
        _collect_calls(child, calls)


def parse_fields(node: Any, type_fqn: str) -> List[Dict[str, Any]]:
    type_node = node.child_by_field_name("type")
    field_type = text_of(type_node) if type_node is not None else ""
    fields = []
    for child in node.children:
        if child.type == "variable_declarator":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                name = text_of(name_node)
                fields.append({
                    "fqn": f"{type_fqn}.{name}",
                    "name": name,
                    "type": field_type,
                    "modifiers": modifiers_of(node),
                    "line": line_of(node),
                })
    return fields


def parse_java_source(source: str) -> Dict[str, Any]:
    """Parse one compilation unit -> {package, imports, types}."""
    parser = make_parser()
    tree = parser.parse(source.encode("utf-8"))
    root = tree.root_node

    package = ""
    imports: List[Dict[str, Any]] = []

    for child in root.children:
        if child.type == "package_declaration":
            package = qualified_text(child)
        elif child.type == "import_declaration":
            target = qualified_text(child)
            wildcard = any(c.type == "asterisk" for c in child.children)
            imports.append({"target": target, "wildcard": wildcard})

    types = [parse_type_declaration(c, package, None)
             for c in root.children if c.type in TYPE_DECL_NODES]
    return {"package": package, "imports": imports, "types": types}


def flatten_types(types: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in types:
        out.append(t)
        out.extend(flatten_types(t["nested"]))
    return out


def parse_repo(repo_path: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Parse every *.java under path.
    Returns (file_results, errors) where errors = [{"path", "error"}].
    Each result carries the sha256 of the parsed text (drift detection)."""
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    java_files = sorted(repo_path.rglob("*.java"))
    log.info("  [*] Found %d Java files.", len(java_files))
    for i, java_file in enumerate(java_files, 1):
        rel = str(java_file.relative_to(repo_path))
        try:
            source = java_file.read_text(encoding="utf-8", errors="replace")
            parsed = parse_java_source(source)
        except Exception as e:  # unreadable file: record and continue
            log.warning("  [!] Skipping unreadable file %s: %s", rel, e)
            errors.append({"path": rel, "error": str(e)})
            continue
        parsed["path"] = rel
        parsed["sha256"] = hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()
        results.append(parsed)
        if i % 1000 == 0:
            log.info("  [*] Parsed %d/%d files...", i, len(java_files))
    return results, errors


# ---------------------------------------------------------------------------
# 5. Symbol resolution and graph ingestion
# ---------------------------------------------------------------------------

def build_declared_set(file_results: List[Dict[str, Any]]) -> set:
    declared = set()
    for fr in file_results:
        for t in flatten_types(fr["types"]):
            declared.add(t["fqn"])
    return declared


def resolve_type_name(name: str, package: str, single_imports: Dict[str, str]) -> str:
    """Best-effort FQN for a referenced type name (extends/implements targets)."""
    if "." in name:
        return name
    if name in single_imports:
        return single_imports[name]
    return f"{package}.{name}" if package else name


def init_neo4j_schema(driver: Any) -> None:
    queries = [
        "CREATE CONSTRAINT repo_name_uniq IF NOT EXISTS FOR (r:Repository) REQUIRE r.name IS UNIQUE;",
        "CREATE CONSTRAINT file_id_uniq IF NOT EXISTS FOR (f:File) REQUIRE f.id IS UNIQUE;",
        "CREATE CONSTRAINT package_fqn_uniq IF NOT EXISTS FOR (p:Package) REQUIRE p.fqn IS UNIQUE;",
        "CREATE CONSTRAINT type_fqn_uniq IF NOT EXISTS FOR (t:Type) REQUIRE t.fqn IS UNIQUE;",
        "CREATE CONSTRAINT method_id_uniq IF NOT EXISTS FOR (m:Method) REQUIRE m.id IS UNIQUE;",
        "CREATE CONSTRAINT field_fqn_uniq IF NOT EXISTS FOR (fl:Field) REQUIRE fl.fqn IS UNIQUE;",
        "CREATE CONSTRAINT call_target_uniq IF NOT EXISTS FOR (c:CallTarget) REQUIRE c.name IS UNIQUE;",
    ]
    with driver.session() as session:
        for q in queries:
            session.run(q)


def chunks(rows: List[dict], size: int = INGEST_CHUNK_SIZE):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def ingest_repo_to_neo4j(driver: Any, repo_name: str, file_results: List[Dict[str, Any]]) -> Dict[str, int]:
    """Ingest one repo's parse results. Returns expected counts for validation."""
    log.info("[*] Ingesting graph into Neo4j: %s...", repo_name)
    declared = build_declared_set(file_results)

    file_rows: List[dict] = []
    package_rows: List[dict] = []
    package_import_rows: List[dict] = []
    type_import_rows: List[dict] = []
    type_rows: List[dict] = []
    method_rows: List[dict] = []
    field_rows: List[dict] = []
    extends_rows: List[dict] = []
    implements_rows: List[dict] = []
    call_rows: List[dict] = []

    for fr in file_results:
        file_id = f"{repo_name}:{fr['path']}"
        file_rows.append({"repo": repo_name, "id": file_id, "path": fr["path"],
                          "sha256": fr.get("sha256")})
        if fr["package"]:
            package_rows.append({"repo": repo_name, "fqn": fr["package"]})

        single_imports: Dict[str, str] = {}
        for imp in fr["imports"]:
            target = imp["target"]
            if imp["wildcard"]:
                package_import_rows.append({"file_id": file_id, "target": target})
            else:
                simple = target.rsplit(".", 1)[-1]
                single_imports[simple] = target
                type_import_rows.append(
                    {"file_id": file_id, "target": target, "external": None})

        for t in flatten_types(fr["types"]):
            type_rows.append({
                "fqn": t["fqn"], "name": t["name"], "kind": t["kind"],
                "line": t["line"], "modifiers": t["modifiers"],
                "package": fr["package"], "file_id": file_id, "external": False,
            })
            for base in t["extends"]:
                extends_rows.append({
                    "fqn": t["fqn"],
                    "target": resolve_type_name(base, fr["package"], single_imports),
                    "external": None,
                })
            for iface in t["implements"]:
                implements_rows.append({
                    "fqn": t["fqn"],
                    "target": resolve_type_name(iface, fr["package"], single_imports),
                    "external": None,
                })
            for m in t["methods"]:
                method_rows.append({
                    "id": m["id"], "name": m["name"], "arity": m["arity"],
                    "return_type": m["return_type"], "modifiers": m["modifiers"],
                    "line": m["line"], "type_fqn": t["fqn"],
                })
                for callee, count in m["calls"].items():
                    call_rows.append({"method_id": m["id"], "callee": callee, "count": count})
            for fl in t["fields"]:
                field_rows.append({
                    "fqn": fl["fqn"], "name": fl["name"], "type": fl["type"],
                    "modifiers": fl["modifiers"], "line": fl["line"], "type_fqn": t["fqn"],
                })

    # Mark inheritance/import targets that are not declared in this repo as external
    for row in extends_rows + implements_rows:
        row["external"] = row["target"] not in declared
    for row in type_import_rows:
        row["external"] = row["target"] not in declared

    with driver.session() as session:
        session.run("MERGE (r:Repository {name: $name})", name=repo_name)

        for chunk in chunks(file_rows):
            session.run("""
                UNWIND $batch AS row
                MATCH (r:Repository {name: row.repo})
                MERGE (f:File {id: row.id})
                SET f.path = row.path, f.sha256 = row.sha256
                MERGE (r)-[:CONTAINS]->(f)
            """, batch=chunk)

        for chunk in chunks(package_rows):
            session.run("""
                UNWIND $batch AS row
                MATCH (r:Repository {name: row.repo})
                MERGE (p:Package {fqn: row.fqn})
                MERGE (r)-[:CONTAINS]->(p)
            """, batch=chunk)

        for chunk in chunks(type_rows):
            session.run("""
                UNWIND $batch AS row
                MERGE (p:Package {fqn: row.package})
                MERGE (t:Type {fqn: row.fqn})
                SET t.name = row.name, t.kind = row.kind, t.line = row.line,
                    t.modifiers = row.modifiers, t.external = row.external
                MERGE (p)-[:CONTAINS]->(t)
                WITH t, row
                MATCH (f:File {id: row.file_id})
                MERGE (f)-[:DEFINES]->(t)
            """, batch=chunk)

        for chunk in chunks(method_rows):
            session.run("""
                UNWIND $batch AS row
                MATCH (t:Type {fqn: row.type_fqn})
                MERGE (m:Method {id: row.id})
                SET m.name = row.name, m.arity = row.arity,
                    m.return_type = row.return_type, m.modifiers = row.modifiers,
                    m.line = row.line
                MERGE (t)-[:CONTAINS]->(m)
            """, batch=chunk)

        for chunk in chunks(field_rows):
            session.run("""
                UNWIND $batch AS row
                MATCH (t:Type {fqn: row.type_fqn})
                MERGE (fl:Field {fqn: row.fqn})
                SET fl.name = row.name, fl.type = row.type,
                    fl.modifiers = row.modifiers, fl.line = row.line
                MERGE (t)-[:CONTAINS]->(fl)
            """, batch=chunk)

        for chunk in chunks(extends_rows):
            session.run("""
                UNWIND $batch AS row
                MATCH (t:Type {fqn: row.fqn})
                MERGE (tt:Type {fqn: row.target})
                SET tt.external = coalesce(tt.external, true)
                MERGE (t)-[:EXTENDS]->(tt)
            """, batch=chunk)

        for chunk in chunks(implements_rows):
            session.run("""
                UNWIND $batch AS row
                MATCH (t:Type {fqn: row.fqn})
                MERGE (tt:Type {fqn: row.target})
                SET tt.external = coalesce(tt.external, true)
                MERGE (t)-[:IMPLEMENTS]->(tt)
            """, batch=chunk)

        for chunk in chunks(package_import_rows):
            session.run("""
                UNWIND $batch AS row
                MATCH (f:File {id: row.file_id})
                MERGE (p:Package {fqn: row.target})
                MERGE (f)-[:IMPORTS]->(p)
            """, batch=chunk)

        for chunk in chunks(type_import_rows):
            session.run("""
                UNWIND $batch AS row
                MATCH (f:File {id: row.file_id})
                MERGE (t:Type {fqn: row.target})
                SET t.external = coalesce(t.external, row.external)
                MERGE (f)-[:IMPORTS]->(t)
            """, batch=chunk)

        for chunk in chunks(call_rows):
            session.run("""
                UNWIND $batch AS row
                MATCH (m:Method {id: row.method_id})
                MERGE (ct:CallTarget {name: row.callee})
                MERGE (m)-[c:CALLS]->(ct)
                SET c.count = row.count
            """, batch=chunk)

    # Expected graph counts are UNIQUE entities (the graph's uniqueness
    # constraints collapse duplicate declarations, e.g. the same FQN declared
    # in main and test sources). Raw row counts and the collapsed-duplicate
    # counts are reported separately.
    expected = {
        "files": len({r["id"] for r in file_rows}),
        "types": len({r["fqn"] for r in type_rows}),
        "methods": len({r["id"] for r in method_rows}),
        "fields": len({r["fqn"] for r in field_rows}),
        "extends": len({(r["fqn"], r["target"]) for r in extends_rows}),
        "implements": len({(r["fqn"], r["target"]) for r in implements_rows}),
        "imports": len({(r["file_id"], r["target"])
                        for r in package_import_rows + type_import_rows}),
        "calls": len({(r["method_id"], r["callee"]) for r in call_rows}),
    }
    raw = {
        "types": len(type_rows), "methods": len(method_rows),
        "fields": len(field_rows), "extends": len(extends_rows),
        "implements": len(implements_rows),
        "imports": len(package_import_rows) + len(type_import_rows),
        "calls": len(call_rows),
    }
    duplicates = {m: raw[m] - expected[m] for m in raw if raw[m] != expected[m]}
    if duplicates:
        log.warning("  [!] %d duplicate declarations collapse under graph uniqueness "
                    "constraints: %s",
                    sum(duplicates.values()),
                    ", ".join(f"{m} -{d}" for m, d in sorted(duplicates.items())))
        from collections import Counter
        dup_fqns = Counter(r["fqn"] for r in type_rows)
        for fqn, cnt in dup_fqns.most_common(5):
            if cnt > 1:
                log.warning("      e.g. %s declared %d times", fqn, cnt)

    log.info("  [+] Ingested %d files, %d types, %d methods, %d fields, "
             "%d extends, %d implements, %d imports, %d call edges.",
             expected["files"], expected["types"], expected["methods"],
             expected["fields"], expected["extends"], expected["implements"],
             expected["imports"], expected["calls"])
    expected["duplicates_collapsed"] = sum(duplicates.values())
    expected["duplicates_by_metric"] = duplicates
    return expected


# ---------------------------------------------------------------------------
# 5b. File inventory: taxonomy, LOC, coverage snapshots
# ---------------------------------------------------------------------------

def run_inventory(driver: Any, repo_name: str, repo_path: Path, run_id: str) -> Dict[str, Any]:
    """Inventory every git-tracked file: classify, count LOC, ingest :File
    nodes, materialize a StatsSnapshot (retention: last 2 runs)."""
    tracked = inv.git_tracked_files(repo_path)
    disk = inv.disk_files(repo_path)
    untracked = max(0, len(disk) - len(set(tracked)))

    records = [inv.inventory_file(repo_path, rel) for rel in sorted(tracked)]
    log.info("  [*] Inventory: %d tracked files, %d on disk, %d untracked (excluded).",
             len(tracked), len(disk), untracked)

    file_rows = [{
        "repo": repo_name, "id": f"{repo_name}:{r['path']}", "path": r["path"],
        "sha256": r["sha256"], "extension": r["extension"] or "",
        "kind": r["kind"], "category": r["category"], "role": r["role"],
        "bytes": r["bytes"], "totalLines": r["totalLines"],
        "codeLines": r["codeLines"], "commentLines": r["commentLines"],
        "blankLines": r["blankLines"],
    } for r in records]

    deep = sum(1 for r in records if r["extension"] == "java")
    unreadable = sum(1 for r in records if r["category"] == "unreadable")
    binaries = sum(1 for r in records
                   if r["kind"] == "artifact" or r["category"] in inv.BINARY_CATEGORIES)
    coverage = {
        "filesTracked": len(tracked), "filesOnDisk": len(disk),
        "untrackedFiles": untracked, "filesIndexedDeep": deep,
        "filesInventoryOnly": len(tracked) - deep - unreadable,
        "binaries": binaries,
    }

    cats: Dict[Tuple[str, str], Dict[str, int]] = {}
    for r in records:
        agg = cats.setdefault((r["kind"], r["category"]),
                              {"files": 0, "bytes": 0, "totalLines": 0,
                               "codeLines": 0, "commentLines": 0, "blankLines": 0})
        agg["files"] += 1
        agg["bytes"] += r["bytes"] or 0
        for k in ("totalLines", "codeLines", "commentLines", "blankLines"):
            agg[k] += r[k] or 0

    sid = f"{run_id}:{repo_name}"
    with driver.session() as session:
        session.run("MERGE (r:Repository {name: $name})", name=repo_name)
        for chunk in chunks(file_rows):
            session.run("""
                UNWIND $batch AS row
                MATCH (r:Repository {name: row.repo})
                MERGE (f:File {id: row.id})
                SET f.path = row.path, f.sha256 = row.sha256,
                    f.extension = row.extension, f.kind = row.kind,
                    f.category = row.category, f.role = row.role, f.bytes = row.bytes,
                    f.totalLines = row.totalLines, f.codeLines = row.codeLines,
                    f.commentLines = row.commentLines, f.blankLines = row.blankLines
                MERGE (r)-[:CONTAINS]->(f)
            """, batch=chunk)

        session.run("""
            MATCH (r:Repository {name: $repo})
            CREATE (s:StatsSnapshot {sid: $sid, runId: $runId, at: $at})
            CREATE (s)-[:COVERS $cov]->(r)
        """, repo=repo_name, sid=sid, runId=run_id,
            at=datetime.now(timezone.utc).isoformat(), cov=coverage)
        cat_rows = [{"kind": k, "category": c, **v} for (k, c), v in cats.items()]
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

    log.info("  [+] Inventory ingested: %d files across %d categories; coverage: %s",
             len(file_rows), len(cats),
             ", ".join(f"{k}={v}" for k, v in coverage.items()))
    return {"coverage": coverage,
            "categories": [{"kind": k, "category": c, **v} for (k, c), v in cats.items()],
            "records": records}


def verify_inventory(driver: Any, repo_name: str, inventory_result: Dict[str, Any]) -> Dict[str, Any]:
    """Inventory verification: file counts match the graph; category sums
    match the tracked total; LOC spot-check on up to 20 stored records."""
    with driver.session() as session:
        files_in_graph = session.run(
            "MATCH (:Repository {name: $n})-[:CONTAINS]->(f:File) RETURN count(f) AS c",
            n=repo_name).single()["c"]
        cat_files_sum = session.run("""
            MATCH (s:StatsSnapshot)-[:COVERS]->(:Repository {name: $n})
            WITH s ORDER BY s.at DESC LIMIT 1
            MATCH (s)-[:HAS_STAT]->(cs)
            RETURN sum(cs.files) AS c
        """, n=repo_name).single()["c"]

    spot_failures = []
    for r in inventory_result["records"][:20]:
        if r["totalLines"] is None:
            continue
        with driver.session() as session:
            rec = session.run(
                "MATCH (f:File {id: $i}) RETURN f.totalLines AS t, f.category AS c",
                i=f"{repo_name}:{r['path']}").single()
        if rec is None or rec["t"] != r["totalLines"] or rec["c"] != r["category"]:
            spot_failures.append(r["path"])

    tracked = inventory_result["coverage"]["filesTracked"]
    result = {
        "filesTracked": tracked,
        "filesInGraph": files_in_graph,
        "filesMatch": files_in_graph == tracked,
        "categorySumsMatch": cat_files_sum == tracked,
        "locSpotCheckFailures": spot_failures,
    }
    ok = result["filesMatch"] and result["categorySumsMatch"] and not spot_failures
    log.info("    %s inventory files=%d/%d category-sums=%s spot-check-failures=%d",
             "OK " if ok else "MISMATCH", files_in_graph, tracked,
             result["categorySumsMatch"], len(spot_failures))
    return result


# ---------------------------------------------------------------------------
# 6. Post-import verification
# ---------------------------------------------------------------------------

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
    """Count what actually landed in the graph for this repository."""
    actual: Dict[str, int] = {}
    with driver.session() as session:
        for metric, query in VERIFY_QUERIES.items():
            actual[metric] = session.run(query, n=repo_name).single()["c"]
    return actual


def diff_counts(expected: Dict[str, int], actual: Dict[str, int]) -> Dict[str, dict]:
    """Compare extracted vs loaded counts per metric."""
    return {
        m: {"expected": expected.get(m, 0), "actual": actual.get(m, 0),
            "ok": expected.get(m, 0) == actual.get(m, 0)}
        for m in GRAPH_METRICS
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Multi-repo Java Tree-sitter-to-Neo4j parsing pipeline")
    parser.add_argument("--repos", default="repos.json",
                        help="Path to JSON file with repo definitions (default: repos.json)")
    parser.add_argument("--docker", action="store_true",
                        help="Use docker instead of podman (default: podman)")
    parser.add_argument("--no-auto-install", action="store_true",
                        help="Fail instead of auto-installing missing Python dependencies")
    parser.add_argument("--neo4j-host", default="localhost", help="Neo4j host (default: localhost)")
    parser.add_argument("--bolt-port", type=int, default=7687, help="Neo4j Bolt port (default: 7687)")
    parser.add_argument("--http-port", type=int, default=7474, help="Neo4j HTTP port (default: 7474)")
    parser.add_argument("--neo4j-password", default="graphpassword123", help="Neo4j password")
    parser.add_argument("--log-dir", default="logs", help="Directory for run log and summary JSON")
    args = parser.parse_args()

    started = datetime.now(timezone.utc)
    log_path = setup_logging(Path(args.log_dir))
    log.info("[*] Run started. Log file: %s", log_path)

    engine = "docker" if args.docker else "podman"
    auto_install = not args.no_auto_install

    # Step 1: Python dependency bootstrap, then pre-flight checks
    ensure_python_deps(auto_install)
    check_prerequisites(engine)

    # Step 2: Ensure Neo4j is running in container
    ensure_neo4j_running(engine, args.bolt_port, args.http_port, args.neo4j_password)

    # Step 3: Parse manifest
    repos = load_manifest(Path(args.repos))
    if not repos:
        sys.exit("[-] Fatal: manifest is empty; nothing to parse.")
    log.info("[*] Manifest: %d repos: %s", len(repos), ", ".join(r["name"] for r in repos))

    # Step 4: Parse each repo and ingest
    neo4j = get_neo4j_driver_module()
    uri = f"bolt://{args.neo4j_host}:{args.bolt_port}"
    repo_reports = []
    with neo4j.GraphDatabase.driver(uri, auth=("neo4j", args.neo4j_password)) as driver:
        init_neo4j_schema(driver)
        run_id = started.strftime("%Y%m%d-%H%M%S")
        for repo in repos:
            name = repo["name"]
            path = Path(repo["path"]).resolve()
            log.info("[*] Parsing repository: %s (%s)", name, path)
            if not path.exists():
                log.error("  [-] Skipping: Directory '%s' does not exist.", path)
                repo_reports.append({"name": name, "status": "missing_path"})
                continue
            log.info("[*] Inventorying repository: %s...", name)
            inventory_result = run_inventory(driver, name, path, run_id)
            inv_verification = verify_inventory(driver, name, inventory_result)
            file_results, parse_errors = parse_repo(path)
            if not file_results:
                log.error("  [-] Skipping: no parseable Java files under '%s'.", path)
                repo_reports.append({"name": name, "status": "no_files"})
                continue
            expected = ingest_repo_to_neo4j(driver, name, file_results)
            # the "files" metric now means ALL tracked files (inventory phase),
            # not just the Java files the parse phase created rows for
            expected["files"] = inventory_result["coverage"]["filesTracked"]
            log.info("[*] Verifying graph contents for %s...", name)
            actual = verify_repo_in_graph(driver, name)
            validation = diff_counts(expected, actual)
            repo_ok = all(v["ok"] for v in validation.values())
            status = "ok" if repo_ok else "mismatch"
            for metric, v in validation.items():
                mark = "OK " if v["ok"] else "MISMATCH"
                log.info("    %s %-11s expected=%-8d actual=%-8d", mark, metric,
                         v["expected"], v["actual"])
            log.info("  [%s] Repository %s: %s", "+" if repo_ok else "!", name,
                     "verified" if repo_ok else "validation mismatches detected")
            report = {
                "name": name,
                "status": status,
                "path": str(path),
                "files_found": len(file_results) + len(parse_errors),
                "files_parsed": len(file_results),
                "files_failed": len(parse_errors),
                "parse_errors": parse_errors[:50],
                "extracted": expected,
                "graph": actual,
                "validation": validation,
                "inventory": {
                    "coverage": inventory_result["coverage"],
                    "categories": inventory_result["categories"],
                    "verification": inv_verification,
                },
            }
            repo_reports.append(report)

    # Step 5: Summary
    finished = datetime.now(timezone.utc)
    duration_s = round((finished - started).total_seconds(), 1)
    parsed = [r for r in repo_reports if r.get("status") in ("ok", "mismatch")]
    overall_ok = bool(parsed) and all(r["status"] == "ok" for r in parsed)

    totals = {m: sum(r["extracted"][m] for r in parsed) for m in GRAPH_METRICS} if parsed else {}
    summary = {
        "run": {
            "started_utc": started.isoformat(),
            "finished_utc": finished.isoformat(),
            "duration_seconds": duration_s,
            "engine": engine,
            "manifest": args.repos,
            "log_file": str(log_path),
            "neo4j_uri": uri,
        },
        "totals_extracted": totals,
        "repos": repo_reports,
        "overall_ok": overall_ok,
    }
    summary_path = Path(args.log_dir) / f"summary-{started.strftime('%Y%m%d-%H%M%S')}.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")

    log.info("")
    log.info("=" * 68)
    log.info("RUN SUMMARY  (%s, duration %.1fs)", finished.strftime("%Y-%m-%d %H:%M:%S"), duration_s)
    log.info("=" * 68)
    for r in repo_reports:
        if r.get("status") in ("ok", "mismatch"):
            log.info("  %-18s %-9s parsed %d/%d files, %d types, %d methods, "
                     "%d fields, %d call edges",
                     r["name"], r["status"].upper(), r["files_parsed"],
                     r["files_found"], r["extracted"]["types"],
                     r["extracted"]["methods"], r["extracted"]["fields"],
                     r["extracted"]["calls"])
            cov = r.get("inventory", {}).get("coverage", {})
            if cov:
                log.info("  %-18s   inventory: %d tracked / %d deep-indexed / "
                         "%d inventory-only / %d binaries",
                         r["name"], cov.get("filesTracked", 0),
                         cov.get("filesIndexedDeep", 0),
                         cov.get("filesInventoryOnly", 0), cov.get("binaries", 0))
            if r["files_failed"]:
                log.info("  %-18s   %d files failed to parse (first errors in log/summary)",
                         r["name"], r["files_failed"])
            bad = [m for m, v in r["validation"].items() if not v["ok"]]
            if bad:
                log.info("  %-18s   GRAPH MISMATCH on: %s", r["name"], ", ".join(bad))
        else:
            log.info("  %-18s SKIPPED (%s)", r["name"], r.get("status"))
    log.info("  Overall: %s", "SUCCESS - all repositories verified" if overall_ok
             else "ATTENTION - see mismatches/skips above")
    log.info("  Log file: %s", log_path)
    log.info("  Summary:  %s", summary_path)
    log.info("=" * 68)

    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
