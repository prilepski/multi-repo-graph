"""Stage 6: timestamp-versioned run artifacts (design.md §4).

    runs/<UTC-timestamp>/
      run-manifest.json        where every codegraph index lives + SHAs
      summary.json             counts, durations, verification, failures
      neo4j/neo4j.dump.zst     neo4j-admin database dump, zstd
      codegraphs/<repo>/codegraph.db.zst   WAL folded before compress

`latest.json` ({"run": <timestamp>}) is written at publish time (stage 7).
"""

import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MULTI_REPO_GRAPH_VERSION = "0.1.0"


def run_id_for(started: datetime) -> str:
    """UTC ISO-8601, second precision, filesystem-safe (design.md §4)."""
    return started.astimezone(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def zstd_compress(src: Path, dest: Path, level: int = 3) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    subprocess.run(["zstd", f"-{level}", "-T0", "-f", str(src),
                    "-o", str(dest)], check=True, capture_output=True)
    return dest


def fold_wal(db_path: Path) -> None:
    """Best-effort WAL fold before compressing (the index worker already
    checkpointed; this guarantees it, cheaply)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


class RunArtifacts:
    """Builds one run directory under `root/runs/<run_id>/`."""

    def __init__(self, root: Path, run_id: str):
        self.run_dir = root / "runs" / run_id
        self.run_id = run_id
        self.codegraphs_dir = self.run_dir / "codegraphs"
        self.neo4j_dir = self.run_dir / "neo4j"

    def stage_codegraph_db(self, repo_name: str, db_path: Path) -> Dict[str, Any]:
        fold_wal(db_path)
        dest = self.codegraphs_dir / repo_name / "codegraph.db.zst"
        zstd_compress(db_path, dest)
        return {
            "codegraphDb": f"codegraphs/{repo_name}/codegraph.db.zst",
            "codegraphDbSha256": sha256_file(dest),
            "codegraphDbBytes": dest.stat().st_size,
        }

    def stage_neo4j_dump(self, dump_path: Path) -> Dict[str, Any]:
        dest = self.neo4j_dir / "neo4j.dump.zst"
        zstd_compress(dump_path, dest)
        dump_path.unlink()  # uncompressed copy is huge; keep only .zst
        return {
            "dump": "neo4j/neo4j.dump.zst",
            "dumpSha256": sha256_file(dest),
            "dumpBytes": dest.stat().st_size,
        }

    def write_run_manifest(self, payload: Dict[str, Any]) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / "run-manifest.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def write_summary(self, payload: Dict[str, Any]) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / "summary.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path


def build_run_manifest(*, run_id: str, started_at: str, finished_at: str,
                       codegraph_version: str, neo4j_image: str,
                       schema_fingerprint: str,
                       repos: List[Dict[str, Any]],
                       neo4j: Dict[str, Any],
                       verification_status: str) -> Dict[str, Any]:
    return {
        "runId": run_id,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "tools": {
            "multiRepoGraph": MULTI_REPO_GRAPH_VERSION,
            "codegraph": codegraph_version,
            "neo4jImage": neo4j_image,
        },
        "schemaFingerprint": schema_fingerprint,
        "repos": repos,
        "neo4j": {"database": "neo4j", **neo4j},
        "verification": {"status": verification_status, "reportRef": "summary.json"},
    }


def list_run_ids(store_root: Path) -> List[str]:
    runs = store_root / "runs"
    if not runs.is_dir():
        return []
    return sorted((d.name for d in runs.iterdir() if d.is_dir()), reverse=True)
