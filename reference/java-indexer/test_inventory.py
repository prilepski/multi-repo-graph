"""Tests for the file inventory: taxonomy classification and LOC counting."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inventory as inv  # noqa: E402


class TestClassify:
    @pytest.mark.parametrize("path,category", [
        ("src/main/java/org/x/A.java", "java_backend"),
        ("src/test/java/org/x/ATest.java", "java_test"),
        ("ui-ngx/src/app/foo.component.ts", "frontend_logic"),
        ("src/main/webapp/app/app.module.js", "frontend_logic"),
        ("ui/styles/theme.scss", "frontend_style"),
        ("src/main/resources/templates/page.html", "frontend_markup"),
        ("deploy/install.sh", "shell_script"),
        ("mvnw", "shell_script"),
        ("tools/migrate.py", "python_script"),
        ("dao/src/main/resources/schema.sql", "sql"),
        ("common/proto/rule.proto", "protocol_schema"),
        ("pom.xml", "build_script"),
        ("package.json", "build_script"),
        ("ui-ngx/angular.json", "build_script"),
        (".github/workflows/build.yml", "ci_cd"),
        ("Dockerfile", "container"),
        ("docker-compose.yml", "container"),
        ("deploy/terraform/main.tf", "iac"),
        ("application/src/main/resources/thingsboard.yml", "runtime_config"),
        ("dao/src/main/resources/dao.properties", "runtime_config"),
        ("common/data/pom.xml", "build_script"),
        ("README.md", "documentation"),
        ("LICENSE", "documentation"),
        ("application/data/res/config.json", "data_fixture"),
        ("ui-ngx/src/assets/logo.png", "image"),
        ("ui-ngx/src/assets/fonts/glyph.ttf", "font"),
        ("common/target/classes/A.class", "binary_blob"),
        ("transport/mqtt/certs/mqtt-server.pem", "binary_blob"),
        ("unknown.zzz", "other"),
    ])
    def test_categories(self, path, category):
        assert inv.classify(path)["category"] == category

    def test_kinds(self):
        assert inv.classify("src/A.java")["kind"] == "source"
        assert inv.classify("pom.xml")["kind"] == "build"
        assert inv.classify("app.yml")["kind"] == "config"
        assert inv.classify("README.md")["kind"] == "docs"
        assert inv.classify("logo.png")["kind"] == "assets"
        assert inv.classify("x.class")["kind"] == "artifact"

    def test_role_test_and_generated(self):
        assert inv.classify("src/test/java/A.java")["role"] == "test"
        assert inv.classify("src/main/java/A.java")["role"] == "main"
        assert inv.classify("target/generated-sources/A.java")["role"] == "generated"
        # test-path rule only flips source categories
        assert inv.classify("src/test/resources/app.yml")["role"] == "main"

    def test_extension_recorded(self):
        assert inv.classify("a/b/c.TS")["extension"] == "ts"  # case-insensitive


class TestCountLines:
    def test_java_family(self):
        text = """// header comment
public class A {
    /* block
       continues */
    int x = 1; // trailing (not a comment line by our rule)
}
"""
        counts = inv.count_lines(text, "java_backend", "java")
        # comments: line 1 (// header), line 3 (/* block); line 4 is code text
        # ending in */ per the line-start heuristic
        assert counts == {"totalLines": 6, "codeLines": 4,
                          "commentLines": 2, "blankLines": 0}
        assert counts["codeLines"] + counts["commentLines"] + counts["blankLines"] == 6

    def test_blank_lines(self):
        text = "a\n\n  \nb\n"
        counts = inv.count_lines(text, "runtime_config", "yml")
        assert counts == {"totalLines": 4, "codeLines": 2,
                          "commentLines": 0, "blankLines": 2}

    def test_hash_family(self):
        text = "# comment\nkey: value\n"
        counts = inv.count_lines(text, "runtime_config", "yml")
        assert counts["commentLines"] == 1 and counts["codeLines"] == 1

    def test_sql_family(self):
        text = "-- comment\nSELECT 1;\n/* c */\n"
        counts = inv.count_lines(text, "sql", "sql")
        assert counts["commentLines"] == 2 and counts["codeLines"] == 1

    def test_xml_family(self):
        text = "<!-- note -->\n<bean/>\n"
        counts = inv.count_lines(text, "xml_config", "xml")
        assert counts["commentLines"] == 1 and counts["codeLines"] == 1

    def test_binary_categories_return_none(self):
        assert inv.count_lines("x", "image", "png") is None
        assert inv.count_lines("x", "binary_blob", "class") is None


class TestInventoryFile:
    def test_full_record_text_file(self, tmp_path):
        f = tmp_path / "src" / "A.java"
        f.parent.mkdir()
        f.write_text("package p;\n\npublic class A {}\n")
        rec = inv.inventory_file(tmp_path, "src/A.java")
        assert rec["category"] == "java_backend"
        assert rec["totalLines"] == 3 and rec["codeLines"] == 2
        assert rec["bytes"] == f.stat().st_size
        assert len(rec["sha256"]) == 64

    def test_binary_nul_sniff_no_loc(self, tmp_path):
        f = tmp_path / "blob.dat"   # unknown ext but NUL byte -> no LOC
        f.write_bytes(b"abc\x00def")
        rec = inv.inventory_file(tmp_path, "blob.dat")
        assert rec["totalLines"] is None
        assert rec["category"] == "other" and rec["bytes"] > 0

    def test_missing_file_marked_unreadable(self, tmp_path):
        rec = inv.inventory_file(tmp_path, "gone.java")
        assert rec["category"] == "unreadable"


class TestFileLists:
    def test_git_tracked_and_disk(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("b")
        (tmp_path / "untracked.txt").write_text("u")
        subprocess.run(["git", "add", "a.txt", "sub/b.txt"], cwd=tmp_path, check=True)
        assert sorted(inv.git_tracked_files(tmp_path)) == ["a.txt", "sub/b.txt"]
        assert inv.disk_files(tmp_path) >= {"a.txt", "sub/b.txt", "untracked.txt"}
