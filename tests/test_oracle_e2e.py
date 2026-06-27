"""End-to-end test against a live Oracle database (skipped unless reachable).

SAFETY — this targets an *active development database*. The test:

* uses uniquely-named throwaway objects (``DBLY_E2E_*``) plus the ``DBLY_STATE`` ledger;
* runs a **pre-flight guard**: if any of those names already exists it SKIPS and touches
  nothing (so it can never drop a pre-existing object);
* in teardown drops **only** the objects it created this run (guarded, ignores ORA-00942).

Runs only when ``ora.connection.properties`` exists at the repo root, oracledb is installed,
and the instance is reachable. The profile is gitignored, so CI never has it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text

from dbly.config import load_profile

pytest.importorskip("oracledb")

PROFILE = Path(__file__).resolve().parents[1] / "ora.connection.properties"
if not PROFILE.exists():
    pytest.skip("no ora.connection.properties — skipping live test", allow_module_level=True)

from dbly.adapters import get_adapter  # noqa: E402
from dbly.model import Severity  # noqa: E402
from dbly.planner import build_plan  # noqa: E402
from dbly.repo import Repo  # noqa: E402

TBL = "DBLY_E2E_KUNDE"
VW = "DBLY_E2E_V"
GUARD_NAMES = (TBL, VW, "DBLY_STATE")


def _drop(obj_type: str, name: str) -> str:
    # guarded drop (ignore ORA-00942). PURGE on tables so nothing lingers in the recyclebin
    # (an Oracle DROP TABLE without PURGE also leaves the identity ISEQ$$ sequence behind).
    purge = " PURGE" if obj_type == "TABLE" else ""
    return (
        f"BEGIN EXECUTE IMMEDIATE 'DROP {obj_type} {name}{purge}'; "
        f"EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;\n/\n"
    )


_CLEANUP = _drop("VIEW", VW) + _drop("TABLE", TBL) + _drop("TABLE", "DBLY_STATE")


def _adapter():
    cfg = load_profile(PROFILE)
    if cfg.environment is None:
        cfg.environment = "oracle"
    return get_adapter(cfg)


@pytest.fixture()
def adapter():
    try:
        a = _adapter()
        with a.engine.connect() as c:
            # pre-flight: refuse to run if any target name already exists
            for n in GUARD_NAMES:
                t = c.execute(
                    text("SELECT COUNT(*) FROM user_tables WHERE table_name=:n"), {"n": n}
                ).scalar()
                v = c.execute(
                    text("SELECT COUNT(*) FROM user_views WHERE view_name=:n"), {"n": n}
                ).scalar()
                if t or v:
                    pytest.skip(f"refusing to run: {n} already exists on the target")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Oracle not reachable: {exc}")
    yield a
    a.run_init_script(_CLEANUP)  # drops only the objects created this run
    a.dispose()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _commit(root: Path, msg: str) -> str:
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def test_oracle_bootstrap_and_additive_upgrade(tmp_path: Path, adapter):
    repo_root = tmp_path / "db"
    repo_root.mkdir()
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "t@e.com")
    _git(repo_root, "config", "user.name", "t")

    # files at root → no schema qualifier → objects land in the connecting user's schema
    (repo_root / f"{TBL}.tbl").write_text(
        f"CREATE TABLE {TBL} (id NUMBER, name VARCHAR2(100))", encoding="utf-8"
    )
    (repo_root / f"{VW}.vw").write_text(
        f"CREATE VIEW {VW} AS SELECT id, name FROM {TBL}", encoding="utf-8"
    )
    ref1 = _commit(repo_root, "v1")
    repo = Repo(repo_root)

    # bootstrap
    plan = build_plan(repo, adapter, from_ref=None, to_ref=ref1,
                      target="oracle", dialect="oracle")
    kinds = [s.kind.value for s in plan.steps]
    assert kinds.index("table") < kinds.index("view")  # dependency order
    adapter.apply([s.sql for s in plan.steps])
    adapter.record_deploy(ref1, [])

    assert adapter.table_exists(None, TBL)
    assert adapter.get_deployed_ref() == ref1
    assert {c.name.lower() for c in adapter.get_columns(None, TBL)} == {"id", "name"}

    # additive upgrade
    (repo_root / f"{TBL}.tbl").write_text(
        f"CREATE TABLE {TBL} (id NUMBER, name VARCHAR2(100), email VARCHAR2(200))",
        encoding="utf-8",
    )
    ref2 = _commit(repo_root, "v2")

    plan2 = build_plan(repo, adapter, from_ref=ref1, to_ref=ref2,
                       target="oracle", dialect="oracle")
    add = [s for s in plan2.steps if s.kind.value == "table"]
    assert len(add) == 1
    assert add[0].severity is Severity.ADDITIVE
    assert "ADD email" in add[0].sql and "ADD COLUMN" not in add[0].sql
    adapter.apply([s.sql for s in plan2.steps])

    assert "email" in {c.name.lower() for c in adapter.get_columns(None, TBL)}
