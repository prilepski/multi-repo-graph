# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Stage 3 (Python half): stream portfolio rows out of a repo + its codegraph
index. Combines two sources (design.md §5/§6):

  files       — our own scan of git-tracked files (sha256 + LOC are OURS;
                codegraph skips >1 MB / unsupported files but still gets
                :File rows — inventory-grade, no DEFINES)
  everything
  else        — `nodejs/export.mjs` streaming JSON lines off the codegraph
                bulk API (D1 preferred path; see docs/verification-bulk-api.md
                Findings), parsed and re-tagged here.

Also carries the schema-fingerprint guard (D1/O2): the run refuses to start
against a codegraph DB whose table/column shape doesn't match the pinned
version's fingerprint.
"""

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

# @colbymchenry/codegraph@1.6.2, schema v11 — measured in the O1 spike
# (docs/verification-bulk-api.md Findings). sha256 over sqlite_master's
# table+column names plus index/trigger names.
SCHEMA_FINGERPRINT = "6f73261f40603bc3d0df494814c2d57616435e3750af89d06b9a01c54e1c78d0"

NODEJS_DIR = Path(__file__).resolve().parent / "nodejs"

# exporter table tag -> ingest table tag (ingest order)
TABLE_MAP = {
    "packages": "packages",
    "types": "types",
    "methods": "methods",
    "fields": "fields",
    "extends": "extends",
    "implements": "implements",
    "packageImports": "package_imports",
    "typeImports": "type_imports",
    "calls": "calls",
}


class ExportError(Exception):
    pass


def schema_fingerprint(db_path: Path) -> str:
    """sha256 over sqlite_master: 'type:name' for non-table objects,
    'name(col,...)' for tables (the algorithm the O1 spike measured and
    docs/verification-bulk-api.md records — keep the three in lockstep)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        parts: List[str] = []
        for typ, name in conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"):
            if typ == "table":
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
                parts.append(f"{name}({','.join(cols)})")
            else:
                parts.append(f"{typ}:{name}")
    finally:
        conn.close()
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def check_schema_fingerprint(db_path: Path) -> str:
    """Raise ExportError on mismatch (never a silent wrong export, design §6)."""
    actual = schema_fingerprint(db_path)
    if actual != SCHEMA_FINGERPRINT:
        raise ExportError(
            f"codegraph schema fingerprint mismatch for {db_path}: "
            f"expected {SCHEMA_FINGERPRINT} (pinned @colbymchenry/codegraph "
            f"1.6.2), got {actual}. Re-spike the schema (O2) and update "
            f"indexer/exporter.py — do not export from an unknown schema.")
    return actual


def codegraph_version() -> str:
    pkg_json = NODEJS_DIR / "node_modules" / "@colbymchenry" / "codegraph" / "package.json"
    return json.loads(pkg_json.read_text())["version"]


# ---------------------------------------------------------------------------
# files table: our scan of git-tracked files
# ---------------------------------------------------------------------------

def git_tracked_files(repo_path: Path) -> List[str]:
    proc = subprocess.run(["git", "-C", str(repo_path), "ls-files", "-z"],
                          capture_output=True, text=True, check=True)
    return [f for f in proc.stdout.split("\0") if f]


def scan_file_rows(repo_name: str, repo_path: Path,
                   tracked: Optional[List[str]] = None
                   ) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield ("files", row) for every git-tracked file — OUR sha256 (of the
    utf-8-decoded/re-encoded text, matching the MFLens drift contract),
    inventory taxonomy (kind/category/role, java-graph rule table) and LOC
    breakdown (binaries get no line counts). Single pass per file."""
    from . import inventory as inv
    for rel in tracked if tracked is not None else git_tracked_files(repo_path):
        path = repo_path / rel
        if not path.is_file():
            # submodule gitlinks and deleted-in-worktree entries: not files
            continue
        meta = inv.classify(rel)
        row: Dict[str, Any] = {
            "repo": repo_name,
            "id": f"{repo_name}:{rel}",
            "path": rel,
            "sha256": None,
            "extension": meta["extension"],
            "kind": meta["kind"],
            "category": meta["category"],
            "role": meta["role"],
            "bytes": 0,
            "totalLines": None, "codeLines": None,
            "commentLines": None, "blankLines": None,
        }
        try:
            data = path.read_bytes()
        except OSError as e:
            raise ExportError(
                f"{repo_name}: cannot read tracked file {rel}: {e}") from e
        row["bytes"] = len(data)
        if meta["category"] in inv.BINARY_CATEGORIES or b"\0" in data[:8192]:
            row["sha256"] = hashlib.sha256(data).hexdigest()
            if meta["category"] not in inv.BINARY_CATEGORIES:
                row["category"] = "binary_blob"
            row["kind"] = ("assets" if row["category"] in ("image", "font")
                           else "artifact")
        else:
            text = data.decode("utf-8", errors="replace")
            row["sha256"] = hashlib.sha256(
                text.encode("utf-8", "replace")).hexdigest()
            counts = inv.count_lines(text, meta["category"], meta["extension"])
            if counts:
                row.update(counts)
        yield ("files", row)


# ---------------------------------------------------------------------------
# combined repo stream: files (ours) + symbols/edges (Node exporter)
# ---------------------------------------------------------------------------

class RepoExport:
    """Streaming export of one repo. Iterate `.rows()` (exactly once) — the
    final row set is followed by `.counts` (codegraph's own stats + the
    exporter's row totals), `.loc` (total lines over tracked text files) and
    `.inventory` ({coverage, categories} for the StatsSnapshot phase)."""

    def __init__(self, repo_name: str, repo_path: Path):
        self.repo_name = repo_name
        self.repo_path = repo_path
        self.counts: Dict[str, Any] = {}
        self.loc = 0
        self.inventory: Dict[str, Any] = {}

    def rows(self) -> Iterator[Tuple[str, Dict[str, Any]]]:
        from . import inventory as inv
        self.loc = 0
        cats: Dict[Tuple[str, str], Dict[str, int]] = {}
        tracked = binaries = unreadable = 0
        for table, row in scan_file_rows(self.repo_name, self.repo_path):
            tracked += 1
            if row["totalLines"]:
                self.loc += row["totalLines"]
            if row["kind"] == "artifact" or row["category"] in inv.BINARY_CATEGORIES:
                binaries += 1
            if row["category"] == "unreadable":
                unreadable += 1
            agg = cats.setdefault((row["kind"], row["category"]),
                                  {"files": 0, "bytes": 0, "totalLines": 0,
                                   "codeLines": 0, "commentLines": 0,
                                   "blankLines": 0})
            agg["files"] += 1
            agg["bytes"] += row["bytes"]
            for k in ("totalLines", "codeLines", "commentLines", "blankLines"):
                agg[k] += row[k] or 0
            yield table, row

        proc = subprocess.Popen(
            ["node", str(NODEJS_DIR / "export.mjs"),
             str(self.repo_path), self.repo_name],
            cwd=str(NODEJS_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                event = json.loads(line)
                tag = event.get("t")
                if tag == "counts":
                    self.counts = event["row"]
                    continue
                table = TABLE_MAP.get(tag)
                if table is None:
                    raise ExportError(
                        f"{self.repo_name}: exporter emitted unknown table {tag!r}")
                yield table, event["row"]
        finally:
            stderr = proc.stderr.read() if proc.stderr else ""
            proc.stdout.close() if proc.stdout else None
            code = proc.wait()
            if proc.stderr:
                proc.stderr.close()
        if code != 0:
            raise ExportError(
                f"{self.repo_name}: export.mjs exited {code}: {stderr.strip()[:800]}")
        if not self.counts:
            raise ExportError(f"{self.repo_name}: export.mjs produced no counts line")

        # inventory summary (coverage + category rollups) for StatsSnapshot
        on_disk = sum(1 for p in self.repo_path.rglob("*")
                      if p.is_file() and ".git" not in p.parts)
        deep = int(self.counts.get("stats", {}).get("fileCount", 0) or 0)
        self.inventory = {
            "coverage": {
                "filesTracked": tracked,
                "filesOnDisk": on_disk,
                "untrackedFiles": max(0, on_disk - tracked),
                "filesIndexedDeep": deep,
                "filesInventoryOnly": max(0, tracked - deep - unreadable),
                "binaries": binaries,
            },
            "categories": [{"kind": k, "category": c, **v}
                           for (k, c), v in sorted(cats.items())],
        }


class RowSampler:
    """Deterministic reservoir over a (table, row) stream — feeds stage-5
    sampled edge checks (design.md §7: verification stays O(sample))."""

    SAMPLE_TABLES = ("types", "methods", "extends", "implements", "calls")
    DEFAULT_N = 200

    def __init__(self, n: int = DEFAULT_N, seed: int = 42):
        import random
        self._rng = random.Random(seed)
        self._n = n
        self._seen = 0
        self.samples: Dict[str, List[Dict[str, Any]]] = {t: [] for t in self.SAMPLE_TABLES}

    def observe(self, table: str, row: Dict[str, Any]) -> None:
        if table not in self.SAMPLE_TABLES:
            return
        self._seen += 1
        reservoir = self.samples[table]
        if len(reservoir) < self._n:
            reservoir.append(row)
        else:
            j = self._rng.randrange(self._seen)
            if j < self._n:
                reservoir[j] = row
