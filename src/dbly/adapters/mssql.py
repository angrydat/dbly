"""SQL Server adapter (T-SQL via pymssql).

DDL in SQL Server is largely transactional — CREATE/ALTER/DROP of tables, views, procedures
roll back inside a transaction — so object deploys wrap in one transaction like Postgres.
T-SQL specifics handled here:

* no ``CREATE TABLE IF NOT EXISTS`` → guarded ``IF NOT EXISTS (…) BEGIN … END``;
* ``ALTER TABLE … ADD`` (not ``ADD COLUMN``);
* init scripts are split on ``GO`` batch separators (required so e.g. ``CREATE PROCEDURE``
  is the first statement in its batch).

Requires the ``mssql`` extra (``pymssql``). End-to-end verification needs a reachable SQL
Server instance; unit-testable pieces (SQL string builders) work without one.
"""
from __future__ import annotations

import re

from sqlalchemy import inspect, text

from dbly.adapters.base import Adapter, Column
from dbly.model import ObjectId, ObjectKind

_GO_RE = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)

_STATE_DDL = """
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'dbly_state')
BEGIN
    CREATE TABLE dbly_state (
        id           BIGINT IDENTITY(1,1) PRIMARY KEY,
        deployed_sha NVARCHAR(64) NOT NULL,
        migration_id NVARCHAR(200) NULL,
        applied_at   DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    )
END
"""


class MssqlAdapter(Adapter):
    transactional_ddl = True

    def table_exists(self, schema: str | None, name: str) -> bool:
        return inspect(self.engine).has_table(name, schema=schema)

    def get_columns(self, schema: str | None, name: str) -> list[Column]:
        cols = inspect(self.engine).get_columns(name, schema=schema)
        return [
            Column(
                name=c["name"],
                type=str(c["type"]),
                nullable=bool(c["nullable"]),
                default=None if c.get("default") is None else str(c["default"]),
            )
            for c in cols
        ]

    def has_object(self, kind: ObjectKind, schema: str | None, name: str) -> bool:
        with self.engine.connect() as conn:
            if kind is ObjectKind.INDEX:
                # index names are unique per table, not globally — best-effort by name
                return conn.execute(
                    text("SELECT TOP 1 1 FROM sys.indexes WHERE name = :n"), {"n": name}
                ).first() is not None
            if kind is ObjectKind.SEQUENCE:
                return conn.execute(
                    text(
                        "SELECT 1 FROM sys.sequences s "
                        "JOIN sys.schemas c ON c.schema_id = s.schema_id "
                        "WHERE s.name = :n AND (:s IS NULL OR c.name = :s)"
                    ),
                    {"n": name, "s": schema},
                ).first() is not None
            qname = f"{schema}.{name}" if schema else name
            return conn.execute(text("SELECT OBJECT_ID(:q)"), {"q": qname}).scalar() is not None

    def add_column_sql(self, table: ObjectId, col: Column) -> str:
        # T-SQL: ADD, not ADD COLUMN
        parts = [f"ALTER TABLE {table} ADD {col.name} {col.type}"]
        if not col.nullable:
            parts.append("NOT NULL")
        if col.default is not None:
            parts.append(f"DEFAULT {col.default}")
        return " ".join(parts) + ";"

    def apply(self, statements: list[str]) -> None:
        with self.engine.begin() as conn:
            for stmt in statements:
                if stmt.strip():
                    conn.execute(text(stmt))

    def run_init_script(self, script: str) -> None:
        # Split on GO batch separators and run each batch in autocommit.
        batches = [b for b in _GO_RE.split(script) if b.strip()]
        with self.engine.connect() as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            for batch in batches:
                conn.exec_driver_sql(batch)

    def state_table_ddl(self) -> str:
        return _STATE_DDL.strip()

    def record_deploy_sql(self, ref: str) -> str:
        safe = ref.replace("'", "''")
        return f"INSERT INTO dbly_state (deployed_sha) VALUES ('{safe}');"

    def ensure_state_table(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(_STATE_DDL))

    def get_deployed_ref(self) -> str | None:
        self.ensure_state_table()
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT TOP 1 deployed_sha FROM dbly_state "
                    "ORDER BY applied_at DESC, id DESC"
                )
            ).first()
        return row[0] if row else None

    def record_deploy(self, ref: str, migration_ids: list[str]) -> None:
        self.ensure_state_table()
        with self.engine.begin() as conn:
            for mid in (migration_ids or [None]):
                conn.execute(
                    text(
                        "INSERT INTO dbly_state (deployed_sha, migration_id) "
                        "VALUES (:sha, :mid)"
                    ),
                    {"sha": ref, "mid": mid},
                )
