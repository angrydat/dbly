"""PostgreSQL adapter — the Leitstern (CONCEPT.md §16).

Postgres has transactional DDL, so the whole apply runs in a single transaction: on any
failure everything rolls back and the database is untouched. This is the clean reference
against which the trickier Oracle/SQL-Server semantics are later measured.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

from dbly.adapters.base import Adapter, Column
from dbly.model import ObjectId, ObjectKind

_STATE_DDL = """
CREATE TABLE IF NOT EXISTS dbly_state (
    id           bigserial PRIMARY KEY,
    deployed_sha text        NOT NULL,
    migration_id text,
    applied_at   timestamptz NOT NULL DEFAULT now()
)
"""


class PostgresAdapter(Adapter):
    transactional_ddl = True

    def table_exists(self, schema: str | None, name: str) -> bool:
        insp = inspect(self.engine)
        return insp.has_table(name, schema=schema)

    def get_columns(self, schema: str | None, name: str) -> list[Column]:
        insp = inspect(self.engine)
        cols = insp.get_columns(name, schema=schema)
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
        qname = f"{schema}.{name}" if schema else name
        with self.engine.connect() as conn:
            if kind in (ObjectKind.INDEX, ObjectKind.SEQUENCE, ObjectKind.TABLE, ObjectKind.VIEW):
                return conn.execute(
                    text("SELECT to_regclass(:q)"), {"q": qname}
                ).scalar() is not None
            return conn.execute(
                text(
                    "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE p.proname = :n AND (:s IS NULL OR n.nspname = :s) LIMIT 1"
                ),
                {"n": name, "s": schema},
            ).first() is not None

    def add_column_sql(self, table: ObjectId, col: Column) -> str:
        parts = [f"ALTER TABLE {table} ADD COLUMN {col.name} {col.type}"]
        if not col.nullable:
            parts.append("NOT NULL")
        if col.default is not None:
            parts.append(f"DEFAULT {col.default}")
        return " ".join(parts) + ";"

    def apply(self, statements: list[str]) -> None:
        # transactional DDL → one atomic transaction
        with self.engine.begin() as conn:
            for stmt in statements:
                if stmt.strip():
                    conn.execute(text(stmt))

    def run_init_script(self, script: str) -> None:
        # autocommit: CREATE DATABASE & friends cannot run inside a transaction block.
        # psycopg3 executes a multi-statement string in a single exec_driver_sql call.
        with self.engine.connect() as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.exec_driver_sql(script)

    def state_table_ddl(self) -> str:
        return _STATE_DDL.strip() + ";"

    def record_deploy_sql(self, ref: str) -> str:
        return f"INSERT INTO dbly_state (deployed_sha) VALUES ('{ref.replace(chr(39), chr(39) * 2)}');"

    def ensure_state_table(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(_STATE_DDL))

    def get_deployed_ref(self) -> str | None:
        self.ensure_state_table()
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT deployed_sha FROM dbly_state "
                    "ORDER BY applied_at DESC, id DESC LIMIT 1"
                )
            ).first()
        return row[0] if row else None

    def record_deploy(self, ref: str, migration_ids: list[str]) -> None:
        self.ensure_state_table()
        with self.engine.begin() as conn:
            if migration_ids:
                for mid in migration_ids:
                    conn.execute(
                        text(
                            "INSERT INTO dbly_state (deployed_sha, migration_id) "
                            "VALUES (:sha, :mid)"
                        ),
                        {"sha": ref, "mid": mid},
                    )
            else:
                conn.execute(
                    text("INSERT INTO dbly_state (deployed_sha) VALUES (:sha)"),
                    {"sha": ref},
                )
