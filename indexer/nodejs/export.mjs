// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
// Stage 3 (Node half): stream portfolio-schema rows from a codegraph index.
// Usage: node export.mjs <repoPath> <repoName>
// Emits JSON lines {"t": <table>, "row": {...}} on stdout in ingest order:
//   packages, types, methods, fields, extends, implements,
//   packageImports, typeImports, calls
// (the Python side streams "files" rows first, from its own git scan).
// Row fields match java-graph's ingest exactly (contract, design.md §5 and
// the mapping table there); a final {"t":"counts", ...} line carries
// codegraph's own stats for cross-checking.
//
// Identity rules (spike findings, docs/verification-bulk-api.md):
//   java/kotlin/scala — Node.qualifiedName "pkg::Type[::Member…]" gives
//     portfolio-global FQNs ("pkg.Type.member"), as java-graph had.
//   everything else — file-scoped: Type fqn "<repo>:<file>::<Name>",
//     synthetic Type {kind:'module'} "<repo>:<file>" owns ownerless
//     methods/fields, Package "<repo>:<dir>".
//   calls group by (caller method id, callee name as written minus receiver)
//     with count — name-level, java-graph semantics; unresolved call refs
//     synthesize CallTarget {external:true}.
import pkg from '@colbymchenry/codegraph';
const { CodeGraph, DatabaseConnection, QueryBuilder, getDatabasePath, NODE_KINDS } = pkg;

const [repoPath, repoName] = process.argv.slice(2);
if (!repoPath || !repoName) {
  console.error('usage: node export.mjs <repoPath> <repoName>');
  process.exit(2);
}

const JAVAISH = new Set(['java', 'kotlin', 'scala']);
const TYPE_KIND_MAP = {
  class: 'class', struct: 'class', trait: 'class',
  interface: 'interface', protocol: 'interface',
  enum: 'enum',
};
const METHOD_KINDS = new Set(['method', 'function']);
const FIELD_KINDS = new Set(['field', 'property', 'enum_member']);

const emit = (t, row) => process.stdout.write(JSON.stringify({ t, row }) + "\n");

const cg = await CodeGraph.open(repoPath, { readOnly: true });
const stats = cg.getStats();
const conn = DatabaseConnection.open(getDatabasePath(repoPath), { readOnly: true });
const qb = new QueryBuilder(conn.getDb());

// ---- pass 1: nodes --------------------------------------------------------
const typeNodes = new Map(), methodNodes = new Map(), fieldNodes = new Map(), importNodes = new Map();
for (const kind of NODE_KINDS) {
  for (const n of qb.iterateNodesByKind(kind)) {
    if (TYPE_KIND_MAP[n.kind]) typeNodes.set(n.id, n);
    else if (METHOD_KINDS.has(n.kind)) methodNodes.set(n.id, n);
    else if (FIELD_KINDS.has(n.kind)) fieldNodes.set(n.id, n);
    else if (n.kind === 'import') importNodes.set(n.id, n);
  }
}

const segsOf = (n) => n.qualifiedName.split('::');
const posixDir = (file) => {
  const i = file.lastIndexOf('/');
  return i <= 0 ? (i === 0 ? '/' : '.') : file.slice(0, i);
};
const typeFqnOf = (n) => {
  const s = segsOf(n);
  if (JAVAISH.has(n.language)) {
    const inner = s.slice(1).join('.');
    return inner ? `${s[0]}.${inner}` : s[0];
  }
  return `${repoName}:${n.filePath}::${s.join('::')}`;
};
const packageOfNode = (n) => JAVAISH.has(n.language)
  ? (segsOf(n).length >= 2 ? segsOf(n)[0] : '') // default-package types -> ''
  : `${repoName}:${posixDir(n.filePath)}`;
const moduleTypeFqn = (file) => `${repoName}:${file}`;
const methodIdOf = (n, ownerFqn) => {
  const arity = arityOf(n);
  return arity === null ? `${ownerFqn}#${n.name}` : `${ownerFqn}#${n.name}/${arity}`;
};
// Arity: top-level commas inside the LAST parenthesized group of signature
// ("AlertResult (AlertInfo info)" -> 1; "void setX(String a, int b)" -> 2).
function arityOf(node) {
  const sig = node.signature;
  if (!sig) return null;
  const open = sig.lastIndexOf('(');
  if (open === -1) return null;
  let depth = 0, commas = 0, saw = false;
  for (let i = open + 1; i < sig.length; i++) {
    const c = sig[i];
    if (c === '<' || c === '(' || c === '[') depth++;
    else if (c === '>' || c === ']' || (c === ')' && depth > 0)) depth--;
    else if (c === ')' && depth === 0) break;
    else if (c === ',' && depth === 0) commas++;
    else if (!/\s/.test(c)) saw = true;
  }
  return saw ? commas + 1 : 0;
}
function returnTypeOf(node) {
  const sig = node.signature;
  if (sig && sig.includes('(')) {
    let head = sig.slice(0, sig.lastIndexOf('(')).trim();
    if (node.name && head.endsWith(node.name) && head !== node.name) {
      head = head.slice(0, -node.name.length).trim();
    }
    if (head) return head;
  }
  return node.returnType ?? '';
}
function fieldTypeOf(node) {
  const sig = node.signature;
  if (sig && node.name && sig.endsWith(node.name)) {
    const t = sig.slice(0, -node.name.length).trim();
    if (t) return t;
  }
  return node.returnType ?? '';
}
const modifiersOf = (n) => [n.visibility, n.isStatic ? 'static' : '',
  n.isAbstract ? 'abstract' : '', n.isAsync ? 'async' : ''].filter(Boolean).join(' ');

// ---- pass 2: edges --------------------------------------------------------
const containsOwner = new Map(); // member node id -> owning Type node id
const extendsRaw = [], implementsRaw = []; // {srcNode, targetFqn, external}
const callsRaw = new Map();      // srcNodeId \0 calleeName -> count

const allIds = [...typeNodes.keys(), ...methodNodes.keys(), ...fieldNodes.keys()];
const BATCH = 5000;
for (let i = 0; i < allIds.length; i += BATCH) {
  for (const e of qb.getOutgoingEdgesFrom(allIds.slice(i, i + BATCH))) {
    if (e.kind === 'contains') {
      if (typeNodes.has(e.source) &&
          (methodNodes.has(e.target) || fieldNodes.has(e.target))) {
        containsOwner.set(e.target, e.source);
      }
    } else if (e.kind === 'extends' || e.kind === 'implements') {
      const src = typeNodes.get(e.source), dst = typeNodes.get(e.target);
      if (src && dst) {
        (e.kind === 'extends' ? extendsRaw : implementsRaw)
          .push({ srcNode: src, targetFqn: typeFqnOf(dst), external: null });
      }
    } else if (e.kind === 'calls') {
      if (!methodNodes.has(e.source)) continue;
      const refName = e.metadata?.refName
        ?? methodNodes.get(e.target)?.name
        ?? fieldNodes.get(e.target)?.name
        ?? typeNodes.get(e.target)?.name;
      if (!refName) continue;
      const callee = String(refName).split('.').pop();
      const key = `${e.source}\u0000${callee}`;
      callsRaw.set(key, (callsRaw.get(key) || 0) + 1);
    }
  }
}

// ---- owner finalization ---------------------------------------------------
// java-ish: walk the qualifiedName path outward past enum-constant bodies
// (enum X { CONST { methods } } — codegraph kinds CONST as enum_member, a
// Field to us; java-graph attached such methods to the enum Type, so we do
// too). Non-java: file's module type, overridden by contains edges below.
const typePathsByFile = new Map();          // file -> Map(pathKey -> typeNode)
const enumMemberPathsByFile = new Map();    // file -> Set(pathKey)
for (const [, n] of typeNodes) {
  if (!JAVAISH.has(n.language)) continue;
  const m = typePathsByFile.get(n.filePath) || new Map();
  m.set(segsOf(n).slice(1).join('::'), n);
  typePathsByFile.set(n.filePath, m);
}
for (const [, n] of fieldNodes) {
  if (n.kind !== 'enum_member' || !JAVAISH.has(n.language)) continue;
  const m = enumMemberPathsByFile.get(n.filePath) || new Set();
  m.add(segsOf(n).slice(1).join('::'));
  enumMemberPathsByFile.set(n.filePath, m);
}
function javaOwnerOf(n) {
  const s = segsOf(n);
  const path = s.slice(1, -1);
  while (path.length) {
    const key = path.join('::');
    const t = typePathsByFile.get(n.filePath)?.get(key);
    if (t) return typeFqnOf(t);
    if (enumMemberPathsByFile.get(n.filePath)?.has(key)) {
      path.pop(); // enum-constant body — keep walking up to its enum Type
      continue;
    }
    break;
  }
  if (s.length >= 3) return `${s[0]}.${s.slice(1, -1).join('.')}`;
  return moduleTypeFqn(n.filePath);
}
const ownerFqn = new Map(); // member/type node id -> owning Type fqn
for (const [id, n] of typeNodes) ownerFqn.set(id, typeFqnOf(n));
for (const [id, n] of methodNodes) {
  ownerFqn.set(id, JAVAISH.has(n.language) ? javaOwnerOf(n)
                                           : moduleTypeFqn(n.filePath));
}
for (const [id, n] of fieldNodes) {
  ownerFqn.set(id, JAVAISH.has(n.language) ? javaOwnerOf(n)
                                           : moduleTypeFqn(n.filePath));
}
for (const [memberId, typeId] of containsOwner) {
  const m = methodNodes.get(memberId) || fieldNodes.get(memberId);
  // java-ish owners already derive correctly from qualifiedName; contains
  // edges are the authority for file-scoped languages
  if (m && !JAVAISH.has(m.language)) ownerFqn.set(memberId, typeFqnOf(typeNodes.get(typeId)));
}

// ---- imports (kind=import nodes) + java single-import maps -----------------
const singleImportsByFile = new Map(); // file -> Map(simpleName -> fqn)
const packageImports = [], typeImports = [];
for (const [, n] of importNodes) {
  if (JAVAISH.has(n.language)) {
    let target = (n.signature || '').replace(/^import\s+/, '').replace(/;+\s*$/, '').trim();
    const isStatic = target.startsWith('static ');
    if (isStatic) target = target.slice(7).trim();
    if (!target) continue;
    const wildcard = target.endsWith('.*');
    if (wildcard) {
      packageImports.push({ file_id: `${repoName}:${n.filePath}`, target: target.slice(0, -2) });
    } else {
      const parts = target.split('.');
      const simple = parts.length > 1 ? parts[parts.length - 1] : target;
      const byFile = singleImportsByFile.get(n.filePath) || new Map();
      byFile.set(simple, target);
      singleImportsByFile.set(n.filePath, byFile);
      typeImports.push({ file_id: `${repoName}:${n.filePath}`, target, external: null });
    }
  } else {
    // non-java: module-specifier "package" (node.name is the module name)
    packageImports.push({ file_id: `${repoName}:${n.filePath}`, target: n.name });
  }
}

// ---- declared set + unresolved refs ----------------------------------------
const declaredFqns = new Set([...typeNodes.values()].map(typeFqnOf));
const unresolvedCalls = new Set(); // callsRaw keys that came from unresolved refs

for (const file of cg.getFiles()) {
  for (const ref of qb.getUnresolvedReferencesInFile(file.path, 1_000_000)) {
    if (ref.referenceKind === 'calls') {
      const src = methodNodes.get(ref.fromNodeId);
      if (!src) continue;
      const callee = ref.referenceName.split('.').pop();
      const key = `${ref.fromNodeId}\u0000${callee}`;
      callsRaw.set(key, (callsRaw.get(key) || 0) + 1);
      unresolvedCalls.add(key);
    } else if (ref.referenceKind === 'extends' || ref.referenceKind === 'implements') {
      const src = typeNodes.get(ref.fromNodeId);
      if (!src) continue;
      const name = ref.referenceName;
      let target;
      if (name.includes('.')) target = name;
      else if (JAVAISH.has(src.language)) {
        target = singleImportsByFile.get(src.filePath)?.get(name)
          ?? `${segsOf(src)[0]}.${name}`;
      } else target = name;
      (ref.referenceKind === 'extends' ? extendsRaw : implementsRaw)
        .push({ srcNode: src, targetFqn: target, external: null });
    }
  }
}

// ---- build + emit rows (deterministic order) -------------------------------
const packages = new Set();
for (const n of typeNodes.values()) packages.add(packageOfNode(n));

// module types actually used as owners of ownerless members
const moduleTypes = new Map(); // fqn -> {file}
for (const [id, n] of methodNodes) {
  if (ownerFqn.get(id) === moduleTypeFqn(n.filePath)) moduleTypes.set(ownerFqn.get(id), { file: n.filePath });
}
for (const [id, n] of fieldNodes) {
  if (ownerFqn.get(id) === moduleTypeFqn(n.filePath)) moduleTypes.set(ownerFqn.get(id), { file: n.filePath });
}
for (const { file } of moduleTypes.values()) packages.add(`${repoName}:${posixDir(file)}`);

const typeRows = [];
for (const [id, n] of typeNodes) {
  typeRows.push({ fqn: typeFqnOf(n), name: n.name, kind: TYPE_KIND_MAP[n.kind],
    line: n.startLine, modifiers: modifiersOf(n), package: packageOfNode(n),
    file_id: `${repoName}:${n.filePath}`, external: false });
}
for (const [fqn, { file }] of moduleTypes) {
  typeRows.push({ fqn, name: file, kind: 'module', line: 1, modifiers: '',
    package: `${repoName}:${posixDir(file)}`, file_id: `${repoName}:${file}`,
    external: false });
}

const methodRows = [];
for (const [id, n] of methodNodes) {
  const owner = ownerFqn.get(id);
  methodRows.push({ id: methodIdOf(n, owner), name: n.name, arity: arityOf(n),
    return_type: returnTypeOf(n), modifiers: modifiersOf(n), line: n.startLine,
    type_fqn: owner });
}
const fieldRows = [];
for (const [id, n] of fieldNodes) {
  const owner = ownerFqn.get(id);
  fieldRows.push({ fqn: `${owner}.${n.name}`, name: n.name, type: fieldTypeOf(n),
    modifiers: modifiersOf(n), line: n.startLine, type_fqn: owner });
}

// extends/implements: mark targets not declared in this repo external
const extendsRows = extendsRaw.map(r => ({ fqn: typeFqnOf(r.srcNode), target: r.targetFqn,
  external: r.external ?? !declaredFqns.has(r.targetFqn) }));
const implementsRows = implementsRaw.map(r => ({ fqn: typeFqnOf(r.srcNode), target: r.targetFqn,
  external: r.external ?? !declaredFqns.has(r.targetFqn) }));
for (const ti of typeImports) ti.external = !declaredFqns.has(ti.target);

// calls: group by (final method id, callee); unresolved ones external
const callRows = new Map(); // methodId \0 callee -> row
for (const [key, count] of callsRaw) {
  const [srcId, callee] = key.split('\u0000');
  const src = methodNodes.get(srcId);
  const method_id = methodIdOf(src, ownerFqn.get(srcId));
  const ckey = `${method_id}\u0000${callee}`;
  const row = callRows.get(ckey) || { method_id, callee, count: 0, external: null };
  row.count += count;
  if (unresolvedCalls.has(key)) row.external = true;
  callRows.set(ckey, row);
}

// emit in ingest order, sorted for determinism
const byFqn = (a, b) => a.fqn < b.fqn ? -1 : a.fqn > b.fqn ? 1 : 0;
const byId = (a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
for (const fqn of [...packages].sort()) emit('packages', { fqn });
for (const r of typeRows.sort(byFqn)) emit('types', r);
for (const r of methodRows.sort(byId)) emit('methods', r);
for (const r of fieldRows.sort(byFqn)) emit('fields', r);
for (const r of extendsRows.sort(byFqn)) emit('extends', r);
for (const r of implementsRows.sort(byFqn)) emit('implements', r);
const byImport = (a, b) => `${a.file_id}|${a.target}` < `${b.file_id}|${b.target}` ? -1 : 1;
for (const r of packageImports.sort(byImport)) emit('packageImports', r);
for (const r of typeImports.sort(byImport)) emit('typeImports', r);
for (const r of [...callRows.values()].sort((a, b) => a.method_id < b.method_id ? -1 : 1)) emit('calls', r);

emit('counts', {
  stats: { nodeCount: stats.nodeCount, edgeCount: stats.edgeCount, fileCount: stats.fileCount },
  exported: {
    packages: packages.size, types: typeRows.length, methods: methodRows.length,
    fields: fieldRows.length, extends: extendsRows.length,
    implements: implementsRows.length, packageImports: packageImports.length,
    typeImports: typeImports.length, calls: callRows.size,
  },
});
conn.close();
cg.close();
