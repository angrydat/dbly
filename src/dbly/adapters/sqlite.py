"""SQLite adapter — transactional DDL, no native deps. Doubles as the test backend.

SQLite has no schemas; schema-qualified identities are treated as schemaless (the folder
hint should be omitted for SQLite repos). ALTER TABLE supports ADD COLUMN, which is all the
additive path needs.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

from dbly.adapters.base import Adapter, Column

_STATE_DDL = """
CREATE TABLE IF NOT EXISTS dbly_state (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    deployed_sha TEXT NOT NULL,
    migration_id TEXT,
    applied_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


class SqliteAdapter(Adapter):
    transactional_ddl = True

    def table_exists(self, schema: str | None, name: str) -> bool:
        return inspect(self.engine).has_table(name)

    def get_columns(self, schema: str | None, name: str) -> list[Column]:
        cols = inspect(self.engine).get_columns(name)
        return [
            Column(
                name=c["name"],
                type=str(c["type"]),
                nullable=bool(c["nullable"]),
                default=None if c.get("default") is None else str(c["default"]),
            )
            for c in cols
        ]

    def apply(self, statements: list[str]) -> None:
        with self.engine.begin() as conn:
            for stmt in statements:
                if stmt.strip():
                    conn.execute(text(stmt))

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
                text("SELECT deployed_sha FROM dbly_state ORDER BY id DESC LIMIT 1")
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
