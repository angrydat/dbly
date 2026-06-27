"""Privileged greenfield groundwork (CONCEPT.md §6).

Init scripts live in ``init/`` (ordered by filename) and are run **verbatim** — they are
imperative groundwork (``CREATE DATABASE`` / roles / extensions / base schemas), not
declarative objects, so they are neither parsed nor classified. Run under a separate
privileged profile (``--init-target``), explicitly via ``dbly init``, never as part of
``apply``. Greenfield runs this once; brownfield skips it entirely.
"""
from __future__ import annotations

from pathlib import Path


def discover_init_scripts(repo_root: Path, dirname: str = "init") -> list[Path]:
    """Ordered ``.sql`` init scripts (lexicographic, so prefix ``01_``, ``02_`` …)."""
    d = repo_root / dirname
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".sql")
