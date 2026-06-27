"""Drift detection for ``dbly check`` — desired (repo) vs. live (introspected) state.

Compares the full desired state at a git ref against the database's live inventory and
reports four kinds of drift:

* **missing**     — in the repo, absent from the DB (would be created on the next apply)
* **orphaned**    — in the DB, absent from the repo (opt-in via ``--orphans``; on a partially
                    managed database everything unmanaged shows up here, so it is off by default)
* **columns**     — tables present in both whose column sets differ
* **definitions** — procedural objects (view/function/procedure/trigger) whose canonical
                    source hash differs (advisory — may have false positives, see parsing)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dbly import parsing
from dbly.adapters.base import Adapter
from dbly.model import ObjectId, ObjectKind
from dbly.repo import Repo

_HASHED_KINDS = {ObjectKind.VIEW, ObjectKind.FUNCTION, ObjectKind.PROCEDURE, ObjectKind.TRIGGER}
_LEDGER_KEY = "table:dbly_state"


@dataclass(slots=True)
class ColumnDrift:
    table: ObjectId
    added: list[str]    # in the desired CREATE TABLE, missing from the DB
    removed: list[str]  # in the DB, absent from the desired CREATE TABLE


@dataclass(slots=True)
class DriftReport:
    missing: list[tuple[ObjectKind, ObjectId]] = field(default_factory=list)
    orphaned: list[tuple[ObjectKind, ObjectId]] = field(default_factory=list)
    columns: list[ColumnDrift] = field(default_factory=list)
    definitions: list[tuple[ObjectKind, ObjectId]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.missing or self.orphaned or self.columns or self.definitions)


def _norm_key(kind: ObjectKind, oid: ObjectId, default_schema: str | None) -> str:
    schema = oid.schema
    if schema and default_schema and schema.lower() == default_schema.lower():
        schema = None  # an unqualified repo object maps to the engine's implicit schema
    body = f"{schema.lower()}.{oid.name.lower()}" if schema else oid.name.lower()
    return f"{kind.value}:{body}"


def compute_drift(
    repo: Repo,
    adapter: Adapter,
    *,
    to_ref: str,
    dialect: str | None,
    include_orphans: bool = False,
) -> DriftReport:
    ds = adapter.default_schema

    desired = {}  # normalized key -> ParsedObject
    for rel in repo.list_files(to_ref):
        sql = repo.read_at(to_ref, rel)
        for obj in parsing.parse_file(
            sql, rel, default_schema=repo.schema_for(rel), dialect=dialect
        ):
            desired[_norm_key(obj.kind, obj.id, ds)] = obj

    live = {_norm_key(o.kind, o.id, ds): o for o in adapter.inventory()}
    live.pop(_LEDGER_KEY, None)  # dbly's own ledger is never "orphaned"

    report = DriftReport()
    for key, obj in desired.items():
        if key not in live:
            report.missing.append((obj.kind, obj.id))

    if include_orphans:
        for key, o in live.items():
            if key not in desired:
                report.orphaned.append((o.kind, o.id))

    for key, obj in desired.items():
        if obj.kind is ObjectKind.TABLE and key in live:
            actual = {c.key() for c in adapter.get_columns(obj.id.schema, obj.id.name)}
            want = {c.key() for c in parsing.desired_columns(obj.sql, dialect=dialect)}
            added, removed = sorted(want - actual), sorted(actual - want)
            if added or removed:
                report.columns.append(ColumnDrift(obj.id, added, removed))

    for key, obj in desired.items():
        if obj.kind in _HASHED_KINDS and key in live and live[key].source_hash:
            want = parsing.canonical_hash(obj.sql, dialect=dialect)
            if want and want != live[key].source_hash:
                report.definitions.append((obj.kind, obj.id))

    return report
