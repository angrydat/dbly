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
