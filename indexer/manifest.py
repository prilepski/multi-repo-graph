"""Stage 0 (validate manifest) and stage 1 (ensure clones) — design.md §2/§3.

Manifest format (see ../manifest.example.json):

    {
      "repos": [
        {"name": "...", "cloneUrl": "...?", "path": "...", "shallow": true?},
        ...
      ],
      "indexStore": {"provider": "github", "repo": "owner/name",
                     "branch": "main"?, "auth": "gh"?}?
    }

Rules enforced here (design.md §3):
  - repos[] required, non-empty, names unique
  - path used if it exists (must be a git checkout); otherwise cloned from
    cloneUrl into path's parent (shallow --depth 1 by default)
  - working trees must be clean of build residue (java-graph lesson:
    1,667 stray target/**/*.class files) — untracked files fail the run
    unless force_clean, which git-clean's them (our own .codegraph/ is
    always exempt)
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

CODEGRAPH_DIR = ".codegraph"


class ManifestError(Exception):
    """Manifest validation or clone-ensurement failure."""


@dataclass
class RepoSpec:
    name: str
    path: Path
    clone_url: Optional[str] = None
    shallow: bool = True


@dataclass
class IndexStoreSpec:
    provider: str
    repo: str
    branch: str = "main"
    auth: str = "gh"


@dataclass
class Manifest:
    repos: List[RepoSpec]
    index_store: Optional[IndexStoreSpec] = None


def load_manifest(path: Path) -> Manifest:
    """Load + validate the manifest; collects ALL problems before failing."""
    if not path.exists():
        raise ManifestError(f"manifest '{path}' not found")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ManifestError(f"manifest '{path}' is not valid JSON: {e}") from e

    errors: List[str] = []
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest '{path}': expected a JSON object")
    repos_raw = raw.get("repos")
    if not isinstance(repos_raw, list) or not repos_raw:
        raise ManifestError("manifest: 'repos' must be a non-empty array")

    repos: List[RepoSpec] = []
    names: set = set()
    for i, r in enumerate(repos_raw):
        if not isinstance(r, dict):
            errors.append(f"repos[{i}]: expected an object, got {type(r).__name__}")
            continue
        name = r.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"repos[{i}]: 'name' must be a non-empty string")
            continue
        if name in names:
            errors.append(f"repos[{i}]: duplicate name '{name}'")
            continue
        names.add(name)
        path = r.get("path")
        clone_url = r.get("cloneUrl")
        if not isinstance(path, str) or not path:
            errors.append(f"repos[{name}]: 'path' is required")
            continue
        if not Path(path).exists() and not isinstance(clone_url, str):
            errors.append(
                f"repos[{name}]: path '{path}' does not exist and 'cloneUrl' "
                f"is missing — nothing to clone from")
            continue
        shallow = r.get("shallow", True)
        if not isinstance(shallow, bool):
            errors.append(f"repos[{name}]: 'shallow' must be a boolean")
            shallow = True
        repos.append(RepoSpec(name=name, path=Path(path),
                              clone_url=clone_url, shallow=shallow))

    index_store = None
    store_raw = raw.get("indexStore")
    if store_raw is not None:
        if not isinstance(store_raw, dict):
            errors.append("indexStore: expected an object")
        else:
            provider = store_raw.get("provider", "github")
            if provider != "github":
                errors.append(f"indexStore: unsupported provider '{provider}' "
                              f"(only 'github')")
            store_repo = store_raw.get("repo")
            if not isinstance(store_repo, str) or store_repo.count("/") != 1:
                errors.append("indexStore: 'repo' must be 'owner/name'")
                store_repo = store_repo if isinstance(store_repo, str) else ""
            index_store = IndexStoreSpec(
                provider=provider,
                repo=store_repo or "",
                branch=store_raw.get("branch", "main"),
                auth=store_raw.get("auth", "gh"),
            )

    if errors:
        raise ManifestError("invalid manifest:\n  - " + "\n  - ".join(errors))
    return Manifest(repos=repos, index_store=index_store)


def _git(repo_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_dir), *args],
                          capture_output=True, text=True, check=check)


def is_git_checkout(path: Path) -> bool:
    try:
        _git(path, "rev-parse", "--git-dir")
        return True
    except subprocess.CalledProcessError:
        return False


def head_sha(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def untracked_files(path: Path) -> List[str]:
    """Untracked files, excluding our own .codegraph/ index directory."""
    out = _git(path, "status", "--porcelain", "--untracked-files=all").stdout
    return [line[3:] for line in out.splitlines()
            if line.startswith("??") and not line[3:].startswith(CODEGRAPH_DIR)]


def modified_files(path: Path) -> List[str]:
    out = _git(path, "status", "--porcelain").stdout
    return [line[3:] for line in out.splitlines()
            if line and not line.startswith("??")]


@dataclass
class RepoReady:
    """A repo validated for indexing (stage 1 output)."""
    spec: RepoSpec
    cloned: bool = False
    sha: str = ""
    cleaned: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def ensure_repo(spec: RepoSpec, force_clean: bool = False) -> RepoReady:
    """Validate one repo entry: clone if missing, enforce a clean tree,
    record HEAD sha. Raises ManifestError on refusal."""
    ready = RepoReady(spec=spec)
    if not spec.path.exists():
        if spec.clone_url is None:
            raise ManifestError(f"{spec.name}: path missing and no cloneUrl")
        parent = spec.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone"]
        if spec.shallow:
            cmd.append("--depth")
            cmd.append("1")
        cmd += [spec.clone_url, str(spec.path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise ManifestError(f"{spec.name}: clone failed: {proc.stderr.strip()[:500]}")
        ready.cloned = True
    elif not is_git_checkout(spec.path):
        raise ManifestError(
            f"{spec.name}: path '{spec.path}' exists but is not a git checkout")

    untracked = untracked_files(spec.path)
    if untracked:
        if force_clean:
            _git(spec.path, "clean", "-fdx", f"-e={CODEGRAPH_DIR}")
            ready.cleaned = untracked
        else:
            sample = ", ".join(untracked[:5])
            more = f" (+{len(untracked) - 5} more)" if len(untracked) > 5 else ""
            raise ManifestError(
                f"{spec.name}: {len(untracked)} untracked file(s) in working tree "
                f"(build residue guard, design.md §3) — clean them or re-run with "
                f"--force-clean. e.g. {sample}{more}")

    modified = modified_files(spec.path)
    if modified:
        ready.warnings.append(
            f"{len(modified)} tracked file(s) locally modified "
            f"(indexed as-is; e.g. {modified[0]})")

    ready.sha = head_sha(spec.path)
    return ready


def ensure_all(manifest: Manifest, force_clean: bool = False) -> List[RepoReady]:
    return [ensure_repo(spec, force_clean) for spec in manifest.repos]


def codegraph_db_path(repo_path: Path) -> Path:
    return repo_path / CODEGRAPH_DIR / "codegraph.db"
