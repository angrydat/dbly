"""Global pre-/post-deploy hooks (CONCEPT.md §11).

Hooks accept ``.sql`` and ``.py`` files. ``.py`` hooks run as an isolated subprocess under
a **configurable interpreter** — crucial for ArcPy, which lives in ArcGIS's bundled Python,
not in dbly's uv environment. Robust error handling: timeout, full stdout/stderr capture,
explicit failure semantics.

Layout convention (best practice, not enforced)::

    hooks/pre/*.sql|*.py
    hooks/post/*.sql|*.py
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class HookResult:
    hook: Path
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None


class HookError(RuntimeError):
    def __init__(self, result: HookResult):
        self.result = result
        super().__init__(
            f"hook failed: {result.hook} (rc={result.returncode})\n{result.stderr}"
        )


def discover_hooks(repo_root: Path, phase: str) -> list[Path]:
    d = repo_root / "hooks" / phase
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in (".sql", ".py"))


def run_py_hook(
    hook: Path,
    *,
    interpreter: str,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> HookResult:
    """Run a ``.py`` hook under an external interpreter (e.g. ArcGIS ``propy``)."""
    try:
        proc = subprocess.run(
            [interpreter, str(hook)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return HookResult(hook, False, exc.stdout or "", "timeout exceeded", None)
    return HookResult(hook, proc.returncode == 0, proc.stdout, proc.stderr, proc.returncode)
