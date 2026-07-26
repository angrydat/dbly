# 3. Configurable repository file layout

- Status: accepted
- Date: 2026-07-26
- Deciders: dbly maintainers

## Context

dbly derives two things from where a file sits and what it contains:

- **schema** — hard-wired to the *first path segment below `object_root`* (overridden by a
  schema-qualified name in the DDL);
- **object kind** — always from *parsing the SQL* (the file extension only marks "this is SQL").

That is exactly one of the several layouts a DDL source can have. DataGrip's DDL Data Source,
for instance, offers: *File per Object*, *…by Schema* (dbly's default), *…by Schema by
Database*, *…with order*, and *…with schema and type*, plus knobs for where the schema name
comes from (folder vs. `search_path` vs. qualified name). Real repos differ, and dbly should let
a team **pin its layout** instead of assuming one.

## Decision

Add an optional `[layout]` table to `dbly.toml`. Absent, every key keeps today's behaviour, so
existing repos are unaffected.

```toml
[layout]
schema_from    = "folder"      # folder | search-path | qualified-name
schema_depth   = 1             # folder mode: which segment under object_root is the schema
database_depth = 0             # >0: which segment is the database (for <db>/<schema>/… repos)
type_from      = "sql"         # sql | extension
order          = "dependency"  # dependency | filename
```

### `schema_from` — where the schema name comes from
- **folder** (default): the path segment at `schema_depth` (relative to `object_root`).
- **search-path**: the first schema in the file's `SET search_path = <schema>, …` — matches
  repos that set the search path and then write unqualified DDL.
- **qualified-name**: no folder/path hint at all; the schema must come from a schema-qualified
  name in the DDL (`CREATE TABLE sales.customer …`). Flat "File per Object" layouts.

A schema-qualified name in the DDL always wins, as today — these settings decide the *hint* for
unqualified objects.

### `schema_depth` / `database_depth` — position in the path
1-based segment indices under `object_root`. `schema_depth = 1` is today's behaviour.
`database_depth = 1, schema_depth = 2` reads `<db>/<schema>/<object>` (DataGrip's *by Schema by
Database*). When `database_depth > 0` and the connection profile names a `database`, files whose
database segment doesn't match that database are excluded — a multi-database repo deploys only
the target's database.

### `type_from` — where the object kind comes from
- **sql** (default): parse the DDL (robust, extension-agnostic).
- **extension**: the kind is taken from the file extension (`.tbl`→table, `.vw`→view,
  `.fnc`→function, `.prc`→procedure, `.trg`→trigger, `.typ`→type, `.pkg`→package,
  `.seq`→sequence). This also gives a **filename-based identity fallback**: a replaceable file
  the SQL parser can't read (e.g. a PostGIS/PL-pgSQL body sqlglot rejects) is still recognized —
  kind from extension, name from the filename, schema from the layout — and applied verbatim
  per-file (ADR 0002) instead of being silently dropped.

### `order` — how replaceable objects are ordered
- **dependency** (default): topological order over the object graph.
- **filename**: apply replaceable files in lexicographic filename order — for repos that encode
  order in filenames (`01_…`, `02_…`), DataGrip's *File per Object with order*.

## Consequences

- **Positive:** dbly adapts to the common DDL layouts instead of forcing one; the `extension`
  fallback rescues files the parser can't read; multi-database repos become expressible.
- **Costs / limits:** `search-path` and `extension` modes read the file to find the hint/kind;
  the `database` filter is best-effort (only when the profile names a database). Type/kind from
  extension is only as reliable as the extension convention. Constraints-in-separate-files
  (vs. inline) is a *parsing* concern, out of scope here.
- **Compatibility:** all defaults equal today's behaviour; `[layout]` is purely additive.
