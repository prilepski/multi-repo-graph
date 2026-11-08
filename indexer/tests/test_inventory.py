# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Inventory layer tests (ported java-graph taxonomy + LOC rules, §10)."""

from indexer import inventory as inv
from indexer.exporter import scan_file_rows


class TestClassify:
    def test_java_and_roles(self):
        assert inv.classify("src/main/java/Foo.java")["category"] == "java_backend"
        assert inv.classify("src/test/java/FooTest.java") == {
            "kind": "source", "category": "java_test", "role": "test",
            "extension": "java"}
        assert inv.classify("ui/src/__tests__/a.ts")["category"] == "frontend_test"

    def test_build_overrides(self):
        assert inv.classify("pom.xml")["category"] == "build_script"
        assert inv.classify("package.json")["category"] == "build_script"
        assert inv.classify("vite.config.ts")["category"] == "build_script"

    def test_ci_paths(self):
        assert inv.classify(".github/workflows/ci.yml")["category"] == "ci_cd"
        assert inv.classify(".circleci/config.yml")["category"] == "ci_cd"

    def test_assets_docs_other(self):
        assert inv.classify("a/b.png")["kind"] == "assets"
        assert inv.classify("README.md")["category"] == "documentation"
        assert inv.classify("some/oddfile") == {
            "kind": "other", "category": "other", "role": "main",
            "extension": ""}

    def test_generated_role_wins_over_test(self):
        assert inv.classify("src/generated/test/x.java")["role"] == "generated"


class TestCountLines:
    def test_java_comments_and_blanks(self):
        counts = inv.count_lines(
            "// header\npublic class A {}\n\n/* block\n still */\nint x;\n",
            "java_backend", "java")
        # reference rule: only //, /*, *, */ prefixes count — " still */" is code
        assert counts == {"totalLines": 6, "codeLines": 3,
                          "commentLines": 2, "blankLines": 1}

    def test_shell_uses_c_style_rules(self):
        # reference behavior: shell_script sits in the C-style comment branch
        counts = inv.count_lines("# c\nls\n", "shell_script", "sh")
        assert counts["commentLines"] == 0 and counts["codeLines"] == 2
        # while yml/properties use '#' (HASH_COMMENT branch)
        assert inv.count_lines("# c\nk: v\n", "runtime_config",
                               "yml")["commentLines"] == 1

    def test_binary_is_none(self):
        assert inv.count_lines("x", "image", "png") is None


class TestFileRows:
    def test_rows_carry_taxonomy_and_loc(self, tmp_path):
        import subprocess
        repo = tmp_path / "r"
        (repo / "src").mkdir(parents=True)
        (repo / "src/A.java").write_text("// c\nclass A {}\n")
        (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        (repo / "run.sh").write_text("#!/bin/sh\nls\n")
        for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "x"]):
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                            "-C", str(repo), *args], check=True,
                           capture_output=True)
        rows = {r["path"]: r for t, r in scan_file_rows("r", repo)}
        a = rows["src/A.java"]
        assert a["category"] == "java_backend" and a["kind"] == "source"
        assert a["totalLines"] == 2 and a["commentLines"] == 1
        assert a["codeLines"] == 1 and a["blankLines"] == 0
        # sha256 is the drift-contract variant (decoded/re-encoded text)
        import hashlib
        assert a["sha256"] == hashlib.sha256(b"// c\nclass A {}\n").hexdigest()
        png = rows["logo.png"]
        assert png["kind"] == "assets" and png["totalLines"] is None
        assert png["sha256"] == hashlib.sha256(
            (repo / "logo.png").read_bytes()).hexdigest()
        sh = rows["run.sh"]
        assert sh["category"] == "shell_script"

    def test_nul_sniffed_binary_gets_blob_category(self, tmp_path):
        import subprocess
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "weird.md").write_bytes(b"text\x00binary")
        for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "x"]):
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                            "-C", str(repo), *args], check=True,
                           capture_output=True)
        row = list(scan_file_rows("r", repo))[0][1]
        assert row["category"] == "binary_blob" and row["kind"] == "artifact"
        assert row["totalLines"] is None
