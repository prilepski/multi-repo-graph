# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Workspace restructure tests (workspace-restructure.md): workspace.json
loading, skeleton/git ensure, publish-into-workspace, runtime deploy."""

import json
import subprocess
from pathlib import Path

import pytest

from indexer import deploy, publish
from indexer import workspace as ws_mod
from indexer.manifest import ManifestError


def write_workspace(root: Path, repos=None, settings=None) -> Path:
    payload = {"repos": repos if repos is not None else [
        {"name": "a", "path": "repos/a", "cloneUrl": "https://x.git"}]}
    if settings:
        payload["settings"] = settings
    root.mkdir(parents=True, exist_ok=True)
    (root / "workspace.json").write_text(json.dumps(payload))
    return root


class TestLoadWorkspace:
    def test_minimal(self, tmp_path):
        root = write_workspace(tmp_path)
        ws = ws_mod.load_workspace(root)
        assert ws.repos[0].name == "a"
        # workspace-relative path resolved against the workspace root
        assert ws.repos[0].path == (root / "repos" / "a").resolve()
        assert ws.settings.keep_last == 3
        assert ws.settings.remote_push is False

    def test_settings_applied(self, tmp_path):
        root = write_workspace(tmp_path, settings={
            "keepLast": 5, "viewerPort": 7700,
            "neo4j": {"boltPort": 7800, "password": "pw"}})
        ws = ws_mod.load_workspace(root)
        assert ws.settings.keep_last == 5
        assert ws.settings.viewer_port == 7700
        assert ws.settings.neo4j_bolt_port == 7800
        assert ws.settings.neo4j_password == "pw"
        assert ws.settings.remote_push is False

    def test_missing_workspace_json(self, tmp_path):
        with pytest.raises(ManifestError, match="workspace.json"):
            ws_mod.load_workspace(tmp_path)

    def test_repos_validation_reused(self, tmp_path):
        root = write_workspace(tmp_path, repos=[{"name": "", "path": "x"}])
        with pytest.raises(ManifestError):
            ws_mod.load_workspace(root)


class TestEnsureWorkspace:
    def test_creates_git_repo_and_skeleton(self, tmp_path):
        root = write_workspace(tmp_path)
        ws = ws_mod.ensure_workspace(root)
        assert (ws.root / ".git").exists()
        for d in (ws.repos_dir, ws.snapshot_dir, ws.logs_dir):
            assert d.is_dir()
        gi = (ws.root / ".gitignore").read_text()
        assert "runs" not in gi.split("repos")[0]  # runs/ must be committed
        assert "repos/" in gi and "snapshot/" in gi
        assert "*.dump.zst filter=lfs" in (ws.root / ".gitattributes").read_text()

    def test_idempotent(self, tmp_path):
        root = write_workspace(tmp_path)
        ws1 = ws_mod.ensure_workspace(root)
        ws2 = ws_mod.ensure_workspace(root)
        assert ws1.root == ws2.root


class TestPublishWorkspace:
    def _make_run(self, path: Path, run_id: str) -> Path:
        run_dir = path / f"staging/{run_id}"
        (run_dir / "codegraphs/x").mkdir(parents=True)
        (run_dir / "neo4j").mkdir()
        (run_dir / "run-manifest.json").write_text(json.dumps({"runId": run_id}))
        (run_dir / "summary.json").write_text("{}")
        (run_dir / "codegraphs/x/codegraph.db.zst").write_bytes(b"z")
        (run_dir / "neo4j/neo4j.dump.zst").write_bytes(b"d")
        return run_dir

    def test_commit_prune_retention(self, tmp_path):
        ws_root = write_workspace(tmp_path / "ws")
        ws = ws_mod.ensure_workspace(ws_root)
        for i, rid in enumerate(["r1", "r2", "r3", "r4"]):
            run_dir = self._make_run(tmp_path, rid)
            result = publish.publish_workspace(
                run_dir, ws.root, rid, f"index-run: {rid}",
                keep_last=3, push=False)
            assert result["committed"]
            assert not result["pushed"]
        assert not (ws_root / "runs/r1").exists()  # pruned from the tree
        latest = json.loads((ws_root / "latest.json").read_text())
        assert latest["run"] == "r4"
        log = subprocess.run(["git", "-C", str(ws_root), "log", "--oneline"],
                             capture_output=True, text=True).stdout
        assert "index-run: r4" in log
        # pruned runs stay REACHABLE in history (rollback), gone from HEAD
        tree = subprocess.run(["git", "-C", str(ws_root), "ls-tree",
                               "--name-only", "HEAD", "runs/"],
                              capture_output=True, text=True).stdout.split()
        assert "runs/r1" not in tree and "runs/r4" in tree

    def test_dry_run_never_pushes(self, tmp_path):
        ws_root = write_workspace(tmp_path / "ws")
        ws_mod.ensure_workspace(ws_root)
        run_dir = self._make_run(tmp_path, "r1")
        result = publish.publish_workspace(
            run_dir, ws_root, "r1", "msg", keep_last=3,
            push=True, dry_run=True)
        assert result["committed"] and not result["pushed"] and result["dry_run"]
        remotes = subprocess.run(["git", "-C", str(ws_root), "remote"],
                                 capture_output=True, text=True).stdout
        assert remotes.strip() == ""  # nothing to push to anyway; committed only


class TestDeployRuntime:
    def test_copies_components_scripts_and_version(self, tmp_path):
        ws_root = write_workspace(tmp_path / "ws")
        ws = ws_mod.ensure_workspace(ws_root)
        deployed = deploy.deploy_runtime(ws.root)
        rt = ws.root / "runtime"
        assert (rt / "mcp-server" / "server.py").exists()
        assert (rt / "mcp-server" / "backend.py").exists()
        assert (rt / "codegraph-mcp" / "fanout.mjs").exists()
        assert (rt / "graph-viewer" / "viewer_server.py").exists()
        pkg = json.loads((rt / "codegraph-mcp" / "package.json").read_text())
        assert pkg["dependencies"]["@colbymchenry/codegraph"] == "1.6.2"
        for shim in ("up.sh", "up.ps1", "down.sh", "down.ps1"):
            assert (rt / "scripts" / shim).exists()
        assert (rt / "scripts" / "up.py").exists()
        assert (rt / "VERSION").exists()
        assert deployed["version"]["toolCommit"]

    def test_hand_written_readme_preserved(self, tmp_path):
        ws_root = write_workspace(tmp_path / "ws")
        ws = ws_mod.ensure_workspace(ws_root)
        (ws.root / "README.md").write_text(
            "# Curated sample\n<!-- mrg:keep-readme -->\n")
        deploy.deploy_runtime(ws.root)
        content = (ws.root / "README.md").read_text()
        assert "Curated sample" in content
        assert "up.sh" not in content  # generated version did not clobber

    def test_idempotent_second_deploy(self, tmp_path):
        ws_root = write_workspace(tmp_path / "ws")
        ws = ws_mod.ensure_workspace(ws_root)
        deploy.deploy_runtime(ws.root)
        second = deploy.deploy_runtime(ws.root)
        assert second["copied"] == []
