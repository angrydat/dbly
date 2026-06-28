<p align="center">
  <img src="docs/dbly_head.png" alt="dbly — state-based database deployment" width="100%">
</p>

<p align="center"><b>Declare your desired database state in git. dbly makes it real.</b></p>

---

**dbly** deploys your database objects — tables, views, functions, procedures, packages,
triggers, grants — from version control to **PostgreSQL, SQL Server, Oracle and SQLite**.
You keep one SQL file per object, like normal source code; dbly works out what changed and
brings the target database in sync. Think *Terraform for your database*: **declarative,
predictable, repeatable.**

## Why

- **Declarative** — your repo is the source of truth. No hand-written migration chains, no
  version-number collisions on parallel branches.
- **Plan before apply** — preview exactly what will run, then execute. No surprises.
- **Idempotent & safe** — only the necessary changes are applied. Additive changes go
  automatically; destructive ones (dropping columns, etc.) are flagged and never run unless
  you explicitly allow them.
- **Multi-database** — one workflow across all four engines.

## Install

```sh
uv sync                 # PostgreSQL + SQLite work out of the box
uv sync --extra oracle  # add the Oracle driver
uv sync --extra mssql   # add the SQL Server driver
uv sync --extra all     # both
```

## Organize your repo

One file per object; folder names map to database schemas. Use any extension you like
(`.sql`, `.tbl`, `.vw`, `.prc`, …) — dbly reads the SQL to know what each object is.

```
db/
  sales/
    customer.tbl
    v_open_orders.vw
    grants.sql
  init/                 # optional: privileged greenfield groundwork (CREATE DATABASE/ROLE…)
  migrations/           # optional: ordered, run-once ALTERs for renames / data backfills
hooks/pre/  hooks/post/  # optional: .sql or .py hooks (e.g. ArcGIS/ArcPy steps)
.dbignore               # files in the repo that should not be deployed
```

Most changes are declarative — edit the object file, dbly figures out the additive diff. For
changes the diff can't do safely (renaming a column, moving data), drop an ordered script in
`migrations/` (`0001_…sql`); it runs exactly once, is recorded in the ledger, and the table
it touches defers to it for that deploy. On a fresh database, migrations are *baselined*
(recorded, not run) since the object files already describe the end state.

## Connect

A connection profile (reuses the familiar `connection.properties` format):

```properties
environment=postgres            # postgres | sqlserver | oracle | sqlite
service=db.example.com:5432
username=app
password=${DB_PASSWORD}         # ${ENV} placeholders → keep secrets out of the repo (CI/CD-safe)
database=appdb
```

## Use

```sh
# preview the changes between the deployed state and a git ref
dbly plan  --to main --target prod.connection.properties

# apply them
dbly apply --to main --target prod.connection.properties

# export a plain SQL script to run by hand (e.g. through a customer VPN, no dbly needed)
dbly plan  --to main --target prod.connection.properties --sql deploy.sql

# what is currently deployed?
dbly status --target prod.connection.properties

# has the database drifted from the desired state?
dbly check  --target prod.connection.properties

# greenfield only: run privileged groundwork once under a superuser profile
dbly init   --init-target super.connection.properties
```

**Typical workflow:** edit your object files → commit → `dbly plan` to review → `dbly apply`.

Deploying a *subset* of features is just choosing the git ref you deploy (a release tag or
branch). Destructive steps require `--allow-destructive`.

## Built for trunk-based development

Database teams are usually locked out of trunk-based development: migration scripts collide
on parallel branches, and "merge" effectively means "deploy to the customer". dbly is
designed to break that deadlock for teams who write their logic **in SQL, in the database**:

- **No migration numbers, no collisions.** Two developers edit different objects — git
  merges them like any other code. Integrate early, every day.
- **Merge ≠ deploy.** The trunk is your desired state; `dbly apply --to <tag>` ships the ref
  you choose, in the maintenance window you choose. Release *what* you want, *when* you want.
- **One trunk, every customer.** dbly reads each database's real state, so customers on
  different versions are no problem.

Integrate continuously, deploy on your own schedule — trunk-based development, finally
practical for state-based database developers.

## Status

Early alpha — all four engines are implemented and tested against live databases.

## License

MIT
