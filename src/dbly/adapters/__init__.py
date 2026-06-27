"""Engine-specific adapters. Postgres is the Leitstern (CONCEPT.md §10, §16)."""
from __future__ import annotations

from dbly.adapters.base import Adapter, Column
from dbly.config import ConnectionConfig
from dbly.engine import detect_dialect

_POSTGRES = {"postgres", "postgresql", "pg"}
_SQLITE = {"sqlite", "sqlite3"}


def get_adapter(cfg: ConnectionConfig) -> Adapter:
    env = detect_dialect(cfg)
    if env in _POSTGRES:
        from dbly.adapters.postgres import PostgresAdapter

        return PostgresAdapter(cfg)
    if env in _SQLITE:
        from dbly.adapters.sqlite import SqliteAdapter

        return SqliteAdapter(cfg)
    raise NotImplementedError(
        f"adapter for {env!r} not implemented yet — Postgres first (CONCEPT.md §16). "
        "SQL Server and Oracle follow."
    )


__all__ = ["Adapter", "Column", "get_adapter"]
