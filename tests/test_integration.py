"""End-to-end pipeline test against a real SQLite database: git → parse → plan → apply."""
from __future__ import annotations

import subprocess
from pathlib import Path

from dbly import initializer
from dbly.adapters.sqlite import SqliteAdapter
from dbly.config import ConnectionConfig
from dbly.drift import compute_drift
from dbly.model import ObjectKind, Severity
from dbly.planner import build_plan
from dbly.repo import Repo


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")


def _commit(root: Path, msg: str) -> str:
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def test_bootstrap_then_additive_upgrade(tmp_path: Path):
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)

    # v1: a table + a view on it (files at root → no schema, suits SQLite)
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, name TEXT);", encoding="utf-8"
    )
    (repo_root / "v_kunde.vw").write_text(
        "CREATE VIEW v_kunde AS SELECT id, name FROM kunde;", encoding="utf-8"
    )
    ref1 = _commit(repo_root, "v1")

    db = tmp_path / "target.db"
    cfg = ConnectionConfig(environment="sqlite", service=str(db))
    repo = Repo(repo_root)

    # bootstrap: no baseline → full apply
    adapter = SqliteAdapter(cfg)
    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref1,
                      target="sqlite", dialect="sqlite")
    assert {s.kind.value for s in plan.steps} == {"table", "view"}
    # table step must precede the view that depends on it
    kinds = [s.kind.value for s in plan.steps]
    assert kinds.index("table") < kinds.index("view")

    adapter.apply([s.sql for s in plan.steps])
    adapter.record_deploy(ref1, [])
    assert adapter.table_exists(None, "kunde")
    assert adapter.get_deployed_ref() == ref1

    # v2: add a column — additive, single ALTER
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, name TEXT, email TEXT);",
        encoding="utf-8",
    )
    ref2 = _commit(repo_root, "v2: add email")

    plan2 = build_plan(repo, adapter, from_ref=ref1, to_ref=ref2,
                       target="sqlite", dialect="sqlite")
    add_steps = [s for s in plan2.steps if s.kind.value == "table"]
    assert len(add_steps) == 1
    assert add_steps[0].severity is Severity.ADDITIVE
    assert "email" in add_steps[0].sql.lower()

    adapter.apply([s.sql for s in plan2.steps])
    cols = {c.name.lower() for c in adapter.get_columns(None, "kunde")}
    assert "email" in cols
    adapter.dispose()


def test_index_is_created_once_then_skipped(tmp_path: Path):
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, name TEXT);", encoding="utf-8"
    )
    (repo_root / "ix_kunde_name.sql").write_text(
        "CREATE INDEX ix_kunde_name ON kunde (name);", encoding="utf-8"
    )
    ref = _commit(repo_root, "v1")

    db = tmp_path / "idx.db"
    cfg = ConnectionConfig(environment="sqlite", service=str(db))
    repo = Repo(repo_root)
    adapter = SqliteAdapter(cfg)

    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref,
                      target="sqlite", dialect="sqlite")
    assert any(s.kind.value == "index" for s in plan.steps)  # index planned
    adapter.apply([s.sql for s in plan.steps])
    assert adapter.has_object(ObjectKind.INDEX, None, "ix_kunde_name")

    # re-plan against the same ref but with the live DB: index already exists → not replanned
    plan2 = build_plan(repo, adapter, from_ref=None, to_ref=ref,
                       target="sqlite", dialect="sqlite")
    assert not any(s.kind.value == "index" for s in plan2.steps)
    adapter.dispose()


def test_check_drift_against_live_db(tmp_path: Path):
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, name TEXT);", encoding="utf-8"
    )
    (repo_root / "v_kunde.vw").write_text(
        "CREATE VIEW v_kunde AS SELECT id, name FROM kunde;", encoding="utf-8"
    )
    ref = _commit(repo_root, "v1")

    db = tmp_path / "drift.db"
    cfg = ConnectionConfig(environment="sqlite", service=str(db))
    repo = Repo(repo_root)
    adapter = SqliteAdapter(cfg)

    # deploy → no drift
    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref, target="sqlite", dialect="sqlite")
    adapter.apply([s.sql for s in plan.steps])
    rep = compute_drift(repo, adapter, to_ref=ref, dialect="sqlite", include_orphans=True)
    assert rep.clean, (rep.missing, rep.columns, rep.orphaned, rep.definitions)

    # desired adds a column + a new object that isn't deployed → drift
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, name TEXT, email TEXT);", encoding="utf-8"
    )
    (repo_root / "ix_kunde_name.sql").write_text(
        "CREATE INDEX ix_kunde_name ON kunde (name);", encoding="utf-8"
    )
    ref2 = _commit(repo_root, "v2")
    rep2 = compute_drift(repo, adapter, to_ref=ref2, dialect="sqlite", include_orphans=True)
    assert not rep2.clean
    assert any(k is ObjectKind.INDEX for k, _ in rep2.missing)       # new index not deployed
    assert any(cd.added == ["email"] for cd in rep2.columns)         # new column
    adapter.dispose()


def _apply(adapter, plan, to_ref):
    """Mirror cli.apply's order: baseline records, migrations run, then object steps."""
    adapter.ensure_state_table()
    for mid in plan.baselined:
        adapter.record_migration(to_ref, mid)
    for m in plan.migrations:
        adapter.run_init_script(m.sql)
        adapter.record_migration(to_ref, m.id)
    if plan.steps:
        adapter.apply([s.sql for s in plan.steps])
    adapter.record_deploy(to_ref, [])


def test_explicit_migration_runs_on_upgrade_once(tmp_path: Path):
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, name TEXT);", encoding="utf-8"
    )
    ref1 = _commit(repo_root, "v1")

    db = tmp_path / "mig.db"
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(db)))
    repo = Repo(repo_root)
    _apply(adapter, build_plan(repo, adapter, from_ref=None, to_ref=ref1,
                               target="sqlite", dialect="sqlite"), ref1)

    # v2: rename name -> full_name via an explicit migration; canonical table reflects it
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, full_name TEXT);", encoding="utf-8"
    )
    (repo_root / "migrations").mkdir()
    (repo_root / "migrations" / "0001_rename.sql").write_text(
        "ALTER TABLE kunde RENAME COLUMN name TO full_name;", encoding="utf-8"
    )
    ref2 = _commit(repo_root, "v2")

    plan2 = build_plan(repo, adapter, from_ref=ref1, to_ref=ref2, target="sqlite", dialect="sqlite")
    assert [m.id for m in plan2.migrations] == ["0001_rename.sql"]   # pending, will run
    _apply(adapter, plan2, ref2)
    cols = {c.name.lower() for c in adapter.get_columns(None, "kunde")}
    assert "full_name" in cols and "name" not in cols                # rename happened
    assert "0001_rename.sql" in adapter.applied_migrations()

    # re-plan: migration already applied → not pending again
    plan3 = build_plan(repo, adapter, from_ref=ref1, to_ref=ref2, target="sqlite", dialect="sqlite")
    assert plan3.migrations == []
    adapter.dispose()


def test_bootstrap_baselines_migrations_without_running(tmp_path: Path):
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, full_name TEXT);", encoding="utf-8"
    )
    (repo_root / "migrations").mkdir()
    # a rename that would FAIL on a fresh DB (column `name` never existed)
    (repo_root / "migrations" / "0001_rename.sql").write_text(
        "ALTER TABLE kunde RENAME COLUMN name TO full_name;", encoding="utf-8"
    )
    ref = _commit(repo_root, "v1")

    db = tmp_path / "fresh.db"
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(db)))
    repo = Repo(repo_root)
    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref, target="sqlite", dialect="sqlite")
    assert plan.baselined == ["0001_rename.sql"] and plan.migrations == []
    _apply(adapter, plan, ref)  # must NOT run the rename (would error) — only record it

    cols = {c.name.lower() for c in adapter.get_columns(None, "kunde")}
    assert cols == {"id", "full_name"}                               # built from canonical CREATE
    assert "0001_rename.sql" in adapter.applied_migrations()         # recorded as baseline
    adapter.dispose()


def test_migration_files_excluded_from_objects(tmp_path: Path):
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text("CREATE TABLE kunde (id INTEGER);", encoding="utf-8")
    (repo_root / "migrations").mkdir()
    (repo_root / "migrations" / "0001_x.sql").write_text("ALTER TABLE kunde ADD c TEXT;",
                                                         encoding="utf-8")
    ref = _commit(repo_root, "v1")
    repo = Repo(repo_root)
    objs = [p.name for p in repo.list_files(ref)]
    assert "kunde.tbl" in objs and "0001_x.sql" not in objs          # migrations not objects
    assert [mid for mid, _ in repo.migration_files(ref)] == ["0001_x.sql"]


def test_init_runs_ordered_multistatement_scripts(tmp_path: Path):
    repo_root = tmp_path / "db"
    (repo_root / "init").mkdir(parents=True)
    # multi-statement script + ordering by filename prefix
    (repo_root / "init" / "01_schema.sql").write_text(
        "CREATE TABLE meta (k TEXT);\nINSERT INTO meta (k) VALUES ('init');", encoding="utf-8"
    )
    (repo_root / "init" / "02_more.sql").write_text(
        "CREATE TABLE audit (id INTEGER);", encoding="utf-8"
    )
    _init_repo(repo_root)  # discovery reads the working tree, but keep it a real repo

    scripts = initializer.discover_init_scripts(repo_root)
    assert [p.name for p in scripts] == ["01_schema.sql", "02_more.sql"]

    db = tmp_path / "init.db"
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(db)))
    for s in scripts:
        adapter.run_init_script(s.read_text(encoding="utf-8"))

    assert adapter.table_exists(None, "meta")
    assert adapter.table_exists(None, "audit")
    adapter.dispose()


def test_type_change_produces_destructive_modify_step(tmp_path: Path):
    from dbly.repo import WORKTREE  # noqa: F401 — import guard; used indirectly below
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, code VARCHAR(10));", encoding="utf-8"
    )
    ref1 = _commit(repo_root, "v1")

    db = tmp_path / "types.db"
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(db)))
    repo = Repo(repo_root)
    plan1 = build_plan(repo, adapter, from_ref=None, to_ref=ref1,
                       target="sqlite", dialect="sqlite")
    adapter.apply([s.sql for s in plan1.steps])
    adapter.record_deploy(ref1, [])

    # widen code VARCHAR(10) -> VARCHAR(20): a type change, must surface as a MODIFY step
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, code VARCHAR(20));", encoding="utf-8"
    )
    ref2 = _commit(repo_root, "v2")
    plan2 = build_plan(repo, adapter, from_ref=ref1, to_ref=ref2,
                       target="sqlite", dialect="sqlite")
    mod = [s for s in plan2.steps if "modify column" in s.title]
    assert len(mod) == 1
    assert mod[0].severity is Severity.DESTRUCTIVE            # never auto-applied
    assert "code" in mod[0].title
    assert "→" in mod[0].note and "10" in mod[0].note and "20" in mod[0].note
    adapter.dispose()


def test_type_change_no_false_positive_when_unchanged(tmp_path: Path):
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, code VARCHAR(10));", encoding="utf-8"
    )
    ref1 = _commit(repo_root, "v1")
    db = tmp_path / "stable.db"
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(db)))
    repo = Repo(repo_root)
    plan1 = build_plan(repo, adapter, from_ref=None, to_ref=ref1,
                       target="sqlite", dialect="sqlite")
    adapter.apply([s.sql for s in plan1.steps])
    adapter.record_deploy(ref1, [])
    # re-plan the same schema against the live DB → no phantom modify step
    plan2 = build_plan(repo, adapter, from_ref=None, to_ref=ref1,
                       target="sqlite", dialect="sqlite")
    assert not [s for s in plan2.steps if "modify column" in s.title]
    adapter.dispose()


def test_plan_against_worktree_sees_uncommitted_and_untracked(tmp_path: Path):
    from dbly.repo import WORKTREE
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, name TEXT);", encoding="utf-8"
    )
    ref1 = _commit(repo_root, "v1")

    db = tmp_path / "wt.db"
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(db)))
    repo = Repo(repo_root)
    adapter.apply([s.sql for s in build_plan(
        repo, adapter, from_ref=None, to_ref=ref1, target="sqlite", dialect="sqlite").steps])
    adapter.record_deploy(ref1, [])

    # edit an existing file (uncommitted) + add a brand-new untracked object file
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, name TEXT, email TEXT);", encoding="utf-8"
    )
    (repo_root / "v_kunde.vw").write_text(
        "CREATE VIEW v_kunde AS SELECT id FROM kunde;", encoding="utf-8"
    )
    plan = build_plan(repo, adapter, from_ref=ref1, to_ref=WORKTREE,
                      target="sqlite", dialect="sqlite")
    titles = " ".join(s.title for s in plan.steps)
    assert "email" in titles                       # uncommitted column edit is planned
    assert any(s.kind.value == "view" for s in plan.steps)  # untracked new object is planned
    adapter.dispose()


def test_repo_ref_names_decorates_tag_and_branch(tmp_path: Path):
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text("CREATE TABLE kunde (id INTEGER);", encoding="utf-8")
    sha = _commit(repo_root, "v1")
    _git(repo_root, "tag", "v0.1")
    repo = Repo(repo_root)
    names = repo.ref_names(sha)
    assert "v0.1" in names
    assert any(n in ("main", "master") for n in names)  # the current branch points here too


def test_object_root_scopes_discovery_and_schema(tmp_path: Path):
    """object_root: schema = first segment BELOW the root; files outside it are ignored."""
    from dbly import parsing
    repo_root = tmp_path / "db"
    (repo_root / "pgsql" / "schema" / "bas").mkdir(parents=True)
    (repo_root / "ora").mkdir(parents=True)
    _init_repo(repo_root)
    # unqualified CREATE (schema comes from the folder), under pgsql/schema/bas
    (repo_root / "pgsql" / "schema" / "bas" / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, name TEXT);", encoding="utf-8"
    )
    # an Oracle file outside object_root must NOT be discovered
    (repo_root / "ora" / "irrelevant.tbl").write_text(
        "CREATE TABLE junk (id NUMBER);", encoding="utf-8"
    )
    _commit(repo_root, "v1")

    repo = Repo(repo_root, object_root="pgsql/schema")
    files = repo.list_files("HEAD")
    assert files == [Path("pgsql/schema/bas/kunde.tbl")]          # ora/ excluded
    assert repo.schema_for(files[0]) == "bas"                      # segment below object_root

    # parse with that hint → object id carries the real schema, not "pgsql"
    sql = repo.read_at("HEAD", files[0])
    obj = parsing.parse_file(sql, files[0], default_schema=repo.schema_for(files[0]),
                             dialect="postgres")[0]
    assert str(obj.id) == "bas.kunde"


def test_extra_ignore_excludes_unparseable_files(tmp_path: Path):
    repo_root = tmp_path / "db"
    (repo_root / "schema" / "app").mkdir(parents=True)
    _init_repo(repo_root)
    (repo_root / "schema" / "app" / "good.tbl").write_text(
        "CREATE TABLE app.good (id INTEGER);", encoding="utf-8"
    )
    (repo_root / "schema" / "app" / "bad.vw").write_text(
        "CREATE VIEW app.bad AS SELECT 1;", encoding="utf-8"
    )
    _commit(repo_root, "v1")

    repo = Repo(repo_root, object_root="schema", extra_ignore=["schema/app/bad.vw"])
    files = repo.list_files("HEAD")
    assert Path("schema/app/good.tbl") in files
    assert Path("schema/app/bad.vw") not in files                 # ignored via extra_ignore


def test_select_schemas_and_paths_scope_discovery(tmp_path: Path):
    repo_root = tmp_path / "db"
    for sub in ("pgsql/schema/bas", "pgsql/schema/bas/domains", "pgsql/schema/gzp"):
        (repo_root / sub).mkdir(parents=True)
    _init_repo(repo_root)
    (repo_root / "pgsql/schema/bas/kunde.tbl").write_text("CREATE TABLE bas.kunde (id int);", encoding="utf-8")
    (repo_root / "pgsql/schema/bas/domains/d_typ.tbl").write_text("CREATE TABLE bas.d_typ (id int);", encoding="utf-8")
    (repo_root / "pgsql/schema/gzp/plan.tbl").write_text("CREATE TABLE gzp.plan (id int);", encoding="utf-8")
    _commit(repo_root, "v1")

    # --schema bas → only the bas subtree (incl. bas/domains), not gzp
    r_schema = Repo(repo_root, object_root="pgsql/schema", select_schemas=["bas"])
    got = {p.as_posix() for p in r_schema.list_files("HEAD")}
    assert got == {"pgsql/schema/bas/kunde.tbl", "pgsql/schema/bas/domains/d_typ.tbl"}

    # --schema is case-insensitive
    assert {p.as_posix() for p in Repo(repo_root, object_root="pgsql/schema",
            select_schemas=["BAS"]).list_files("HEAD")} == got

    # --path bas/domains → only that subpath
    r_path = Repo(repo_root, object_root="pgsql/schema", select_paths=["bas/domains"])
    assert {p.as_posix() for p in r_path.list_files("HEAD")} == {"pgsql/schema/bas/domains/d_typ.tbl"}

    # no selection → everything under object_root
    assert len(Repo(repo_root, object_root="pgsql/schema").list_files("HEAD")) == 3


def test_export_roundtrip_and_cross_dialect(tmp_path: Path):
    from dbly.export import export_ddl
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text(
        "CREATE TABLE IF NOT EXISTS kunde (id INTEGER, name TEXT NOT NULL);", encoding="utf-8"
    )
    (repo_root / "v_kunde.vw").write_text(
        "CREATE VIEW v_kunde AS SELECT id, name FROM kunde;", encoding="utf-8"
    )
    ref = _commit(repo_root, "v1")
    db = tmp_path / "exp.db"
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(db)))
    repo = Repo(repo_root)
    adapter.apply([s.sql for s in build_plan(repo, adapter, from_ref=None, to_ref=ref,
                   target="sqlite", dialect="sqlite").steps])

    # same-dialect export: table + view present, table before view
    res = export_ddl(adapter, source_dialect="sqlite")
    assert res.object_count == 2
    assert "CREATE TABLE" in res.ddl and "kunde" in res.ddl
    assert res.ddl.index("kunde") < res.ddl.index("v_kunde")     # dependency order
    assert "NOT NULL" in res.ddl                                  # SQLite keeps its stored DDL

    # cross-dialect export to postgres: structural objects transpile, no crash
    res_pg = export_ddl(adapter, source_dialect="sqlite", target_dialect="postgres")
    assert res_pg.object_count == 2
    assert "CREATE TABLE" in res_pg.ddl
    assert any("transpiling sqlite → postgres" in w for w in res_pg.warnings)

    # dbly_state ledger is never exported
    adapter.ensure_state_table()
    assert "dbly_state" not in export_ddl(adapter, source_dialect="sqlite").ddl
    adapter.dispose()


def test_view_drift_only_when_body_actually_differs(tmp_path: Path):
    """Regression: identical views must NOT report drift (old code hashed CREATE-VIEW vs SELECT)."""
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text("CREATE TABLE kunde (id INTEGER, name TEXT);", encoding="utf-8")
    (repo_root / "v_kunde.vw").write_text(
        "CREATE VIEW v_kunde AS SELECT id, name FROM kunde;", encoding="utf-8")
    ref1 = _commit(repo_root, "v1")
    db = tmp_path / "vd.db"
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(db)))
    repo = Repo(repo_root)
    adapter.apply([s.sql for s in build_plan(repo, adapter, from_ref=None, to_ref=ref1,
                   target="sqlite", dialect="sqlite").steps])

    # unchanged view → no definition drift
    rep = compute_drift(repo, adapter, to_ref=ref1, dialect="sqlite")
    assert not rep.definitions, [str(o) for _, o in rep.definitions]

    # genuinely changed view body → exactly one definition drift
    (repo_root / "v_kunde.vw").write_text(
        "CREATE VIEW v_kunde AS SELECT id, name FROM kunde WHERE id > 0;", encoding="utf-8")
    ref2 = _commit(repo_root, "v2")
    rep2 = compute_drift(repo, adapter, to_ref=ref2, dialect="sqlite")
    assert [o.name for _, o in rep2.definitions] == ["v_kunde"]
    adapter.dispose()


def test_check_show_diff_reveals_real_view_change(tmp_path: Path):
    from dbly.drift import compute_drift
    from dbly.report import render_drift
    from rich.console import Console
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text("CREATE TABLE kunde (id INTEGER, name TEXT);", encoding="utf-8")
    (repo_root / "v_kunde.vw").write_text(
        "CREATE VIEW v_kunde AS SELECT id, name FROM kunde;", encoding="utf-8")
    ref1 = _commit(repo_root, "v1")
    db = tmp_path / "sd.db"
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(db)))
    repo = Repo(repo_root)
    adapter.apply([s.sql for s in build_plan(repo, adapter, from_ref=None, to_ref=ref1,
                   target="sqlite", dialect="sqlite").steps])
    # change the view body
    (repo_root / "v_kunde.vw").write_text(
        "CREATE VIEW v_kunde AS SELECT id, name FROM kunde WHERE id > 0;", encoding="utf-8")
    ref2 = _commit(repo_root, "v2")

    rep = compute_drift(repo, adapter, to_ref=ref2, dialect="sqlite", include_diff=True)
    assert f"view:{list(rep.diffs)[0].split(':',1)[1]}" in rep.diffs  # a diff was captured
    import io
    con = Console(file=io.StringIO(), force_terminal=False, width=200)
    render_drift(rep, con, target="dev", ref=ref2, show_diff=True, dialect="sqlite")
    out = con.file.getvalue()
    assert "WHERE" in out and ("+" in out)   # the added filter shows in the diff
    adapter.dispose()


def test_grants_are_apply_only_not_drift(tmp_path: Path):
    from dbly.drift import compute_drift
    from dbly.model import ObjectKind
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text("CREATE TABLE kunde (id INTEGER);", encoding="utf-8")
    (repo_root / "grants.sql").write_text(
        "GRANT SELECT ON kunde TO reader;\nGRANT INSERT ON kunde TO writer;", encoding="utf-8")
    ref = _commit(repo_root, "v1")
    db = tmp_path / "g.db"
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(db)))
    repo = Repo(repo_root)
    # deploy the table only (grants aren't introspectable in sqlite anyway)
    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref, target="sqlite", dialect="sqlite")
    adapter.apply([s.sql for s in plan.steps if s.kind is not ObjectKind.GRANT])
    adapter.record_deploy(ref, [])

    rep = compute_drift(repo, adapter, to_ref=ref, dialect="sqlite")
    # grants must NOT show as missing drift, but must be surfaced as apply-only
    assert not any(k is ObjectKind.GRANT for k, _ in rep.missing)
    assert any(k is ObjectKind.GRANT for k, _ in rep.apply_only)
    assert rep.clean                      # apply-only never makes the check dirty
    adapter.dispose()


def test_plan_creates_missing_schema_first(tmp_path: Path, monkeypatch):
    """On a greenfield target, the schema of a managed object is created before the object."""
    repo_root = tmp_path / "db"
    (repo_root / "download").mkdir(parents=True)
    _init_repo(repo_root)
    (repo_root / "download" / "cache.tbl").write_text(
        "CREATE TABLE download.cache (id INTEGER);", encoding="utf-8")
    ref = _commit(repo_root, "v1")
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(tmp_path / "s.db")))
    # simulate a schema-managing engine (like Postgres) on top of the sqlite test backend
    monkeypatch.setattr(adapter, "ensure_schema_sql", lambda s: f'CREATE SCHEMA IF NOT EXISTS "{s}";')
    monkeypatch.setattr(adapter, "schema_exists", lambda s: False)
    monkeypatch.setattr(adapter, "table_exists", lambda schema, name: False)
    repo = Repo(repo_root)
    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref, target="sqlite", dialect="sqlite")
    titles = [s.title for s in plan.steps]
    assert "create schema download" in titles
    assert titles.index("create schema download") < next(
        i for i, t in enumerate(titles) if "cache" in t)          # schema before the table
    adapter.dispose()


def test_tables_ordered_by_fk_dependency(tmp_path: Path):
    """A table with an inline FK is created after the table it references (fresh target)."""
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    # file order puts the dependent (cache) first — the planner must reorder
    (repo_root / "cache.tbl").write_text(
        "CREATE TABLE cache (id INTEGER PRIMARY KEY, "
        "paket_id INTEGER NOT NULL REFERENCES paket(paket_id));", encoding="utf-8")
    (repo_root / "paket.tbl").write_text(
        "CREATE TABLE paket (paket_id INTEGER PRIMARY KEY);", encoding="utf-8")
    ref = _commit(repo_root, "v1")
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(tmp_path / "fk.db")))
    repo = Repo(repo_root)
    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref, target="sqlite", dialect="sqlite")
    # fresh tables are applied per-file (ADR 0002); the FK target's file comes first
    order = [s.title for s in plan.steps if s.kind.value == "table"]
    assert order.index("apply table paket") < order.index("apply table cache")
    # and it actually applies cleanly (FK target exists first)
    for s in plan.steps:
        adapter.run_init_script(s.sql) if s.script else adapter.apply([s.sql])
    assert adapter.table_exists(None, "cache") and adapter.table_exists(None, "paket")
    adapter.dispose()


def test_with_deps_pulls_only_missing_cross_schema_dependency(tmp_path: Path):
    """--with-deps on a scoped deploy pulls a referenced object from another schema — but only
    if it's missing — without dragging in that schema's other objects."""
    repo_root = tmp_path / "db"
    for d in ("schema/app", "schema/shared"):
        (repo_root / d).mkdir(parents=True)
    _init_repo(repo_root)
    (repo_root / "schema/app/orders.tbl").write_text(
        "CREATE TABLE app.orders (id INTEGER PRIMARY KEY, "
        "cust_id INTEGER REFERENCES shared.customer(id));", encoding="utf-8")
    (repo_root / "schema/shared/customer.tbl").write_text(
        "CREATE TABLE shared.customer (id INTEGER PRIMARY KEY);", encoding="utf-8")
    (repo_root / "schema/shared/unrelated.tbl").write_text(
        "CREATE TABLE shared.unrelated (id INTEGER);", encoding="utf-8")  # must NOT be pulled
    ref = _commit(repo_root, "v1")
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(tmp_path / "d.db")))
    repo = Repo(repo_root, object_root="schema", select_schemas=["app"])
    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref,
                      target="sqlite", dialect="sqlite", with_deps=True)
    tables = {s.title for s in plan.steps if s.kind.value == "table"}
    assert "apply table app.orders" in tables               # fresh table → per-file apply
    assert "apply table shared.customer" in tables          # missing dep pulled
    assert "apply table shared.unrelated" not in tables     # unrelated sibling NOT pulled
    # and customer is ordered before orders (FK-safe)
    order = [s.title for s in plan.steps if s.kind.value == "table"]
    assert order.index("apply table shared.customer") < order.index("apply table app.orders")
    adapter.dispose()


def test_baseline_records_ref_and_migrations_without_running_sql(tmp_path: Path):
    repo_root = tmp_path / "db"
    (repo_root / "migrations").mkdir(parents=True)
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text("CREATE TABLE kunde (id INTEGER);", encoding="utf-8")
    (repo_root / "migrations" / "0001_x.sql").write_text("ALTER TABLE kunde ADD y INTEGER;", encoding="utf-8")
    ref = _commit(repo_root, "v1")
    db = tmp_path / "b.db"
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(db)))
    repo = Repo(repo_root)
    # brownfield: nothing deployed via dbly yet
    assert adapter.get_deployed_ref() is None

    # mirror the baseline command: record ref + mark migrations applied, run no object SQL
    adapter.ensure_state_table()
    for mid, _ in repo.migration_files(ref):
        if mid not in adapter.applied_migrations():
            adapter.record_migration(ref, mid)
    adapter.record_deploy(ref, [])

    assert adapter.get_deployed_ref() == ref                 # ref now recorded
    assert "0001_x.sql" in adapter.applied_migrations()      # migration marked applied, not run
    assert not adapter.table_exists(None, "kunde")           # NO object SQL ran — table absent
    adapter.dispose()


def test_replaceable_applied_per_file_verbatim(tmp_path: Path):
    """A replaceable file with several statements is one script step, applied whole & in order."""
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text("CREATE TABLE kunde (id INTEGER, name TEXT);", encoding="utf-8")
    (repo_root / "views.vw").write_text(
        "-- two views, second depends on first\n"
        "CREATE VIEW v_a AS SELECT id, name FROM kunde;\n"
        "CREATE VIEW v_b AS SELECT name FROM v_a WHERE name IS NOT NULL;\n", encoding="utf-8")
    ref = _commit(repo_root, "v1")
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(tmp_path / "s.db")))
    repo = Repo(repo_root)
    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref, target="sqlite", dialect="sqlite")

    # the multi-view file is one script step carrying the whole file verbatim
    views_step = next(s for s in plan.steps if s.script and s.source_file.name == "views.vw")
    assert "CREATE VIEW v_a" in views_step.sql and "CREATE VIEW v_b" in views_step.sql
    assert "-- two views" in views_step.sql                    # comment preserved (verbatim file)

    # apply the way the CLI does: statement steps transactionally, script steps via run_init_script
    for s in plan.steps:
        adapter.run_init_script(s.sql) if s.script else adapter.apply([s.sql])
    views = {r for r in ("v_a", "v_b")}
    assert all(adapter.has_object(ObjectKind.VIEW, None, v) for v in views)  # both created, in order
    adapter.dispose()


def test_layout_database_filter_and_filename_order(tmp_path: Path):
    from dbly.project import LayoutConfig
    repo_root = tmp_path / "db"
    for d in ("appdb/sales", "otherdb/sales"):
        (repo_root / d).mkdir(parents=True)
    _init_repo(repo_root)
    # <database>/<schema>/<object> layout
    (repo_root / "appdb/sales/customer.tbl").write_text("CREATE TABLE sales.customer (id INTEGER);", encoding="utf-8")
    (repo_root / "otherdb/sales/thing.tbl").write_text("CREATE TABLE sales.thing (id INTEGER);", encoding="utf-8")
    # two replaceable views whose filenames encode order (02 depends on nothing; test order only)
    (repo_root / "appdb/sales/02_second.vw").write_text("CREATE VIEW sales.v2 AS SELECT 1 AS x;", encoding="utf-8")
    (repo_root / "appdb/sales/01_first.vw").write_text("CREATE VIEW sales.v1 AS SELECT 1 AS x;", encoding="utf-8")
    ref = _commit(repo_root, "v1")

    lay = LayoutConfig(schema_depth=2, database_depth=1, order="filename")
    # database filter: only appdb's files, otherdb excluded
    repo = Repo(repo_root, layout=lay, target_database="appdb")
    files = {p.as_posix() for p in repo.list_files(ref)}
    assert any("appdb/sales/customer.tbl" in f for f in files)
    assert not any("otherdb/" in f for f in files)             # other database filtered out
    assert repo.schema_for(Path("appdb/sales/customer.tbl")) == "sales"  # schema at depth 2

    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(tmp_path / "l.db")))
    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref, target="sqlite", dialect="sqlite")
    view_titles = [s.title for s in plan.steps if s.kind.value == "view"]
    assert view_titles.index(next(t for t in view_titles if "v1" in t)) < \
           view_titles.index(next(t for t in view_titles if "v2" in t))  # 01_ before 02_
    adapter.dispose()


def test_plan_warns_on_unrecognized_changed_file(tmp_path: Path):
    """A changed object file the parser can't identify must warn, never vanish silently."""
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _init_repo(repo_root)
    (repo_root / "kunde.tbl").write_text("CREATE TABLE kunde (id INTEGER);", encoding="utf-8")
    (repo_root / "mystery.fnc").write_text("-- just a comment, no recognizable object\n", encoding="utf-8")
    ref = _commit(repo_root, "v1")
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(tmp_path / "w.db")))
    plan = build_plan(Repo(repo_root), adapter, from_ref=None, to_ref=ref,
                      target="sqlite", dialect="sqlite")
    assert any("mystery.fnc" in w and "no deployable object" in w for w in plan.warnings)
    adapter.dispose()


def test_fresh_table_emits_full_file_step_for_ownership(tmp_path: Path):
    """A newly-created table is applied as its whole file (so ALTER … OWNER / co-located
    indexes run), as one script step — not a dbly-generated bare CREATE."""
    repo_root = tmp_path / "db"
    (repo_root / "app").mkdir(parents=True)
    _init_repo(repo_root)
    (repo_root / "app" / "thing.tbl").write_text(
        "CREATE TABLE app.thing (id INTEGER);\n"
        "CREATE INDEX ix_thing ON app.thing (id);\n"
        "ALTER TABLE app.thing OWNER TO appown;\n", encoding="utf-8")
    ref = _commit(repo_root, "v1")
    adapter = SqliteAdapter(ConnectionConfig(environment="sqlite", service=str(tmp_path / "o.db")))
    plan = build_plan(Repo(repo_root), adapter, from_ref=None, to_ref=ref,
                      target="sqlite", dialect="sqlite")
    table_steps = [s for s in plan.steps if s.kind.value == "table"]
    assert len(table_steps) == 1 and table_steps[0].script                # one per-file step
    assert "OWNER TO appown" in table_steps[0].sql                        # ownership DDL carried
    # the co-located index is NOT emitted as a separate step (the file creates it)
    assert not [s for s in plan.steps if s.kind.value == "index"]
    adapter.dispose()
