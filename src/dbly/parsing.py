"""The *semantic* layer (CONCEPT.md §2, §8) — powered by sqlglot.

Given a SQL file, derive the objects it defines: their kind (→ object class), their
schema-qualified identity, and the objects they depend on (for the dependency DAG).

Honest scope (CONCEPT.md §10): sqlglot parses DDL structure, identity and references well.
It does **not** transpile procedural bodies (PL/SQL / T-SQL / PL/pgSQL); for procedures and
packages we still extract identity, and dependency extraction is best-effort. Bodies are
applied verbatim per dialect by the adapter.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import sqlglot
from sqlglot import exp

from dbly.model import Column, ObjectId, ObjectKind, ParsedObject

# sqlglot dialect names per dbly environment
_DIALECTS = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "pg": "postgres",
    "oracle": "oracle",
    "sqlserver": "tsql",
    "mssql": "tsql",
    "sqlite": "sqlite",
}


def sqlglot_dialect(environment: str | None) -> str | None:
    if not environment:
        return None
    return _DIALECTS.get(environment.strip().lower())


def _kind_from_expression(e: exp.Expression) -> ObjectKind | None:
    if isinstance(e, exp.Create):
        kind = (e.args.get("kind") or "").upper()
        mapping = {
            "TABLE": ObjectKind.TABLE,
            "VIEW": ObjectKind.VIEW,
            "MATERIALIZED VIEW": ObjectKind.VIEW,
            "FUNCTION": ObjectKind.FUNCTION,
            "PROCEDURE": ObjectKind.PROCEDURE,
            "PACKAGE": ObjectKind.PACKAGE,
            "TRIGGER": ObjectKind.TRIGGER,
            "TYPE": ObjectKind.TYPE,
            "INDEX": ObjectKind.INDEX,
            "SEQUENCE": ObjectKind.SEQUENCE,
        }
        return mapping.get(kind, ObjectKind.UNKNOWN)
    if isinstance(e, exp.Grant):
        return ObjectKind.GRANT
    return None


def _identity(e: exp.Expression, default_schema: str | None) -> ObjectId:
    # An index's name lives in the Index node; its schema follows the indexed table
    # (e.find(exp.Table) would otherwise return the *indexed table*, not the index).
    if isinstance(e, exp.Create) and (e.args.get("kind") or "").upper() == "INDEX":
        idx = e.find(exp.Index)
        name = idx.this.name if idx is not None and idx.this is not None else "unknown"
        tbl = e.find(exp.Table)
        schema = (tbl.db if tbl is not None else None) or default_schema
        return ObjectId(schema=schema or None, name=name)
    table = e.find(exp.Table)
    if table is not None:
        schema = table.db or default_schema
        return ObjectId(schema=schema or None, name=table.name)
    # Fallback for objects sqlglot exposes via Identifier (some procedures/types)
    ident = e.find(exp.Identifier)
    name = ident.name if ident else "unknown"
    return ObjectId(schema=default_schema, name=name)


def _dependencies(e: exp.Expression, self_key: str) -> set[str]:
    """Referenced tables/views — the edges of the dependency DAG (best-effort)."""
    deps: set[str] = set()
    for tbl in e.find_all(exp.Table):
        oid = ObjectId(schema=tbl.db or None, name=tbl.name)
        key = oid.key()
        if key and key != self_key:
            deps.add(key)
    return deps


def parse_file(
    sql: str,
    source_file: Path,
    *,
    default_schema: str | None = None,
    dialect: str | None = None,
) -> list[ParsedObject]:
    """Parse one source file into the objects it defines.

    A file may define multiple objects (e.g. a collected ``grants.sql``). Statements that
    sqlglot cannot parse are not silently dropped — they raise, so misconfiguration is loud.
    """
    objects: list[ParsedObject] = []
    statements = sqlglot.parse(sql, read=dialect)
    for stmt in statements:
        if stmt is None:
            continue
        kind = _kind_from_expression(stmt)
        if kind is None:
            continue  # not an object definition (e.g. a comment-only statement)
        oid = _identity(stmt, default_schema)
        deps = _dependencies(stmt, oid.key())
        objects.append(
            ParsedObject(
                id=oid,
                kind=kind,
                sql=stmt.sql(dialect=dialect),
                source_file=source_file,
                depends_on=deps,
            )
        )
    return objects


def canonical_hash(sql: str | None, *, dialect: str | None = None) -> str | None:
    """A formatting-insensitive hash of a definition, for advisory drift detection.

    Both sides (repo desired + live DB source) are canonicalized the same way: parse with
    sqlglot and re-render in one dialect, then hash. Views/SELECTs canonicalize reliably;
    procedural bodies that sqlglot cannot fully parse fall back to whitespace/case
    normalization (best effort — may still yield false positives across the repo↔DB boundary).
    """
    if not sql or not sql.strip():
        return None
    try:
        rendered = ";".join(
            e.sql(dialect=dialect, normalize=True, pretty=False)
            for e in sqlglot.parse(sql, read=dialect)
            if e is not None
        )
        canon = rendered.lower()
    except Exception:  # noqa: BLE001 — procedural body sqlglot can't parse → text fallback
        canon = " ".join(sql.lower().split())
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def desired_columns(sql: str, *, dialect: str | None = None) -> list[Column]:
    """Extract column definitions from a ``CREATE TABLE`` statement (the desired state).

    Constraints (PK/FK/CHECK) are ignored here — the MVP additive diff works at column
    granularity (CONCEPT.md §5). Returns [] for anything that isn't a CREATE TABLE.
    """
    parsed = sqlglot.parse_one(sql, read=dialect)
    if not isinstance(parsed, exp.Create) or (parsed.args.get("kind") or "").upper() != "TABLE":
        return []
    schema = parsed.find(exp.Schema)
    if schema is None:
        return []
    columns: list[Column] = []
    for cdef in schema.expressions:
        if not isinstance(cdef, exp.ColumnDef):
            continue  # table-level constraint, not a column
        name = cdef.name
        col_type = cdef.args.get("kind")
        type_str = col_type.sql(dialect=dialect) if col_type is not None else "unknown"
        nullable = True
        default = None
        for constraint in cdef.constraints:
            ckind = constraint.kind
            if isinstance(ckind, exp.NotNullColumnConstraint):
                nullable = not bool(ckind.args.get("allow_null"))
                nullable = False
            elif isinstance(ckind, exp.DefaultColumnConstraint):
                default = ckind.this.sql(dialect=dialect) if ckind.this is not None else None
        columns.append(Column(name=name, type=type_str, nullable=nullable, default=default))
    return columns


def topological_order(objects: list[ParsedObject]) -> list[ParsedObject]:
    """Order replaceable objects so dependencies come first (CONCEPT.md §8).

    Kahn's algorithm over the in-repo dependency graph. Edges to objects outside this set
    (already-deployed dependencies) are ignored. Cycles are broken deterministically and
    left to the adapter's retry-until-stable fallback.
    """
    by_key = {o.id.key(): o for o in objects}
    in_repo = set(by_key)
    incoming: dict[str, set[str]] = {
        k: {d for d in by_key[k].depends_on if d in in_repo} for k in by_key
    }
    ordered: list[ParsedObject] = []
    ready = sorted(k for k, deps in incoming.items() if not deps)
    seen: set[str] = set()
    while ready:
        k = ready.pop(0)
        seen.add(k)
        ordered.append(by_key[k])
        for other, deps in incoming.items():
            if other in seen or other in ready:
                continue
            if k in deps and deps <= seen:
                ready.append(other)
        ready.sort()
    # leftovers (cycles / unresolved) appended deterministically — retry handles them
    for k in sorted(set(by_key) - seen):
        ordered.append(by_key[k])
    return ordered
