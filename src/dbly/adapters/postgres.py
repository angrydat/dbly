"""PostgreSQL adapter — the Leitstern (CONCEPT.md §16).

Postgres has transactional DDL, so the whole apply runs in a single transaction: on any
failure everything rolls back and the database is untouched. This is the clean reference
against which the trickier Oracle/SQL-Server semantics are later measured.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

from dbly.adapters.base import Adapter, Column

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

    def apply(self, statements: list[str]) -> None:
        # transactional DDL → one atomic transaction
        with self.engine.begin() as conn:
            for stmt in statements:
                if stmt.strip():
                    conn.execute(text(stmt))

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
