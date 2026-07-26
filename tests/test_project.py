"""Tests for dbly.toml project configuration and object_root scoping."""
from __future__ import annotations

from pathlib import Path

from dbly.project import ProjectConfig, load_project


def test_load_project_absent_returns_defaults(tmp_path: Path):
    cfg = load_project(tmp_path)
    assert cfg == ProjectConfig()
    assert cfg.object_root == "." and cfg.targets == {} and cfg.ignore == []


def test_load_project_parses_toml(tmp_path: Path):
    (tmp_path / "dbly.toml").write_text(
        """
        object_root = "pgsql/schema"
        environment = "postgres"
        ignore = ["pgsql/schema/ggn/vw_bad.vw", "**/deploy-*.sql"]

        [targets]
        dev  = "test/dbly.dev.properties"
        prod = "test/dbly.prod.properties"
        """,
        encoding="utf-8",
    )
    cfg = load_project(tmp_path)
    assert cfg.object_root == "pgsql/schema"
    assert cfg.environment == "postgres"
    assert cfg.targets == {
        "dev": "test/dbly.dev.properties",
        "prod": "test/dbly.prod.properties",
    }
    assert "**/deploy-*.sql" in cfg.ignore


def test_load_project_parses_layout(tmp_path: Path):
    (tmp_path / "dbly.toml").write_text(
        """
        object_root = "db"
        [layout]
        schema_from = "search-path"
        schema_depth = 2
        database_depth = 1
        type_from = "extension"
        order = "filename"
        """,
        encoding="utf-8",
    )
    lay = load_project(tmp_path).layout
    assert lay.schema_from == "search-path"
    assert lay.schema_depth == 2 and lay.database_depth == 1
    assert lay.type_from == "extension" and lay.order == "filename"


def test_load_project_rejects_bad_layout_choice(tmp_path: Path):
    (tmp_path / "dbly.toml").write_text(
        'object_root = "db"\n[layout]\nschema_from = "nonsense"\n', encoding="utf-8")
    import pytest
    with pytest.raises(ValueError):
        load_project(tmp_path)


def test_layout_defaults_unchanged_without_config(tmp_path: Path):
    lay = load_project(tmp_path).layout
    assert lay.schema_from == "folder" and lay.schema_depth == 1
    assert lay.database_depth == 0 and lay.type_from == "sql" and lay.order == "dependency"
