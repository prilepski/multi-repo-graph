# Java Tree-sitter-to-Neo4j Parsing Pipeline

Parses Java source across multiple repositories and loads the resulting code
graph into **Neo4j**. No builds, no JDK, no Maven — pure parsing.

Reference design: the "Local Java Code Parsers and Indexers" conversation
(https://share.gemini.google/EKt8ZSwCiH1C), which prescribes:

> Indexer: "scip-java (if code builds via Maven/Gradle) or **JavaParser /
> Tree-sitter (if parsing raw, unbuilt multi-repo checkouts)**."

Our targets are raw, unbuilt multi-repo checkouts, so this pipeline uses
**Tree-sitter** (`tree-sitter-java`): fast, resilient to syntax errors, zero
build-tool requirements.

## Layout

| File | Purpose |
|---|---|
| `index_to_neo4j.py` | The pipeline (env validation → Neo4j container → parse → ingest) |
| `repos.json` | Manifest of target repos: `[{"name", "path"}, ...]` |
| `test_index_to_neo4j.py` | 30 offline unit tests + 1 Neo4j integration test (`NEO4J_TEST_URI`) |

## Graph schema (per the reference conversation)

```
Nodes: (:Repository), (:Package), (:File {path, sha256}), (:Type {kind: class|interface|enum|record|annotation}),
       (:Method), (:Field), (:CallTarget)

`:File.sha256` is the content hash of the parsed text — consumers can detect
when the working tree has drifted from the indexed graph.
Edges: (Repository)-[:CONTAINS]->(File | Package)
       (Package)-[:CONTAINS]->(Type)
       (File)-[:DEFINES]->(Type)
       (Type)-[:CONTAINS]->(Method | Field)
       (Type)-[:EXTENDS | :IMPLEMENTS]->(Type)      # best-effort FQN resolution
       (File)-[:IMPORTS]->(Package | Type)
       (Method)-[:CALLS {count}]->(CallTarget)      # syntactic (name-level)
```

Per the reference's Tree-sitter trade-off note, `CALLS` is syntactic: it links
a caller method to a name-level call target (overloads are not resolved —
that requires a compile-based indexer such as scip-java). `Type` nodes for
targets not declared in the parsed repos are created with `external: true`.

## Usage

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Default engine is podman; use --docker for Docker:
.venv/bin/python index_to_neo4j.py --docker --repos repos.json
```

Pipeline steps:

1. **Prerequisites** — container engine check; missing Python packages
   (`neo4j`, `tree-sitter`, `tree-sitter-java`) are pip-installed
   automatically (`--no-auto-install` to fail fast instead).
2. **Neo4j** — reuses/starts container `scip-neo4j-instance`
   (`neo4j:5-community`, ports 7474/7687, password `graphpassword123`, APOC),
   waits for Bolt readiness.
3. **Parse** — every `*.java` under each repo path is parsed with
   Tree-sitter: package, imports (single/wildcard), types (incl. nested),
   members (methods with arity/visibility, fields), inheritance, and
   syntactic method-call counts per method.
4. **Ingest** — chunked UNWIND Cypher (5 000 rows/batch), idempotent MERGEs,
   uniqueness constraints on all node keys.

## Verify in Neo4j Browser (http://localhost:7474)

```cypher
// biggest types by method count
MATCH (r:Repository)-[:CONTAINS]->(:Package)-[:CONTAINS]->(t:Type)-[:CONTAINS]->(m:Method)
RETURN r.name AS repo, t.fqn AS type, count(m) AS methods
ORDER BY methods DESC LIMIT 20;

// inheritance chains within a repo
MATCH (t:Type {external: false})-[:EXTENDS]->(base {external: false})
WHERE base.fqn STARTS WITH 'org.thingsboard'
RETURN t.fqn, base.fqn LIMIT 25;

// most-called methods across all repos
MATCH (m:Method)-[c:CALLS]->(ct:CallTarget)
RETURN ct.name AS method, sum(c.count) AS total_calls
ORDER BY total_calls DESC LIMIT 20;
```

## Tests

```bash
.venv/bin/python -m pytest test_index_to_neo4j.py -v
# with a live Neo4j (integration test included):
NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_PASSWORD=graphpassword123 \
  .venv/bin/python -m pytest test_index_to_neo4j.py
```

Unit tests are fully offline (synthetic Java source covering packages,
single/wildcard imports, class/interface/enum/record, nested types,
constructors, fields, per-method call collection, resilience to truncated
source). The integration test ingests twice (idempotency) and asserts the
graph shape, FQN resolution through imports, and CALLS edges.

## File inventory & code statistics

Each run inventories every **git-tracked** file before parsing:

- **Taxonomy**: `kind` (source/build/config/docs/data/assets/artifact/other)
  → `category` (23 rules: java_backend, java_test, frontend_logic/style/
  markup, build_script, ci_cd, container, iac, runtime_config, sql, …) plus
  `role` (main/test/generated) — rule table in `inventory.py`, design in
  `docs/inventory-stats-design.md`
- **LOC**: totalLines / codeLines / commentLines / blankLines per file with
  language-aware comment rules; binaries (extension or NUL-sniff) get bytes
  only
- **Coverage** per repository: filesTracked vs filesOnDisk vs untracked,
  deep-indexed (Java, symbol-level) vs inventory-only, binaries — verified
  (file counts vs graph, category sums, LOC spot-checks) and included in the
  run summary JSON
- **Snapshots**: `(:StatsSnapshot)-[:COVERS]->(:Repository)` +
  `(:CategoryStat)` rollups, retention **last 2 runs**
- Untracked working-tree files are counted as a pollution guard, never
  indexed (use `git ls-files -z` for the canonical list)

## Design note

An earlier iteration implemented the scip-java (compile-based) branch of the
reference design; it required full Maven builds and per-repo JDKs (the target
repos demand JDK 25 and JDK 8-era toolchains respectively). Per the
reference's own guidance for raw, unbuilt checkouts, the pipeline was
switched to Tree-sitter — parse-only, as required for this use case.
