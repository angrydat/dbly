"""End-to-end test against a live SQL Server (skipped unless reachable).

Runs only when ``mssql.connection.properties`` exists at the repo root, pymssql is
installed, and the instance is reachable. Creates throwaway objects in ``dbo`` (prefixed
``dbly_e2e_``) and cleans them up. Safe to run repeatedly.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dbly.config import load_profile

pytest.importorskip("pymssql")

PROFILE = Path(__file__).resolve().parents[1] / "mssql.connection.properties"
if not PROFILE.exists():
    pytest.skip("no mssql.connection.properties — skipping live test", allow_module_level=True)

from dbly.adapters import get_adapter  # noqa: E402
from dbly.model import Severity  # noqa: E402
from dbly.planner import build_plan  # noqa: E402
from dbly.repo import Repo  # noqa: E402

TBL = "dbly_e2e_kunde"
VW = "dbly_e2e_v"
_CLEANUP = (
    f"DROP VIEW IF EXISTS dbo.{VW};\n"
    f"DROP TABLE IF EXISTS dbo.{TBL};\n"
)


def _adapter():
    cfg = load_profile(PROFILE)
    if cfg.environment is None:
        cfg.environment = "sqlserver"
    return get_adapter(cfg)


@pytest.fixture()
def adapter():
    try:
        a = _adapter()
        with a.engine.connect():
            pass
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"SQL Server not reachable: {exc}")
    a.run_init_script(_CLEANUP)
    yield a
    a.run_init_script(_CLEANUP)
    a.dispose()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _commit(root: Path, msg: str) -> str:
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def test_mssql_bootstrap_and_additive_upgrade(tmp_path: Path, adapter):
    repo_root = tmp_path / "db"
    (repo_root / "dbo").mkdir(parents=True)
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "t@e.com")
    _git(repo_root, "config", "user.name", "t")

    (repo_root / "dbo" / f"{TBL}.tbl").write_text(
        f"CREATE TABLE dbo.{TBL} (id INT, name NVARCHAR(100));", encoding="utf-8"
    )
    (repo_root / "dbo" / f"{VW}.vw").write_text(
        f"CREATE VIEW dbo.{VW} AS SELECT id, name FROM dbo.{TBL};", encoding="utf-8"
    )
    ref1 = _commit(repo_root, "v1")
    repo = Repo(repo_root)

    # bootstrap
    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref1,
                      target="mssql", dialect="tsql")
    kinds = [s.kind.value for s in plan.steps]
    assert "table" in kinds and "view" in kinds
    assert kinds.index("table") < kinds.index("view")  # dependency order
    adapter.apply([s.sql for s in plan.steps])
    adapter.record_deploy(ref1, [])

    assert adapter.table_exists("dbo", TBL)
    assert adapter.get_deployed_ref() == ref1
    assert {c.name.lower() for c in adapter.get_columns("dbo", TBL)} == {"id", "name"}

    # additive upgrade: add a column
    (repo_root / "dbo" / f"{TBL}.tbl").write_text(
        f"CREATE TABLE dbo.{TBL} (id INT, name NVARCHAR(100), email NVARCHAR(200));",
        encoding="utf-8",
    )
    ref2 = _commit(repo_root, "v2")

    plan2 = build_plan(repo, adapter, from_ref=ref1, to_ref=ref2,
                       target="mssql", dialect="tsql")
    add = [s for s in plan2.steps if s.kind.value == "table"]
    assert len(add) == 1
    assert add[0].severity is Severity.ADDITIVE
    assert "ADD email" in add[0].sql and "ADD COLUMN" not in add[0].sql  # T-SQL form
    adapter.apply([s.sql for s in plan2.steps])

    assert "email" in {c.name.lower() for c in adapter.get_columns("dbo", TBL)}
