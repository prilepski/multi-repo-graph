"""File inventory: taxonomy classification and LOC counting.

Implements the rule table from docs/inventory-stats-design.md (rev 2):
kind (8 values) -> category (ordered rules, first match wins), plus a role
attribute (main | test | generated) and language-aware line counting.
"""

import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BINARY_CATEGORIES = {"image", "font", "binary_blob"}

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Ordered filename rules (basename, lowercase) — checked before extensions.
FILENAME_RULES = [
    ("mvnw", "source", "shell_script"),
    ("gradlew", "source", "shell_script"),
    ("makefile", "build", "build_script"),
    ("jenkinsfile", "build", "ci_cd"),
    (".dockerignore", "build", "container"),
]
FILENAME_PREFIX_RULES = [
    ("dockerfile", "build", "container"),
    ("docker-compose", "build", "container"),
    ("license", "docs", "documentation"),
    ("notice", "docs", "documentation"),
]
# Ordered extension rules: extension -> (kind, category). First match wins.
EXTENSION_RULES: Dict[str, Tuple[str, str]] = {
    "java": ("source", "java_backend"),
    "ts": ("source", "frontend_logic"),
    "tsx": ("source", "frontend_logic"),
    "js": ("source", "frontend_logic"),
    "mjs": ("source", "frontend_logic"),
    "scss": ("source", "frontend_style"),
    "css": ("source", "frontend_style"),
    "less": ("source", "frontend_style"),
    "html": ("source", "frontend_markup"),
    "vue": ("source", "frontend_markup"),
    "sh": ("source", "shell_script"),
    "bat": ("source", "shell_script"),
    "py": ("source", "python_script"),
    "sql": ("source", "sql"),
    "proto": ("source", "protocol_schema"),
    "thrift": ("source", "protocol_schema"),
    "avsc": ("source", "protocol_schema"),
    "gradle": ("build", "build_script"),
    "tf": ("build", "iac"),
    "tfvars": ("build", "iac"),
    "json": ("data", "data_fixture"),          # package.json overridden below
    "yml": ("config", "runtime_config"),
    "yaml": ("config", "runtime_config"),
    "properties": ("config", "runtime_config"),
    "conf": ("config", "runtime_config"),
    "cfg": ("config", "runtime_config"),
    "env": ("config", "runtime_config"),
    "toml": ("config", "runtime_config"),
    "xml": ("config", "xml_config"),
    "md": ("docs", "documentation"),
    "rst": ("docs", "documentation"),
    "txt": ("docs", "documentation"),
    "csv": ("data", "data_fixture"),
    "factories": ("data", "data_fixture"),
    "png": ("assets", "image"),
    "jpg": ("assets", "image"),
    "jpeg": ("assets", "image"),
    "svg": ("assets", "image"),
    "gif": ("assets", "image"),
    "ico": ("assets", "image"),
    "ttf": ("assets", "font"),
    "woff": ("assets", "font"),
    "woff2": ("assets", "font"),
    "eot": ("assets", "font"),
    "class": ("artifact", "binary_blob"),
    "jar": ("artifact", "binary_blob"),
    "raw": ("artifact", "binary_blob"),
    "bin": ("artifact", "binary_blob"),
    "pem": ("artifact", "binary_blob"),
    "key": ("artifact", "binary_blob"),
    "keystore": ("artifact", "binary_blob"),
}
# Basenames that must beat the extension table.
FILENAME_OVERRIDE = {
    "pom.xml": ("build", "build_script"),
    "package.json": ("build", "build_script"),
    "tsconfig.json": ("build", "build_script"),
    "settings.gradle": ("build", "build_script"),
    "angular.json": ("build", "build_script"),
}
FILENAME_CONFIG_PREFIXES = [
    ("vite.config", "build", "build_script"),
    ("webpack.config", "build", "build_script"),
    ("tsconfig.", "build", "build_script"),
]
TEST_SEGMENTS = {"test", "__tests__", "it"}
GENERATED_SEGMENTS_PREFIX = ("generated",)


def classify(rel_path: str) -> Dict[str, str]:
    """-> {kind, category, role, extension} for a repo-relative path."""
    parts = [p.lower() for p in rel_path.split("/")]
    basename = parts[-1]
    extension = basename.rsplit(".", 1)[1] if "." in basename else ""

    kind: Optional[str] = None
    category: Optional[str] = None

    if basename in FILENAME_OVERRIDE:
        kind, category = FILENAME_OVERRIDE[basename]
    else:
        for name, k, c in FILENAME_RULES:
            if basename == name:
                kind, category = k, c
                break
        if kind is None:
            for prefix, k, c in FILENAME_PREFIX_RULES:
                if basename.startswith(prefix):
                    kind, category = k, c
                    break
        if kind is None:
            for prefix, k, c in FILENAME_CONFIG_PREFIXES:
                if basename.startswith(prefix):
                    kind, category = k, c
                    break
    if kind is None and extension:
        rule = EXTENSION_RULES.get(extension)
        if rule:
            kind, category = rule
    if kind is None:
        kind, category = "other", "other"
    # CI path rules (before role handling, after table)
    if ".github" in parts[:-1] and "workflows" in parts[:-1]:
        kind, category = "build", "ci_cd"
    elif ".circleci" in parts[:-1] or basename.startswith(".gitlab-ci"):
        kind, category = "build", "ci_cd"

    # role: test paths flip *_backend categories into *_test; generated wins
    segments = set(parts[:-1])
    role = "main"
    if any(seg.startswith(GENERATED_SEGMENTS_PREFIX) for seg in segments):
        role = "generated"
    elif segments & TEST_SEGMENTS and category in ("java_backend", "frontend_logic"):
        category = "java_test" if category == "java_backend" else "frontend_test"
        role = "test"

    return {"kind": kind, "category": category, "role": role,
            "extension": extension}


# ---------------------------------------------------------------------------
# Line counting
# ---------------------------------------------------------------------------

HASH_COMMENT = {"sh", "py", "yml", "yaml", "properties", "conf", "cfg",
                "env", "toml"}
SQL_COMMENT = {"sql"}


def count_lines(text: str, category: str, extension: str) -> Optional[Dict[str, int]]:
    """-> {totalLines, codeLines, commentLines, blankLines}; None for binaries."""
    if category in BINARY_CATEGORIES:
        return None
    total = code = comment = blank = 0
    for line in text.splitlines():
        total += 1
        stripped = line.strip()
        if not stripped:
            blank += 1
            continue
        is_comment = False
        if category in ("java_backend", "java_test", "frontend_logic",
                        "frontend_test", "frontend_style", "frontend_markup",
                        "shell_script") and extension != "bat":
            is_comment = (stripped.startswith("//") or stripped.startswith("/*")
                          or stripped.startswith("*") or stripped.startswith("*/"))
        elif category in ("xml_config", "frontend_markup") or extension in ("html", "xml"):
            is_comment = stripped.startswith("<!--")
        elif extension in HASH_COMMENT:
            is_comment = stripped.startswith("#")
        elif extension in SQL_COMMENT:
            is_comment = stripped.startswith("--") or stripped.startswith("/*")
        elif extension == "bat":
            is_comment = stripped.upper().startswith("REM")
        if is_comment:
            comment += 1
        else:
            code += 1
    return {"totalLines": total, "codeLines": code,
            "commentLines": comment, "blankLines": blank}


def is_binary_sniff(path: Path, chunk: int = 8192) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(chunk)
    except OSError:
        return True


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def inventory_file(repo_root: Path, rel_path: str) -> Dict:
    """Full inventory record for one tracked file."""
    abs_path = repo_root / rel_path
    meta = classify(rel_path)
    binary = meta["category"] in BINARY_CATEGORIES or is_binary_sniff(abs_path)
    record = {
        "path": rel_path,
        "extension": meta["extension"],
        "kind": meta["kind"],
        "category": meta["category"],
        "role": meta["role"],
        "bytes": 0,
        "totalLines": None, "codeLines": None,
        "commentLines": None, "blankLines": None,
        "sha256": None,
    }
    try:
        record["bytes"] = abs_path.stat().st_size
        if binary:
            record["sha256"] = sha256_file(abs_path)
        else:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
            record["sha256"] = hashlib.sha256(
                text.encode("utf-8", "replace")).hexdigest()
            counts = count_lines(text, meta["category"], meta["extension"])
            if counts:
                record.update(counts)
    except OSError:
        record["category"] = "unreadable"
        record["kind"] = "other"
    return record


def git_tracked_files(repo_root: Path) -> List[str]:
    import subprocess
    res = subprocess.run(["git", "ls-files", "-z"], cwd=str(repo_root),
                         capture_output=True, text=True)
    if res.returncode != 0:
        return []
    return [p for p in res.stdout.split("\0") if p.strip()]


def disk_files(repo_root: Path) -> set:
    return {str(p.relative_to(repo_root)) for p in repo_root.rglob("*")
            if p.is_file() and ".git" not in p.parts}
