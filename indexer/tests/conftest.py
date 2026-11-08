"""Shared fixtures: a tiny git repo (java + ts) that gets a REAL codegraph
index — exercises the actual mapping path offline (design.md §8)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

INDEXER_DIR = Path(__file__).resolve().parents[1]
NODEJS_DIR = INDEXER_DIR / "nodejs"

JAVA_BASE = """package org.demo;

public abstract class Base {
    public void helper() {}
}
"""

JAVA_SERVICE = """package org.demo;

import java.util.Map;
import org.other.Thing;

public class Service extends Base implements Runnable {
    private int count;

    public String run(Map<String, String> params) {
        helper();
        unknownExternalFn();
        Thing t = null;
        return "x";
    }
}
"""

JAVA_THING = """package org.other;

public class Thing {
    public String name() { return "thing"; }
}
"""

TS_UTIL = """import { helper } from './helper';

export class Greeter {
    greeting: string;

    greet(name: string): string {
        return this.greeting + name;
    }
}

export function format(x: string): string {
    return x.trim();
}
"""

TS_HELPER = """export function helper(): number {
    return 42;
}
"""

# enum-constant class bodies: methods belong to the enum Type (java-graph
# semantics), not to the constant (which codegraph kinds as enum_member)
JAVA_LEVELS = """package org.demo;

public enum Level {
    HIGH {
        public int score() { return 3; }
    },
    LOW {
        public int score() { return 1; }
    };

    public abstract int score();
}
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c",
                    "user.name=T", "-C", str(repo), *args], check=True,
                   capture_output=True)


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "fixture"
    java = root / "src/main/java/org/demo"
    java.mkdir(parents=True)
    (java / "Base.java").write_text(JAVA_BASE)
    (java / "Service.java").write_text(JAVA_SERVICE)
    other = root / "src/main/java/org/other"
    other.mkdir(parents=True)
    (other / "Thing.java").write_text(JAVA_THING)
    (java / "Level.java").write_text(JAVA_LEVELS)
    ts = root / "ui/src"
    ts.mkdir(parents=True)
    (ts / "util.ts").write_text(TS_UTIL)
    (ts / "helper.ts").write_text(TS_HELPER)
    (root / "README.md").write_text("fixture\n")
    _git(root, "init", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "fixture")
    return root


@pytest.fixture()
def indexed_fixture(fixture_repo: Path) -> Path:
    proc = subprocess.run(
        ["node", str(NODEJS_DIR / "index-worker.mjs"), str(fixture_repo)],
        cwd=str(NODEJS_DIR), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    event = json.loads(proc.stdout.strip().splitlines()[-1])
    assert event["ok"], event
    return fixture_repo


@pytest.fixture()
def fixture_export(indexed_fixture: Path) -> dict:
    """Rows from export.mjs over the indexed fixture: {table: [rows], counts}."""
    proc = subprocess.run(
        ["node", str(NODEJS_DIR / "export.mjs"), str(indexed_fixture), "fixture"],
        cwd=str(NODEJS_DIR), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    tables: dict = {}
    order: list = []
    counts = None
    for line in proc.stdout.splitlines():
        event = json.loads(line)
        if event["t"] == "counts":
            counts = event["row"]
        else:
            if event["t"] not in tables:
                order.append(event["t"])
            tables.setdefault(event["t"], []).append(event["row"])
    assert counts is not None
    return {"tables": tables, "counts": counts, "order": order}
