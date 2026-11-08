"""Fingerprint guard tests: the algorithm is pinned to what the O1 spike
measured (docs/verification-bulk-api.md) — tables hash as name(cols),
other objects as type:name. A mismatch here means the guard itself drifted."""

import sqlite3

from indexer import exporter


def _make_db(path, schema: str) -> None:
    db = sqlite3.connect(path)
    db.executescript(schema)
    db.commit()
    db.close()


def test_fingerprint_algorithm_pinned(tmp_path):
    db = tmp_path / "t.db"
    _make_db(db, """
        CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT);
        CREATE TABLE edges (id TEXT PRIMARY KEY, source TEXT);
        CREATE INDEX idx_edges_source ON edges(source);
        CREATE TRIGGER nodes_ai AFTER INSERT ON nodes BEGIN
            INSERT INTO edges (id, source) VALUES ('x', 'y'); END;
    """)
    # sha256 over objects in sqlite_master ORDER BY type,name:
    # index < table < trigger
    import hashlib
    expected_input = ("idx_edges_source:index\n"
                      "edges(id,source)\n"
                      "nodes(id,kind)\n"
                      "nodes_ai:trigger")
    assert exporter.schema_fingerprint(db) == hashlib.sha256(
        expected_input.encode()).hexdigest()


def test_fingerprint_mismatch_raises(tmp_path):
    db = tmp_path / "t.db"
    _make_db(db, "CREATE TABLE something_else (a INT);")
    import pytest
    with pytest.raises(exporter.ExportError, match="fingerprint mismatch"):
        exporter.check_schema_fingerprint(db)
