# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""The codegraph-workspace: the per-company repo that owns the repo list,
every indexed snapshot, and the deployed runtime (workspace-restructure.md).

    <workspace>/
      workspace.json        repos + settings + remote (validated here)
      runs/<ts>/            committed snapshots (published by the pipeline)
      runtime/              committed, deployed by stage 8
      repos/                gitignored: repo clones (cache + input)
      snapshot/             gitignored: materialized state for services
      logs/                 gitignored: indexer logs

The tool repo contains code only; the pipeline runs out of a tool checkout
and writes everything here.
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import manifest as manifest_mod
from .manifest import ManifestError, RepoSpec

WORKSPACE_FILE = "workspace.json"

GITIGNORE = """\
# machine-local state — snapshots and runtime are the committed content
repos/
snapshot/
logs/
runtime/.venv*/
runtime/codegraph-mcp/node_modules/
__pycache__/
.pytest_cache/
.venv/
.DS_Store
"""

GITATTRIBUTES = """\
*.dump.zst filter=lfs diff=lfs merge=lfs -text
*.db.zst filter=lfs diff=lfs merge=lfs -text
"""


@dataclass
class Settings:
    keep_last: int = 3
    concurrency: Optional[int] = None
    neo4j_bolt_port: int = 7688
    neo4j_http_port: int = 7475
    neo4j_password: str = "graphpassword123"
    neo4j_container: str = "mrg-neo4j-instance"
    viewer_port: int = 7600
    remote_push: bool = False


@dataclass
class Workspace:
    root: Path
    repos: List[RepoSpec] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)

    # ---- derived paths ---------------------------------------------------
    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"

    @property
    def repos_dir(self) -> Path:
        return self.root / "repos"

    @property
    def snapshot_dir(self) -> Path:
        return self.root / "snapshot"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def latest_run(self) -> Optional[str]:
        latest = self.root / "latest.json"
        if not latest.exists():
            return None
        try:
            return json.loads(latest.read_text())["run"]
        except (json.JSONDecodeError, KeyError):
            return None


def load_workspace(root: Path) -> Workspace:
    """Load + validate <root>/workspace.json (repos rules are design.md §3)."""
    path = root / WORKSPACE_FILE
    if not path.exists():
        raise ManifestError(
            f"workspace: {path} not found — a codegraph-workspace needs a "
            f"workspace.json (see workspace.example.json in the tool repo)")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ManifestError(f"workspace: {path} is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ManifestError(f"workspace: {path}: expected a JSON object")

    errors: List[str] = []
    repos_raw = raw.get("repos")
    if not isinstance(repos_raw, list) or not repos_raw:
        raise ManifestError("workspace.json: 'repos' must be a non-empty array")

    # reuse the manifest validator on the repos array (same rules)
    repos: List[RepoSpec] = []
    try:
        dummy = {"repos": repos_raw}
        manifest = manifest_mod.load_manifest_dict(dummy)
        repos = manifest.repos
    except ManifestError as e:
        errors.append(str(e))

    settings_raw = raw.get("settings", {}) or {}
    if not isinstance(settings_raw, dict):
        errors.append("workspace.json: 'settings' must be an object")
        settings_raw = {}
    neo4j_raw = settings_raw.get("neo4j", {}) or {}
    settings = Settings(
        keep_last=int(settings_raw.get("keepLast", 3)),
        concurrency=settings_raw.get("concurrency"),
        neo4j_bolt_port=int(neo4j_raw.get("boltPort", 7688)),
        neo4j_http_port=int(neo4j_raw.get("httpPort", 7475)),
        neo4j_password=str(neo4j_raw.get("password", "graphpassword123")),
        neo4j_container=str(neo4j_raw.get("container", "mrg-neo4j-instance")),
        viewer_port=int(settings_raw.get("viewerPort", 7600)),
        remote_push=bool((raw.get("remote") or {}).get("push", False)),
    )

    if errors:
        raise ManifestError("invalid workspace:\n  - " + "\n  - ".join(errors))

    # repo paths are workspace-relative — resolve against the workspace root
    for spec in repos:
        if not spec.path.is_absolute():
            spec.path = (root / spec.path).resolve()

    return Workspace(root=root.resolve(), repos=repos, settings=settings)


def ensure_workspace(root: Path) -> Workspace:
    """Load the workspace and make sure the repo skeleton + git exist.
    The workspace is ALWAYS a git repo (decision C): one publish path,
    free history and rollback. Local-only use simply has no remote."""
    ws = load_workspace(root)
    root = ws.root
    for d in (ws.repos_dir, ws.snapshot_dir, ws.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-b", "main"], cwd=str(root),
                       check=True, capture_output=True)
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE, encoding="utf-8")
    ga = root / ".gitattributes"
    if not ga.exists():
        ga.write_text(GITATTRIBUTES, encoding="utf-8")
    return ws


def resolve_workspace_arg(arg: str) -> Path:
    """--workspace accepts a local path or a git URL (cloned into
    ./workspaces/<name> once; subsequent runs reuse and pull)."""
    if arg.startswith("http://") or arg.startswith("https://") or arg.endswith(".git"):
        slug = arg.rstrip("/").split("/")[-1].removesuffix(".git")
        target = Path("workspaces") / slug
        if not (target / ".git").exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", arg, str(target)],
                           check=True, capture_output=True, text=True)
        else:
            subprocess.run(["git", "-C", str(target), "pull", "--ff-only"],
                           capture_output=True, text=True)
        return target
    return Path(arg)
