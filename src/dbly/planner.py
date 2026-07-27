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
    Migration,
    ObjectKind,
    ParsedObject,
    Plan,
    Severity,
    Step,
)
from dbly.repo import FileChange, Repo


def _object_exists(adapter: Adapter, obj: ParsedObject) -> bool:
    try:
        if obj.kind is ObjectKind.TABLE:
            return adapter.table_exists(obj.id.schema, obj.id.name)
        return adapter.has_object(obj.kind, obj.id.schema, obj.id.name)
    except Exception:  # noqa: BLE001 — treat an unresolvable check as "not present"
        return False


def _dependency_closure(
    repo: Repo, adapter: Adapter, to_ref: str, seed_paths: set[Path], dialect: str | None
) -> set[Path]:
    """Files defining the **missing** dependency closure of the seed objects.

    A scoped deploy (``--schema``/``--path``) may reference objects in other schemas (a
    cross-schema FK, a view over another schema's table). With ``--with-deps`` we pull in the
    specific objects it needs that **aren't already in the target** — resolved from the full
    repo graph — and stop at anything that already exists (an existing object's own
    dependencies are, by definition, already satisfied). So a new schema pulls in only the
    handful of objects genuinely missing, never whole existing schemas. Dependencies dbly can't
    resolve to a repo file are left to the pre-flight warning.
    """
    # Objects can be *referenced* (FK target, FROM table/view, called function) — a trigger or
    # grant never is, and it collides on name with its table (a trigger ``gzp.funkt`` vs table
    # ``gzp.funkt``), so exclude those as dependency targets; a TABLE always wins a name clash.
    _TARGET_KINDS = {
        ObjectKind.TABLE, ObjectKind.VIEW, ObjectKind.SEQUENCE,
        ObjectKind.FUNCTION, ObjectKind.PROCEDURE, ObjectKind.TYPE,
    }
    by_key: dict[str, tuple[Path, ParsedObject]] = {}
    for rel in repo.all_object_files(to_ref):
        try:
            csql = repo.read_at(to_ref, rel)
            objs = parsing.parse_file(
                csql, rel, default_schema=repo.schema_for(rel, csql), dialect=dialect,
                type_from=repo.layout.type_from,
            )
        except Exception:  # noqa: BLE001 — unparseable file can't contribute edges
            continue
        for obj in objs:
            if obj.kind not in _TARGET_KINDS:
                continue
            existing = by_key.get(obj.id.key())
            if existing is None or (existing[1].kind is not ObjectKind.TABLE):
                by_key[obj.id.key()] = (rel, obj)

    needed = set(seed_paths)
    queue = [dep for k, (p, o) in by_key.items() if p in seed_paths for dep in o.depends_on]
    seen: set[str] = set()
    while queue:
        dep = queue.pop()
        if dep in seen:
            continue
        seen.add(dep)
        entry = by_key.get(dep)
        if entry is None:
            continue  # not a repo object → pre-flight warning handles it
        path, obj = entry
        if path in needed or _object_exists(adapter, obj):
            continue  # already deploying it, or it already exists → stop the walk here
        needed.add(path)
        queue.extend(obj.depends_on)
    return needed


def build_plan(
    repo: Repo,
    adapter: Adapter,
    *,
    from_ref: str | None,
    to_ref: str,
    target: str,
    dialect: str | None,
    with_deps: bool = False,
) -> Plan:
    plan = Plan(target=target, from_ref=from_ref, to_ref=to_ref)

    # Explicit migrations (run-once). On bootstrap (no baseline) the canonical objects
    # already produce the end state, so pending migrations are *baselined* (recorded, not
    # run); on upgrade they run — before the object steps — to reshape the schema.
    applied = adapter.applied_migrations()
    pending = [(mid, p) for mid, p in repo.migration_files(to_ref) if mid not in applied]
    if from_ref is None:
        plan.baselined = [mid for mid, _ in pending]
    else:
        plan.migrations = [
            Migration(mid, repo.read_at(to_ref, p), p) for mid, p in pending
        ]

    changes = repo.changed_files(from_ref, to_ref)

    # --with-deps on a scoped deploy: pull in the specific objects the selection depends on
    # (resolved from the full repo graph) as additive create-if-missing steps — not whole schemas.
    if with_deps and repo.has_selection:
        seed = {fc.path for fc in changes if fc.change_type is not ChangeType.DELETED}
        extra = _dependency_closure(repo, adapter, to_ref, seed, dialect) - seed
        known = {fc.path for fc in changes}
        changes += [FileChange(p, ChangeType.ADDED) for p in sorted(extra) if p not in known]

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
        schema_hint = repo.schema_for(fc.path, sql)
        parsed = parsing.parse_file(sql, fc.path, default_schema=schema_hint, dialect=dialect,
                                    type_from=repo.layout.type_from)
        if not parsed:
            # Never drop a changed object file silently — the parser recognized no object in it
            # (often a PL/pgSQL body sqlglot renders opaquely). Loud, with the actionable knob.
            plan.warnings.append(
                f"{fc.path}: no deployable object recognized — NOT deployed "
                "(parser couldn't identify it; set [layout] type_from=\"extension\" to deploy "
                "it verbatim by file extension)."
            )
        for obj in parsed:
            if obj.kind is ObjectKind.SEQUENCE:
                sequences.append(obj)
            elif obj.kind is ObjectKind.TABLE:
                tables.append(obj)
            elif obj.kind is ObjectKind.INDEX:
                indexes.append(obj)
            else:
                replaceable.append(obj)

    # Ensure the schemas the managed objects live in exist — on a greenfield target
    # `CREATE TABLE download.x` fails if schema `download` was never created. Emitted first,
    # once per absent schema. Engines where schemas aren't a dbly concern (Oracle users, SQLite)
    # return no DDL and are skipped — those belong in `init`.
    seen_schemas: set[str] = set()
    for obj in (*sequences, *tables, *indexes, *replaceable):
        schema = obj.id.schema
        if not schema or schema.lower() in seen_schemas:
            continue
        seen_schemas.add(schema.lower())
        ddl = adapter.ensure_schema_sql(schema)
        if ddl and not adapter.schema_exists(schema):
            plan.steps.append(
                Step(
                    title=f"create schema {schema}",
                    object_id=None,
                    kind=ObjectKind.SCHEMA,
                    severity=Severity.ADDITIVE,
                    sql=ddl,
                    note="schema of a managed object — created if absent",
                )
            )

    # Pre-flight: warn about inter-table FK targets that are neither in this deploy nor already
    # in the target — e.g. a cross-schema FK when the deploy is scoped with --schema/--path.
    # Better an upfront, actionable warning than a cryptic "relation … does not exist" mid-apply.
    deploy_keys = {o.id.key() for o in (*sequences, *tables, *indexes, *replaceable)}
    for t in tables:
        for dep in sorted(t.depends_on):
            if dep in deploy_keys:
                continue
            dep_schema, _, dep_name = dep.rpartition(".")
            if not adapter.table_exists(dep_schema or None, dep_name):
                plan.warnings.append(
                    f"{t.id}: references {dep}, which is not in this deploy and not in the "
                    "target — create it first (or widen --schema/--path)."
                )

    # Tables touched by a pending migration are migration-managed for this deploy — the
    # migration reshapes them at apply time, so the (plan-time) additive diff must defer.
    migration_tables: set[str] = set()
    for m in plan.migrations:
        migration_tables |= parsing.referenced_tables(m.sql, dialect=dialect)

    all_objs = [*sequences, *tables, *indexes, *replaceable]

    def _emit_file(rep_obj: ParsedObject, *, note: str | None = None) -> None:
        """Emit one step that runs ``rep_obj``'s whole source file verbatim (ADR 0002)."""
        src = rep_obj.source_file
        extra = sum(1 for o in all_objs if o.source_file == src) - 1
        title = f"apply {rep_obj.kind.value} {rep_obj.id}"
        if extra > 0:
            title += f" (+{extra} more in {src.name})"
        plan.steps.append(
            Step(title=title, object_id=rep_obj.id, kind=rep_obj.kind,
                 severity=Severity.ADDITIVE, sql=repo.read_at(to_ref, src),
                 source_file=src, note=note, script=True)
        )

    # A table being *created* runs its whole file (ADR 0002 extended) — so ``ALTER … OWNER TO``,
    # constraints and co-located indexes all apply, not just a dbly-generated CREATE owned by the
    # connected (often privileged) user. Existing tables keep the additive column diff.
    fresh_files: set[Path] = {
        obj.source_file for obj in tables
        if obj.id.name.lower() not in migration_tables
        and not adapter.table_exists(obj.id.schema, obj.id.name)
    }

    for obj in sequences:
        if obj.source_file not in fresh_files:  # a fresh table's file creates its own sequences
            _plan_create_if_missing(adapter, plan, obj)
    # FK-dependency order so a referenced table is created before the one referencing it.
    emitted: set[Path] = set()
    for obj in parsing.topological_order(tables):
        if obj.id.name.lower() in migration_tables:
            plan.warnings.append(
                f"{obj.id}: managed by a pending migration — additive diff skipped this deploy"
            )
            continue
        if obj.source_file in fresh_files:
            if obj.source_file not in emitted:
                emitted.add(obj.source_file)
                _emit_file(obj, note="new table — full file applied (sets ownership, constraints)")
        else:
            _plan_table(adapter, plan, obj, dialect)
    for obj in indexes:
        if obj.source_file not in fresh_files:  # co-located indexes ride along with the file
            _plan_create_if_missing(adapter, plan, obj)

    # Replaceable objects: re-applied wholesale, per source file (ADR 0002), one step per file
    # (raw content — SET search_path / ALTER … OWNER / comments / overloads preserved), ordered
    # by dependency (default) or filename (ADR 0003 order=filename).
    if repo.layout.order == "filename":
        ordered_replaceable = sorted(replaceable, key=lambda o: o.source_file.as_posix())
    else:
        ordered_replaceable = parsing.topological_order(replaceable)
    for obj in ordered_replaceable:
        if obj.source_file in emitted or obj.source_file in fresh_files:
            continue
        emitted.add(obj.source_file)
        _emit_file(obj)
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

    # type changes: column present on both sides, but the declared type differs. Never
    # auto-applied (a narrowing/incompatible change can truncate or fail) — flagged for review.
    for col in desired:
        actual_col = actual_by_key.get(col.key())
        if actual_col is None or not parsing.types_differ(col.type, actual_col.type, dialect=dialect):
            continue
        plan.steps.append(
            Step(
                title=f"modify column {obj.id}.{col.name} type → {col.type}",
                object_id=obj.id,
                kind=ObjectKind.TABLE,
                severity=Severity.DESTRUCTIVE,
                sql=adapter.modify_column_sql(obj.id, col),
                source_file=obj.source_file,
                note=f"type change {actual_col.type} → {col.type} — may truncate/convert data",
            )
        )
        plan.warnings.append(
            f"{obj.id}.{col.name}: type change {actual_col.type} → {col.type} is not "
            "auto-applied — review for data compatibility, then use --allow-destructive"
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
    schema_hint = repo.schema_for(path, sql)
    for obj in parsing.parse_file(sql, path, default_schema=schema_hint, dialect=dialect,
                                  type_from=repo.layout.type_from):
        plan.warnings.append(
            f"{obj.id}: source file deleted — DROP {obj.kind.value} not auto-applied"
        )
