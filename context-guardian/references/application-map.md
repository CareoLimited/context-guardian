# Code-Derived Application Map

Read this reference when designing, generating, refreshing, querying, validating, or
reviewing an application's map. The map is a living model of the complete codebase,
not a renamed Git diff or directory listing.

## Core Rule

Context Guardian must analyze application code comprehensively during onboarding and
maintain that analysis as the code evolves. Git state answers what changed; the map
answers what the code is, how it is connected, what behavior it implements, and what
else a change can affect.

The system must support both directions:

```text
task intent -> likely features/flows/contracts -> relevant code and checks
changed code -> symbols/relations -> affected behavior, twins, invariants, and checks
```

Neither direction may rely only on folder names or manually maintained prose.

## Required Map Layers

### 1. Repository topology

Inventory source roots, modules, packages, build targets, applications, extensions,
entry points, configuration, assets, generated-code boundaries, vendored code, test
roots, scripts, and platform-specific trees. Record language, role, and ownership
where these can be established.

### 2. Symbol index

Index stable symbol identities and source locations for relevant declarations:
components, screens, routes, functions, methods, types, classes, protocols,
interfaces, hooks, stores, reducers, services, schemas, endpoints, jobs, tests, and
configuration declarations. Preserve signatures and visibility in compact structured
form when supported by the language analyzer.

### 3. Relation graph

Represent evidence-backed edges such as:

- imports, exports, includes, and package dependencies;
- calls, construction, inheritance, implementation, and type use;
- route registration and navigation transitions;
- event publication, subscription, callbacks, observers, and background jobs;
- reads/writes of state, storage, database tables, caches, and configuration;
- API client calls, endpoints, request/response models, and serialization;
- UI-to-state-to-service-to-storage/network flow;
- tests covering symbols, features, contracts, or flows;
- shared/platform implementations and intentional twins;
- build target, entitlement, permission, asset, and localization relationships.

Edges must identify their source evidence and confidence. Static analysis cannot
prove every dynamic relationship; unresolved reflection, string-based routing,
dependency injection, code generation, or runtime registration must be exposed as a
gap rather than silently omitted.

### 4. Behavioral flows

Build queryable flows for important user and system behavior, such as startup,
authentication, onboarding, purchase restoration, notifications, navigation,
background work, persistence, synchronization, advertising, localization, and error
handling when the application uses them. A flow links entry points, intermediate
symbols, side effects, terminal states, tests, and validations.

Mechanically inferred flows are generated facts. Product meaning, required ordering,
failure behavior, and business guarantees are curated facts and require review.

### 5. Contracts and persistent state

Map externally or historically sensitive contracts: API schemas, deep links, route
parameters, storage keys, database schemas, migrations, serialization formats,
notification payloads, analytics events, feature flags, permissions, entitlements,
localization keys, and public/shared module interfaces. Link producers, consumers,
versions, migrations, tests, and invariants.

### 6. Feature and domain ownership

Associate code graph nodes and flows with application-specific features. Ownership
may be generated from strong evidence and curated where product boundaries are not
encoded in code. A node may belong to multiple features. Cross-cutting infrastructure
must not be forced into one feature merely to simplify routing.

### 7. Verification graph

Connect features, flows, symbols, contracts, and risk classes to unit/integration/UI
tests, static checks, builds, simulator/device scenarios, Maestro flows, and manual
checks that actually exist. A missing verification edge is useful map information and
must be reportable.

### 8. Curated knowledge

Keep product invariants, intentional platform differences, known runtime constraints,
and confirmed regression lessons separate from generated topology. Generated refresh
must never overwrite curated knowledge.

## Extraction Strategy

Use a layered analyzer architecture:

1. **Generic inventory:** portable filesystem, Git, manifest, and configuration
   analysis establishes repository-wide coverage.
2. **Language-aware analysis:** parsers, compiler indexes, or language servers extract
   symbols and semantic relations where reliable.
3. **Framework analyzers:** optional analyzers resolve conventions such as navigation,
   state management, dependency injection, build targets, resources, and generated
   registration.
4. **Domain analyzers:** optional checks understand contracts such as storage,
   migrations, notifications, premium, advertising, and localization.
5. **Curated overlay:** reviewed application knowledge resolves product intent and
   relationships that code alone cannot establish.

The shared engine owns analyzer interfaces and graph/query contracts. Analyzers may
be optional dependencies; unsupported stacks fall back gracefully and lower coverage
confidence. Never label lexical search as semantic certainty.

## Fact Metadata

Every generated node, edge, or flow must be traceable. At minimum record:

- stable identifier and kind;
- repository-relative source path and location when applicable;
- analyzer and analyzer version;
- extraction timestamp or generation ID;
- source-content identity or repository identity;
- confidence such as `exact`, `inferred`, or `unknown`;
- evidence sufficient to explain why the relationship exists.

Curated records must identify that they are curated and must not pretend to carry
mechanical freshness guarantees.

## Map Freshness & Stale Detection

The application map tracks repository-wide SHA-256 source digests, `git_commit` revisions,
and generator version (`context-guardian@1`). The freshness engine (`check_map_freshness`)
and `check-map` CLI command verify whether the persisted map matches the working tree:

```bash
context-guardian check-map --project <path> [--map <path>]
```

`inspect` reports freshness as `map_status` / `map_freshness`. A stale map is the normal
state of any working tree being edited, so it is recorded as a coverage gap (lower routing
confidence), never as a warning for the user. `finish` refreshes a stale generated map
before evaluating a task, unless the profile sets `"map": {"auto_refresh": false}`; only a
refresh that fails becomes a `map-refresh-failed` warning.

## Map Refresh

`refresh-map` brings the persisted map up to date with the working tree. The current
implementation re-reads every repository file and rebuilds nodes and edges (it is correct
but not yet incremental); reusing unchanged nodes is future work. On very large
repositories, measure it before relying on automatic refresh in `finish`:

```bash
context-guardian refresh-map --project <path> [--map <path>] [--path <file> ...]
```

1. Re-indexes the repository, including changed, added, and deleted source files;
2. Drops obsolete nodes and stale import/call edges;
3. Re-resolves import targets and call graph connections over the updated graph;
4. Refreshes feature ownership mappings and recalculates repository-wide SHA-256 digest;
5. Re-attaches updated summary statistics and Git commit revision.

## Python AST Semantic Analyzer

For Python source files (`.py`), the map generator and incremental refresh engine use
Python's built-in `ast` module (`analyzer: "python-ast@1"`) to extract:

- `ClassDef`: class name, line numbers, inheritance `bases`, decorators;
- `FunctionDef` and `AsyncFunctionDef`: function name, line numbers, decorators;
- `Import` and `ImportFrom`: exact module specifiers, relative level (`.`), alias `bindings`;
- Gracefully falls back to regex-based extraction if syntax errors occur.

## Full Build and Incremental Refresh

Onboarding performs a full repository scan. Later operation is incremental for speed,
but incremental refresh must update more than the edited file:

1. re-index changed, added, deleted, and renamed sources;
2. invalidate edges and flows whose evidence depended on those sources;
3. revisit importers, callers, registries, contracts, generated outputs, and twins;
4. refresh feature and verification associations;
5. record coverage gaps and analyzer failures;
6. periodically or on suspicious invalidation perform a full consistency rebuild.

Git is useful for discovering candidate invalidations and history, but filesystem and
content identities remain necessary. Uncommitted and non-Git worktrees must remain
supported.

## Query and Impact Model

The complete graph must stay outside model context. A deterministic query engine
returns bounded subgraphs with full counts and truncation markers. Useful queries
include:

- symbol definition, references, callers, and callees;
- feature/flow membership for a path or symbol;
- upstream producers and downstream consumers of a contract;
- readers/writers of state or persistent data;
- platform twins and intentional differences;
- related migrations and compatibility boundaries;
- tests and validations covering an affected scope;
- unexplained dynamic edges and unmapped nodes;
- task-intent candidates before any file changes exist.

Impact traversal must be typed, direction-aware, depth-bounded, and explainable. A
high-degree shared utility should not automatically select the entire application;
edge kind, confidence, feature boundary, contract sensitivity, and risk policy should
control traversal. Reports must show why each scope was selected.

Map Engine v1 now implements this foundation through `impact-map`. `dependents`
traverses reverse call/import relationships toward callers/importers and connects
symbols to containing files and files to owning features. `dependencies` traverses
toward callees/import targets and from feature/file containers to their contents.
`both` combines the directions. The caller may filter edge kinds and choose depth
0–8.

Traversal is cycle-safe breadth-first search. Returned nodes carry shortest-hop and
bounded reason evidence. Internal visits stop at 5,000 nodes; agent-facing node and
edge lists each stop at 100 while preserving full visited/traversed counts and
truncation flags. An unmatched seed is a coverage gap. These mechanics constrain and
explain existing graph evidence; they cannot compensate for semantic edges the
extractors did not discover.

`inspect` integrates directly with this impact traversal. When an application map is
present, Git-changed files automatically seed dependent impact traversal. The resulting
compact report unifies direct trigger matches with transitive graph-impacted features,
impacted files, impacted symbols, curated references, invariants, and validation
commands in a single bounded payload. If the map is missing, `inspect` falls back
gracefully to trigger-only routing.

## Token and Storage Policy

- Generate, refresh, and query the map with local deterministic computation whenever
  practical.
- Store the large query index in local generated/cache storage, not in prompts.
- Keep portable reviewed summaries and curated knowledge in `.app-context/`.
- Do not commit machine-specific absolute paths or volatile cache data.
- Return only the smallest relevant subgraph, references, invariants, gaps, and checks
  to the agent.
- Preserve counts and provenance when results are truncated.

Map Engine v1 currently writes a portable JSON interchange file to
`.app-context/generated/application-map-v1.json` and supports bounded one-hop queries.
This format is the implemented foundation, not a final scalability decision. A local
SQLite query index remains a strong candidate because Python includes SQLite support,
but it must be benchmarked against JSON diffability, rebuild cost, concurrent tasks,
and schema migration needs before becoming mandatory.

Current v1 extraction includes generic repository inventory, regex-derived symbols
for an initial language set, lexical imports, conservative unique local-file import
resolution, named/aliased import bindings for JavaScript/TypeScript and Python, a
limited binding-aware lexical call graph, and profile-trigger feature ownership.

Import/call edges remain inferred rather than compiler-proven. The engine deliberately
does not guess project-global same-name links. Package aliases, re-exports,
wildcard/namespace/default semantics, dynamic imports, overloads, method dispatch,
callbacks, reflection, generated registries, and runtime dependency injection remain
unresolved. It also does not yet implement behavioral flows, contracts, framework
analyzers, transitive impact, or incremental freshness. These limitations must remain
visible in reports and handoffs.

## Map Quality Measures

A map is not complete because indexing finished without crashing. Evaluate:

- repository and supported-language coverage;
- symbol extraction precision and missing-symbol rate;
- relation precision and recall on reviewed samples;
- feature and flow routing accuracy;
- historical-change impact recall;
- platform-twin and contract coverage;
- verification-edge usefulness;
- stale-fact detection and incremental/full rebuild equivalence;
- query latency, cache size, report byte size, and injected token size;
- explicit unknown/gap reporting.

The first pilot must use real code paths and past regressions to establish baselines.
Unsupported or dynamic areas remain visible uncertainties, not hidden behind one
overall completeness percentage.
