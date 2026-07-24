# 1. View drift detection via engine canonicalization

- Status: accepted
- Date: 2026-07-24
- Deciders: dbly maintainers

## Context

dbly is a **state-based** deployment tool: the repo declares the desired state, dbly makes
the database match. Views are (external) schema — first-class state, exactly like tables.
`check` must therefore reliably answer "does the deployed view match the repo?".

Our first implementation compared a hash of the repo's `CREATE VIEW … AS SELECT …` against a
hash of the database's stored definition (`pg_get_viewdef`). This produced **constant false
positives**: on a correctly deployed schema, ~30 of ~30 views reported as drifted.

Root cause, verified live against Postgres: **the engine does not store view source text — it
stores a normalized rewrite.** Postgres (via the same rule that backs both `pg_get_viewdef`
*and* `INFORMATION_SCHEMA.views.view_definition`):

- **elides no-op casts** — `CAST(bwmn_id AS TEXT)` on an already-`text` column simply vanishes;
  `CAST(geom AS geometry(MultiLineString,31287))` on that exact type vanishes;
- **qualifies** every column (`foo` → `t.foo`) and schema-qualifies tables per `search_path`;
- **reformats** expressions, whitespace, parentheses.

So the repo's SQL text and the engine's stored form legitimately differ for a view that is in
fact deployed correctly. No amount of text/AST normalization on the repo side can close this
gap, because it depends on catalog knowledge (column types) only the engine has. Demoting view
drift to "advisory" was rejected: it breaks the core promise that schema state — including
views — is fully captured.

`INFORMATION_SCHEMA` was considered as a "standard" source. Its `view_definition` column is the
*same* normalized text as `pg_get_viewdef`, so it does not solve body comparison. Its
`columns` view, however, is a reliable standard source for a view's **output column signature**.

## Decision

**Normalize through the engine; compare canonical forms. Never reimplement the engine's
normalization rules.**

To compare a view we reduce *both* sides to the engine's own canonical representation:

- **Actual** — the live view is already engine-stored; read its canonical form
  (`pg_get_viewdef` / `ALL_VIEWS.TEXT` / `sys.sql_modules` / `sqlite_master.sql`).
- **Desired** — dbly creates a **throwaway probe view** from the repo SQL, replaying the
  file's own `SET search_path`, reads back the engine's canonical form, and discards the probe.
- Compare the two canonical forms. Cast elision, qualification and formatting normalize
  **identically** because the same engine produced both.

Per engine:

| Engine     | Probe view          | Read back            | Cleanup                    |
|------------|---------------------|----------------------|----------------------------|
| PostgreSQL | `CREATE TEMP VIEW`  | `pg_get_viewdef`     | rollback (session-local)   |
| SQLite     | `CREATE TEMP VIEW`  | `sqlite_master.sql`  | rollback                   |
| Oracle     | `CREATE VIEW dbly_probe_<n>` | `ALL_VIEWS.TEXT` | explicit `DROP` (DDL auto-commits) |
| SQL Server | scratch view        | `OBJECT_DEFINITION`  | `DROP`                     |

**Fallback** when a probe view cannot be created (insufficient privilege): compare the view's
**column signature** — ordered `(name, type)` from `INFORMATION_SCHEMA.columns` /
`ALL_TAB_COLUMNS`, the same notion of state dbly already uses for tables. Lower fidelity (body
logic not compared) but schema-standard and write-free. If neither is possible, report the view
as `unverified` (advisory) — never silently "clean".

This requires broad `CREATE` privilege for the deploy connection. That is acceptable and
expected: **a database deployment tool needs far-reaching CREATE rights by definition.**

## Comparison, in two layers

1. **Engine round-trip** (above) — makes the desired side go through the engine, so
   engine-specific rewrites (Postgres eliding a no-op `CAST(x AS text)`, name qualification)
   are applied to *both* sides.
2. **Structural comparison** — the two canonical texts are then compared **as parse trees, not
   as text**: parse with sqlglot, strip per-column table qualifiers and **all** parentheses
   (operator precedence lives in the tree shape, so `(a OR b) AND c` stays distinct from
   `a OR b AND c`), and compare the tree structure. This absorbs the redundant parentheses that
   `pg_get_viewdef` copies verbatim from the original source text — the dominant residual after
   layer 1.

On a real schema this took reported view drift from 41 → 1.

## Known limitations

- **Parenthesized `FROM` joins.** `FROM (a JOIN b ON …)` parses to a different AST shape than
  `FROM a JOIN b ON …` (an alias-less sub-node), which our normalization does not yet unwrap —
  such a view can still show a *cosmetic* diff. `--show-diff` makes it obvious it is cosmetic.
  Not worth fragile, version-specific AST surgery for now.
- **Body semantics, not equivalence.** We compare canonical structure, not logical equivalence;
  two views computing the same result by different SQL are (correctly) reported as different.

## Consequences

- **Positive:** views are fully captured state again; comparison is correct regardless of each
  engine's normalization quirks (we delegate, not reimplement); the approach generalizes to any
  engine that can create and introspect a view; `--show-diff` becomes trustworthy.
- **Negative / costs:** `check` now creates a throwaway view per changed view (one extra
  round-trip each); needs `CREATE` privilege (graceful fallback to column signature otherwise);
  Oracle/SQL Server require an explicit create+drop rather than a rolled-back temp view.
- **Rollout:** PostgreSQL first (validated live). Oracle, SQL Server and SQLite follow using the
  same contract — the adapter exposes a `canonical_view(desired_sql, search_path) -> str` probe
  and a `live_view_canonical(id) -> str`; the drift layer stays engine-agnostic. **Not done
  until all four engines implement it.**
