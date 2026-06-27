# dbly

**State-based, cross-engine database deployment** — git-driven, parser-assisted, SQL-first.

dbly deploys the *desired state* of your database objects (one file per object, versioned in
git like any source) to PostgreSQL, SQL Server, Oracle and SQLite. It is the deployment
sibling of [dbression](https://github.com/angrydat/dbression) (which tests): one applies, one
verifies.

> Design rationale and the full model live in [`CONCEPT.md`](../_SYS/dbly/CONCEPT.md).

## How it works — three layers

| Layer | Tool | Answers |
|---|---|---|
| change detection | `git diff` | *which files* changed since the deployed ref |
| semantics | `sqlglot` | *what* they are — kind, identity, dependencies |
| reality | live introspection | what the *target database* actually looks like |

Objects fall into two classes:

* **Replaceable** (views, functions, procedures, packages, triggers, types, grants) →
  re-applied wholesale (`CREATE OR REPLACE`), dependency-ordered. Idempotent.
* **Stateful** (tables) → desired `CREATE TABLE` diffed against the live schema; **additive**
  deltas generated automatically, **destructive** deltas flagged and never auto-applied.

## Install

```sh
uv sync                      # dev / from source
# Postgres driver is included; add engines as needed:
uv sync --extra oracle       # oracledb
uv sync --extra mssql        # pymssql
```

## Usage

```sh
dbly plan  --to <release-tag> --target prod.properties        # review the plan
dbly apply --to <release-tag> --target prod.properties        # execute it
dbly status --target prod.properties                          # show deployed ref
dbly check  --target prod.properties                          # detect drift
```

Connection profiles reuse dbression's DBFit-compatible `connection.properties`. `${ENV}`
placeholders keep credentials out of the repo (CI/CD-safe). A target can also come entirely
from `DBLY_TARGET` / `DBLY_CONNECTION_STRING` + `DBLY_ENVIRONMENT`.

## Status

Early alpha. **Postgres adapter first** (the *Leitstern*); SQL Server and Oracle follow.
