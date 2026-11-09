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
        assert users["refs"] == 3  # two code lines + one md line
        assert users["files"] == 2
        assert users["byClass"] == {"code": 2, "docs": 1}
        assert users["strongest"] == "code"
        assert any("jwt_issuer" in s["text"] for s in users["samples"])
        assert all(s["path"] != "logo.png" for s in users["samples"])
        # samples are class-tagged and ordered strongest class first
        assert all("cls" in s for s in users["samples"])
        assert users["samples"][0]["cls"] == "code"

    def test_no_false_positives_on_word_overlap(self, tmp_path):
        """Bare service short names are English words — only the FULL repo
        name may match. 'content-type' must not count as a demo-content
        reference; 'demo-users-api' IS one (references the service)."""
        repo = _git_repo(tmp_path / "svc-a", {
            "a.py": "h = {'content-type': 'application/json'}\n"
                    "url = 'https://demo-users-api.internal/x'\n"})
        out = {i["target"]: i for i in integrations.detect_integrations(
            repo, "svc-a", ["svc-a", "demo-content", "demo-users"],
            git_tracked_files(repo))}
        assert "demo-content" not in out
        assert out["demo-users"]["refs"] == 1

    def test_self_and_missing_ignored(self, tmp_path):
        repo = _git_repo(tmp_path / "svc-a", {"a.py": "svc-a config\n"})
        out = integrations.detect_integrations(
            repo, "svc-a", ["svc-a"], git_tracked_files(repo))
        assert out == []

    def test_evidence_classification(self, tmp_path):
        """One repo touching another via every evidence class: .gitmodules
        entry, executable code, code comment, infra config, CI, docs."""
        repo = _git_repo(tmp_path / "web", {
            ".gitmodules": (
                '[submodule "external/vp"]\n'
                "\tpath = external/vp\n"
                "\turl = https://github.com/x/demo-pipeline.git\n"),
            "app/config.py": (
                'bucket = "demo-pipeline-incoming"\n'
                "# uploaded to demo-pipeline for transcoding\n"
                "idle = 1\n"),
            "infra/main.tf": (
                'resource "aws_sqs_queue" "q" {\n'
                '  name = "demo-pipeline-events"\n}\n'),
            ".github/workflows/ci.yml": "  # deploy next to demo-pipeline\n",
            "docs/design.md": "See demo-pipeline for encoding.\n",
        })
        out = {i["target"]: i for i in integrations.detect_integrations(
            repo, "web", ["web", "demo-pipeline"], git_tracked_files(repo))}
        vp = out["demo-pipeline"]
        assert vp["refs"] == 6
        assert vp["files"] == 5
        assert vp["byClass"] == {"submodule": 1, "code": 1, "comment": 1,
                                 "config": 2, "docs": 1}
        assert vp["strongest"] == "submodule"
        # strongest-first sample ordering (config ranks above comment);
        # MAX_SAMPLES=5 drops the weakest class (docs) from the slots
        order = [s["cls"] for s in vp["samples"]]
        assert order == ["submodule", "code", "config", "config", "comment"]

    def test_strongest_is_weakest_class_when_only_docs(self, tmp_path):
        repo = _git_repo(tmp_path / "a", {
            "README.md": "Related: demo-wiki.\n",
            "notes.txt": "also demo-wiki\n",
        })
        out = {i["target"]: i for i in integrations.detect_integrations(
            repo, "a", ["a", "demo-wiki"], git_tracked_files(repo))}
        wiki = out["demo-wiki"]
        assert wiki["strongest"] == "docs"
        assert wiki["byClass"] == {"docs": 2}
        assert wiki["refs"] == 2

    def test_classify_file(self):
        """Path -> evidence class mapping (V1: path + extension rules)."""
        f = integrations.classify_file
        assert f(".gitmodules") == "submodule"
        assert f("nested/.gitmodules") == "submodule"
        assert f("app/main.py") == "code"
        assert f("src/x.ts") == "code"
        assert f("infra/main.tf") == "config"
        assert f("k8s/deploy.yaml") == "config"
        assert f("pkg.json") == "config"
        assert f(".github/workflows/ci.yml") == "config"   # path override
        assert f(".github/scripts/selfheal.py") == "config"  # CI tooling
        assert f("Dockerfile") == "config"
        assert f(".env.local") == "config"
        assert f("ws.code-workspace") == "config"
        assert f("README.md") == "docs"
        assert f("docs/spec.rst") == "docs"
        assert f("weird.zzz") == "docs"  # unknown ext -> weakest class


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
                     "byClass": {"code": 2, "docs": 5}, "strongest": "code",
                     "samples": [{"path": "a.py", "line": 1, "cls": "code",
                                  "text": "…"}]}]
            for _ in range(2):  # idempotent
                assert ingest.ingest_integrations(d, "integra", rows) == 1
            with d.session() as s:
                rec = s.run("""
                    MATCH (a:Repository {name:'integra'})-[r:INTEGRATES]->
                          (b:Repository {name:'integrb'})
                    RETURN count(r) AS edges, r.refs AS refs,
                           r.files AS files, r.samples AS samples,
                           r.byClass AS byClass, r.strongest AS strongest
                """).single()
                import json as _json
                assert rec["edges"] == 1 and rec["refs"] == 7
                assert rec["files"] == 3
                assert rec["strongest"] == "code"
                # containers are stored as JSON strings (neo4j props)
                assert _json.loads(rec["byClass"]) == {"code": 2, "docs": 5}
                samples = _json.loads(rec["samples"])
                assert samples[0]["path"] == "a.py"
                assert samples[0]["cls"] == "code"
        finally:
            with d.session() as s:
                s.run("MATCH (a:Repository) WHERE a.name IN ['integra','integrb'] "
                      "DETACH DELETE a")
            d.close()
