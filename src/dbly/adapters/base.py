"""Adapter interface — the per-engine execution + introspection contract.

Each adapter encodes its engine's DDL transaction semantics (CONCEPT.md §10):

* Postgres — transactional DDL: wrap the whole apply in one transaction, clean rollback.
* Oracle   — DDL auto-commits: per-statement, verify-driven, no rollback assumption.
* SQL Server — mixed: wrap where supported.

The adapter is also the *reality* layer (CONCEPT.md §2): it introspects the live schema so
the planner can diff desired vs. actual tables.
"""
from __future__ import annotations

import abc

from sqlalchemy import Engine

from dbly.config import ConnectionConfig
from dbly.engine import make_engine
from dbly.model import Column, ObjectId, ObjectKind

__all__ = ["Adapter", "Column"]


class Adapter(abc.ABC):
    """Base adapter. Subclasses implement engine specifics."""

    #: whether DDL participates in transactions (drives apply strategy)
    transactional_ddl: bool = False

    def __init__(self, cfg: ConnectionConfig):
        self.cfg = cfg
        self._engine: Engine | None = None

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = make_engine(self.cfg)
        return self._engine

    # --- introspection (reality layer) ------------------------------------------------
    @abc.abstractmethod
    def table_exists(self, schema: str | None, name: str) -> bool: ...

    @abc.abstractmethod
    def get_columns(self, schema: str | None, name: str) -> list[Column]: ...

    @abc.abstractmethod
    def has_object(self, kind: ObjectKind, schema: str | None, name: str) -> bool:
        """Existence check for a non-table object (used for index/sequence create-if-missing).

        Indexes and sequences have no ``CREATE OR REPLACE`` form on most engines, so they
        are created only when absent.
        """

    # --- dialect-specific DDL generation -----------------------------------------------
    @abc.abstractmethod
    def add_column_sql(self, table: ObjectId, col: Column) -> str:
        """Generate the additive ``ALTER TABLE … ADD`` for this dialect.

        Postgres/SQLite use ``ADD COLUMN``; T-SQL uses ``ADD``. The planner stays
        dialect-agnostic and delegates the rendering here.
        """

    # --- execution ---------------------------------------------------------------------
    @abc.abstractmethod
    def apply(self, statements: list[str]) -> None:
        """Execute statements with the engine's appropriate transaction strategy."""

    @abc.abstractmethod
    def run_init_script(self, script: str) -> None:
        """Run a privileged init script (CONCEPT.md §6) verbatim.

        Init scripts are imperative groundwork — possibly multi-statement and containing
        statements that cannot run inside a transaction (e.g. Postgres ``CREATE DATABASE``).
        Implementations therefore run in **autocommit** and accept the whole script.
        """

    # --- state ledger ------------------------------------------------------------------
    @abc.abstractmethod
    def ensure_state_table(self) -> None: ...

    @abc.abstractmethod
    def get_deployed_ref(self) -> str | None: ...

    @abc.abstractmethod
    def record_deploy(self, ref: str, migration_ids: list[str]) -> None: ...

    # --- pure SQL builders (no connection — used by `plan --sql` export) ----------------
    @abc.abstractmethod
    def state_table_ddl(self) -> str:
        """``CREATE TABLE IF NOT EXISTS dbly_state …`` for this engine."""

    @abc.abstractmethod
    def record_deploy_sql(self, ref: str) -> str:
        """A standalone ``INSERT`` recording the deploy — for hand-run scripts."""

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
