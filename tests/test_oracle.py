"""Oracle adapter — DB-less tests for the dialect-specific logic.

The engine is built lazily, so the SQL builders and the terminator/split helpers are
testable without oracledb or a live instance. End-to-end needs a reachable Oracle DB.
"""
from __future__ import annotations

from dbly.adapters.oracle import (
    OracleAdapter,
    split_oracle_script,
    strip_terminator,
)
from dbly.config import ConnectionConfig
from dbly.model import Column, ObjectId


def _adapter() -> OracleAdapter:
    return OracleAdapter(ConnectionConfig(environment="oracle", service="host:1521/orcl"))


def test_add_column_default_before_not_null():
    sql = _adapter().add_column_sql(
        ObjectId("APP", "KUNDE"), Column("flag", "NUMBER(1)", nullable=False, default="0")
    )
    # Oracle requires DEFAULT before NOT NULL, and uses ADD (no COLUMN)
    assert sql == "ALTER TABLE APP.KUNDE ADD flag NUMBER(1) DEFAULT 0 NOT NULL;"
    assert "ADD COLUMN" not in sql


def test_add_column_nullable():
    sql = _adapter().add_column_sql(ObjectId("APP", "KUNDE"), Column("email", "VARCHAR2(200)"))
    assert sql == "ALTER TABLE APP.KUNDE ADD email VARCHAR2(200);"


def test_strip_terminator_plain_sql():
    assert strip_terminator("ALTER TABLE t ADD x NUMBER;") == "ALTER TABLE t ADD x NUMBER"


def test_strip_terminator_keeps_plsql_block():
    block = "CREATE OR REPLACE PROCEDURE p AS BEGIN NULL; END;"
    assert strip_terminator(block) == block  # END; must survive


def test_split_oracle_script_mixes_sql_and_plsql():
    script = (
        "CREATE TABLE a (id NUMBER);\n"
        "INSERT INTO a VALUES (1);\n"
        "/\n"
        "BEGIN\n  NULL;\nEND;\n/\n"
    )
    parts = split_oracle_script(script)
    assert parts[0] == "CREATE TABLE a (id NUMBER)"
    assert parts[1] == "INSERT INTO a VALUES (1)"
    assert parts[2].startswith("BEGIN") and parts[2].rstrip().endswith("END;")


def test_state_ddl_guarded_and_slash_terminated():
    ddl = _adapter().state_table_ddl()
    assert "EXECUTE IMMEDIATE" in ddl
    assert "-955" in ddl
    assert ddl.rstrip().endswith("/")


def test_record_deploy_sql_escapes_quotes():
    assert _adapter().record_deploy_sql("a'b") == (
        "INSERT INTO dbly_state (deployed_sha) VALUES ('a''b');"
    )
