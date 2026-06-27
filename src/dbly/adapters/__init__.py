"""Engine-specific adapters. Postgres is the Leitstern (CONCEPT.md §10, §16)."""
from __future__ import annotations

from dbly.adapters.base import Adapter, Column
from dbly.config import ConnectionConfig
from dbly.engine import detect_dialect

_POSTGRES = {"postgres", "postgresql", "pg"}
_SQLITE = {"sqlite", "sqlite3"}
_MSSQL = {"sqlserver", "mssql", "ms-sql"}
_ORACLE = {"oracle"}


def get_adapter(cfg: ConnectionConfig) -> Adapter:
    env = detect_dialect(cfg)
    if env in _POSTGRES:
        from dbly.adapters.postgres import PostgresAdapter

        return PostgresAdapter(cfg)
    if env in _SQLITE:
        from dbly.adapters.sqlite import SqliteAdapter

        return SqliteAdapter(cfg)
    if env in _MSSQL:
        from dbly.adapters.mssql import MssqlAdapter

        return MssqlAdapter(cfg)
    if env in _ORACLE:
        from dbly.adapters.oracle import OracleAdapter

        return OracleAdapter(cfg)
    raise NotImplementedError(f"no adapter for environment {env!r}.")


__all__ = ["Adapter", "Column", "get_adapter"]
