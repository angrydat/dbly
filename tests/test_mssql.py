"""SQL Server adapter — DB-less tests for the T-SQL string builders.

These exercise the dialect-specific SQL generation without a live server (the engine is
built lazily, so no pymssql/connection is needed here). End-to-end verification needs a
reachable SQL Server instance.
"""
from __future__ import annotations

from dbly.adapters.mssql import _GO_RE, MssqlAdapter
from dbly.config import ConnectionConfig
from dbly.model import Column, ObjectId


def _adapter() -> MssqlAdapter:
    return MssqlAdapter(ConnectionConfig(environment="sqlserver", service="host:1433"))


def test_add_column_uses_tsql_add_not_add_column():
    sql = _adapter().add_column_sql(ObjectId("dbo", "kunde"), Column("email", "NVARCHAR(100)"))
    assert sql == "ALTER TABLE dbo.kunde ADD email NVARCHAR(100);"
    assert "ADD COLUMN" not in sql  # the Postgres/SQLite form must NOT appear


def test_add_column_not_null_with_default():
    sql = _adapter().add_column_sql(
        ObjectId("dbo", "kunde"), Column("flag", "BIT", nullable=False, default="0")
    )
    assert sql == "ALTER TABLE dbo.kunde ADD flag BIT NOT NULL DEFAULT 0;"


def test_state_table_ddl_is_guarded():
    ddl = _adapter().state_table_ddl()
    assert "IF NOT EXISTS" in ddl
    assert "CREATE TABLE dbly_state" in ddl


def test_record_deploy_sql_escapes_quotes():
    sql = _adapter().record_deploy_sql("a'b")
    assert sql == "INSERT INTO dbly_state (deployed_sha) VALUES ('a''b');"


def test_go_batch_split():
    script = "CREATE TABLE a (id INT);\nGO\nCREATE PROCEDURE p AS SELECT 1;\nGO\n"
    batches = [b for b in _GO_RE.split(script) if b.strip()]
    assert len(batches) == 2
    assert "CREATE TABLE a" in batches[0]
    assert "CREATE PROCEDURE p" in batches[1]
