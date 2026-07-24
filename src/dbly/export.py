"""Reverse direction — introspect a live database and emit its DDL (``dbly export``).

The inverse of the deploy path: read what actually exists and render it as a SQL script,
optionally transpiled to another engine's dialect. Honest scope (CONCEPT.md §10):

* **tables / views** are *structural* — sqlglot transpiles them across dialects reliably.
  Tables are rebuilt from the live column set (name, type, nullability, default); constraints
  and indexes are **not** reconstructed (a warning says so).
* **functions / procedures / triggers / packages / types** carry procedural bodies sqlglot
  cannot transpile. They are emitted **verbatim** in the source dialect; requesting a
  different target dialect keeps them as-is and adds a warning.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot

from dbly.adapters.base import Adapter
from dbly.model import LiveObject, ObjectId, ObjectKind

# emit order: providers before dependants (best-effort; FK ordering between tables is not solved)
_ORDER = {
    ObjectKind.SEQUENCE: 0, ObjectKind.TYPE: 1, ObjectKind.TABLE: 2, ObjectKind.INDEX: 3,
    ObjectKind.VIEW: 4, ObjectKind.FUNCTION: 5, ObjectKind.PROCEDURE: 6,
    ObjectKind.PACKAGE: 7, ObjectKind.TRIGGER: 8, ObjectKind.GRANT: 9, ObjectKind.UNKNOWN: 10,
}
_STRUCTURAL = {ObjectKind.TABLE, ObjectKind.VIEW}
_PROCEDURAL = {
    ObjectKind.FUNCTION, ObjectKind.PROCEDURE, ObjectKind.TRIGGER,
    ObjectKind.PACKAGE, ObjectKind.TYPE,
}


@dataclass(slots=True)
class ExportResult:
    ddl: str
    warnings: list[str] = field(default_factory=list)
    object_count: int = 0


def _create_table_sql(adapter: Adapter, oid: ObjectId, dialect: str | None) -> str:
    cols = adapter.get_columns(oid.schema, oid.name)
    lines = []
    for c in cols:
        piece = f"  {c.name} {c.type}"
        if c.default is not None:
            piece += f" DEFAULT {c.default}"
        if not c.nullable:
            piece += " NOT NULL"
        lines.append(piece)
    body = ",\n".join(lines)
    return f"CREATE TABLE {oid} (\n{body}\n);"


def _transpile(sql: str, *, read: str | None, write: str | None) -> str:
    """Transpile one statement; on any failure the caller keeps the verbatim source."""
    out = sqlglot.transpile(sql, read=read, write=write)
    return ";\n".join(s.rstrip(";") for s in out) + ";"


def export_ddl(
    adapter: Adapter,
    *,
    source_dialect: str | None,
    target_dialect: str | None = None,
    schemas: list[str] | None = None,
) -> ExportResult:
    """Render the live database as a DDL script (optionally transpiled to ``target_dialect``)."""
    sel = {s.lower() for s in schemas} if schemas else None
    live: list[LiveObject] = [
        o for o in adapter.inventory()
        if o.key() != "table:dbly_state"                       # never export dbly's own ledger
        and (sel is None or (o.id.schema and o.id.schema.lower() in sel))
    ]
    live.sort(key=lambda o: (_ORDER.get(o.kind, 99), str(o.id).lower()))

    cross = bool(target_dialect and target_dialect != source_dialect)
    warnings: list[str] = []
    if cross:
        warnings.append(
            f"transpiling {source_dialect} → {target_dialect}: tables/views are converted; "
            "procedural objects (function/procedure/trigger/package/type) are emitted verbatim"
        )
    warned_table = False

    parts: list[str] = [
        "-- dbly export — DDL reconstructed from a live database. Review before running.",
        f"-- source dialect: {source_dialect or '?'}"
        + (f"   target dialect: {target_dialect}" if cross else ""),
        "",
    ]
    count = 0
    for o in live:
        if o.kind is ObjectKind.TABLE:
            if o.definition:                        # engine gave us the real DDL (e.g. SQLite)
                raw = o.definition.rstrip().rstrip(";") + ";"
            else:                                   # rebuilt from columns — flag the limits once
                if not warned_table:
                    warnings.append(
                        "tables are rebuilt from columns only — primary keys, foreign keys, "
                        "checks and indexes are not reconstructed"
                    )
                    warned_table = True
                raw = _create_table_sql(adapter, o.id, source_dialect)
        elif o.definition:
            raw = o.definition.rstrip().rstrip(";") + ";"
        else:
            warnings.append(f"{o.kind.value} {o.id}: no definition available — skipped")
            continue

        sql = raw
        if cross:
            if o.kind in _STRUCTURAL:
                try:
                    sql = _transpile(raw, read=source_dialect, write=target_dialect)
                except Exception as exc:  # noqa: BLE001 — fall back to verbatim, flag it
                    warnings.append(f"{o.kind.value} {o.id}: could not transpile ({exc}); verbatim")
            elif o.kind in _PROCEDURAL:
                warnings.append(f"{o.kind.value} {o.id}: emitted verbatim ({source_dialect})")

        parts.append(f"-- {o.kind.value} {o.id}")
        parts.append(sql)
        parts.append("")
        count += 1

    return ExportResult(ddl="\n".join(parts), warnings=warnings, object_count=count)
