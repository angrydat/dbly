"""Build SQLAlchemy engines from connection profiles + detect the dialect.

Mirrors dbression's engine builder (same URL conventions) so profiles are interchangeable.
The ``environment`` key (postgres | oracle | sqlserver | sqlite) selects the driver; for a
full ``connection-string`` we sniff the scheme when ``environment`` is absent.
"""
from __future__ import annotations

import os
import threading

from sqlalchemy import URL, Engine, create_engine

from dbly.config import ConnectionConfig

_POSTGRES = {"postgres", "postgresql", "pg"}
_ORACLE = {"oracle"}
_MSSQL = {"sqlserver", "mssql", "ms-sql"}
_SQLITE = {"sqlite", "sqlite3"}

_THICK_LOCK = threading.Lock()
_THICK_DONE = False


def detect_dialect(cfg: ConnectionConfig) -> str:
    if cfg.environment:
        return cfg.environment.strip().lower()
    cs = (cfg.connection_string or "").lower()
    if cs.startswith(("postgres", "jdbc:postgresql")):
        return "postgres"
    if cs.startswith(("oracle", "jdbc:oracle")):
        return "oracle"
    if cs.startswith(("mssql", "sqlserver", "jdbc:sqlserver")):
        return "sqlserver"
    if cs.startswith("sqlite"):
        return "sqlite"
    raise ValueError(
        "cannot determine database environment — set `environment=` in the profile"
    )


def make_engine(cfg: ConnectionConfig) -> Engine:
    """Build a SQLAlchemy engine. No autocommit — adapters manage transactions."""
    env = detect_dialect(cfg)
    if env in _POSTGRES:
        url = _postgres_url(cfg)
    elif env in _ORACLE:
        _maybe_init_oracle_thick()
        url = _oracle_url(cfg)
    elif env in _MSSQL:
        url = _mssql_url(cfg)
    elif env in _SQLITE:
        url = _sqlite_url(cfg)
    else:
        raise ValueError(f"unknown environment: {env!r}")
    return create_engine(url, pool_pre_ping=True)


def _maybe_init_oracle_thick() -> None:
    global _THICK_DONE
    if _THICK_DONE:
        return
    lib_dir = os.environ.get("DBLY_ORACLE_CLIENT_LIB_DIR")
    if not lib_dir:
        return
    with _THICK_LOCK:
        if _THICK_DONE:
            return
        import oracledb  # noqa: PLC0415

        oracledb.init_oracle_client(lib_dir=lib_dir)
        _THICK_DONE = True


def _postgres_url(cfg: ConnectionConfig) -> URL | str:
    if cfg.connection_string:
        cs = cfg.connection_string
        if cs.startswith("jdbc:postgresql://"):
            return f"postgresql+psycopg://{cs[len('jdbc:postgresql://'):]}"
        if cs.startswith(("postgresql://", "postgres://")):
            _, _, rest = cs.partition("://")
            return f"postgresql+psycopg://{rest}"
        return cs
    host, port = _split_host_port(cfg.service)
    return URL.create(
        "postgresql+psycopg",
        username=cfg.username,
        password=cfg.password,
        host=host,
        port=port,
        database=cfg.extra.get("database"),
    )


def _oracle_url(cfg: ConnectionConfig) -> URL | str:
    if cfg.connection_string:
        cs = cfg.connection_string.replace("jdbc:oracle:thin:@", "")
        return f"oracle+oracledb://{cfg.username or ''}:{cfg.password or ''}@{cs}"
    return URL.create(
        "oracle+oracledb",
        username=cfg.username,
        password=cfg.password,
        query={"dsn": cfg.service or ""},
    )


def _mssql_url(cfg: ConnectionConfig) -> URL | str:
    if cfg.connection_string:
        return cfg.connection_string
    host, port = _split_host_port(cfg.service)
    return URL.create(
        "mssql+pymssql",
        username=cfg.username,
        password=cfg.password,
        host=host,
        port=port,
        database=cfg.extra.get("database"),
    )


def _sqlite_url(cfg: ConnectionConfig) -> URL | str:
    if cfg.connection_string:
        return cfg.connection_string
    path = cfg.service or cfg.extra.get("database") or ":memory:"
    return f"sqlite:///{path}"


def _split_host_port(service: str | None) -> tuple[str | None, int | None]:
    if not service:
        return None, None
    if ":" in service:
        host, _, port_str = service.partition(":")
        port_str = port_str.split("/", 1)[0]
        try:
            return host, int(port_str)
        except ValueError:
            return service, None
    return service, None
