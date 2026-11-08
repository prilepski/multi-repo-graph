"""Neo4j container lifecycle — ported from java-graph (reference/
java-indexer/index_to_neo4j.py) with this project's own container so the
java-graph instance (`scip-neo4j-instance`, ports 7474/7687) is never
touched (AGENTS.md ground rule):

  container : mrg-neo4j-instance
  image     : neo4j:5-community + APOC (same as java-graph)
  ports     : bolt 7688, http 7475
  password  : graphpassword123 (java-graph constant)

Also owns the database dump for stage 6 (neo4j-admin database dump requires
the DB stopped: `neo4j stop` inside the container, dump, restart, wait Bolt).
"""

import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("indexer.container")

NEO4J_CONTAINER_NAME = "mrg-neo4j-instance"
NEO4J_IMAGE = "neo4j:5-community"
DEFAULT_BOLT_PORT = 7688
DEFAULT_HTTP_PORT = 7475
DEFAULT_PASSWORD = "graphpassword123"


def check_prerequisites(engine: str) -> None:
    log.info("[*] Checking system prerequisites...")
    if not shutil.which(engine):
        sys.exit(f"[-] Fatal: '{engine}' is not installed or not found on PATH.")
    if not shutil.which("node"):
        sys.exit("[-] Fatal: Node.js 22.5+ is required (node:sqlite for codegraph).")
    log.info("  [+] Container engine found: %s", engine)


def _docker(engine: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([engine, *args], capture_output=True, text=True, check=check)


def container_running(engine: str, name: str) -> bool:
    res = _docker(engine, "ps", "--filter", f"name={name}",
                  "--format", "{{.Names}}")
    return name in res.stdout


def ensure_neo4j_running(engine: str, bolt_port: int,
                         http_port: int | None,
                         password: str, neo4j_module: Any) -> None:
    name = NEO4J_CONTAINER_NAME
    log.info("[*] Verifying Neo4j container status using %s...", engine)
    if container_running(engine, name):
        log.info("  [+] Neo4j container is already active.")
    else:
        res_all = _docker(engine, "ps", "-a", "--filter", f"name={name}",
                          "--format", "{{.Names}}")
        if name in res_all.stdout:
            log.info("  [*] Starting stopped container '%s'...", name)
            _docker(engine, "start", name)
        else:
            log.info("  [*] Launching new Neo4j container '%s'...", name)
            run_cmd = [engine, "run", "-d", "--name", name,
                       "-p", f"{bolt_port}:7687"]
            if http_port:
                run_cmd += ["-p", f"{http_port}:7474"]
            run_cmd += ["-e", f"NEO4J_AUTH=neo4j/{password}",
                        "-e", 'NEO4J_PLUGINS=["apoc"]', NEO4J_IMAGE]
            subprocess.run(run_cmd, check=True)

    log.info("  [*] Waiting for Neo4j Bolt interface to become healthy...")
    uri = f"bolt://localhost:{bolt_port}"
    for _ in range(60):
        try:
            with neo4j_module.GraphDatabase.driver(uri, auth=("neo4j", password)) as driver:
                driver.verify_connectivity()
                log.info("  [+] Connected to Neo4j successfully.")
                return
        except Exception:
            time.sleep(2)
    sys.exit("[-] Timeout: Failed to connect to Neo4j within 120 seconds.")


def dump_database(engine: str, dest_dir: Path,
                  bolt_port: int, password: str, neo4j_module: Any) -> Path:
    """Dump the `neo4j` database to dest_dir/neo4j.dump (stage 6 input).
    Stops the DB inside the container (neo4j-admin requires it), dumps to
    /dumps, copies out, restarts, waits for Bolt again."""
    name = NEO4J_CONTAINER_NAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    dump_path = dest_dir / "neo4j.dump"
    if dump_path.exists():
        dump_path.unlink()

    log.info("[*] Dumping Neo4j database (stops the DB inside the container)...")
    _docker(engine, "exec", name, "neo4j", "stop")
    try:
        _docker(engine, "exec", name, "rm", "-rf", "/dumps")
        _docker(engine, "exec", name, "neo4j-admin", "database", "dump", "neo4j",
                "--to-path=/dumps", "--overwrite-destination")
        _docker(engine, "cp", f"{name}:/dumps/neo4j.dump", str(dump_path))
    finally:
        _docker(engine, "restart", name)
        ensure_neo4j_running(engine, bolt_port, 0, password, neo4j_module)
    if not dump_path.exists() or dump_path.stat().st_size == 0:
        raise RuntimeError(f"neo4j dump did not land at {dump_path}")
    log.info("  [+] Dump written: %s (%d bytes)", dump_path, dump_path.stat().st_size)
    return dump_path
