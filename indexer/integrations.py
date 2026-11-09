# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Cross-repo integration detection: derives (:Repository)-[:INTEGRATES
{refs, files, byClass, strongest, samples}]->(:Repository) edges for the
portfolio graph.

The extractor is name-level syntax — an HTTP call from service A to
service B is a string literal that connects to nothing in B, so the base
graph has no repo-to-repo edges at all (D7 schema has no such edge type;
INTEGRATES is ADDITIVE). What DOES exist in the source, unambiguously, is
references to the other repo's NAME — config keys, SSM/secret paths, JWT
issuers, base URLs, deployment names (`/demo/demo-users/{env}/
api-gateway-url`, `jwt_issuer="demo-users"`, `USERS_API_URL` docs).
V1 matches the full repo name as a whole token (`\\bdemo-users\\b`)
— bare short names would be English words ("content").

Every reference is classified by EVIDENCE CLASS, strongest first:

  submodule  the match is in `.gitmodules` — the repo literally embeds
             the other repo (e.g. web carries videopipeline as
             `external/videopipeline`). Strongest signal there is.
  code       executable line in a source file (config values, bucket
             names, API URLs wired into the app at runtime).
  config     infrastructure/build wiring — terraform, yaml/CI (anything
             under `.github/`), Makefile/Dockerfile, `*.tf`, `*.yml`,
             `*.json`, shell scripts, `.env`, `*.code-workspace`.
  comment    comment line inside a source file (`#`, `//`, `/*`, `*`).
             Human documentation that happens to live in code.
  docs       markdown/rst/text documentation.

Class ranking matters because raw `refs` scales with documentation
verbosity, not coupling: 300 doc mentions say less than one `.gitmodules`
entry. Consumers (viewer, summary) weight/filter by `strongest` —
docs/comment edges are mention-level evidence, code/config/submodule are
integration-level. Known V1 limits: Python docstrings count as `code`
(not `comment`); unknown extensions fall to `docs` (cannot inflate the
signal).
"""

import re
from pathlib import Path
from typing import Dict, List

MAX_SAMPLES = 5          # evidence lines kept per (source, target) pair
MAX_SAMPLES_PER_CLASS = 3
SAMPLE_TEXT_LIMIT = 120  # chars of the matching line kept

# ranked strongest -> weakest; drives `strongest` and sample ordering
EVIDENCE_CLASSES = ("submodule", "code", "config", "comment", "docs")
CLASS_WEIGHT = {"submodule": 100.0, "code": 40.0, "config": 20.0,
                "comment": 5.0, "docs": 1.0}

_CODE_EXTS = {
    "py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "java", "go", "rs",
    "rb", "cs", "php", "kt", "swift", "scala", "c", "h", "cpp", "hpp",
}
_DOC_EXTS = {"md", "mdx", "rst", "adoc", "txt", "text"}
_CONFIG_EXTS = {
    "tf", "tfvars", "hcl", "yml", "yaml", "json", "toml", "ini", "cfg",
    "conf", "properties", "gradle", "xml", "sh", "bash", "zsh", "fish",
}
_HASH_COMMENT_EXTS = {"py", "rb"}          # '#' line comments in code files
_SLASH_COMMENT_EXTS = _CODE_EXTS - _HASH_COMMENT_EXTS  # '//' family


def classify_file(rel: str) -> str:
    """Evidence class of a tracked file, by path (V1: path + extension)."""
    if rel.endswith(".gitmodules"):
        return "submodule"
    parts = rel.split("/")
    if any(p in (".github", ".gitlab", ".circleci") for p in parts[:-1]):
        return "config"  # CI/ops tooling — even .py there is automation
    name = parts[-1]
    if (name.startswith(".env") or name.startswith("Dockerfile")
            or name.startswith("Makefile") or name.endswith(".code-workspace")):
        return "config"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in _CODE_EXTS:
        return "code"
    if ext in _CONFIG_EXTS:
        return "config"
    return "docs"  # docs + unknown text — weakest, cannot inflate signal


def _is_comment_line(ext: str, line: str) -> bool:
    stripped = line.lstrip()
    if ext in _HASH_COMMENT_EXTS:
        return stripped.startswith("#")
    if ext in _SLASH_COMMENT_EXTS:
        return (stripped.startswith("//") or stripped.startswith("/*")
                or stripped.startswith("*"))
    return False


def _file_ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def detect_integrations(repo_path: Path, repo_name: str,
                        all_repo_names: List[str],
                        tracked_files: List[str]) -> List[Dict]:
    """One pass over the repo's tracked text files; returns one record per
    other repo referenced:

        {target, refs, files, byClass: {cls: n}, strongest: cls,
         samples: [{path, line, text, cls}]}

    `refs`/`files` are totals over all classes (unchanged semantics);
    `byClass` counts refs per evidence class, `strongest` is the
    highest-weight class with refs > 0, and samples are ordered
    strongest-class first. Self-references and binary files are ignored."""
    targets = [n for n in all_repo_names if n != repo_name and n]
    if not targets:
        return []
    # alnum boundaries (not hyphen): "demo-users-api" and
    # "demo-users.internal" ARE references to the users service
    patterns = {n: re.compile(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])")
                for n in targets}
    counts = {n: 0 for n in targets}
    files = {n: set() for n in targets}
    by_class = {n: {c: 0 for c in EVIDENCE_CLASSES} for n in targets}
    samples: Dict[str, Dict[str, List[Dict]]] = {
        n: {c: [] for c in EVIDENCE_CLASSES} for n in targets}

    for rel in tracked_files:
        path = repo_path / rel
        if not path.is_file():
            continue  # submodule gitlinks / deleted entries
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue  # binary
        file_cls = classify_file(rel)
        ext = _file_ext(rel)
        text = data.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            for name, rx in patterns.items():
                if not rx.search(line):
                    continue
                cls = file_cls
                if cls == "code" and _is_comment_line(ext, line):
                    cls = "comment"
                counts[name] += 1
                files[name].add(rel)
                by_class[name][cls] += 1
                if len(samples[name][cls]) < MAX_SAMPLES_PER_CLASS:
                    samples[name][cls].append({
                        "path": rel, "line": line_no, "cls": cls,
                        "text": line.strip()[:SAMPLE_TEXT_LIMIT]})
                # no break: other targets may appear on the same line

    out = []
    for n in targets:
        if counts[n] == 0:
            continue
        strongest = max(EVIDENCE_CLASSES, key=lambda c: (by_class[n][c] > 0,
                                                         CLASS_WEIGHT[c]))
        kept = [s for c in EVIDENCE_CLASSES  # strongest classes first
                for s in samples[n][c]][:MAX_SAMPLES]
        out.append({"target": n, "refs": counts[n], "files": len(files[n]),
                    "byClass": {c: k for c, k in by_class[n].items() if k},
                    "strongest": strongest, "samples": kept})
    return out
