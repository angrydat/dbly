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
MIGRATIONS_DIR = "migrations"  # run-once scripts — not declarative objects
WORKTREE = "WORKTREE"  # sentinel `to_ref`: plan/check against the working tree, not a git ref


@dataclass(slots=True)
class FileChange:
    path: Path           # repo-relative
    change_type: ChangeType


class Repo:
    def __init__(
        self,
        root: Path,
        *,
        object_root: str | None = None,
        extra_ignore: list[str] | None = None,
        select_schemas: list[str] | None = None,
        select_paths: list[str] | None = None,
    ):
        self.root = root.resolve()
        if not (self.root / ".git").exists():
            raise ValueError(f"not a git repository: {self.root}")
        # the subtree object files live under; the schema hint is the first segment *below* it.
        self.object_root = (
            Path(object_root) if object_root and object_root not in (".", "") else None
        )
        # optional subset selection (deploy/check only part of the tree)
        self.select_schemas = {s.lower() for s in select_schemas} if select_schemas else None
        self.select_paths = [Path(p) for p in select_paths] if select_paths else None
        self._ignore = self._load_dbignore(extra_ignore or [])

    def _git(self, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout

    def _load_dbignore(self, extra: list[str]) -> pathspec.PathSpec:
        f = self.root / ".dbignore"
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
        return pathspec.PathSpec.from_lines("gitwildmatch", [*lines, *extra])

    def is_ignored(self, rel: Path) -> bool:
        return self._ignore.match_file(rel.as_posix())

    @staticmethod
    def _is_sql(rel: Path) -> bool:
        return rel.suffix.lower() in _SQL_SUFFIXES

    @staticmethod
    def _is_migration(rel: Path) -> bool:
        return len(rel.parts) > 0 and rel.parts[0] == MIGRATIONS_DIR

    def _under_object_root(self, rel: Path) -> bool:
        if self.object_root is None:
            return True
        return rel == self.object_root or self.object_root in rel.parents

    def _selected(self, rel: Path) -> bool:
        """Honour an optional subset selection (``--schema`` / ``--path``). Both AND together."""
        if self.select_paths is not None:
            base = self.object_root or Path()
            prefixes = [base / p for p in self.select_paths]
            if not any(rel == pre or pre in rel.parents for pre in prefixes):
                return False
        if self.select_schemas is not None:
            sch = self.schema_for(rel)
            if sch is None or sch.lower() not in self.select_schemas:
                return False
        return True

    def _is_object(self, rel: Path) -> bool:
        """A deployable declarative object file (SQL, under object_root, selected, not ignored)."""
        return (
            self._is_sql(rel)
            and not self._is_migration(rel)
            and self._under_object_root(rel)
            and self._selected(rel)
            and not self.is_ignored(rel)
        )

    def _is_object_unscoped(self, rel: Path) -> bool:
        """Like ``_is_object`` but ignoring the ``--schema``/``--path`` selection — the full
        deployable object graph, used to resolve a scoped deploy's dependency closure."""
        return (
            self._is_sql(rel)
            and not self._is_migration(rel)
            and self._under_object_root(rel)
            and not self.is_ignored(rel)
        )

    def all_object_files(self, ref: str) -> list[Path]:
        """Every deployable object file at ``ref`` regardless of the subset selection."""
        if ref == WORKTREE:
            return [p for p in self._worktree_paths() if self._is_object_unscoped(p)]
        raw = self._git("ls-tree", "-r", "--name-only", "-z", ref)
        return [Path(n) for n in filter(None, raw.split("\0")) if self._is_object_unscoped(Path(n))]

    @property
    def has_selection(self) -> bool:
        return self.select_schemas is not None or self.select_paths is not None

    def _untracked_files(self) -> list[Path]:
        """New files in the working tree not yet added to git (``git status`` "??")."""
        raw = self._git("ls-files", "--others", "--exclude-standard", "-z")
        return [Path(n) for n in filter(None, raw.split("\0"))]

    def _worktree_paths(self) -> list[Path]:
        """Every file in the working tree: tracked (``ls-files``) + untracked new files."""
        raw = self._git("ls-files", "-z")
        tracked = [Path(n) for n in filter(None, raw.split("\0"))]
        return tracked + self._untracked_files()

    def changed_files(self, from_ref: str | None, to_ref: str) -> list[FileChange]:
        """Files changed between two refs (or the full tree at ``to_ref`` for bootstrap)."""
        if from_ref is None:
            return [FileChange(p, ChangeType.ADDED) for p in self.list_files(to_ref)]
        if to_ref == WORKTREE:
            # `git diff <from>` (no second ref) diffs the ref against the working tree
            # (staged + unstaged tracked changes); untracked new files are added separately.
            changes = self._parse_name_status(
                self._git("diff", "--name-status", "-z", from_ref)
            )
            known = {c.path for c in changes}
            for p in self._untracked_files():
                if self._is_object(p) and p not in known:
                    changes.append(FileChange(p, ChangeType.ADDED))
            return changes
        raw = self._git("diff", "--name-status", "-z", f"{from_ref}..{to_ref}")
        return self._parse_name_status(raw)

    def list_files(self, ref: str) -> list[Path]:
        """All deployable declarative object files present at ``ref`` (excludes migrations)."""
        if ref == WORKTREE:
            return [p for p in self._worktree_paths() if self._is_object(p)]
        raw = self._git("ls-tree", "-r", "--name-only", "-z", ref)
        return [Path(n) for n in filter(None, raw.split("\0")) if self._is_object(Path(n))]

    def migration_files(self, ref: str) -> list[tuple[str, Path]]:
        """Ordered (id, path) of migration scripts under ``migrations/`` at ``ref``.

        Id is the filename; order is lexicographic, so prefix files ``0001_…`` / a timestamp.
        """
        if ref == WORKTREE:
            names = self._worktree_paths()
        else:
            raw = self._git("ls-tree", "-r", "--name-only", "-z", ref)
            names = [Path(n) for n in filter(None, raw.split("\0"))]
        out = [p for p in names if self._is_migration(p) and p.suffix.lower() == ".sql"]
        return [(p.name, p) for p in sorted(out, key=lambda p: p.as_posix())]

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
                if self._is_object(new):
                    changes.append(FileChange(new, ChangeType.MODIFIED))
                continue
            rel = Path(tokens[i + 1])
            i += 2
            if not self._is_object(rel):
                continue
            mapping = {"A": ChangeType.ADDED, "M": ChangeType.MODIFIED,
                       "D": ChangeType.DELETED}
            changes.append(FileChange(rel, mapping.get(code, ChangeType.MODIFIED)))
        return changes

    def resolve_ref(self, ref: str) -> str:
        """Resolve a symbolic ref (HEAD, a tag, a branch) to its immutable commit SHA.

        The ledger and plan headers store the SHA, not ``HEAD`` — so a later ``--from``
        diff is stable regardless of where HEAD has since moved. The ``WORKTREE`` sentinel
        has no SHA and is returned unchanged.
        """
        if ref == WORKTREE:
            return WORKTREE
        return self._git("rev-parse", ref).strip()

    def ref_names(self, sha: str) -> list[str]:
        """Tag and branch names pointing exactly at ``sha`` — for git-style plan/status
        decorations (e.g. ``v0.1, main``). Display-only; the ledger still stores the SHA."""
        names: list[str] = []
        try:
            names += [n for n in self._git("tag", "--points-at", sha).split() if n]
            names += [
                n for n in self._git(
                    "branch", "--points-at", sha, "--format=%(refname:short)"
                ).split() if n
            ]
        except subprocess.CalledProcessError:
            return []
        seen: set[str] = set()
        return [n for n in names if not (n in seen or seen.add(n))]

    def read_at(self, ref: str, rel: Path) -> str:
        """File content at a given ref (the *desired* state)."""
        if ref == WORKTREE:
            return (self.root / rel).read_text(encoding="utf-8")
        return self._git("show", f"{ref}:{rel.as_posix()}")

    def schema_for(self, rel: Path) -> str | None:
        """Best-practice convention: the first path segment names the DB schema.

        With an ``object_root`` set, the segment is taken *relative to that root* — so
        ``pgsql/schema/bas/foo.tbl`` under root ``pgsql/schema`` yields schema ``bas``.
        Only a *hint* — the parser overrides it when the DDL is schema-qualified. Returns
        None when the file sits directly at the (object) root.
        """
        r = rel
        if self.object_root is not None:
            try:
                r = rel.relative_to(self.object_root)
            except ValueError:
                return None
        parts = r.parts
        return parts[0] if len(parts) > 1 else None
