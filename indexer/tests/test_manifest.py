"""Stage 0/1 unit tests: manifest validation + clone ensurement +
clean-tree enforcement (design.md §3)."""

import json
import subprocess
from pathlib import Path

import pytest

from indexer import manifest as M


def write_manifest(tmp_path: Path, payload) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(payload))
    return p


class TestValidation:
    def test_valid_manifest(self, tmp_path):
        p = write_manifest(tmp_path, {"repos": [
            {"name": "a", "path": str(tmp_path / "a"), "cloneUrl": "https://x.git"}]})
        m = M.load_manifest(p)
        assert len(m.repos) == 1
        assert m.repos[0].shallow is True
        assert m.index_store is None

    def test_missing_file(self, tmp_path):
        with pytest.raises(M.ManifestError, match="not found"):
            M.load_manifest(tmp_path / "nope.json")

    def test_repos_required_nonempty(self, tmp_path):
        p = write_manifest(tmp_path, {"repos": []})
        with pytest.raises(M.ManifestError, match="non-empty"):
            M.load_manifest(p)

    def test_duplicate_names(self, tmp_path):
        p = write_manifest(tmp_path, {"repos": [
            {"name": "a", "path": "/tmp/a", "cloneUrl": "u"},
            {"name": "a", "path": "/tmp/b", "cloneUrl": "u"}]})
        with pytest.raises(M.ManifestError, match="duplicate name 'a'"):
            M.load_manifest(p)

    def test_missing_path_and_cloneurl(self, tmp_path):
        p = write_manifest(tmp_path, {"repos": [{"name": "a", "path": str(tmp_path / "missing")}]})
        with pytest.raises(M.ManifestError, match="cloneUrl"):
            M.load_manifest(p)

    def test_index_store_validation(self, tmp_path):
        p = write_manifest(tmp_path, {
            "repos": [{"name": "a", "path": "/tmp/a", "cloneUrl": "u"}],
            "indexStore": {"provider": "gitlab", "repo": "o/n"}})
        with pytest.raises(M.ManifestError, match="unsupported provider"):
            M.load_manifest(p)

    def test_index_store_ok(self, tmp_path):
        p = write_manifest(tmp_path, {
            "repos": [{"name": "a", "path": "/tmp/a", "cloneUrl": "u"}],
            "indexStore": {"repo": "prilepski/multi-repo-index-store"}})
        m = M.load_manifest(p)
        assert m.index_store.provider == "github"
        assert m.index_store.branch == "main"
        assert m.index_store.auth == "gh"

    def test_all_errors_collected(self, tmp_path):
        p = write_manifest(tmp_path, {"repos": [
            {"name": "", "path": "/tmp/a"},
            {"name": "b", "path": str(tmp_path / "nope")}]})
        with pytest.raises(M.ManifestError) as exc:
            M.load_manifest(p)
        assert "name" in str(exc.value) and "cloneUrl" in str(exc.value)


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".gitkeep").write_text("")
    for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "x"]):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-C", str(path), *args], check=True, capture_output=True)
    return path


class TestEnsureRepo:
    def _spec(self, repo: Path) -> M.RepoSpec:
        return M.RepoSpec(name="r", path=repo, clone_url=None, shallow=True)

    def test_clean_repo_ok(self, tmp_path):
        repo = make_git_repo(tmp_path / "r")
        (repo / "f.txt").write_text("hi\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-C", str(repo), "commit", "-m", "f"], check=True,
                       capture_output=True)
        ready = M.ensure_repo(self._spec(repo))
        assert ready.sha
        assert not ready.cleaned

    def test_untracked_refused(self, tmp_path):
        repo = make_git_repo(tmp_path / "r")
        (repo / "target").mkdir()
        (repo / "target" / "X.class").write_text("junk")
        with pytest.raises(M.ManifestError, match="build residue"):
            M.ensure_repo(self._spec(repo))

    def test_untracked_force_cleaned(self, tmp_path):
        repo = make_git_repo(tmp_path / "r")
        (repo / "target").mkdir()
        (repo / "target" / "X.class").write_text("junk")
        ready = M.ensure_repo(self._spec(repo), force_clean=True)
        assert len(ready.cleaned) == 1
        assert not (repo / "target").exists()

    def test_codegraph_dir_exempt(self, tmp_path):
        repo = make_git_repo(tmp_path / "r")
        (repo / ".codegraph").mkdir()
        (repo / ".codegraph" / "codegraph.db").write_bytes(b"x" * 16)
        ready = M.ensure_repo(self._spec(repo))
        assert ready.sha

    def test_clone_when_missing(self, tmp_path):
        origin = make_git_repo(tmp_path / "origin")
        spec = M.RepoSpec(name="r", path=tmp_path / "clones" / "r",
                          clone_url=str(origin), shallow=True)
        ready = M.ensure_repo(spec)
        assert ready.cloned
        assert ready.sha

    def test_not_a_git_checkout(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(M.ManifestError, match="not a git checkout"):
            M.ensure_repo(self._spec(plain))
