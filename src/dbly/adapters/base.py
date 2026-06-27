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
from dbly.model import Column

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

    # --- execution ---------------------------------------------------------------------
    @abc.abstractmethod
    def apply(self, statements: list[str]) -> None:
        """Execute statements with the engine's appropriate transaction strategy."""

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
