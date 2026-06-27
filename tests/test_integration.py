"""End-to-end pipeline test against a real SQLite database: git → parse → plan → apply."""
from __future__ import annotations

import subprocess
from pathlib import Path

from dbly import initializer
from dbly.adapters.sqlite import SqliteAdapter
from dbly.config import ConnectionConfig
from dbly.model import Severity
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
