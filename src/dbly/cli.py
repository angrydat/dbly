"""dbly command-line interface (CONCEPT.md §14).

    dbly plan      --to <ref> [--from <ref>] --target <profile>
    dbly apply     [<plan.yaml>] [--to <ref>] --target <profile> [--allow-destructive]
    dbly bootstrap --to <ref> --target <profile>
    dbly check     --target <profile> [--to <ref>]
    dbly status    --target <profile>
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from dbly import __version__, hooks, report
from dbly.adapters import get_adapter
from dbly.config import resolve_target
from dbly.engine import detect_dialect
from dbly.parsing import sqlglot_dialect
from dbly.planner import build_plan
from dbly.model import Plan, Severity
from dbly.repo import Repo

app = typer.Typer(
    name="dbly",
    help="State-based, cross-engine database deployment — git-driven, parser-assisted.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)


def _version(value: bool) -> None:
    if value:
        console.print(f"dbly {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(  # noqa: ARG001
        False, "--version", callback=_version, is_eager=True, help="Show version and exit."
    ),
) -> None:
    pass


def _make_plan(repo_path: Path, target: str, from_ref: Optional[str], to_ref: str) -> Plan:
    repo = Repo(repo_path)
    cfg = resolve_target(target)
    dialect = sqlglot_dialect(detect_dialect(cfg))
    adapter = get_adapter(cfg)
    try:
        resolved_to = repo.resolve_ref(to_ref)
        if from_ref is not None:
            resolved_from = repo.resolve_ref(from_ref)
        else:
            resolved_from = adapter.get_deployed_ref()  # already a SHA, or None (bootstrap)
        return build_plan(
            repo, adapter,
            from_ref=resolved_from, to_ref=resolved_to,
            target=target, dialect=dialect,
        )
    finally:
        adapter.dispose()


@app.command()
def plan(
    to: str = typer.Option("HEAD", "--to", help="git ref to deploy (release tag/branch)."),
    from_ref: Optional[str] = typer.Option(
        None, "--from", help="baseline ref (default: deployed ref from dbly_state)."
    ),
    target: str = typer.Option(..., "--target", help="connection profile or env name."),
    repo_path: Path = typer.Option(Path("."), "--repo", help="repository root."),
    out: Optional[Path] = typer.Option(None, "--out", help="write the plan as YAML."),
    sql: Optional[Path] = typer.Option(
        None, "--sql", help="write an executable SQL script for a hand/offline deploy."
    ),
) -> None:
    """Compute and show the deployment plan."""
    plan_obj = _make_plan(repo_path, target, from_ref, to)
    report.render_plan(plan_obj, console)
    if out:
        out.write_text(report.plan_to_yaml(plan_obj), encoding="utf-8")
        console.print(f"\n[dim]plan (YAML) written to {out}[/dim]")
    if sql:
        # state_table_ddl / record_deploy_sql are pure string builders — no DB connection.
        adapter = get_adapter(resolve_target(target))
        try:
            script = report.plan_to_sql(
                plan_obj,
                state_ddl=adapter.state_table_ddl(),
                record_sql=adapter.record_deploy_sql(plan_obj.to_ref),
            )
        finally:
            adapter.dispose()
        sql.write_text(script, encoding="utf-8")
        console.print(f"[dim]deploy SQL written to {sql}[/dim]")


@app.command()
def apply(
    plan_file: Optional[Path] = typer.Argument(None, help="a YAML plan from `dbly plan`."),
    to: str = typer.Option("HEAD", "--to"),
    from_ref: Optional[str] = typer.Option(None, "--from"),
    target: str = typer.Option(..., "--target"),
    repo_path: Path = typer.Option(Path("."), "--repo"),
    allow_destructive: bool = typer.Option(
        False, "--allow-destructive", help="execute destructive steps too."
    ),
    py_interpreter: str = typer.Option(
        "python", "--py-interpreter", help="interpreter for .py hooks (e.g. ArcGIS propy)."
    ),
) -> None:
    """Apply a plan to the target database (re-computes one unless a file is given)."""
    if plan_file is not None:
        plan_obj = report.plan_from_yaml(plan_file.read_text(encoding="utf-8"))
        target = plan_obj.target
    else:
        plan_obj = _make_plan(repo_path, target, from_ref, to)

    report.render_plan(plan_obj, console)

    destructive = [s for s in plan_obj.steps if s.severity is Severity.DESTRUCTIVE]
    if destructive and not allow_destructive:
        err.print(
            "[red]aborting:[/red] plan has destructive steps; pass --allow-destructive "
            "to proceed."
        )
        raise typer.Exit(code=1)

    statements = [
        s.sql for s in plan_obj.steps
        if allow_destructive or s.severity is not Severity.DESTRUCTIVE
    ]
    if not statements:
        console.print("[green]nothing to apply.[/green]")
        return

    repo = Repo(repo_path)
    cfg = resolve_target(target)
    adapter = get_adapter(cfg)
    try:
        _run_hooks(repo, "pre", py_interpreter)
        adapter.ensure_state_table()
        adapter.apply(statements)
        adapter.record_deploy(plan_obj.to_ref, migration_ids=[])
        _run_hooks(repo, "post", py_interpreter)
    finally:
        adapter.dispose()
    console.print(f"[green]applied[/green] {len(statements)} step(s); "
                  f"deployed ref → {plan_obj.to_ref}")


@app.command()
def bootstrap(
    to: str = typer.Option("HEAD", "--to"),
    target: str = typer.Option(..., "--target"),
    repo_path: Path = typer.Option(Path("."), "--repo"),
) -> None:
    """Install into an empty database (no baseline — full apply)."""
    plan_obj = _make_plan(repo_path, target, None, to)
    report.render_plan(plan_obj, console)
    console.print("\n[dim]review, then run `dbly apply` to execute.[/dim]")


@app.command()
def status(target: str = typer.Option(..., "--target")) -> None:
    """Show the deployed ref recorded on the target."""
    cfg = resolve_target(target)
    adapter = get_adapter(cfg)
    try:
        ref = adapter.get_deployed_ref()
    finally:
        adapter.dispose()
    if ref:
        console.print(f"deployed ref: [cyan]{ref}[/cyan]")
    else:
        console.print("[yellow]no deploy recorded — database is unmanaged or empty.[/yellow]")


@app.command()
def check(
    target: str = typer.Option(..., "--target"),
    to: str = typer.Option("HEAD", "--to"),
    repo_path: Path = typer.Option(Path("."), "--repo"),
) -> None:
    """Detect drift: compare desired state at <to> against the live database."""
    plan_obj = _make_plan(repo_path, target, None, to)
    drift = [s for s in plan_obj.steps] + plan_obj.warnings
    if not drift:
        console.print("[green]no drift — database matches desired state.[/green]")
        return
    report.render_plan(plan_obj, console)
    console.print("\n[yellow]drift detected (see steps/warnings above).[/yellow]")


def _run_hooks(repo: Repo, phase: str, py_interpreter: str) -> None:
    for hook in hooks.discover_hooks(repo.root, phase):
        if hook.suffix.lower() == ".py":
            res = hooks.run_py_hook(hook, interpreter=py_interpreter)
            if not res.ok:
                raise hooks.HookError(res)
            console.print(f"[dim]hook ok: {hook.name}[/dim]")
        # NOTE: .sql hooks are applied via the adapter in a later iteration.


def main() -> None:
    app()


if __name__ == "__main__":
    main()
