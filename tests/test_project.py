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
