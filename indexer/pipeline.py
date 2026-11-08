#!/usr/bin/env python3
"""multi-repo-graph batch pipeline — stages 0-7 (docs/design.md §2).

  0 validate manifest          4 Neo4j ingest (single writer)
  1 ensure clones              5 verify (counts + sampled edges)
  2 codegraph index (pool)     6 artifacts (dump/codegraph zstd, manifests)
  3 export -> portfolio rows   7 publish (local folder | GitHub store)

Exit code 0 only if every repo indexed, exported, ingested, verified, and
published cleanly; partial runs still write artifacts + summary with explicit
failure markers but fail the run (design.md §2).

Usage:
  python -m indexer.pipeline --manifest manifest.json [--force-clean]
                             [--publish-dry-run] [--keep-last 3] ...
"""

import argparse
import importlib
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indexer import artifacts as art
from indexer import container, exporter, ingest, publish, verify
from indexer import manifest as manifest_mod
from indexer.manifest import ManifestError

log = logging.getLogger("indexer.pipeline")
PYTHON_DEPS = [("neo4j", "neo4j")]


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    filehandler = logging.FileHandler(log_path, encoding="utf-8")
    filehandler.setLevel(logging.DEBUG)
    filehandler.setFormatter(fmt)
    log.handlers.clear()
    log.addHandler(console)
    log.addHandler(filehandler)
    for mod in (container.log, ingest.log, verify.log, publish.log):
        mod.setLevel(logging.DEBUG)
        mod.handlers.clear()
        mod.addHandler(console)
        mod.addHandler(filehandler)
    return log_path


def ensure_python_deps() -> Any:
    try:
        return importlib.import_module("neo4j")
    except ImportError:
        sys.exit("[-] Fatal: 'neo4j' package missing. Run: pip install neo4j")


def run_stage2(manifest_paths: List[Dict[str, str]],
               concurrency: int | None) -> Dict[str, Any]:
    """codegraph index each repo via the Node worker pool."""
    import tempfile
    config: Dict[str, Any] = {"repos": manifest_paths}
    if concurrency:
        config["concurrency"] = concurrency
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(config, tf)
        config_path = tf.name
    try:
        proc = subprocess.run(
            ["node", str(exporter.NODEJS_DIR / "index-pool.mjs"), config_path],
            cwd=str(exporter.NODEJS_DIR), capture_output=True, text=True)
    finally:
        Path(config_path).unlink(missing_ok=True)
    for line in proc.stderr.splitlines():
        log.debug("  [stage2] %s", line)
    results = {}
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("summary"):
            if event["failed"]:
                log.error("  [-] stage 2: %d repo(s) failed to index", event["failed"])
        else:
            results[event["name"]] = event
            mark = "+" if event.get("ok") else "!"
            log.info("  [%s] %-20s indexed %s files, %s nodes, %s edges in %.1fs",
                     mark, event["name"], event.get("filesIndexed"),
                     event.get("nodesCreated"), event.get("edgesCreated"),
                     event.get("ms", 0) / 1000)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="multi-repo-graph: codegraph -> Neo4j portfolio -> artifacts")
    parser.add_argument("--manifest", default="manifest.json")
    parser.add_argument("--engine", default="docker",
                        help="container engine (default: docker)")
    parser.add_argument("--bolt-port", type=int,
                        default=container.DEFAULT_BOLT_PORT)
    parser.add_argument("--http-port", type=int,
                        default=container.DEFAULT_HTTP_PORT)
    parser.add_argument("--neo4j-password", default=container.DEFAULT_PASSWORD)
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--force-clean", action="store_true",
                        help="git-clean untracked build residue instead of refusing")
    parser.add_argument("--publish-dry-run", action="store_true",
                        help="stage 7 GitHub mode: build the commit, push nothing")
    parser.add_argument("--keep-last", type=int, default=3,
                        help="retention: run dirs kept in the store (default 3)")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="stage 2 worker pool size (default min(4, CPUs))")
    parser.add_argument("--local-store-dir", default=None,
                        help="override the local index-store directory")
    args = parser.parse_args()

    started = datetime.now(timezone.utc)
    run_id = art.run_id_for(started)
    log_path = setup_logging(Path(args.log_dir))
    log.info("[*] Run %s started. Log file: %s", run_id, log_path)

    neo4j = ensure_python_deps()

    # ---- stage 0: manifest -------------------------------------------------
    try:
        manifest = manifest_mod.load_manifest(Path(args.manifest))
    except ManifestError as e:
        sys.exit(f"[-] Fatal: {e}")
    log.info("[*] Stage 0: manifest OK — %d repo(s): %s", len(manifest.repos),
             ", ".join(r.name for r in manifest.repos))

    # ---- stage 1: ensure clones (clean-tree guard) --------------------------
    log.info("[*] Stage 1: ensuring clones...")
    try:
        readys = manifest_mod.ensure_all(manifest, force_clean=args.force_clean)
    except ManifestError as e:
        sys.exit(f"[-] Fatal: {e}")
    for ready in readys:
        if ready.cloned:
            log.info("  [+] %-20s cloned @ %s", ready.spec.name, ready.sha[:12])
        if ready.cleaned:
            log.info("  [+] %-20s force-cleaned %d untracked file(s)",
                     ready.spec.name, len(ready.cleaned))
        for warning in ready.warnings:
            log.warning("  [!] %-20s %s", ready.spec.name, warning)

    # fingerprint guard (D1/O2): existing indexes must match the pinned schema
    for ready in readys:
        db = manifest_mod.codegraph_db_path(ready.spec.path)
        if db.exists():
            try:
                exporter.check_schema_fingerprint(db)
            except exporter.ExportError as e:
                sys.exit(f"[-] Fatal: {e}")

    # ---- stage 2: codegraph index (Node worker pool) ------------------------
    log.info("[*] Stage 2: codegraph indexing (worker pool)...")
    t2 = time.monotonic()
    pool_input = [{"name": r.spec.name, "path": str(r.spec.path)}
                  for r in readys]
    index_results = run_stage2(pool_input, args.concurrency)
    log.info("  [+] Stage 2 done in %.1fs", time.monotonic() - t2)

    # fresh indexes must also match the fingerprint
    stage2_ok = True
    for ready in readys:
        res = index_results.get(ready.spec.name, {})
        if not res.get("ok"):
            stage2_ok = False
            log.error("  [-] %-20s indexing failed: %s", ready.spec.name,
                      str(res.get("error"))[:300])
            continue
        stats = res.get("stats", {})
        if not stats.get("fileCount"):
            stage2_ok = False
            log.error("  [-] %-20s index is empty after stage 2 "
                      "(stats.fileCount=0) — wrong path or empty repo?",
                      ready.spec.name)
            continue
        try:
            exporter.check_schema_fingerprint(
                manifest_mod.codegraph_db_path(ready.spec.path))
        except exporter.ExportError as e:
            stage2_ok = False
            log.error("  [-] %s", e)

    # ---- container + schema -------------------------------------------------
    container.check_prerequisites(args.engine)
    container.ensure_neo4j_running(args.engine, args.bolt_port, args.http_port,
                                   args.neo4j_password, neo4j)
    uri = f"bolt://localhost:{args.bolt_port}"

    # ---- stages 3-5 per repo, single Neo4j writer ---------------------------
    repo_reports: List[Dict[str, Any]] = []
    run_dir = Path(args.log_dir) / "runs" / run_id
    artifacts_builder = art.RunArtifacts(Path(args.log_dir), run_id)
    manifest_repos: List[Dict[str, Any]] = []
    overall_ok = stage2_ok

    with neo4j.GraphDatabase.driver(uri, auth=("neo4j", args.neo4j_password)) as driver:
        ingest.init_neo4j_schema(driver)
        for ready in readys:
            name = ready.spec.name
            report: Dict[str, Any] = {"name": name, "path": str(ready.spec.path),
                                      "commitSha": ready.sha}
            if not index_results.get(name, {}).get("ok"):
                report.update(status="index_failed")
                repo_reports.append(report)
                overall_ok = False
                continue
            log.info("[*] Stages 3-4 (%s): export -> ingest...", name)
            t34 = time.monotonic()
            try:
                repo_export = exporter.RepoExport(name, ready.spec.path)
                sampler = exporter.RowSampler()

                def sampled_rows():
                    for table, row in repo_export.rows():
                        sampler.observe(table, row)
                        yield table, row

                expected = ingest.ingest_repo(driver, name, sampled_rows())
            except Exception as e:  # noqa: BLE001 - stage failure fails the run
                log.error("  [-] %s export/ingest failed: %s", name, e)
                report.update(status="export_failed", error=str(e)[:500])
                repo_reports.append(report)
                overall_ok = False
                continue
            log.info("  [+] export+ingest %.1fs", time.monotonic() - t34)

            log.info("[*] Stage 5 (%s): verifying graph contents...", name)
            actual = verify.verify_repo_in_graph(driver, name)
            validation = verify.diff_counts(expected, actual)
            sampled = verify.verify_samples(driver, sampler.samples)
            repo_ok = all(v["ok"] for v in validation.values()) and sampled["ok"]
            for metric, v in validation.items():
                mark = "OK " if v["ok"] else "MISMATCH"
                log.info("    %s %-11s expected=%-8d actual=%-8d", mark, metric,
                         v["expected"], v["actual"])
            report.update(status="ok" if repo_ok else "mismatch",
                          extracted=expected, graph=actual, validation=validation,
                          sampled=sampled, loc=repo_export.loc,
                          indexStats={k: index_results[name].get(k) for k in
                                      ("filesIndexed", "nodesCreated",
                                       "edgesCreated", "ms")},
                          counts=repo_export.counts)
            if not repo_ok:
                overall_ok = False
            repo_reports.append(report)

            # ---- stage 6 (repo half): codegraph artifact ----------------------
            try:
                staged = artifacts_builder.stage_codegraph_db(
                    name, manifest_mod.codegraph_db_path(ready.spec.path))
                cg_artifact = staged
            except Exception as e:  # noqa: BLE001 - artifact failure fails run
                log.error("  [-] %s artifact staging failed: %s", name, e)
                report["artifactError"] = str(e)[:300]
                overall_ok = False
                cg_artifact = {}
            manifest_repos.append({
                "name": name,
                "cloneUrl": ready.spec.clone_url,
                "commitSha": ready.sha,
                "loc": report.get("loc"),
                **cg_artifact,
                "nodesExported": sum(report.get("counts", {}).get("exported", {})
                                     .get(k, 0)
                                     for k in ("types", "methods", "fields")),
                "edgesExported": sum(report.get("counts", {}).get("exported", {})
                                     .get(k, 0)
                                     for k in ("extends", "implements",
                                               "packageImports", "typeImports",
                                               "calls")),
            })

        # ---- stage 6 (graph half): neo4j dump --------------------------------
        neo4j_artifact: Dict[str, Any] = {}
        if overall_ok:
            try:
                dump = container.dump_database(
                    args.engine, artifacts_builder.neo4j_dir, args.bolt_port,
                    args.neo4j_password, neo4j)
                neo4j_artifact = artifacts_builder.stage_neo4j_dump(dump)
            except Exception as e:  # noqa: BLE001
                log.error("[-] Neo4j dump failed: %s", e)
                overall_ok = False
                neo4j_artifact = {"error": str(e)[:300]}

    # ---- stage 6 (manifest half) + summary -----------------------------------
    finished = datetime.now(timezone.utc)
    verification_status = "passed" if overall_ok else "failed"
    run_manifest = art.build_run_manifest(
        run_id=run_id,
        started_at=started.isoformat(), finished_at=finished.isoformat(),
        codegraph_version=exporter.codegraph_version(),
        neo4j_image=container.NEO4J_IMAGE,
        schema_fingerprint=exporter.SCHEMA_FINGERPRINT,
        repos=manifest_repos, neo4j=neo4j_artifact,
        verification_status=verification_status)
    rm_path = artifacts_builder.write_run_manifest(run_manifest)

    parsed = [r for r in repo_reports if r.get("status") in ("ok", "mismatch")]
    totals: Dict[str, int] = {}
    for r in parsed:
        for m in verify.GRAPH_METRICS:
            totals[m] = totals.get(m, 0) + r.get("extracted", {}).get(m, 0)
    summary = {
        "run": {
            "runId": run_id,
            "started_utc": started.isoformat(),
            "finished_utc": finished.isoformat(),
            "duration_seconds": round((finished - started).total_seconds(), 1),
            "manifest": args.manifest,
            "log_file": str(log_path),
            "neo4j_uri": uri,
            "indexStore": (manifest.index_store.repo
                           if manifest.index_store else "local"),
        },
        "totals_extracted": totals,
        "repos": repo_reports,
        "run_manifest": str(rm_path),
        "overall_ok": overall_ok,
    }
    artifacts_builder.write_summary(summary)

    # ---- stage 7: publish -----------------------------------------------------
    if overall_ok:
        log.info("[*] Stage 7: publishing...")
        total_loc = sum(r.get("loc") or 0 for r in parsed)
        commit_msg = (f"index-run: {run_id} "
                      f"({', '.join(r['name'] for r in parsed)}, {total_loc:,} LOC)")
        try:
            if manifest.index_store:
                publish.publish_github(run_dir, manifest.index_store, run_id,
                                       commit_msg, args.keep_last,
                                       dry_run=args.publish_dry_run)
            else:
                if args.local_store_dir:
                    store_root = Path(args.local_store_dir)
                else:
                    parents = {r.path.parent.resolve() for r in manifest.repos}
                    common = parents.pop() if len(parents) == 1 else Path(".")
                    store_root = common / "index-store"
                publish.publish_local(run_dir, store_root, run_id, args.keep_last)
        except Exception as e:  # noqa: BLE001
            log.error("[-] Publish failed: %s", e)
            overall_ok = False
    else:
        log.error("[!] Run failed — artifacts + summary written to %s "
                  "(explicit failure markers inside), nothing published.",
                  run_dir)

    log.info("=" * 68)
    log.info("RUN SUMMARY %s — %s", run_id,
             "SUCCESS" if overall_ok else "FAILED (see above)")
    for r in repo_reports:
        log.info("  %-20s %-14s %s", r["name"], r.get("status", "?"),
                 f"{r.get('loc', 0):,} LOC" if r.get("loc") is not None else "")
    log.info("  Artifacts: %s", run_dir)
    log.info("=" * 68)
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
