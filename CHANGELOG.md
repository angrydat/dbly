# Changelog

All notable changes to `dbly` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
