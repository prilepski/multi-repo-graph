# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
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
from typing import Any

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
                         password: str, neo4j_module: Any,
                         container_name: str = NEO4J_CONTAINER_NAME) -> None:
    name = container_name
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


def dump_database(engine: str, dest_dir: Path, bolt_port: int,
                  password: str, neo4j_module: Any,
                  container_name: str = NEO4J_CONTAINER_NAME) -> Path:
    """Dump `system` + `neo4j` databases into dest_dir/ (stage 6 input).

    BOTH are required: users/roles (credentials) live in `system` — a
    neo4j-only dump restores a graph nobody can log into. neo4j-admin
    requires the DBs stopped — and in the neo4j image the server IS the
    container's main process, so `docker exec neo4j stop` kills the whole
    container (exit 137). Instead: docker stop, dump through a helper
    container (--volumes-from for /data, dest_dir bind-mounted at /dumps so
    the dumps land on the host directly), docker start, wait for Bolt."""
    name = container_name
    dest_dir = dest_dir.resolve()  # bind mounts need absolute paths
    dest_dir.mkdir(parents=True, exist_ok=True)

    log.info("[*] Dumping Neo4j databases (container stopped for the dump)...")
    subprocess.run([engine, "stop", name], check=True, capture_output=True)
    try:
        for db in ("system", "neo4j"):
            res = subprocess.run(
                [engine, "run", "--rm", "--volumes-from", name,
                 "-v", f"{dest_dir}:/dumps", NEO4J_IMAGE,
                 "neo4j-admin", "database", "dump", db,
                 "--to-path=/dumps", "--overwrite-destination"],
                capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(
                    f"neo4j-admin dump {db} failed ({res.returncode}): "
                    f"{res.stderr.strip()[-500:]}")
    finally:
        subprocess.run([engine, "start", name], check=True, capture_output=True)
        ensure_neo4j_running(engine, bolt_port, None, password, neo4j_module,
                             container_name)
    dump_path = dest_dir / "neo4j.dump"
    if not dump_path.exists() or dump_path.stat().st_size == 0:
        raise RuntimeError(f"neo4j dump did not land at {dump_path}")
    log.info("  [+] Dumps written: %s", ", ".join(
        f"{p.name} ({p.stat().st_size} bytes)"
        for p in sorted(dest_dir.glob("*.dump"))))
    return dump_path


def stop_container(engine: str, container_name: str = NEO4J_CONTAINER_NAME) -> None:
    """Stop the ingest container (batch cleanliness: the pipeline leaves
    nothing running — the dump is the artifact, and the workspace's ports
    must be free for `up.py` to restore the snapshot on this machine)."""
    subprocess.run([engine, "stop", container_name], capture_output=True)
    log.info("[+] Ingest container '%s' stopped.", container_name)
