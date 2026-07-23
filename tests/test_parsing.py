"""DB-less tests for the parsing/planning core."""
from __future__ import annotations

from pathlib import Path

from dbly import parsing, report
from dbly.model import ObjectClass, ObjectKind, Plan, Severity, Step


def test_classify_view_and_table():
    sql = "CREATE OR REPLACE VIEW app.v_kunde AS SELECT * FROM app.kunde;"
    objs = parsing.parse_file(sql, Path("app/v_kunde.vw"), dialect="postgres")
    assert len(objs) == 1
    assert objs[0].kind is ObjectKind.VIEW
    assert objs[0].object_class is ObjectClass.REPLACEABLE
    assert objs[0].id.schema == "app"
    assert "app.kunde" in objs[0].depends_on


def test_table_is_stateful():
    sql = "CREATE TABLE IF NOT EXISTS app.kunde (id int, name text);"
    obj = parsing.parse_file(sql, Path("app/kunde.tbl"), dialect="postgres")[0]
    assert obj.kind is ObjectKind.TABLE
    assert obj.object_class is ObjectClass.STATEFUL


def test_index_identity_and_classification():
    sql = "CREATE INDEX ix_kunde_name ON sales.kunde (name);"
    obj = parsing.parse_file(sql, Path("sales/ix_kunde_name.sql"), dialect="postgres")[0]
    assert obj.kind is ObjectKind.INDEX
    assert obj.object_class is ObjectClass.STATEFUL          # not blindly re-applied
    assert obj.id.name == "ix_kunde_name"                    # index name, not the table
    assert obj.id.schema == "sales"                          # follows the indexed table
    assert "sales.kunde" in obj.depends_on                   # depends on the indexed table


def test_sequence_identity_and_classification():
    obj = parsing.parse_file(
        "CREATE SEQUENCE sales.order_seq START 1;", Path("sales/order_seq.sql"),
        dialect="postgres",
    )[0]
    assert obj.kind is ObjectKind.SEQUENCE
    assert obj.object_class is ObjectClass.STATEFUL
    assert obj.id.name == "order_seq"
    assert obj.id.schema == "sales"


def test_default_schema_hint_applies_when_unqualified():
    sql = "CREATE VIEW v_x AS SELECT 1;"
    obj = parsing.parse_file(sql, Path("app/v_x.vw"), default_schema="app", dialect="postgres")[0]
    assert obj.id.schema == "app"


def test_desired_columns():
    sql = (
        "CREATE TABLE IF NOT EXISTS app.kunde ("
        "id int NOT NULL, name text, created timestamptz DEFAULT now());"
    )
    cols = parsing.desired_columns(sql, dialect="postgres")
    by = {c.name.lower(): c for c in cols}
    assert by["id"].nullable is False
    assert by["name"].nullable is True
    assert by["created"].default is not None


def test_topological_order_dependencies_first():
    a = parsing.parse_file(
        "CREATE VIEW app.a AS SELECT * FROM app.b;", Path("a.vw"), dialect="postgres"
    )[0]
    b = parsing.parse_file(
        "CREATE VIEW app.b AS SELECT 1;", Path("b.vw"), dialect="postgres"
    )[0]
    ordered = parsing.topological_order([a, b])
    names = [o.id.name for o in ordered]
    assert names.index("b") < names.index("a")


def test_plan_to_sql_is_executable_script():
    plan = Plan(target="prod", from_ref="abc123", to_ref="def456")
    plan.steps.append(
        Step(
            title="add column app.kunde.email",
            object_id=None,
            kind=ObjectKind.TABLE,
            severity=Severity.ADDITIVE,
            sql="ALTER TABLE app.kunde ADD COLUMN email text",  # no trailing ;
        )
    )
    plan.steps.append(
        Step(
            title="drop column app.kunde.legacy",
            object_id=None,
            kind=ObjectKind.TABLE,
            severity=Severity.DESTRUCTIVE,
            sql="ALTER TABLE app.kunde DROP COLUMN legacy;",
        )
    )
    script = report.plan_to_sql(
        plan,
        state_ddl="CREATE TABLE IF NOT EXISTS dbly_state (deployed_sha text);",
        record_sql="INSERT INTO dbly_state (deployed_sha) VALUES ('def456');",
    )
    assert "ALTER TABLE app.kunde ADD COLUMN email text;" in script  # ; appended
    assert "!! DESTRUCTIVE" in script
    assert "dbly_state" in script
    assert "VALUES ('def456')" in script
    assert "def456" in script and "abc123" in script  # header refs


def test_plan_yaml_roundtrip():
    plan = Plan(target="t", from_ref="abc", to_ref="HEAD")
    plan.steps.append(
        Step(
            title="add column app.kunde.email",
            object_id=None,
            kind=ObjectKind.TABLE,
            severity=Severity.ADDITIVE,
            sql="ALTER TABLE app.kunde ADD COLUMN email text;",
        )
    )
    plan.warnings.append("something")
    text = report.plan_to_yaml(plan)
    back = report.plan_from_yaml(text)
    assert back.to_ref == "HEAD"
    assert back.steps[0].severity is Severity.ADDITIVE
    assert back.warnings == ["something"]


def test_canonical_type_normalizes_numeric_scale_and_collation():
    # omitted scale ≡ scale 0 (Oracle NUMBER(p) == NUMBER(p,0)); case/space-insensitive
    assert parsing.canonical_type("NUMBER(10)") == parsing.canonical_type("number(10, 0)")
    # a genuine precision change is preserved (bug: NUMBER → NUMBER(10) was missed)
    assert parsing.types_differ("NUMBER", "NUMBER(10)")
    assert parsing.types_differ("NUMBER(10)", "NUMBER(12)")
    # SQL Server appends a COLLATE clause on reflected string types — not a real change
    assert not parsing.types_differ("NVARCHAR(100)", 'NVARCHAR(100) COLLATE "SQL_Latin1_CI_AS"')
    assert not parsing.types_differ("VARCHAR2(200)", "varchar2(200)")


def test_render_plan_shows_step_note(capsys):
    from rich.console import Console
    plan = Plan(target="t", from_ref="abc", to_ref="HEAD")
    plan.steps.append(
        Step(
            title="add NOT NULL column app.kunde.flag",
            object_id=None,
            kind=ObjectKind.TABLE,
            severity=Severity.DESTRUCTIVE,
            sql="ALTER TABLE app.kunde ADD flag NUMBER(1) NOT NULL;",
            note="NOT NULL without default on existing table — unsafe",
        )
    )
    report.render_plan(plan, Console(force_terminal=False, width=200))
    out = capsys.readouterr().out
    assert "NOT NULL without default" in out  # the hint is surfaced, not just stored


def test_decorate_ref_git_style():
    assert report._decorate_ref(None, None) == "∅"
    assert report._decorate_ref("WORKTREE", None) == "working tree"
    assert report._decorate_ref("9ff5e440abc", {"9ff5e440abc": "v0.1, main"}) == "v0.1, main (9ff5e440)"
    assert report._decorate_ref("deadbeef", None) == "deadbeef"  # no names → raw sha
