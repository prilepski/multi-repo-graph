"""Fixtures for the codegraph portfolio wrapper: TWO codegraph-indexed repos
(the multi-repo fan-out is the thing under test)."""

import json
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NODEJS_DIR = PROJECT_ROOT / "indexer" / "nodejs"
FANOUT = Path(__file__).resolve().parents[1] / "fanout.mjs"

REPO_A_CODES = """package org.acme.util;

public class Codes {
    public String encode(String s) { return s.trim(); }
}
"""

REPO_A_BASE = """package org.acme.core;

public abstract class Base {
    protected void before() {}
}
"""

REPO_A_APP = """package org.acme;

import org.acme.util.Codes;

public class AppA extends org.acme.core.Base {
    public void run() {
        Codes c = new Codes();
        c.encode("x");
        before();
    }
}
"""

REPO_B_DECODER = """package org.acme.util;

public class Decoder {
    public int decode(String s) { return s.length(); }
}
"""

REPO_B_APP = """package org.acme.other;

import org.acme.util.Decoder;

public class AppB {
    public void run() {
        new Decoder().decode("y");
    }
}
"""

REPO_B_TS = """export function format(x: string): string {
    return x.trim();
}
"""


def _git_repo(path: Path, files: dict) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "x"]):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-C", str(path), *args], check=True, capture_output=True)
    return path


def _index(path: Path) -> None:
    proc = subprocess.run(
        ["node", str(NODEJS_DIR / "index-worker.mjs"), str(path)],
        cwd=str(NODEJS_DIR), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-1500:]
    event = json.loads(proc.stdout.strip().splitlines()[-1])
    assert event["ok"], event


@pytest.fixture(scope="module")
def wrapper_repos(tmp_path_factory):
    """(repos_json_path, {name: path}) with both repos codegraph-indexed."""
    root = tmp_path_factory.mktemp("cgwrapper")
    a = _git_repo(root / "repoa", {
        "src/org/acme/util/Codes.java": REPO_A_CODES,
        "src/org/acme/core/Base.java": REPO_A_BASE,
        "src/org/acme/AppA.java": REPO_A_APP,
    })
    b = _git_repo(root / "repob", {
        "src/org/acme/util/Decoder.java": REPO_B_DECODER,
        "src/org/acme/other/AppB.java": REPO_B_APP,
        "ui/src/format.ts": REPO_B_TS,
    })
    _index(a)
    _index(b)
    repos_json = root / "repos.json"
    repos_json.write_text(json.dumps([
        {"name": "repoa", "path": str(a)},
        {"name": "repob", "path": str(b)},
    ]))
    return repos_json, {"repoa": a, "repob": b}


def fanout(repos_json: Path, command: str, args: dict | None = None) -> dict:
    """Run fanout.mjs exactly the way server.py does."""
    proc = subprocess.run(
        ["node", str(FANOUT), str(repos_json), command,
         json.dumps(args or {})],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"fanout {command}: {proc.stderr[-800:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])
