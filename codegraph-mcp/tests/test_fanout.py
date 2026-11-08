"""fanout.mjs contract tests: multi-repo merge, repo tagging, per-repo
failure isolation, repo filtering."""

from conftest import fanout


class TestSearch:
    def test_hits_from_both_repos_tagged(self, wrapper_repos):
        repos_json, _ = wrapper_repos
        out = fanout(repos_json, "search", {"query": "App"})
        assert out["command"] == "search"
        assert out["repos"] == ["repoa", "repob"]
        by_repo = {}
        for r in out["results"]:
            if "error" in r:
                raise AssertionError(r)
            for h in r["hits"]:
                by_repo.setdefault(r["repo"], []).append(h["name"])
        assert any("AppA" in names for names in by_repo.get("repoa", []))
        assert any("AppB" in names for names in by_repo.get("repob", []))

    def test_simple_name_collision_yields_distinct_hits(self, wrapper_repos):
        """Both repos declare types under org.acme — the same simple name in
        two repos must surface as repo-tagged hits, never silently merged;
        a name unique to one repo must not appear under the other."""
        repos_json, _ = wrapper_repos
        out = fanout(repos_json, "search", {"query": "Codes"})
        fqns = {(r["repo"], h["qualifiedName"])
                for r in out["results"] for h in r["hits"]}
        assert ("repoa", "org.acme.util::Codes") in fqns
        # Codes exists only in repo A — repo B must never claim it
        assert not any("Codes" in qn and repo == "repob"
                       for repo, qn in fqns)

    def test_language_fanout_typescript(self, wrapper_repos):
        repos_json, _ = wrapper_repos
        out = fanout(repos_json, "search", {"query": "format"})
        hits = {(r["repo"], h["name"], h["language"])
                for r in out["results"] for h in r["hits"]}
        assert any(repo == "repob" and name == "format" and lang == "typescript"
                   for repo, name, lang in hits)


class TestSymbolAndCallers:
    def test_symbol_qualified_name_lookup(self, wrapper_repos):
        repos_json, _ = wrapper_repos
        out = fanout(repos_json, "symbol", {"qualifiedName": "org.acme.util::Codes"})
        nodes = [(r["repo"], n["kind"])
                 for r in out["results"] for n in r.get("nodes", [])]
        assert ("repoa", "class") in nodes

    def test_callers_resolved_within_repo(self, wrapper_repos):
        repos_json, _ = wrapper_repos
        out = fanout(repos_json, "callers", {"name": "encode"})
        callers = [(r["repo"], c["caller"])
                   for r in out["results"] for c in r.get("callers", [])]
        assert any(repo == "repoa" and "AppA" in caller
                   for repo, caller in callers)

    def test_callers_same_name_both_repos(self, wrapper_repos):
        """`run` exists in BOTH repos — fan-out must answer for each."""
        repos_json, _ = wrapper_repos
        out = fanout(repos_json, "callers", {"name": "run"})
        answered = {r["repo"] for r in out["results"]
                    if "callers" in r}
        assert answered == {"repoa", "repob"}

    def test_repo_filter_narrows_fanout(self, wrapper_repos):
        repos_json, _ = wrapper_repos
        out = fanout(repos_json, "callers", {"name": "encode", "repo": "repoa"})
        assert out["repos"] == ["repoa"]
        assert [r["repo"] for r in out["results"]] == ["repoa"]


class TestImpactAndExplore:
    def test_impact_includes_caller(self, wrapper_repos):
        repos_json, _ = wrapper_repos
        out = fanout(repos_json, "impact", {"name": "encode", "depth": 2})
        impacted = {r["repo"]: [i["qualifiedName"] for i in s["impacted"]]
                    for r in out["results"] for s in r.get("impact", [])}
        assert any("AppA" in qn for qn in impacted.get("repoa", []))

    def test_explore_returns_summary_per_repo(self, wrapper_repos):
        repos_json, _ = wrapper_repos
        out = fanout(repos_json, "explore", {"query": "encoding utilities"})
        for r in out["results"]:
            assert "summary" in r or "error" in r, r


class TestFailureIsolation:
    def test_unindexed_repo_isolated(self, wrapper_repos, tmp_path):
        """A repo without a codegraph index must not fail the others."""
        import json as _json
        import subprocess
        from conftest import _git_repo
        repos_json, repos = wrapper_repos
        empty = _git_repo(tmp_path / "empty", {"README.md": "x\n"})
        broken = tmp_path / "repos-broken.json"
        broken.write_text(_json.dumps([
            {"name": "repoa", "path": str(repos["repoa"])},
            {"name": "noindex", "path": str(empty)},
        ]))
        out = fanout(broken, "search", {"query": "Codes"})
        statuses = {r["repo"]: "error" in r for r in out["results"]}
        assert statuses["repoa"] is False          # healthy
        assert statuses["noindex"] is True         # isolated failure
        hits = [h for r in out["results"] if r["repo"] == "repoa"
                for h in r["hits"]]
        assert any("Codes" in h["name"] for h in hits)
