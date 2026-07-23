"""Project configuration — an optional ``dbly.toml`` at the repository root.

The flag-driven workflow (``--target <profile>`` + ``.dbignore``) stays the default; a
``dbly.toml`` adds the terraform-style project setup a real repo needs:

* **object_root** — the subtree the declarative object files live under. The schema hint is the
  first path segment *below* this root, so a repo laid out as ``pgsql/schema/<schema>/<obj>``
  maps to the right schema instead of the literal top folder.
* **environment** — the default engine/dialect when a profile omits ``environment=``.
* **[targets]** — named connection profiles, so ``dbly plan --target dev`` resolves to a
  profile path instead of spelling it out each time.
* **ignore** — extra ignore patterns (gitwildmatch), merged with ``.dbignore`` — e.g. to
  exclude a handful of files a parser cannot read.

Absent config → :class:`ProjectConfig` defaults (``object_root="."``, no named targets), i.e.
the previous behaviour unchanged.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "dbly.toml"


@dataclass(slots=True)
class ProjectConfig:
    object_root: str = "."
    environment: str | None = None
    targets: dict[str, str] = field(default_factory=dict)
    ignore: list[str] = field(default_factory=list)


def load_project(repo_root: Path) -> ProjectConfig:
    """Load ``<repo_root>/dbly.toml`` if present, else return defaults."""
    path = repo_root / CONFIG_NAME
    if not path.exists():
        return ProjectConfig()
    data = tomllib.loads(path.read_text(encoding="utf-8"))

    targets = data.get("targets") or {}
    if not isinstance(targets, dict):
        raise ValueError(f"{CONFIG_NAME}: [targets] must be a table of name = \"profile-path\"")
    ignore = data.get("ignore") or []
    if not isinstance(ignore, list):
        raise ValueError(f"{CONFIG_NAME}: ignore must be a list of patterns")

    return ProjectConfig(
        object_root=str(data.get("object_root", ".")),
        environment=data.get("environment"),
        targets={str(k): str(v) for k, v in targets.items()},
        ignore=[str(p) for p in ignore],
    )
