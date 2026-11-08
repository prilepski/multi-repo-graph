# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Stage 7: publish a run directory to the index store (design.md §2/§4, D3).

- manifest WITHOUT indexStore → local mode: copy runs/<id>/ into a folder
  next to the repos directory (default <repos-common-parent>/index-store)
- manifest WITH indexStore   → GitHub mode: clone/pull the private store
  repo (git+gh auth, LFS-smudge-skip), copy the run in, ensure LFS
  .gitattributes, write latest.json, prune beyond keepLast, commit, push.
  `dry_run` builds everything and prints the commit, pushes nothing
  (design.md §8: GitHub mode is exercised via --dry-run).

Retention: keepLast (default 3) newest run dirs after a successful publish;
the prune is part of the same push.
"""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import artifacts as artifacts_mod
from .manifest import IndexStoreSpec

log = logging.getLogger("indexer.publish")

LFS_ATTRIBUTES = [
    "*.dump.zst filter=lfs diff=lfs merge=lfs -text",
    "*.db.zst filter=lfs diff=lfs merge=lfs -text",
]


def _run(cmd: List[str], cwd: Optional[Path] = None, env_extra: Optional[dict] = None
         ) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, env=env)


def write_latest(store_root: Path, run_id: str) -> None:
    (store_root / "latest.json").write_text(
        json.dumps({"run": run_id}) + "\n", encoding="utf-8")


def prune_old_runs(store_root: Path, keep_last: int) -> List[str]:
    """Delete all but the newest keepLast run dirs; returns removed ids."""
    run_ids = artifacts_mod.list_run_ids(store_root)
    removed = []
    for old in run_ids[keep_last:]:
        target = store_root / "runs" / old
        shutil.rmtree(target)
        removed.append(old)
    return removed


# ---------------------------------------------------------------------------
# local mode
# ---------------------------------------------------------------------------

def publish_local(run_dir: Path, store_root: Path, run_id: str,
                  keep_last: int) -> Path:
    store_root.mkdir(parents=True, exist_ok=True)
    dest = store_root / "runs" / run_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(run_dir, dest)
    write_latest(store_root, run_id)
    removed = prune_old_runs(store_root, keep_last)
    if removed:
        log.info("  [+] Pruned %d old run(s): %s", len(removed), ", ".join(removed))
    log.info("  [+] Published run %s to %s", run_id, dest)
    return dest


# ---------------------------------------------------------------------------
# GitHub mode
# ---------------------------------------------------------------------------

def _work_dir(store: IndexStoreSpec) -> Path:
    slug = store.repo.replace("/", "__")
    return Path(".publish-work") / slug


def _git(store: IndexStoreSpec, workdir: Path, *args: str
         ) -> subprocess.CompletedProcess:
    env = {"GIT_LFS_SKIP_SMUDGE": "1"}
    res = _run(["git", *args], cwd=workdir, env_extra=env)
    return res


def publish_github(run_dir: Path, store: IndexStoreSpec, run_id: str,
                   commit_message: str, keep_last: int, dry_run: bool) -> bool:
    """Returns True if pushed (False in dry-run). Raises on failure.
    Handles a brand-new (empty, unborn-HEAD) store repo."""
    workdir = _work_dir(store)
    url = f"https://github.com/{store.repo}.git"

    if not (workdir / ".git").exists():
        workdir.mkdir(parents=True, exist_ok=True)
        log.info("[*] Cloning store repo %s (LFS smudge off)...", store.repo)
        auth = _run(["gh", "auth", "setup-git"])
        if auth.returncode != 0:
            log.warning("  [!] gh auth setup-git failed (push may fail): %s",
                        auth.stderr.strip()[:200])
        # no --branch: an empty repo has no branches to name
        res = _run(["git", "clone", url, "."], cwd=workdir,
                   env_extra={"GIT_LFS_SKIP_SMUDGE": "1"})
        if res.returncode != 0:
            raise RuntimeError(f"git clone of {url} failed: {res.stderr.strip()[:500]}")
        # unborn HEAD (fresh repo) -> create the branch locally
        if _git(store, workdir, "rev-parse", "--verify", "HEAD").returncode != 0:
            _git(store, workdir, "checkout", "-B", store.branch)
    else:
        _git(store, workdir, "fetch", "origin")
        if _git(store, workdir, "rev-parse", "--verify",
                f"origin/{store.branch}").returncode == 0:
            _git(store, workdir, "reset", "--hard", f"origin/{store.branch}")

    # LFS attributes for the big artifacts (GitHub rejects >100 MB blobs)
    attrs = workdir / ".gitattributes"
    existing = attrs.read_text().splitlines() if attrs.exists() else []
    merged = existing[:]
    for line in LFS_ATTRIBUTES:
        if line.split()[0] not in [l.split()[0] for l in merged if l.strip()]:
            merged.append(line)
    attrs.write_text("\n".join(merged) + "\n", encoding="utf-8")

    dest = workdir / "runs" / run_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(run_dir, dest)
    write_latest(workdir, run_id)
    removed = prune_old_runs(workdir, keep_last)

    _git(store, workdir, "add", "-A")
    _git(store, workdir, "commit", "-m", commit_message)
    stat = _git(store, workdir, "show", "--stat", "--oneline", "HEAD")

    if dry_run:
        log.info("[*] DRY RUN — commit built but NOT pushed:")
        for line in stat.stdout.splitlines()[:40]:
            log.info("    %s", line)
        if removed:
            log.info("    (would prune: %s)", ", ".join(removed))
        # roll back so the next run rebuilds from the remote state
        if _git(store, workdir, "rev-parse", "--verify",
                f"origin/{store.branch}").returncode == 0:
            _git(store, workdir, "reset", "--hard", f"origin/{store.branch}")
        return False

    res = _git(store, workdir, "push", "origin", store.branch)
    if res.returncode != 0:
        raise RuntimeError(f"git push to {url} failed: {res.stderr.strip()[:500]}")
    log.info("  [+] Pushed run %s to %s@%s%s", run_id, store.repo, store.branch,
             f" (pruned {', '.join(removed)})" if removed else "")
    return True


# ---------------------------------------------------------------------------
# workspace mode (workspace-restructure.md): the workspace IS the store
# ---------------------------------------------------------------------------

def _git_ws(ws_root: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=ws_root)


def _ensure_git_identity(ws_root: Path) -> None:
    res = _git_ws(ws_root, "config", "user.email")
    if res.returncode != 0:
        _git_ws(ws_root, "config", "user.email", "multi-repo-graph@localhost")
        _git_ws(ws_root, "config", "user.name", "multi-repo-graph")


def publish_workspace(run_dir: Path, ws_root: Path, run_id: str,
                      commit_message: str, keep_last: int,
                      push: bool, dry_run: bool = False) -> Dict[str, Any]:
    """Copy the run into runs/, write latest.json, prune beyond keepLast,
    commit (and push when enabled). The workspace is always a git repo."""
    runs_root = ws_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    dest = runs_root / run_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(run_dir, dest)
    write_latest(ws_root, run_id)
    removed = prune_old_runs(ws_root, keep_last)

    _ensure_git_identity(ws_root)
    _git_ws(ws_root, "add", "-A")
    committed = _git_ws(ws_root, "commit", "-m", commit_message).returncode == 0
    stat = _git_ws(ws_root, "log", "-1", "--stat", "--oneline")
    pushed = False
    if committed and push and not dry_run:
        res = _git_ws(ws_root, "push")
        if res.returncode != 0:
            raise RuntimeError(
                f"git push of workspace failed: {res.stderr.strip()[:500]}")
        pushed = True
    return {"dest": str(dest), "committed": committed, "pushed": pushed,
            "pruned": removed, "log": stat.stdout[:2000],
            "dry_run": dry_run}
