# Changelog

All notable changes to `dbly` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.18.0] — 2026-07-27

### Fixed

- **No more silent drops.** A changed object file the parser recognizes no object in (commonly
  a PL/pgSQL function/trigger sqlglot renders as opaque `Command` nodes) was skipped without a
  word — not planned, not deployed, not drift-checked. `plan` now **warns** for each such file
  and points at the `type_from = "extension"` knob.
- **`type_from = "extension"` now also rescues the `Command` case.** The extension/filename
  fallback previously only triggered when sqlglot *raised*; it now also applies when sqlglot
  "succeeds" but yields no `CREATE` (the common PL/pgSQL case), so those objects are recognized
  (kind from extension, name from filename) and deployed verbatim per file (ADR 0002/0003).
- **`dbly status` resolves named targets.** `status --target <name>` crashed because it used the
  raw profile-path resolver instead of the project-aware one; it now resolves `[targets]` names
  from `dbly.toml` like every other command.

### Added

- **Configurable repository file layout (`[layout]` in `dbly.toml`, ADR 0003).** Pin how dbly
  reads a repo instead of assuming one convention:
  - `schema_from` — where the schema name comes from: `folder` (default), `search-path` (the
    file's `SET search_path`), or `qualified-name` (the DDL must qualify it).
  - `schema_depth` / `database_depth` — which path segment under `object_root` is the schema
    (default 1) and, for `<database>/<schema>/…` repos, the database (a multi-database repo then
    deploys only the target profile's database).
  - `type_from` — object kind from `sql` (parse, default) or `extension`; extension mode also
    gives a **filename-based identity fallback**, so a replaceable file the parser can't read
    (PostGIS/PL-pgSQL) is still recognized and applied verbatim instead of dropped.
  - `order` — apply replaceable objects by `dependency` (default) or `filename`.

  All defaults equal the previous behaviour; `[layout]` is purely additive.

## [0.16.0] — 2026-07-26

### Changed

- **Replaceable objects are applied per source file, verbatim (ADR 0002).** Views, functions,
  procedures, triggers, types and grants now deploy their raw source file (dependency-ordered,
  one step per file) via the engine's multi-statement runner, instead of a single re-rendered
  `CREATE` statement per object. This preserves `SET search_path`, `ALTER … OWNER`, comments,
  intra-file statement order and function overloads — everything the author wrote in the file —
  and avoids re-rendering procedural bodies. The plan shows one row per file (a multi-object
  file notes "+N more in <file>").

## [0.15.0] — 2026-07-26

### Added

- **`dbly baseline`** — record a ref as deployed without running any SQL, to adopt an existing
  (brownfield / hand-deployed) database. `plan` then diffs incrementally from that ref instead
  of treating the target as empty; migrations up to the ref are marked applied (not run).
  Nothing in the schema is touched.

## [0.14.1] — 2026-07-25

### Fixed

- **Last view-drift false positive removed.** `pg_get_viewdef` wraps a `FROM` join in an
  alias-less subquery (`FROM (a JOIN b)`) where the source wrote it flat; the view-canonical
  form now flattens that, so an identical view reports clean. On the real `download` schema,
  reported view drift is now 0 (all 41 originally reported were normalization artifacts).

## [0.14.0] — 2026-07-25

### Added

- **`--with-deps`: isolated deploy that pulls in what it needs.** `plan`/`apply --schema X
  --with-deps` deploys schema `X` plus the *specific* objects it depends on that aren't already
  in the target (a cross-schema FK target, a referenced table/view/function) — their missing
  dependency closure, resolved from the full repo graph. It stops at anything that already
  exists, so it never drags in whole existing schemas or proposes to modify their objects.

### Fixed

- **Overloaded functions are no longer dropped.** Two functions with the same name but
  different signatures (e.g. `fn(text[])` + `fn(text)` in one file) share an object key;
  `topological_order` deduplicated them, so one overload was never deployed — breaking the
  other that called it. Overloads are now kept together in file order.
- **Function-call ordering.** A trigger/view that calls a repo function now orders that
  function first (function references — sqlglot `Anonymous` — are captured as graph edges;
  calls buried in PL/pgSQL bodies remain out of reach and rely on file order).
- **Unqualified references resolve to the object's schema**, so a same-schema FK/`FROM` links
  to the right object and a table's own name isn't mistaken for a self-dependency.

## [0.13.0] — 2026-07-25

### Fixed

- **Tables are created in FK-dependency order.** A table with an inline
  `REFERENCES other_table` was emitted in file order, so on a fresh target the referencing
  table could be created before its target (`relation "…" does not exist`). Tables are now
  topologically sorted by their inter-table foreign keys, like replaceable objects already were.

### Added

- **Pre-flight dependency warning.** `plan` warns when a table's FK target is neither in the
  deploy nor already in the target — e.g. a cross-schema FK left out by `--schema`/`--path` —
  instead of failing deep in `apply` with a cryptic error.
- **Clean `apply` failure.** A failed statement now prints a one-line cause (and whether the
  transaction rolled back) rather than a full Python traceback.

## [0.12.0] — 2026-07-25

### Fixed

- **Greenfield deploy no longer fails on a missing schema.** `plan`/`apply` now emit
  `CREATE SCHEMA` for the schemas its managed objects live in, before those objects — so a
  fresh target where schema `download` doesn't exist no longer dies with
  `schema "download" does not exist` on the first `CREATE TABLE download.…`. One additive step
  per absent schema, first in the plan. PostgreSQL (`CREATE SCHEMA IF NOT EXISTS`) and SQL
  Server (guarded `EXEC('CREATE SCHEMA …')`); Oracle (schemas are users → `init`) and SQLite
  (no schemas) are unaffected.

### Added

- `ObjectKind.SCHEMA` so the schema step renders as `+ create schema <name>`.

## [0.11.0] — 2026-07-24

### Changed

- **Grants are "apply-only" in `check`.** GRANT statements (typically collected in a
  `grants.sql`) aren't introspectable as state, so `check` used to report them as permanently
  "missing". They are now excluded from drift and shown as a one-line note ("N grant
  statement(s) run on every apply but not verified by check"). `plan`/`apply` still execute
  them unchanged — grants matter, they just can't be *verified* by a drift check.

## [0.10.0] — 2026-07-24

### Changed

- **Views are compared by canonical structure, not text (ADR 0001).** A database stores a view
  as a *normalized rewrite*, not the source text (Postgres elides no-op casts, qualifies names,
  re-parenthesizes), so text/hash comparison flagged correctly-deployed views as drifted. View
  drift now works in two layers: (1) the desired view is run through the engine — a throwaway,
  rolled-back `TEMP VIEW` — so both sides carry the *same* engine normalization; (2) the two
  are compared as **parse trees** with all redundant parentheses removed and column qualifiers
  stripped (operator precedence is preserved because it lives in the tree shape). On a real
  schema this took reported view drift from 41 to 1. See `docs/adr/0001-*.md`, incl. known
  limitations (parenthesized `FROM` joins may still show a cosmetic diff — use `--show-diff`).
  Requires `CREATE` privilege for the deploy connection (expected for a deploy tool); without
  it, view comparison falls back to the raw definition.

### Note

- Engine round-trip is implemented for **PostgreSQL**. Oracle, SQL Server and SQLite still use
  the direct definition comparison and will adopt the round-trip in a follow-up (ADR 0001).

## [0.9.0] — 2026-07-24

### Added

- **`dbly check --show-diff`.** For each changed view (and, with `--advisory`, procedural
  object), print a unified diff (live → repo) of the normalized definition. This is what tells
  a genuine change (e.g. a `CAST` the deployed view is missing) apart from `pg_get_viewdef`
  reformatting that normalization can't fully iron out — the residual after the 0.8.1 fixes.

## [0.8.1] — 2026-07-24

### Fixed

- **Fewer false-positive view drifts.** `check` now strips two artifacts of Postgres'
  `pg_get_viewdef` before comparing a view: per-column table qualifiers (`foo` → `t.foo`) and
  redundant parentheses (`st_multi(g)` → `(st_multi(g))`). Both are semantics-preserving, so a
  genuine change (an added cast, a changed filter, a new column) still shows. On a real schema
  this cut reported view drift by roughly a third; the remainder are genuine differences or
  deeper `pg_get_viewdef` reformatting that only a textual diff can adjudicate.

## [0.8.0] — 2026-07-24

### Fixed

- **Column type comparison no longer cries wolf.** `plan` flagged dozens of non-changes
  (`INTEGER → INT`, `NUMERIC → DECIMAL`, `TIMESTAMP → TIMESTAMPTZ`, `ARRAY → TEXT[]`,
  `geometry → NULL`) because it compared type *strings* across the sqlglot↔SQLAlchemy
  boundary. Types are now parsed through sqlglot's type parser and compared structurally, so
  synonyms collapse; and reflected DB types are rendered faithfully (a TIMESTAMP keeps its
  timezone, an ARRAY its element type) instead of via lossy `str()`. When either side is an
  unknown/unmodelled type (e.g. PostGIS `geometry`), no change is reported rather than a false
  positive. A genuine precision change (`NUMBER → NUMBER(10)`) is still caught.

### Changed

- **`check` output now matches `plan`.** Drift is rendered in the same Terraform-style rows —
  `+ create`, `+ add column`, `- only-in-db`, `~ modify` — with a `Drift: N to create, N to
  change, N only in DB.` summary, so the two commands read alike.

## [0.7.0] — 2026-07-24

### Fixed

- **Views no longer always report drift.** `check` compared a hash of the repo's full
  `CREATE VIEW … AS SELECT …` against a hash of the database's bare `SELECT` (Postgres
  `pg_get_viewdef`), so *every* view looked changed. Both sides are now reduced to the SELECT
  body and compared consistently — an identical view reports clean.

### Changed

- **Terraform-style `plan` / `apply` output.** `plan` now prints `Plan: N to change, M to
  destroy.` followed by aligned rows with action markers (`+` create/add, `~` modify,
  `!` unsafe, `-` drop), kind and target. `apply` reports each step as `✓ … OK` and finishes
  with `✓ Apply complete!`.
- **Procedural definition drift is advisory and off by default.** Function/procedure/trigger
  bodies cannot be canonicalized reliably across the repo↔DB boundary (sqlglot does not parse
  PL/*), so they produced constant false positives. They are now shown only with
  `dbly check --advisory` and never make a check "dirty" (non-zero exit) on their own.

## [0.6.0] — 2026-07-24

### Added

- **`dbly export` — reverse direction.** Introspect a live database and emit its DDL as a SQL
  script, optionally transpiled to another engine with `--dialect` (postgres | oracle |
  sqlserver | sqlite). Tables and views are converted across dialects (tables rebuilt from the
  live column set — constraints/indexes are not reconstructed, and a warning says so);
  procedural objects (function/procedure/trigger/package/type) are emitted **verbatim** in the
  source dialect. Scope with `--schema`; write to a file with `--out`. dbly's own `dbly_state`
  ledger is never exported.

## [0.5.0] — 2026-07-24

### Added

- **Partial deploy / check.** `plan`, `apply` and `check` take `--schema NAME` (the folder
  under `object_root`; repeatable, case-insensitive) and `--path SUBPATH` (any subtree under
  `object_root`; repeatable) to scope the operation to part of the repo — e.g. deploy only
  `bas/`. Both filters AND together; orphan reporting is scoped to the same selection.

### Changed

- **Redesigned `check` output.** Drift is now grouped with an explicit direction —
  *"Only in the repo — will be created on apply"* vs. *"Only in the database — not in the
  repo"* — with per-group counts, a summary tally, and `+`/`−` markers for column drift
  (`+` in the repo but missing from the DB, `−` in the DB but not the repo). The header shows
  the ref (decorated) and the active scope.

## [0.4.1] — 2026-07-23

### Changed

- **Quiet by default.** sqlglot's per-statement fallback WARNINGs (it echoes any procedural /
  engine-specific DDL it can't fully parse) and SQLAlchemy's "Did not recognize type" warnings
  (e.g. PostGIS `geometry`) flooded the output on real repos. Both are now silenced; pass
  `--debug` to see them again.

## [0.4.0] — 2026-07-23

### Added

- **Project configuration (`dbly.toml`).** An optional file at the repo root, for repos that
  need more than a single connection profile:
  - `object_root` — the subtree the declarative object files live under. The schema hint is
    taken from the first path segment *below* this root, so a layout like
    `pgsql/schema/<schema>/<obj>` maps to the right schema instead of the literal top folder.
  - `environment` — default engine/dialect when a profile omits `environment=`.
  - `[targets]` — named connection profiles, so `dbly plan --target dev` resolves to a
    profile path.
  - `ignore` — extra ignore patterns (gitwildmatch), merged with `.dbignore` (e.g. to skip a
    handful of files a parser cannot read).

  Absent config → unchanged behaviour (`object_root="."`, `--target` is a profile path).

### Fixed

- **Case-insensitive identifier matching on PostgreSQL.** Postgres folds unquoted identifiers
  to lower case, so a DDL `CREATE TABLE FOO` lives as `foo`. `plan` (`table_exists`/
  `get_columns`) and `check` looked the object up by the *declared* name and either planned a
  spurious full `CREATE` or crashed with `NoSuchTableError`. Both now resolve the object
  case-insensitively and reflect it by its real, live identity.
- **`check` no longer aborts on a single unreflectable table.** A table whose columns cannot
  be introspected is reported as advisory `unreadable` and the drift scan continues, instead
  of failing the whole run.

## [0.3.0] — 2026-07-21

### Added

- **Plan against the working tree.** `dbly plan --worktree` (alias `--dirty`) and
  `dbly check --worktree` diff the *working directory* — including uncommitted edits and
  untracked new object files — instead of a git ref, for the fast edit→plan loop. Preview
  only; `apply` still requires a real committed ref for the ledger.
- **Git-style ref decoration.** `dbly status` and the plan header now show the tag/branch
  names pointing at a SHA next to it — e.g. `deployed ref: v0.1, main (a712b631)`. The
  ledger still stores only the SHA (for stable diffs); the names are resolved at display time.

### Fixed

- **Column type changes are now detected.** A changed column type (e.g. Oracle `NUMBER` →
  `NUMBER(10)`) previously produced "nothing to do" — the diff matched columns by name only.
  It now emits an `ALTER TABLE … MODIFY`/`ALTER COLUMN` step (flagged destructive, never
  auto-applied) with a data-compatibility warning. Comparison is precision/scale-aware and
  normalizes introspection noise (e.g. SQL Server `COLLATE`) to avoid false positives.
- **`check` no longer reports live Oracle objects as "missing".** The Oracle adapter now
  resolves its `default_schema` to the connected user and carries the owner on introspected
  objects, so drift keys align with the desired side (previously the owner was dropped on the
  live side, so every object looked missing even right after a successful `apply`).

### Changed

- The plan output now surfaces each step's **note inline** (e.g. *"NOT NULL without default
  on existing table — unsafe"*), so *why* a step is flagged is visible without cross-checking
  the warnings block.

## [0.2.0] — 2026-06-28

### Added

- **Explicit, run-once migrations.** Drop ordered SQL scripts in `migrations/` (`0001_…sql`)
  for changes the additive diff cannot do safely — renaming a column, type changes with data
  transformation, backfills. Each is tracked by id in the `dbly_state` ledger and runs
  exactly once.
- On **upgrade**, pending migrations run *before* the object reconciliation, so they reshape
  the schema first. On a **fresh database** they are *baselined* (recorded, not run) since the
  canonical object files already describe the end state.

### Changed

- A table touched by a pending migration **defers its additive diff** for that deploy, so an
  explicit rename no longer collides with an auto-generated `ADD COLUMN`.
- `migrations/` files are excluded from object and drift discovery (they are imperative
  scripts, not declarative objects).

## [0.1.0] — 2026-06-27

### Added

- **Indexes and sequences as first-class managed objects.** Correct identity (index name +
  indexed-table schema), dependency-safe ordering (sequences → tables → indexes), and
  create-if-missing handling (no more erroneous re-apply of non-idempotent
  `CREATE INDEX`/`CREATE SEQUENCE`).
- **Live inventory introspection** across tables, views, functions, procedures, triggers,
  indexes and sequences for all four engines, with a canonical source hash for procedural
  objects.
- **Real drift detection in `dbly check`** — reports missing (in repo, not in DB), orphaned
  (`--orphans`), table column drift, and advisory definition drift; schema-normalized so an
  unqualified repo object matches the engine's implicit schema.

## [0.0.1] — 2026-06-27

### Added

- Initial release. **State-based, cross-engine database deployment** (PostgreSQL, SQL Server,
  Oracle, SQLite), git-driven and parser-assisted (sqlglot).
- `plan` / `apply` workflow with additive table diffs generated from
  `CREATE TABLE IF NOT EXISTS` (destructive changes flagged, never auto-applied).
- `plan --sql` — exports a self-contained SQL script for a hand/offline deploy.
- `dbly init` — privileged greenfield groundwork (`CREATE DATABASE`/roles/extensions).
- Pre-/post-deploy hooks accepting `.sql` and `.py` (configurable interpreter, e.g. ArcPy).
- Connection profiles (DBFit-compatible `connection.properties`) with `${ENV}` placeholders
  for CI/CD.
- CI and PyPI publish workflows (trusted publishing).

[Unreleased]: https://github.com/angrydat/dbly/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/angrydat/dbly/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/angrydat/dbly/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/angrydat/dbly/releases/tag/v0.0.1
