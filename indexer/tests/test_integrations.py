# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Cross-repo integration detection tests (integrations.py + INTEGRATES
ingest) — the derived repo-to-repo edges the base extractor cannot
produce (REST calls are string literals)."""

import subprocess
from pathlib import Path

from indexer import integrations
from indexer.exporter import git_tracked_files


def _git_repo(path: Path, files: dict) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "x"]):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-C", str(path), *args], check=True, capture_output=True)
    return path


class TestDetect:
    def test_references_detected_with_evidence(self, tmp_path):
        repo = _git_repo(tmp_path / "svc-content", {
            "app/config.py": (
                'jwt_issuer = "demo-users"\n'
                'SSM = "/demo/demo-users/prod/api-gateway-url"\n'
                'x = 1\n'),
            "docs/integrations.md": "calls demo-users and demo-audit\n",
            "logo.png": "binary",  # replaced below with real bytes
        })
        (repo / "logo.png").write_bytes(b'\x89PNG\r\n\x1a\n' + bytes(64))
        tracked = git_tracked_files(repo)
        out = {i["target"]: i for i in integrations.detect_integrations(
            repo, "svc-content",
            ["svc-content", "demo-users", "demo-audit", "demo-web"],
            tracked)}
        assert set(out) == {"demo-users", "demo-audit"}
        users = out["demo-users"]
        assert users["refs"] == 3  # two config lines + one md line
        assert users["files"] == 2
        assert any("jwt_issuer" in s["text"] for s in users["samples"])
        assert all(s["path"] != "logo.png" for s in users["samples"])

    def test_no_false_positives_on_word_overlap(self, tmp_path):
        """Bare service short names are English words — only the FULL repo
        name may match. 'content-type' must not count as a demo-content
        reference; 'demo-users-api' IS one (references the service)."""
        repo = _git_repo(tmp_path / "svc-a", {
            "a.py": "h = {'content-type': 'application/json'}\n"
                    "url = 'https://demo-users-api.internal/x'\n"})
        out = {i["target"]: i["refs"] for i in integrations.detect_integrations(
            repo, "svc-a", ["svc-a", "demo-content", "demo-users"],
            git_tracked_files(repo))}
        assert "demo-content" not in out
        assert out.get("demo-users") == 1

    def test_self_and_missing_ignored(self, tmp_path):
        repo = _git_repo(tmp_path / "svc-a", {"a.py": "svc-a config\n"})
        out = integrations.detect_integrations(
            repo, "svc-a", ["svc-a"], git_tracked_files(repo))
        assert out == []


class TestIngest:
    def test_integrates_edges_in_graph(self, tmp_path):
        """Detector output -> INTEGRATES edges, idempotent on re-ingest."""
        import pytest
        pytest.importorskip("neo4j")
        import os
        uri = os.environ.get("NEO4J_TEST_URI")
        if not uri:
            pytest.skip("NEO4J_TEST_URI not set")
        import neo4j
        pw = os.environ.get("NEO4J_TEST_PASSWORD", "graphpassword123")
        d = neo4j.GraphDatabase.driver(uri, auth=("neo4j", pw))
        try:
            from indexer import ingest
            with d.session() as s:
                for n in ("integra", "integrb"):
                    s.run("MERGE (r:Repository {name: $n})", n=n)
            rows = [{"target": "integrb", "refs": 7, "files": 3,
                     "samples": [{"path": "a.py", "line": 1, "text": "…"}]}]
            for _ in range(2):  # idempotent
                assert ingest.ingest_integrations(d, "integra", rows) == 1
            with d.session() as s:
                rec = s.run("""
                    MATCH (a:Repository {name:'integra'})-[r:INTEGRATES]->
                          (b:Repository {name:'integrb'})
                    RETURN count(r) AS edges, r.refs AS refs,
                           r.files AS files, r.samples AS samples
                """).single()
                import json as _json
                assert rec["edges"] == 1 and rec["refs"] == 7
                assert rec["files"] == 3
                samples = _json.loads(rec["samples"])  # stored as JSON string
                assert samples[0]["path"] == "a.py"
        finally:
            with d.session() as s:
                s.run("MATCH (a:Repository) WHERE a.name IN ['integra','integrb'] "
                      "DETACH DELETE a")
            d.close()
