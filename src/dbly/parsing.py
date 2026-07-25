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
import re
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


def _dependencies(e: exp.Expression, self_key: str, default_schema: str | None) -> set[str]:
    """Referenced tables/views — the edges of the dependency DAG (best-effort).

    An unqualified reference is resolved against the object's own schema (``default_schema``),
    so a same-schema FK/``FROM`` matches the referenced object's qualified key — and a table's
    own unqualified name in its ``CREATE`` doesn't read as a self-dependency.
    """
    deps: set[str] = set()
    for tbl in e.find_all(exp.Table):
        schema = tbl.db or default_schema
        oid = ObjectId(schema=schema or None, name=tbl.name)
        key = oid.key()
        if key and key != self_key:
            deps.add(key)
    # Function calls (a trigger's EXECUTE FUNCTION, a view's FROM func(), a call in a body) are
    # exp.Anonymous to sqlglot — capture them too, so a function is ordered before the trigger/
    # view that calls it. Names are qualified with the object's schema; built-ins that resolve
    # to no repo object are harmlessly ignored by topological_order / the closure.
    for fn in e.find_all(exp.Anonymous):
        name = fn.this if isinstance(fn.this, str) else None
        if not name:
            continue
        key = ObjectId(schema=default_schema or None, name=name).key()
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
        deps = _dependencies(stmt, oid.key(), oid.schema)
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


def referenced_tables(sql: str, *, dialect: str | None = None) -> set[str]:
    """Lower-cased table names referenced anywhere in a SQL script (best effort).

    Used to detect which tables a pending migration touches, so the additive diff defers to
    the migration for those tables. Returns an empty set if sqlglot cannot parse the script.
    """
    names: set[str] = set()
    try:
        for stmt in sqlglot.parse(sql, read=dialect):
            if stmt is None:
                continue
            for tbl in stmt.find_all(exp.Table):
                names.add(tbl.name.lower())
    except Exception:  # noqa: BLE001 — procedural/odd SQL; suppression is best-effort
        pass
    return names


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


def canonical_view_query(sql: str | None, *, dialect: str | None = None) -> str | None:
    """Hash of a view's SELECT body, tolerant of the CREATE-VIEW wrapper being present or not.

    The two sides of a view comparison arrive shaped differently: the repo file is a full
    ``CREATE [OR REPLACE] VIEW … AS SELECT …`` while Postgres' ``pg_get_viewdef`` returns only
    the ``SELECT``. Hashing the raw text on each side made *every* view look drifted. Here both
    sides are reduced to just the query and rendered the same way, so an identical view matches.
    """
    expr = _normalized_view_expr(sql, dialect=dialect)
    if expr is None:  # unparseable → consistent text fallback
        return hashlib.sha256(" ".join(sql.lower().split()).encode("utf-8")).hexdigest()[:16]
    # Hash the *structure* (repr), not rendered SQL: precedence lives in the tree, so this
    # distinguishes ``(a OR b) AND c`` from ``a OR b AND c`` while ignoring redundant parens —
    # and sidesteps sqlglot's renderer, which doesn't re-insert precedence parens.
    return hashlib.sha256(repr(expr).lower().encode("utf-8")).hexdigest()[:16]


def _normalized_view_expr(sql: str | None, *, dialect: str | None = None) -> exp.Expression | None:
    """Parse a view/SELECT and reduce it to a canonical structural form (shared, ADR 0001).

    * unwrap the ``CREATE VIEW`` — keep only the query;
    * strip per-column table qualifiers (``t.foo`` → ``foo``) that Postgres always adds;
    * remove **all** parentheses — operator precedence is encoded in the tree structure, so
      redundant parens (which ``pg_get_viewdef`` keeps from the original source text) vanish
      while a real precedence difference remains a different tree.
    """
    if not sql or not sql.strip():
        return None
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return None
    query = parsed.expression if isinstance(parsed, exp.Create) else parsed
    if query is None:
        return None
    for col in query.find_all(exp.Column):
        if col.args.get("table") and not col.args.get("db"):
            col.set("table", None)
    for paren in list(query.find_all(exp.Paren)):
        paren.replace(paren.this)
    return query


def normalize_view_sql(sql: str | None, *, dialect: str | None = None, pretty: bool = True) -> str | None:
    """Human-readable normalized view SQL for the ``--show-diff`` display (not for hashing)."""
    expr = _normalized_view_expr(sql, dialect=dialect)
    if expr is None:
        return " ".join(sql.split()) if sql else None
    return expr.sql(dialect=dialect, normalize=True, pretty=pretty)


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


# sqlglot DataType families we treat as "unknown" — comparison can't be trusted, so skip.
_UNKNOWN_TYPES = {exp.DataType.Type.NULL, exp.DataType.Type.UNKNOWN, exp.DataType.Type.USERDEFINED}


def canonical_type(type_str: str, *, dialect: str | None = None) -> str | None:
    """Canonicalize a column type through sqlglot's type parser (structural, not text).

    Both sides of a comparison arrive spelled differently — the desired side rendered by
    sqlglot, the actual side by SQLAlchemy introspection. Parsing each into a ``DataType`` and
    re-rendering collapses synonyms that mean the same thing (``INTEGER``≡``INT``,
    ``NUMERIC``≡``DECIMAL``, ``TIMESTAMP``≡``timestamp without time zone``) while preserving a
    genuine precision/scale change (``NUMBER``→``NUMBER(10)``).

    Returns ``None`` when the type can't be parsed or is an unknown/user-defined type (e.g. a
    PostGIS ``geometry`` that SQLAlchemy reflects as ``NULL``) — the caller then declines to
    report a change rather than guessing.
    """
    s = re.split(r"\s+COLLATE\s+", type_str.strip(), maxsplit=1)[0].strip()  # drop collation noise
    if not s:
        return None
    try:
        dt = exp.DataType.build(s, dialect=dialect)
    except Exception:  # noqa: BLE001 — unparseable spelling → "don't know"
        return None
    if dt.this in _UNKNOWN_TYPES:
        return None
    return dt.sql(dialect=dialect).upper()


def types_differ(desired: str, actual: str, *, dialect: str | None = None) -> bool:
    """Whether a declared (desired) column type genuinely differs from the live (actual) one.

    Conservative: if either side can't be canonicalized (unknown/user-defined/unparseable),
    returns ``False`` — dbly would rather miss an exotic type change than cry wolf on every
    deploy (which lossy introspection of arrays/geometry/tz types otherwise caused)."""
    cd = canonical_type(desired, dialect=dialect)
    ca = canonical_type(actual, dialect=dialect)
    if cd is None or ca is None:
        return False
    return cd != ca


def topological_order(objects: list[ParsedObject]) -> list[ParsedObject]:
    """Order replaceable objects so dependencies come first (CONCEPT.md §8).

    Kahn's algorithm over the in-repo dependency graph. Edges to objects outside this set
    (already-deployed dependencies) are ignored. Cycles are broken deterministically and
    left to the adapter's retry-until-stable fallback.

    Objects that share a key (e.g. overloaded functions — same name, different signatures) are
    kept together as a group in their original (file) order, never deduplicated away.
    """
    groups: dict[str, list[ParsedObject]] = {}
    for o in objects:
        groups.setdefault(o.id.key(), []).append(o)
    in_repo = set(groups)
    incoming: dict[str, set[str]] = {
        k: {d for obj in groups[k] for d in obj.depends_on if d in in_repo and d != k}
        for k in groups
    }
    ordered_keys: list[str] = []
    ready = sorted(k for k, deps in incoming.items() if not deps)
    seen: set[str] = set()
    while ready:
        k = ready.pop(0)
        seen.add(k)
        ordered_keys.append(k)
        for other, deps in incoming.items():
            if other in seen or other in ready:
                continue
            if k in deps and deps <= seen:
                ready.append(other)
        ready.sort()
    # leftovers (cycles / unresolved) appended deterministically — retry handles them
    ordered_keys += sorted(set(groups) - seen)
    return [obj for k in ordered_keys for obj in groups[k]]
