# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Stage 3 mapping tests over the real codegraph-indexed fixture
(docs/design.md §5 mapping table, each rule asserted once)."""

import hashlib

from indexer import exporter


def by_fqn(rows):
    return {r["fqn"]: r for r in rows}


class TestJavaMapping:
    def test_type_fqn_and_kinds(self, fixture_export):
        types = by_fqn(fixture_export["tables"]["types"])
        assert types["org.demo.Base"]["kind"] == "class"
        assert types["org.demo.Service"]["kind"] == "class"
        assert types["org.demo.Service"]["package"] == "org.demo"
        assert types["org.demo.Service"]["file_id"] == (
            "fixture:src/main/java/org/demo/Service.java")
        assert types["org.other.Thing"]["kind"] == "class"

    def test_extends_resolved_internal(self, fixture_export):
        ext = {(r["fqn"], r["target"], r["external"])
               for r in fixture_export["tables"]["extends"]}
        assert ("org.demo.Service", "org.demo.Base", False) in ext

    def test_implements_resolved(self, fixture_export):
        imp = {(r["fqn"], r["target"])
               for r in fixture_export["tables"]["implements"]}
        # Runnable is a JDK interface -> unresolved ref -> external target
        assert any(fqn == "org.demo.Service" and
                   tgt.endswith("Runnable") for fqn, tgt in imp)

    def test_method_arity_and_id(self, fixture_export):
        methods = {r["id"]: r for r in fixture_export["tables"]["methods"]}
        run_id = "org.demo.Service#run/1"  # Map<String,String> -> 1 param
        assert run_id in methods
        assert methods[run_id]["return_type"].startswith("String")

    def test_enum_constant_body_methods_attach_to_enum_type(self, fixture_export):
        # HIGH.score()/LOW.score()/abstract score() all collapse onto the
        # enum Type's Method id (java-graph id convention)
        methods = {r["id"] for r in fixture_export["tables"]["methods"]}
        assert "org.demo.Level#score/0" in methods
        # and no method is orphaned on the enum CONSTANT
        assert not any("Level.HIGH#" in mid or "Level.LOW#" in mid
                       for mid in methods)

    def test_field(self, fixture_export):
        fields = by_fqn(fixture_export["tables"]["fields"])
        assert "org.demo.Service.count" in fields
        assert fields["org.demo.Service.count"]["type"] == "int"

    def test_imports_single_and_external(self, fixture_export):
        ti = {(r["file_id"], r["target"], r["external"])
              for r in fixture_export["tables"]["typeImports"]}
        assert ("fixture:src/main/java/org/demo/Service.java",
                "java.util.Map", True) in ti
        assert ("fixture:src/main/java/org/demo/Service.java",
                "org.other.Thing", False) in ti

    def test_calls_resolved_and_external(self, fixture_export):
        calls = {(r["method_id"], r["callee"], r["external"], r["count"])
                 for r in fixture_export["tables"]["calls"]}
        # helper() resolves to Base.helper -> same-package name-level target
        assert any(mid == "org.demo.Service#run/1" and callee == "helper"
                   for mid, callee, ext, cnt in calls)
        # unknownExternalFn() never resolves -> synthesized external target
        assert any(mid == "org.demo.Service#run/1" and
                   callee == "unknownExternalFn" and ext is True
                   for mid, callee, ext, cnt in calls)


class TestNonJavaMapping:
    def test_class_and_members_file_scoped(self, fixture_export):
        types = by_fqn(fixture_export["tables"]["types"])
        greeter = types["fixture:ui/src/util.ts::Greeter"]
        assert greeter["kind"] == "class"
        assert greeter["package"] == "fixture:ui/src"

        methods = {r["id"]: r for r in fixture_export["tables"]["methods"]}
        assert "fixture:ui/src/util.ts::Greeter#greet/1" in methods

        fields = by_fqn(fixture_export["tables"]["fields"])
        assert "fixture:ui/src/util.ts::Greeter.greeting" in fields

    def test_module_type_for_top_level_function(self, fixture_export):
        types = by_fqn(fixture_export["tables"]["types"])
        assert types["fixture:ui/src/util.ts"]["kind"] == "module"
        methods = {r["id"] for r in fixture_export["tables"]["methods"]}
        assert "fixture:ui/src/util.ts#format/1" in methods

    def test_module_import_is_package(self, fixture_export):
        pi = {(r["file_id"], r["target"])
              for r in fixture_export["tables"]["packageImports"]}
        assert ("fixture:ui/src/util.ts", "./helper") in pi


class TestFileScan:
    def test_files_rows_sha_and_loc(self, fixture_export, indexed_fixture):
        # files rows come from the Python scan (RepoExport), not export.mjs —
        # exercise it directly here
        rows = list(exporter.scan_file_rows("fixture", indexed_fixture))
        by_path = {r["path"]: r for t, r in rows if t == "files"}
        svc = by_path["src/main/java/org/demo/Service.java"]
        text = (indexed_fixture / "src/main/java/org/demo/Service.java").read_bytes()
        assert svc["sha256"] == hashlib.sha256(text).hexdigest()
        assert svc["totalLines"] == text.count(b"\n")
        assert svc["id"] == "fixture:src/main/java/org/demo/Service.java"
        assert svc["bytes"] == len(text)

    def test_binary_and_unsupported_still_present(self, fixture_export,
                                                  indexed_fixture):
        rows = dict(((r["path"], r) for t, r in
                     exporter.scan_file_rows("fixture", indexed_fixture)))
        assert "README.md" in rows  # unsupported by codegraph, still a :File


class TestCountsCrossCheck:
    def test_exporter_counts_match_rows(self, fixture_export):
        exported = fixture_export["counts"]["exported"]
        for table, key in (("types", "types"), ("methods", "methods"),
                           ("fields", "fields"), ("extends", "extends"),
                           ("implements", "implements"),
                           ("packageImports", "packageImports"),
                           ("typeImports", "typeImports"), ("calls", "calls")):
            assert exported[key] == len(fixture_export["tables"].get(table, [])), table

    def test_row_order_is_ingest_order(self, fixture_export):
        assert fixture_export["order"] == [
            "packages", "types", "methods", "fields", "extends",
            "implements", "packageImports", "typeImports", "calls"]
