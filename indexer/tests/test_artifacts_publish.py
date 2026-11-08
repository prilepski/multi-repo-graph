"""Stage 6/7 unit tests: run-manifest writer, retention pruning, local
publish end-to-end against a fake store dir (design.md §4, §8)."""

import json
from datetime import datetime, timezone
from pathlib import Path

from indexer import artifacts as art
from indexer import publish


class TestRunManifest:
    def test_fields(self):
        rm = art.build_run_manifest(
            run_id="2026-11-08T12-00-00Z", started_at="t0", finished_at="t1",
            codegraph_version="1.6.2", neo4j_image="neo4j:5-community",
            schema_fingerprint="fp", repos=[{"name": "a"}],
            neo4j={"dump": "neo4j/neo4j.dump.zst", "dumpSha256": "x"},
            verification_status="passed")
        assert rm["runId"] == "2026-11-08T12-00-00Z"
        assert rm["tools"]["codegraph"] == "1.6.2"
        assert rm["neo4j"]["database"] == "neo4j"
        assert rm["verification"]["reportRef"] == "summary.json"

    def test_write(self, tmp_path):
        builder = art.RunArtifacts(tmp_path, "2026-11-08T12-00-00Z")
        path = builder.write_run_manifest({"runId": "x"})
        assert json.loads(path.read_text())["runId"] == "x"
        assert path == tmp_path / "runs/2026-11-08T12-00-00Z/run-manifest.json"

    def test_run_id_format(self):
        dt = datetime(2026, 11, 8, 12, 0, 3, tzinfo=timezone.utc)
        assert art.run_id_for(dt) == "2026-11-08T12-00-03Z"


class TestRetention:
    def _make_store(self, tmp_path: Path, run_ids):
        for rid in run_ids:
            (tmp_path / "runs" / rid / "run-manifest.json").parent.mkdir(
                parents=True, exist_ok=True)
            (tmp_path / "runs" / rid / "run-manifest.json").write_text("{}")
        return tmp_path

    def test_prune_keeps_last_n(self, tmp_path):
        store = self._make_store(tmp_path, ["r1", "r2", "r3", "r4", "r5"])
        removed = publish.prune_old_runs(store, keep_last=3)
        assert removed == ["r2", "r1"]
        assert art.list_run_ids(store) == ["r5", "r4", "r3"]

    def test_prune_noop_when_under_limit(self, tmp_path):
        store = self._make_store(tmp_path, ["r1"])
        assert publish.prune_old_runs(store, keep_last=3) == []

    def test_latest_json(self, tmp_path):
        publish.write_latest(tmp_path, "r9")
        assert json.loads((tmp_path / "latest.json").read_text()) == {"run": "r9"}


class TestPublishLocal:
    def test_end_to_end(self, tmp_path):
        # build a fake run dir with both artifacts + manifests
        run_dir = tmp_path / "staging/runs/2026-11-08T00-00-00Z"
        (run_dir / "codegraphs/fixture").mkdir(parents=True)
        (run_dir / "neo4j").mkdir(parents=True)
        (run_dir / "codegraphs/fixture/codegraph.db.zst").write_bytes(b"zst")
        (run_dir / "neo4j/neo4j.dump.zst").write_bytes(b"dump")
        (run_dir / "summary.json").write_text("{}")
        (run_dir / "run-manifest.json").write_text("{}")

        store = tmp_path / "store"
        publish.publish_local(run_dir, store, "2026-11-08T00-00-00Z", keep_last=3)
        assert (store / "runs/2026-11-08T00-00-00Z/neo4j/neo4j.dump.zst").exists()
        assert json.loads((store / "latest.json").read_text())["run"] == \
            "2026-11-08T00-00-00Z"

        # 2nd and 3rd runs fit under keepLast=3; the 4th prunes the 1st
        for rid in ("2026-11-09T00-00-00Z", "2026-11-10T00-00-00Z",
                    "2026-11-11T00-00-00Z"):
            run2 = tmp_path / f"staging/runs/{rid}"
            run2.mkdir(parents=True)
            (run2 / "summary.json").write_text("{}")
            publish.publish_local(run2, store, rid, keep_last=3)
        assert not (store / "runs/2026-11-08T00-00-00Z").exists()
        assert art.list_run_ids(store) == [
            "2026-11-11T00-00-00Z", "2026-11-10T00-00-00Z",
            "2026-11-09T00-00-00Z"]


class TestZstd:
    def test_roundtrip(self, tmp_path):
        src = tmp_path / "f.bin"
        src.write_bytes(b"hello" * 10000)
        dest = art.zstd_compress(src, tmp_path / "out/f.bin.zst")
        assert dest.exists() and dest.stat().st_size < src.stat().st_size
        import subprocess
        back = subprocess.run(["zstd", "-dc", str(dest)], capture_output=True)
        assert back.stdout == src.read_bytes()
