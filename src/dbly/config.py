"""Connection profiles — reuses dbression's DBFit-compatible ``connection.properties``.

Format (same as dbression, so existing profiles work unchanged):

    # 1) full connection string
    connection-string=postgresql://user:pw@host:5432/db

    # 2) OR separate parts
    service=host:5432
    username=app
    password=${DB_PASSWORD}      # ${ENV} placeholders expand from os.environ
    database=appdb

dbly adds one key:

    environment=postgres         # postgres | oracle | sqlserver | sqlite

``${VAR}`` placeholders make profiles CI/CD-safe — keep credentials in pipeline secrets
(e.g. Bitbucket), never in the repo. A profile may also be supplied entirely via env:
``DBLY_TARGET`` (a path) or inline ``DBLY_CONNECTION_STRING`` + ``DBLY_ENVIRONMENT``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_vars(value: str) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        try:
            return os.environ[name]
        except KeyError as exc:
            raise KeyError(
                f"connection profile references undefined environment variable: ${{{name}}}"
            ) from exc

    return _VAR_RE.sub(repl, value)


@dataclass(slots=True)
class ConnectionConfig:
    environment: str | None = None
    connection_string: str | None = None
    service: str | None = None
    username: str | None = None
    password: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def load_profile(path: Path) -> ConnectionConfig:
    """Parse a Java-style ``.properties`` connection profile."""
    cfg = ConnectionConfig()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = _expand_vars(value.strip())
        if key == "connection-string":
            cfg.connection_string = value
        elif key in ("environment", "databaseenvironment", "engine"):
            cfg.environment = value
        elif key == "service":
            cfg.service = value
        elif key == "username":
            cfg.username = value
        elif key == "password":
            cfg.password = value
        else:
            cfg.extra[key] = value
    return cfg


def resolve_target(target: str | None) -> ConnectionConfig:
    """Resolve a ``--target`` into a ConnectionConfig.

    Order: explicit file path → ``DBLY_TARGET`` (file) → inline env vars.
    """
    if target:
        return load_profile(Path(target))
    env_path = os.environ.get("DBLY_TARGET")
    if env_path:
        return load_profile(Path(env_path))
    cs = os.environ.get("DBLY_CONNECTION_STRING")
    if cs:
        return ConnectionConfig(
            environment=os.environ.get("DBLY_ENVIRONMENT"),
            connection_string=cs,
        )
    raise ValueError(
        "no target given — pass --target <profile> or set DBLY_TARGET / "
        "DBLY_CONNECTION_STRING (+ DBLY_ENVIRONMENT)"
    )
