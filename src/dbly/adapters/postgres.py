"""PostgreSQL adapter — the Leitstern (CONCEPT.md §16).

Postgres has transactional DDL, so the whole apply runs in a single transaction: on any
failure everything rolls back and the database is untouched. This is the clean reference
against which the trickier Oracle/SQL-Server semantics are later measured.
"""
from __future__ import annotations

import sqlglot
from sqlalchemy import inspect, text
from sqlglot import exp

from dbly.adapters.base import Adapter, Column, render_column_type
from dbly.model import LiveObject, ObjectId, ObjectKind
from dbly.parsing import canonical_hash

_STATE_DDL = """
CREATE TABLE IF NOT EXISTS public.dbly_state (
    id           bigserial PRIMARY KEY,
    deployed_sha text        NOT NULL,
    migration_id text,
    applied_at   timestamptz NOT NULL DEFAULT now()
)
"""


class PostgresAdapter(Adapter):
    transactional_ddl = True
    default_schema = "public"
    # pinned to public so the ledger doesn't move with the connecting user's search_path
    ledger_table = "public.dbly_state"

    def _resolve(self, schema: str | None, name: str) -> tuple[str, str] | None:
        """Find a relation case-insensitively, returning its real (schema, name).

        Postgres folds unquoted identifiers to lower case, so a DDL ``CREATE TABLE FOO`` lives
        as ``foo``; the desired name (as written) need not match verbatim. Exact matches are
        preferred over case-folded ones when both exist.
        """
        params = {"n": name}
        schema_pred = ""
        if schema is not None:
            schema_pred = "AND lower(n.nspname) = lower(:s) "
            params["s"] = schema
        q = text(
            "SELECT n.nspname, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE lower(c.relname) = lower(:n) {schema_pred}"
            "AND c.relkind IN ('r','p','v','m') "
            "ORDER BY (c.relname = :n) DESC LIMIT 1"
        )
        with self.engine.connect() as conn:
            row = conn.execute(q, params).first()
        return (row[0], row[1]) if row else None

    def table_exists(self, schema: str | None, name: str) -> bool:
        params = {"n": name}
        schema_pred = ""
        if schema is not None:
            schema_pred = "AND lower(n.nspname) = lower(:s) "
            params["s"] = schema
        with self.engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    f"WHERE lower(c.relname) = lower(:n) {schema_pred}"
                    "AND c.relkind IN ('r','p') LIMIT 1"
                ),
                params,
            ).first() is not None

    def get_columns(self, schema: str | None, name: str) -> list[Column]:
        insp = inspect(self.engine)
        rs, rn = self._resolve(schema, name) or (schema, name)
        cols = insp.get_columns(rn, schema=rs)
        dialect = self.engine.dialect
        return [
            Column(
                name=c["name"],
                type=render_column_type(c["type"], dialect),
                nullable=bool(c["nullable"]),
                default=None if c.get("default") is None else str(c["default"]),
            )
            for c in cols
        ]

    def has_object(self, kind: ObjectKind, schema: str | None, name: str) -> bool:
        qname = f"{schema}.{name}" if schema else name
        with self.engine.connect() as conn:
            if kind in (ObjectKind.INDEX, ObjectKind.SEQUENCE, ObjectKind.TABLE, ObjectKind.VIEW):
                return conn.execute(
                    text("SELECT to_regclass(:q)"), {"q": qname}
                ).scalar() is not None
            return conn.execute(
                text(
                    "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE p.proname = :n AND (:s IS NULL OR n.nspname = :s) LIMIT 1"
                ),
                {"n": name, "s": schema},
            ).first() is not None

    _RELKIND = {
        "r": ObjectKind.TABLE, "p": ObjectKind.TABLE, "v": ObjectKind.VIEW,
        "m": ObjectKind.VIEW, "i": ObjectKind.INDEX, "S": ObjectKind.SEQUENCE,
    }

    def inventory(self) -> list[LiveObject]:
        rels = text(
            "SELECT n.nspname, c.relname, c.relkind, "
            "  CASE WHEN c.relkind IN ('v','m') THEN pg_get_viewdef(c.oid) END "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') "
            "  AND c.relkind IN ('r','p','v','m','i','S')"
        )
        routines = text(
            "SELECT n.nspname, p.proname, p.prokind, pg_get_functiondef(p.oid) "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
            "  AND p.prokind IN ('f','p')"
        )
        triggers = text(
            "SELECT n.nspname, t.tgname, pg_get_triggerdef(t.oid) "
            "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE NOT t.tgisinternal "
            "  AND n.nspname NOT IN ('pg_catalog','information_schema')"
        )
        found: dict[str, LiveObject] = {}
        with self.engine.connect() as conn:
            for schema, name, relkind, src in conn.execute(rels):
                kind = self._RELKIND.get(relkind)
                if kind is None:
                    continue
                is_view = kind is ObjectKind.VIEW
                h = canonical_hash(src, dialect="postgres") if is_view else None
                defn = f"CREATE VIEW {schema}.{name} AS\n{src}" if is_view and src else None
                obj = LiveObject(kind, ObjectId(schema, name), h, defn)
                found[obj.key()] = obj
            for schema, name, prokind, src in conn.execute(routines):
                kind = ObjectKind.PROCEDURE if prokind == "p" else ObjectKind.FUNCTION
                obj = LiveObject(kind, ObjectId(schema, name),
                                 canonical_hash(src, dialect="postgres"), src)
                found[obj.key()] = obj
            for schema, name, src in conn.execute(triggers):
                obj = LiveObject(ObjectKind.TRIGGER, ObjectId(schema, name),
                                 canonical_hash(src, dialect="postgres"), src)
                found[obj.key()] = obj
        return list(found.values())

    def schema_exists(self, schema: str) -> bool:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
                {"s": schema},
            ).first() is not None

    def ensure_schema_sql(self, schema: str) -> str | None:
        return f'CREATE SCHEMA IF NOT EXISTS "{schema}";'

    def canonicalize_view(self, create_view_sql: str) -> str | None:
        # ADR 0001: run the desired view through the engine (throwaway TEMP view) and read back
        # pg_get_viewdef, so it's normalized exactly like the live view (casts elided, names
        # qualified) — then the two canonical forms compare cleanly. Rolled back; no residue.
        try:
            parsed = sqlglot.parse_one(create_view_sql, read="postgres")
            query = parsed.expression if isinstance(parsed, exp.Create) else parsed
            if query is None:
                return None
            select_sql = query.sql(dialect="postgres")
        except Exception:  # noqa: BLE001 — unparseable desired SQL → caller falls back
            return None
        try:
            with self.engine.connect() as conn:
                trans = conn.begin()
                try:
                    conn.exec_driver_sql("CREATE TEMP VIEW __dbly_probe AS " + select_sql)
                    return conn.exec_driver_sql(
                        "SELECT pg_get_viewdef('__dbly_probe'::regclass, true)"
                    ).scalar()
                finally:
                    trans.rollback()  # discard the probe (temp + rolled back → no side effect)
        except Exception:  # noqa: BLE001 — no privilege / unresolvable tables → fall back
            return None

    def add_column_sql(self, table: ObjectId, col: Column) -> str:
        parts = [f"ALTER TABLE {table} ADD COLUMN {col.name} {col.type}"]
        if not col.nullable:
            parts.append("NOT NULL")
        if col.default is not None:
            parts.append(f"DEFAULT {col.default}")
        return " ".join(parts) + ";"

    def modify_column_sql(self, table: ObjectId, col: Column) -> str:
        return f"ALTER TABLE {table} ALTER COLUMN {col.name} TYPE {col.type};"

    def apply(self, statements: list[str]) -> None:
        # transactional DDL → one atomic transaction
        with self.engine.begin() as conn:
            for stmt in statements:
                if stmt.strip():
                    conn.execute(text(stmt))

    def run_init_script(self, script: str) -> None:
        # autocommit: CREATE DATABASE & friends can't run inside a transaction block.
        # Execute via the raw psycopg cursor with **no parameters** — SQLAlchemy's
        # exec_driver_sql passes an empty param collection, which makes psycopg3 scan the SQL
        # for %-placeholders and choke on a legitimate `%` in a PL/pgSQL `format('%I', …)` body
        # or a LIKE pattern. With params omitted, psycopg3 sends the script verbatim.
        with self.engine.connect() as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            dbapi_conn = conn.connection.driver_connection  # the psycopg3 Connection
            with dbapi_conn.cursor() as cur:
                cur.execute(script)

    def state_table_ddl(self) -> str:
        return _STATE_DDL.strip() + ";"

    def record_deploy_sql(self, ref: str) -> str:
        return (f"INSERT INTO {self.ledger_table} (deployed_sha) "
                f"VALUES ('{ref.replace(chr(39), chr(39) * 2)}');")

    def ensure_state_table(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(_STATE_DDL))

    def get_deployed_ref(self) -> str | None:
        self.ensure_state_table()
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT deployed_sha FROM {self.ledger_table} "
                    "ORDER BY applied_at DESC, id DESC LIMIT 1"
                )
            ).first()
        return row[0] if row else None

    def record_deploy(self, ref: str, migration_ids: list[str]) -> None:
        self.ensure_state_table()
        with self.engine.begin() as conn:
            if migration_ids:
                for mid in migration_ids:
                    conn.execute(
                        text(
                            f"INSERT INTO {self.ledger_table} (deployed_sha, migration_id) "
                            "VALUES (:sha, :mid)"
                        ),
                        {"sha": ref, "mid": mid},
                    )
            else:
                conn.execute(
                    text(f"INSERT INTO {self.ledger_table} (deployed_sha) VALUES (:sha)"),
                    {"sha": ref},
                )
