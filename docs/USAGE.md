# dbly — Usage Guide

A task-oriented walkthrough of the day-to-day workflows. For the elevator pitch and install,
see the [README](../README.md); for design decisions, see the [ADRs](adr/).

- [Mental model](#mental-model)
- [1. Set up a repo](#1-set-up-a-repo)
- [2. Connect to a database](#2-connect-to-a-database)
- [3. The core loop: plan → apply](#3-the-core-loop-plan--apply)
- [4. Adopt an existing database (`baseline`)](#4-adopt-an-existing-database-baseline)
- [5. Deploy part of a repo (`--schema` / `--path` / `--with-deps`)](#5-deploy-part-of-a-repo)
- [6. Check for drift](#6-check-for-drift)
- [7. Fast iteration against the working tree](#7-fast-iteration-against-the-working-tree)
- [8. Run-once migrations](#8-run-once-migrations)
- [9. Export a live database (reverse)](#9-export-a-live-database-reverse)
- [10. Greenfield groundwork & hooks](#10-greenfield-groundwork--hooks)
- [Command reference](#command-reference)
- [Honest limitations](#honest-limitations)

---

## Mental model

dbly is **state-based**: your git repo declares the *desired* state (one SQL file per object);
dbly reads the database's *actual* state and works out the difference.

- **Tables** (and indexes/sequences) are **stateful** — never blindly re-run. dbly diffs the
  desired columns against the live table and generates an additive `ALTER`. Destructive deltas
  (dropped columns, type changes) are flagged and never auto-applied.
- **Views, functions, procedures, triggers, types, grants** are **replaceable** — re-applied
  wholesale each deploy, dependency-ordered, from their raw source file (see
  [ADR 0002](adr/0002-per-file-application-of-replaceable-objects.md)).
- A small ledger table (`dbly_state`) records which git ref is deployed and which run-once
  migrations have run. `plan` diffs from that recorded ref.

Engines: **PostgreSQL, SQL Server, Oracle, SQLite**.

## 1. Set up a repo

One file per object; the folder names your schema. Any extension dbly recognizes as SQL works
(`.sql`, `.tbl`, `.vw`, `.fnc`, `.prc`, `.trg`, …).

```
db/
  sales/
    customer.tbl
    v_open_orders.vw
    grants.sql
  init/          # optional: privileged greenfield groundwork (CREATE DATABASE/ROLE/EXTENSION)
  migrations/    # optional: ordered, run-once scripts (renames, backfills)
.dbignore        # gitignore-style: files in the repo that must not be deployed
```

**`dbly.toml`** (optional, repo root) unlocks larger repos:

```toml
object_root = "pgsql/schema"   # objects live here; schema = first segment BELOW this root
environment = "postgres"       # default engine when a profile omits environment=
ignore = ["**/*_1252.sql"]     # extra ignore patterns, merged with .dbignore

[targets]                      # name profiles: dbly plan --target dev
dev  = "conn/dev.properties"
beta = "conn/beta.properties"
prod = "conn/prod.properties"
```

`object_root` lets dbly sit on a repo whose objects live deeper than the top level: with
`object_root = "pgsql/schema"`, the file `pgsql/schema/sales/customer.tbl` maps to schema
`sales` (the first segment *below* the root), not `pgsql`. Put the root-level keys before the
`[targets]` table (a TOML rule).

### Pinning the file layout

By default the schema is the first folder below `object_root` and the object kind comes from
parsing the SQL. If your repo uses a different convention, pin it with a `[layout]` table
(all keys optional; defaults shown — see [ADR 0003](adr/0003-configurable-file-layout.md)):

```toml
[layout]
schema_from    = "folder"      # folder | search-path | qualified-name
schema_depth   = 1             # folder mode: which segment under object_root is the schema
database_depth = 0             # >0 for a <database>/<schema>/… repo
type_from      = "sql"         # sql | extension
order          = "dependency"  # dependency | filename
```

| Option | Use it when… |
|---|---|
| `schema_from = "search-path"` | files set `SET search_path = <schema>, …` and then write **unqualified** DDL — dbly takes the schema from the search path. |
| `schema_from = "qualified-name"` | a flat *file-per-object* repo with no schema folders — the schema must be in the DDL (`CREATE TABLE sales.customer …`). |
| `schema_depth = 2` + `database_depth = 1` | a `<database>/<schema>/<object>` layout — the schema sits one level deeper; a multi-database repo then deploys **only** the database named in the target profile. |
| `type_from = "extension"` | the extension is authoritative (`.tbl`/`.vw`/`.fnc`/…). Bonus: a replaceable file the SQL parser can't read (PostGIS/PL-pgSQL) is still recognized — kind from the extension, name from the filename — and applied verbatim instead of dropped. |
| `order = "filename"` | replaceable objects should apply in filename order (`01_…`, `02_…`) instead of dependency order. |

A schema-qualified name in the DDL always overrides the hint. All defaults equal the built-in
behaviour, so `[layout]` is purely opt-in.

## 2. Connect to a database

A connection profile (same `connection.properties` format as DBFit/dbression):

```properties
environment=postgres        # postgres | sqlserver | oracle | sqlite
service=db.example.com:5432
username=app
password=${DB_PASSWORD}     # ${ENV} expands from the environment — keep secrets out of git
database=appdb
```

Pass it as `--target path/to/profile.properties`, or — with `[targets]` in `dbly.toml` — by
name: `--target dev`. A tool needs broad `CREATE` rights (it creates schemas and, for view
drift, throwaway probe views).

## 3. The core loop: plan → apply

```sh
dbly plan  --target dev --to HEAD      # preview: what would change
dbly apply --target dev --to HEAD      # execute it
```

`plan` prints a Terraform-style summary:

```
Plan: 3 to change, 0 to destroy.

  + create  table   sales.customer
  ~ modify  column  sales.customer.email   (VARCHAR(100) → VARCHAR(200))
  + replace view    sales.v_open_orders
```

Markers: `+` create/add · `~` modify · `!` unsafe (e.g. NOT NULL without default) · `-` drop.
Destructive steps need `--allow-destructive` to apply. `--to` is any git ref (tag/branch/SHA);
`plan` diffs from the ref recorded in the ledger (or treats the target as empty on first use).

Export a plain SQL script instead of applying (hand-run through a customer VPN, no dbly needed):

```sh
dbly plan --target dev --to v1.4 --sql deploy.sql      # or --out plan.yaml
```

`dbly status --target dev` shows the deployed ref (decorated with tag/branch names).

## 4. Adopt an existing database (`baseline`)

If a database was deployed **out-of-band** (by hand via psql/DataGrip, or predates dbly),
`plan` would otherwise treat it as empty. Tell the ledger where it stands — **without running
any SQL**:

```sh
dbly baseline --target prod --to HEAD
```

This records the ref as deployed and marks migrations up to it as applied (not run). Nothing in
the schema is touched. Afterwards `plan` diffs incrementally from there.

## 5. Deploy part of a repo

Scope any of `plan`/`apply`/`check` to part of the tree:

```sh
dbly plan --target dev --schema sales           # only the sales schema (repeatable)
dbly plan --target dev --path sales/domains     # only a subpath under object_root (repeatable)
```

A scoped deploy may reference objects in **other** schemas (a cross-schema FK, a view over
another schema's table). `--with-deps` pulls in *exactly those* missing objects — their
dependency closure — **without** dragging in the rest of that schema:

```sh
dbly apply --target beta --schema download --with-deps
```

It stops at anything already present in the target, so it never proposes to modify existing
objects in other schemas. Tables are created in FK order; a dependency that isn't in the repo
*or* the target is flagged up front rather than failing mid-apply.

## 6. Check for drift

`check` compares the full desired state at a ref against the live database (read-only):

```sh
dbly check --target prod --to HEAD
dbly check --target prod --schema sales
dbly check --target prod --show-diff     # unified diff (live → repo) for each changed view
dbly check --target prod --orphans       # also list objects in the DB but not the repo
dbly check --target prod --advisory      # also compare procedural bodies (unreliable; off by default)
```

Output uses the same row language as `plan`. `check` exits non-zero on drift — handy as a CI
gate. Notes:

- **Views** are compared by canonical structure via an engine round-trip (Postgres), so a
  correctly-deployed view reports clean despite the engine reformatting it internally
  ([ADR 0001](adr/0001-view-drift-via-engine-canonicalization.md)).
- **Grants** run on every apply but can't be introspected, so they're shown as a footnote, not
  drift.
- **`plan` vs `check`:** `plan` = "what will the next deploy change?" (git changeset from the
  deployed ref). `check` = "does reality match the repo?" (full desired-vs-live audit). They
  answer different questions and can disagree legitimately.

## 7. Fast iteration against the working tree

Preview uncommitted edits (including new, untracked object files) without committing:

```sh
dbly plan  --target dev --worktree     # alias: --dirty
dbly check --target dev --worktree
```

Preview only — `apply` still needs a committed ref for the ledger.

## 8. Run-once migrations

For changes the additive diff can't do safely (renaming a column, moving data), drop an ordered
script in `migrations/`:

```
migrations/0001_rename_email_to_contact.sql
```

Each runs **exactly once**, tracked by filename in the ledger. On an **upgrade** it runs before
the object reconciliation; on a **fresh** database it's *baselined* (recorded, not run) since
the object files already describe the end state. The table a migration touches defers its
additive diff for that deploy.

## 9. Export a live database (reverse)

The inverse of deploy — introspect a live database and emit its DDL, optionally in another
engine's dialect:

```sh
dbly export --target prod
dbly export --target prod --dialect postgres --out schema.sql
dbly export --target prod --schema sales
```

Tables and views transpile across dialects (tables are rebuilt from columns — constraints and
indexes aren't reconstructed); procedural objects are emitted verbatim in the source dialect.

## 10. Greenfield groundwork & hooks

`init` runs privileged, imperative groundwork once (CREATE DATABASE/ROLE/EXTENSION) from ordered
scripts under `init/`, under a superuser profile:

```sh
dbly init --init-target super.properties
```

`hooks/pre/` and `hooks/post/` hold `.sql` or `.py` scripts run before/after apply (e.g. ArcGIS
`propy` steps): `dbly apply … --py-interpreter propy`.

## Command reference

| Command | What it does |
|---|---|
| `dbly plan` | Preview the changes between the deployed ref and `--to`. |
| `dbly apply` | Execute the plan (or a saved `plan.yaml`). |
| `dbly baseline` | Record a ref as deployed **without running SQL** (adopt an existing DB). |
| `dbly bootstrap` | Show the full plan for an empty database (no baseline). |
| `dbly status` | Show the deployed ref (with tag/branch names). |
| `dbly check` | Detect drift: desired-vs-live audit (read-only). |
| `dbly export` | Introspect a live DB → DDL, optionally cross-dialect. |
| `dbly init` | Run privileged greenfield groundwork from `init/`. |

Common flags: `--target` (profile path or `dbly.toml` name) · `--to` (git ref) · `--from`
(baseline ref) · `--repo` · `--schema` / `--path` (scope) · `--with-deps` · `--worktree`/`--dirty`
· `--allow-destructive` · `--sql` / `--out` · `--show-diff` · `--orphans` · `--advisory` ·
`--debug` (show parser/introspection diagnostics).

## Honest limitations

- **View drift round-trip is Postgres-only for now.** Oracle and SQL Server fall back to a
  structural comparison of the raw definition (handles reformatting, but not engine-specific
  rewrites like cast elision) — deferred until a live target exists to verify against
  ([ADR 0001](adr/0001-view-drift-via-engine-canonicalization.md)).
- **Procedural body comparison is advisory.** Function/procedure/trigger bodies can't be
  canonicalized reliably across the repo↔DB boundary; `check` compares them only with
  `--advisory`, and function-to-function ordering buried in PL/pgSQL bodies relies on file order.
- **Table export** rebuilds from columns only — primary/foreign keys, checks and indexes are
  not reconstructed.
