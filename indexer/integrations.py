# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Cross-repo integration detection: derives (:Repository)-[:INTEGRATES
{refs, samples}]->(:Repository) edges for the portfolio graph.

The extractor is name-level syntax — an HTTP call from service A to
service B is a string literal that connects to nothing in B, so the base
graph has no repo-to-repo edges at all (D7 schema has no such edge type;
INTEGRATES is ADDITIVE). What DOES exist in the source, unambiguously, is
references to the other repo's NAME — config keys, SSM/secret paths, JWT
issuers, base URLs, deployment names (`/demo/demo-users/{env}/
api-gateway-url`, `jwt_issuer="demo-users"`, `USERS_API_URL` docs).
V1 matches the full repo name as a whole token (`\\bdemo-users\\b`)
— zero false positives (unlike bare short names: "content" is an English
word). Shortname/URL patterns can be added when a portfolio needs them.
"""

import re
from pathlib import Path
from typing import Dict, List

MAX_SAMPLES = 5          # evidence lines kept per (source, target) pair
SAMPLE_TEXT_LIMIT = 120  # chars of the matching line kept


def detect_integrations(repo_path: Path, repo_name: str,
                        all_repo_names: List[str],
                        tracked_files: List[str]) -> List[Dict]:
    """One pass over the repo's tracked text files; returns one record per
    other repo referenced: {target, refs, files, samples:[{path,line,text}]}.
    Self-references and binary files are ignored."""
    targets = [n for n in all_repo_names if n != repo_name and n]
    if not targets:
        return []
    # alnum boundaries (not hyphen): "demo-users-api" and
    # "demo-users.internal" ARE references to the users service
    patterns = {n: re.compile(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])")
                for n in targets}
    counts = {n: 0 for n in targets}
    files = {n: set() for n in targets}
    samples: Dict[str, List[Dict]] = {n: [] for n in targets}

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
        text = data.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            for name, rx in patterns.items():
                if rx.search(line):
                    counts[name] += 1
                    files[name].add(rel)
                    if len(samples[name]) < MAX_SAMPLES:
                        samples[name].append({
                            "path": rel, "line": line_no,
                            "text": line.strip()[:SAMPLE_TEXT_LIMIT]})
                    # no break: other targets may appear on the same line

    return [{"target": n, "refs": counts[n], "files": len(files[n]),
             "samples": samples[n]}
            for n in targets if counts[n] > 0]
