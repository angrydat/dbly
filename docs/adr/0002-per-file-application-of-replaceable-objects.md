# 2. Apply replaceable objects per source file, verbatim

- Status: accepted
- Date: 2026-07-26
- Deciders: dbly maintainers

## Context

Replaceable objects (views, functions, procedures, triggers, types, grants) are re-applied
wholesale on every deploy. Until now each *object* became one plan step whose SQL was the
single `CREATE …` statement **re-rendered by sqlglot** (`stmt.sql(dialect)`), and steps were
applied one statement at a time inside the apply transaction.

On a real repo (swwat) this lost or broke things that live in the object's file:

- **Non-`CREATE` statements were dropped** — `SET search_path`, `ALTER … OWNER TO …`, comments.
  Ownership and search-path context simply never got applied.
- **Overloaded functions collided** — two `CREATE FUNCTION f(...)` with different signatures in
  one file share an object key; the object-level model deduplicated/misordered them (worked
  around in ADR-adjacent commits, but fragile).
- **Re-rendering risk** — sqlglot re-rendering a complex PL/pgSQL body is best-effort and can
  subtly differ from the source the author wrote and tested.

The author's mental model (and the repo's reality) is: **a file is the unit** — an idempotent
script that declares an object *with its adjustments* (owner, grants, search_path, several
related statements) in a deliberate order.

## Decision

**Deploy replaceable objects by applying their source file verbatim, once per file, in
dependency order** — not as re-rendered per-statement steps.

- The planner groups replaceable objects by `source_file`, orders them by the inter-object
  dependency graph (which also groups function overloads), and emits **one step per file**
  carrying the file's **raw content** with `Step.script = True`.
- `apply` runs statement steps (schema/table/index — diffed, single-statement) transactionally
  as before, then runs each script step through the engine's existing multi-statement runner
  (`run_init_script`: Postgres multi-statement, Oracle `/`-split, SQL Server `GO`-split, SQLite
  `executescript`) — the same path migrations already use.

Replaceable objects are idempotent (`CREATE OR REPLACE` / drop-and-create), so applying them
per file — and, on Postgres, in autocommit *after* the transactional table steps rather than
inside that transaction — is safe: a mid-way failure leaves earlier idempotent files applied
and the ledger unrecorded, so a re-run simply re-applies them. The ledger is written only after
everything succeeds.

## Extension (2026-07-27): fresh tables too

The same reasoning applies to a **table being created**: dbly's generated `CREATE TABLE` (from
the parsed column model) is owned by the *connecting* user and omits the file's
`ALTER TABLE … OWNER TO …`, co-located indexes, and other statements. So a table that does not
yet exist is now also applied **as its whole file** (dependency-ordered), which sets ownership
and creates everything the file declares. An **existing** table keeps the additive column diff
(its ownership was set when it was first created). Objects co-located in a fresh table's file
(its indexes/sequences) are not emitted as separate steps — the file creates them.

## Consequences

- **Positive:** `SET search_path`, `ALTER … OWNER`, comments and intra-file statement order are
  preserved; overloaded functions and multi-object files "just work"; no re-rendering of
  procedural bodies; the deployed object matches the source the author wrote.
- **Negative / trade-offs:**
  - On transactional engines the replaceable phase is no longer inside the table transaction
    (idempotency makes this acceptable; tables remain atomic among themselves).
  - The plan shows **one row per file** for replaceable objects (a multi-object file notes
    "+N more in <file>") rather than one row per object.
  - `check`/drift still compares per *object* (`obj.sql` → engine round-trip). Since sqlglot's
    re-render is faithful for the parts drift compares and both sides pass through the engine,
    the deployed (raw) and desired (rendered) forms converge — verified clean on Postgres for
    the `download` schema. If a divergence ever surfaces, align the drift desired-side to the
    raw file too.
