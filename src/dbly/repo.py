"""Source repository access — the *change detection* layer (CONCEPT.md §2).

git answers "which files changed since the deployed ref"; that's all we use it for. The
semantic layer (parsing.py) decides what those files *are*. ``.dbignore`` (gitignore
syntax) excludes files that live in the repo but must not be deployed — runbooks, ad-hoc
SQL, ArcGIS/SDE objects handled via hooks.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pathspec

from dbly.model import ChangeType

_SQL_SUFFIXES = {".sql", ".tbl", ".vw", ".prc", ".fnc", ".pkg", ".trg", ".typ", ".ddl"}


@dataclass(slots=True)
class FileChange:
    path: Path           # repo-relative
    change_type: ChangeType


class Repo:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not (self.root / ".git").exists():
            raise ValueError(f"not a git repository: {self.root}")
        self._ignore = self._load_dbignore()

    def _git(self, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout

    def _load_dbignore(self) -> pathspec.PathSpec:
        f = self.root / ".dbignore"
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
        return pathspec.PathSpec.from_lines("gitwildmatch", lines)

    def is_ignored(self, rel: Path) -> bool:
        return self._ignore.match_file(rel.as_posix())

    @staticmethod
    def _is_sql(rel: Path) -> bool:
        return rel.suffix.lower() in _SQL_SUFFIXES

    def changed_files(self, from_ref: str | None, to_ref: str) -> list[FileChange]:
        """Files changed between two refs (or the full tree at ``to_ref`` for bootstrap)."""
        if from_ref is None:
            return [FileChange(p, ChangeType.ADDED) for p in self.list_files(to_ref)]
        raw = self._git("diff", "--name-status", "-z", f"{from_ref}..{to_ref}")
        return self._parse_name_status(raw)

    def list_files(self, ref: str) -> list[Path]:
        """All deployable SQL files present at ``ref``."""
        raw = self._git("ls-tree", "-r", "--name-only", "-z", ref)
        result = []
        for name in filter(None, raw.split("\0")):
            rel = Path(name)
            if self._is_sql(rel) and not self.is_ignored(rel):
                result.append(rel)
        return result

    def _parse_name_status(self, raw: str) -> list[FileChange]:
        tokens = [t for t in raw.split("\0") if t]
        changes: list[FileChange] = []
        i = 0
        while i < len(tokens):
            status = tokens[i]
            code = status[0]
            if code == "R":  # rename: status, old, new
                new = Path(tokens[i + 2])
                i += 3
                if self._is_sql(new) and not self.is_ignored(new):
                    changes.append(FileChange(new, ChangeType.MODIFIED))
                continue
            rel = Path(tokens[i + 1])
            i += 2
            if not self._is_sql(rel) or self.is_ignored(rel):
                continue
            mapping = {"A": ChangeType.ADDED, "M": ChangeType.MODIFIED,
                       "D": ChangeType.DELETED}
            changes.append(FileChange(rel, mapping.get(code, ChangeType.MODIFIED)))
        return changes

    def resolve_ref(self, ref: str) -> str:
        """Resolve a symbolic ref (HEAD, a tag, a branch) to its immutable commit SHA.

        The ledger and plan headers store the SHA, not ``HEAD`` — so a later ``--from``
        diff is stable regardless of where HEAD has since moved.
        """
        return self._git("rev-parse", ref).strip()

    def read_at(self, ref: str, rel: Path) -> str:
        """File content at a given ref (the *desired* state)."""
        return self._git("show", f"{ref}:{rel.as_posix()}")

    def schema_for(self, rel: Path) -> str | None:
        """Best-practice convention: the first path segment names the DB schema.

        Only a *hint* — the parser overrides it when the DDL is schema-qualified.
        Returns None when the file sits at the repo root.
        """
        parts = rel.parts
        return parts[0] if len(parts) > 1 else None
