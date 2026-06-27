"""Turn a git changeset into an ordered, reviewable plan (CONCEPT.md §5, §7, §8).

Replaceable objects (Klasse 1) are re-applied wholesale, dependency-ordered. Tables
(Klasse 2) are diffed desired-vs-actual: additive deltas are generated automatically,
destructive deltas are flagged and never auto-applied.
"""
from __future__ import annotations

from pathlib import Path

from dbly import parsing
from dbly.adapters.base import Adapter
from dbly.model import (
    ChangeType,
    ObjectKind,
    ParsedObject,
    Plan,
    Severity,
    Step,
)
from dbly.repo import Repo


def build_plan(
    repo: Repo,
    adapter: Adapter,
    *,
    from_ref: str | None,
    to_ref: str,
    target: str,
    dialect: str | None,
) -> Plan:
    plan = Plan(target=target, from_ref=from_ref, to_ref=to_ref)
    changes = repo.changed_files(from_ref, to_ref)

    # Bucket by kind so the plan is emitted in dependency-safe order regardless of file
    # order: sequences → tables → indexes → replaceable (views/functions/…).
    sequences: list[ParsedObject] = []
    tables: list[ParsedObject] = []
    indexes: list[ParsedObject] = []
    replaceable: list[ParsedObject] = []
    for fc in changes:
        if fc.change_type is ChangeType.DELETED:
            _plan_deletion(repo, plan, fc.path, from_ref, dialect)
            continue
        sql = repo.read_at(to_ref, fc.path)
        schema_hint = repo.schema_for(fc.path)
        for obj in parsing.parse_file(sql, fc.path, default_schema=schema_hint, dialect=dialect):
            if obj.kind is ObjectKind.SEQUENCE:
                sequences.append(obj)
            elif obj.kind is ObjectKind.TABLE:
                tables.append(obj)
            elif obj.kind is ObjectKind.INDEX:
                indexes.append(obj)
            else:
                replaceable.append(obj)

    for obj in sequences:
        _plan_create_if_missing(adapter, plan, obj)
    for obj in tables:
        _plan_table(adapter, plan, obj, dialect)
    for obj in indexes:
        _plan_create_if_missing(adapter, plan, obj)

    # replaceable objects: dependency-ordered, re-applied wholesale
    for obj in parsing.topological_order(replaceable):
        plan.steps.append(
            Step(
                title=f"apply {obj.kind.value} {obj.id}",
                object_id=obj.id,
                kind=obj.kind,
                severity=Severity.ADDITIVE,
                sql=obj.sql if obj.sql.strip().endswith(";") else obj.sql + ";",
                source_file=obj.source_file,
            )
        )
    return plan


def _plan_create_if_missing(adapter: Adapter, plan: Plan, obj: ParsedObject) -> None:
    """Indexes/sequences: CREATE only when absent (no idempotent CREATE OR REPLACE form).

    A *changed* definition is not detected here (that surfaces as drift in `dbly check`);
    re-creating would need an explicit drop, which is destructive and left to the human.
    """
    if adapter.has_object(obj.kind, obj.id.schema, obj.id.name):
        return
    plan.steps.append(
        Step(
            title=f"create {obj.kind.value} {obj.id}",
            object_id=obj.id,
            kind=obj.kind,
            severity=Severity.ADDITIVE,
            sql=obj.sql if obj.sql.strip().endswith(";") else obj.sql + ";",
            source_file=obj.source_file,
        )
    )


def _plan_table(adapter: Adapter, plan: Plan, obj: ParsedObject, dialect: str | None) -> None:
    if not adapter.table_exists(obj.id.schema, obj.id.name):
        plan.steps.append(
            Step(
                title=f"create table {obj.id}",
                object_id=obj.id,
                kind=ObjectKind.TABLE,
                severity=Severity.ADDITIVE,
                sql=obj.sql,
                source_file=obj.source_file,
                note="table does not exist — full CREATE",
            )
        )
        return

    desired = parsing.desired_columns(obj.sql, dialect=dialect)
    actual = adapter.get_columns(obj.id.schema, obj.id.name)
    actual_by_key = {c.key(): c for c in actual}
    desired_by_key = {c.key(): c for c in desired}

    # additive: columns present in desired, missing in actual
    for col in desired:
        if col.key() in actual_by_key:
            continue
        if not col.nullable and col.default is None:
            plan.steps.append(
                Step(
                    title=f"add NOT NULL column {obj.id}.{col.name}",
                    object_id=obj.id,
                    kind=ObjectKind.TABLE,
                    severity=Severity.DESTRUCTIVE,
                    sql=adapter.add_column_sql(obj.id, col),
                    source_file=obj.source_file,
                    note="NOT NULL without default on existing table — unsafe",
                )
            )
            plan.warnings.append(
                f"{obj.id}.{col.name}: NOT NULL without default cannot be added safely "
                "to a populated table"
            )
        else:
            plan.steps.append(
                Step(
                    title=f"add column {obj.id}.{col.name}",
                    object_id=obj.id,
                    kind=ObjectKind.TABLE,
                    severity=Severity.ADDITIVE,
                    sql=adapter.add_column_sql(obj.id, col),
                    source_file=obj.source_file,
                )
            )

    # destructive: columns present in actual, gone from desired
    for col in actual:
        if col.key() not in desired_by_key:
            plan.warnings.append(
                f"{obj.id}.{col.name}: present in DB, absent from desired CREATE TABLE — "
                "potential DROP COLUMN (not auto-applied; use an explicit ALTER)"
            )


def _plan_deletion(
    repo: Repo, plan: Plan, path: Path, from_ref: str | None, dialect: str | None
) -> None:
    """A deleted source file → its objects would be dropped (destructive, flagged)."""
    if from_ref is None:
        return
    try:
        sql = repo.read_at(from_ref, path)
    except Exception:  # noqa: BLE001 — file may not exist at from_ref
        return
    schema_hint = repo.schema_for(path)
    for obj in parsing.parse_file(sql, path, default_schema=schema_hint, dialect=dialect):
        plan.warnings.append(
            f"{obj.id}: source file deleted — DROP {obj.kind.value} not auto-applied"
        )
