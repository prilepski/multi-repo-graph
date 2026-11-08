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
    """Yield ("files", row) for every git-tracked file, with OUR sha256 and
    line count (binary files: totalLines None). Single pass per file."""
    for rel in tracked if tracked is not None else git_tracked_files(repo_path):
        path = repo_path / rel
        sha = hashlib.sha256()
        lines = 0
        is_text = True
        size = 0
        last_byte = b""
        try:
            with open(path, "rb") as f:
                head = f.read(8192)
                is_text = b"\0" not in head
                sha.update(head)
                size += len(head)
                if is_text:
                    lines += head.count(b"\n")
                    last_byte = head[-1:] if head else b""
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    sha.update(chunk)
                    size += len(chunk)
                    if is_text:
                        lines += chunk.count(b"\n")
                        last_byte = chunk[-1:]
            if is_text and size and last_byte != b"\n":
                lines += 1  # final line without trailing newline
        except OSError as e:
            raise ExportError(f"{repo_name}: cannot read tracked file {rel}: {e}") from e
        yield ("files", {
            "repo": repo_name,
            "id": f"{repo_name}:{rel}",
            "path": rel,
            "sha256": sha.hexdigest(),
            "totalLines": lines if is_text else None,
            "bytes": size,
        })


# ---------------------------------------------------------------------------
# combined repo stream: files (ours) + symbols/edges (Node exporter)
# ---------------------------------------------------------------------------

class RepoExport:
    """Streaming export of one repo. Iterate `.rows()` (exactly once) — the
    final row set is followed by `.counts` (codegraph's own stats + the
    exporter's row totals) and `.loc` (total lines over tracked text files)."""

    def __init__(self, repo_name: str, repo_path: Path):
        self.repo_name = repo_name
        self.repo_path = repo_path
        self.counts: Dict[str, Any] = {}
        self.loc = 0

    def rows(self) -> Iterator[Tuple[str, Dict[str, Any]]]:
        self.loc = 0
        for table, row in scan_file_rows(self.repo_name, self.repo_path):
            if row["totalLines"]:
                self.loc += row["totalLines"]
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
