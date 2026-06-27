"""Core data model shared across parsing, planning and applying.

Two object classes drive everything (CONCEPT.md §3):

* **REPLACEABLE** (Klasse 1) — views, functions, procedures, packages, triggers, types,
  grants. Deployed by re-applying the object wholesale (``CREATE OR REPLACE`` /
  drop-and-create). Idempotent, no ledger needed.
* **STATEFUL** (Klasse 2) — tables. Never blindly re-applied; the desired ``CREATE TABLE``
  is diffed against the live schema and an additive ``ALTER`` is generated. Destructive
  deltas are flagged, never auto-applied.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ObjectClass(str, Enum):
    REPLACEABLE = "replaceable"  # Klasse 1
    STATEFUL = "stateful"        # Klasse 2 (tables)


class ObjectKind(str, Enum):
    TABLE = "table"
    VIEW = "view"
    FUNCTION = "function"
    PROCEDURE = "procedure"
    PACKAGE = "package"
    TRIGGER = "trigger"
    TYPE = "type"
    GRANT = "grant"
    INDEX = "index"
    SEQUENCE = "sequence"
    UNKNOWN = "unknown"

    @property
    def object_class(self) -> ObjectClass:
        # Stateful objects are never blindly re-applied: tables get an additive column
        # diff; indexes/sequences are created only when missing (CREATE is not idempotent
        # and has no CREATE OR REPLACE form on most engines).
        return ObjectClass.STATEFUL if self in _STATEFUL_KINDS else ObjectClass.REPLACEABLE


_STATEFUL_KINDS = {ObjectKind.TABLE, ObjectKind.INDEX, ObjectKind.SEQUENCE}


@dataclass(slots=True)
class Column:
    """A table column — used both for the desired state (parsed from ``CREATE TABLE``)
    and the actual state (introspected from the live database)."""

    name: str
    type: str
    nullable: bool = True
    default: str | None = None

    def key(self) -> str:
        return self.name.lower()


@dataclass(slots=True)
class ObjectId:
    """Schema-qualified identity of a database object."""

    schema: str | None
    name: str

    def __str__(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def key(self) -> str:
        return str(self).lower()


@dataclass(slots=True)
class ParsedObject:
    """A single database object discovered by parsing a source file."""

    id: ObjectId
    kind: ObjectKind
    sql: str
    source_file: Path
    depends_on: set[str] = field(default_factory=set)  # ObjectId.key() of referenced objects

    @property
    def object_class(self) -> ObjectClass:
        return self.kind.object_class


@dataclass(slots=True)
class LiveObject:
    """An object discovered in the live database by introspection (the *reality* layer).

    ``source_hash`` is a canonicalized hash of the definition for procedural/definitional
    objects (views, functions, procedures, triggers) — used for advisory drift detection.
    """

    kind: ObjectKind
    id: ObjectId
    source_hash: str | None = None

    def key(self) -> str:
        return f"{self.kind.value}:{self.id.key()}"


class ChangeType(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


class Severity(str, Enum):
    ADDITIVE = "additive"        # safe to auto-apply
    DESTRUCTIVE = "destructive"  # requires --allow-destructive or an explicit ALTER


@dataclass(slots=True)
class Step:
    """One ordered unit of work in a plan."""

    title: str
    object_id: ObjectId | None
    kind: ObjectKind
    severity: Severity
    sql: str
    source_file: Path | None = None
    note: str | None = None


@dataclass(slots=True)
class Plan:
    """An ordered, reviewable set of steps — the artifact of ``dbly plan``."""

    target: str
    from_ref: str | None
    to_ref: str
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_destructive(self) -> bool:
        return any(s.severity is Severity.DESTRUCTIVE for s in self.steps)
