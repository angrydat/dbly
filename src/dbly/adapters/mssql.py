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
from dbly.model import LiveObject, ObjectId, ObjectKind
from dbly.parsing import canonical_hash

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
    default_schema = "dbo"

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

    _OBJTYPE = {
        "U": ObjectKind.TABLE, "V": ObjectKind.VIEW, "P": ObjectKind.PROCEDURE,
        "FN": ObjectKind.FUNCTION, "IF": ObjectKind.FUNCTION, "TF": ObjectKind.FUNCTION,
        "TR": ObjectKind.TRIGGER,
    }

    def inventory(self) -> list[LiveObject]:
        objs = text(
            "SELECT s.name, o.name, RTRIM(o.type), OBJECT_DEFINITION(o.object_id) "
            "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "WHERE o.is_ms_shipped = 0 AND RTRIM(o.type) IN ('U','V','P','FN','IF','TF','TR')"
        )
        idx = text(
            "SELECT s.name, i.name FROM sys.indexes i "
            "JOIN sys.objects o ON o.object_id = i.object_id "
            "JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "WHERE o.is_ms_shipped = 0 AND i.name IS NOT NULL "
            "  AND i.is_primary_key = 0 AND i.type > 0"
        )
        seqs = text(
            "SELECT s.name, q.name FROM sys.sequences q "
            "JOIN sys.schemas s ON s.schema_id = q.schema_id"
        )
        hashed = {ObjectKind.VIEW, ObjectKind.PROCEDURE, ObjectKind.FUNCTION, ObjectKind.TRIGGER}
        found: dict[str, LiveObject] = {}
        with self.engine.connect() as conn:
            for schema, name, otype, src in conn.execute(objs):
                kind = self._OBJTYPE.get(otype)
                if kind is None:
                    continue
                h = canonical_hash(src, dialect="tsql") if kind in hashed else None
                obj = LiveObject(kind, ObjectId(schema, name), h)
                found[obj.key()] = obj
            for schema, name in conn.execute(idx):
                obj = LiveObject(ObjectKind.INDEX, ObjectId(schema, name))
                found[obj.key()] = obj
            for schema, name in conn.execute(seqs):
                obj = LiveObject(ObjectKind.SEQUENCE, ObjectId(schema, name))
                found[obj.key()] = obj
        return list(found.values())

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
