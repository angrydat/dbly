"""dbly command-line interface (CONCEPT.md §14).

    dbly plan      --to <ref> [--from <ref>] --target <profile>
    dbly apply     [<plan.yaml>] [--to <ref>] --target <profile> [--allow-destructive]
    dbly bootstrap --to <ref> --target <profile>
    dbly check     --target <profile> [--to <ref>]
    dbly status    --target <profile>
"""
from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from dbly import __version__, drift, export as export_mod, hooks, initializer, report
from dbly.adapters import get_adapter
from dbly.config import ConnectionConfig, load_profile, resolve_target
from dbly.engine import detect_dialect
from dbly.parsing import sqlglot_dialect
from dbly.planner import build_plan
from dbly.model import Plan, Severity
from dbly.project import ProjectConfig, load_project
from dbly.repo import WORKTREE, Repo

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


def _quiet_parser_noise(debug: bool) -> None:
    """Silence third-party parser/introspection chatter unless --debug.

    sqlglot logs a WARNING (echoing the statement) every time it can't fully parse procedural
    or engine-specific DDL and falls back to a raw command — expected and harmless for dbly,
    but it floods real repos. SQLAlchemy likewise warns on types it doesn't model (e.g. PostGIS
    ``geometry``), which dbly doesn't need for column identity.
    """
    if debug:
        logging.getLogger("sqlglot").setLevel(logging.DEBUG)
        return
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    warnings.filterwarnings("ignore", message="Did not recognize type")


@app.callback()
def _main(
    version: bool = typer.Option(  # noqa: ARG001
        False, "--version", callback=_version, is_eager=True, help="Show version and exit."
    ),
    debug: bool = typer.Option(
        False, "--debug", help="show parser/introspection diagnostics (sqlglot, SQLAlchemy)."
    ),
) -> None:
    _quiet_parser_noise(debug)


def _open_repo(
    repo_path: Path, project: ProjectConfig,
    *, schemas: Optional[list[str]] = None, paths: Optional[list[str]] = None,
    cfg: Optional[ConnectionConfig] = None,
) -> Repo:
    return Repo(
        repo_path,
        object_root=project.object_root,
        extra_ignore=project.ignore,
        layout=project.layout,
        target_database=(cfg.extra.get("database") if cfg else None),
        select_schemas=schemas or None,
        select_paths=paths or None,
    )


def _scope_label(schemas: Optional[list[str]], paths: Optional[list[str]]) -> Optional[str]:
    bits = []
    if schemas:
        bits.append("schema=" + ",".join(schemas))
    if paths:
        bits.append("path=" + ",".join(paths))
    return " ".join(bits) or None


def _resolve_target(project: ProjectConfig, repo_path: Path, target: str) -> ConnectionConfig:
    """Resolve ``--target``: a named target from ``dbly.toml`` [targets], else a profile path.

    The project's ``environment`` fills in when the profile omits ``environment=``.
    """
    if target in project.targets:
        cfg = load_profile(repo_path / project.targets[target])
    else:
        cfg = resolve_target(target)
    if cfg.environment is None and project.environment:
        cfg.environment = project.environment
    return cfg


def _decorations(repo_path: Path, plan: Plan) -> dict[str, str]:
    """Map each real ref in the plan to its git tag/branch names (for the plan header)."""
    try:
        repo = Repo(repo_path)
    except ValueError:
        return {}
    out: dict[str, str] = {}
    for ref in {plan.from_ref, plan.to_ref}:
        if not ref or ref == WORKTREE:
            continue
        names = repo.ref_names(ref)
        if names:
            out[ref] = ", ".join(names)
    return out


def _make_plan(
    repo_path: Path, target: str, from_ref: Optional[str], to_ref: str,
    *, worktree: bool = False,
    schemas: Optional[list[str]] = None, paths: Optional[list[str]] = None,
    with_deps: bool = False,
) -> Plan:
    project = load_project(repo_path)
    cfg = _resolve_target(project, repo_path, target)
    repo = _open_repo(repo_path, project, schemas=schemas, paths=paths, cfg=cfg)
    dialect = sqlglot_dialect(detect_dialect(cfg))
    adapter = get_adapter(cfg)
    try:
        resolved_to = repo.resolve_ref(WORKTREE if worktree else to_ref)
        if from_ref is not None:
            resolved_from = repo.resolve_ref(from_ref)
        else:
            resolved_from = adapter.get_deployed_ref()  # already a SHA, or None (bootstrap)
        return build_plan(
            repo, adapter,
            from_ref=resolved_from, to_ref=resolved_to,
            target=target, dialect=dialect, with_deps=with_deps,
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
    worktree: bool = typer.Option(
        False, "--worktree", "--dirty",
        help="plan against the working tree (uncommitted + untracked object files), "
             "not a git ref — for the fast edit→plan loop.",
    ),
    schema: Optional[list[str]] = typer.Option(
        None, "--schema", help="limit to these schemas (folder under object_root); repeatable."
    ),
    path: Optional[list[str]] = typer.Option(
        None, "--path", help="limit to this subpath under object_root; repeatable."
    ),
    with_deps: bool = typer.Option(
        False, "--with-deps",
        help="also deploy the specific objects the selection depends on (cross-schema FKs, "
             "referenced views/tables) — their dependency closure, not their whole schemas.",
    ),
) -> None:
    """Compute and show the deployment plan."""
    plan_obj = _make_plan(repo_path, target, from_ref, to, worktree=worktree,
                          schemas=schema, paths=path, with_deps=with_deps)
    report.render_plan(plan_obj, console, ref_names=_decorations(repo_path, plan_obj))
    if out:
        out.write_text(report.plan_to_yaml(plan_obj), encoding="utf-8")
        console.print(f"\n[dim]plan (YAML) written to {out}[/dim]")
    if sql:
        # state_table_ddl / record_deploy_sql are pure string builders — no DB connection.
        adapter = get_adapter(_resolve_target(load_project(repo_path), repo_path, target))
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
    schema: Optional[list[str]] = typer.Option(
        None, "--schema", help="limit to these schemas (folder under object_root); repeatable."
    ),
    path: Optional[list[str]] = typer.Option(
        None, "--path", help="limit to this subpath under object_root; repeatable."
    ),
    with_deps: bool = typer.Option(
        False, "--with-deps",
        help="also deploy the specific objects the selection depends on (dependency closure).",
    ),
) -> None:
    """Apply a plan to the target database (re-computes one unless a file is given)."""
    if plan_file is not None:
        plan_obj = report.plan_from_yaml(plan_file.read_text(encoding="utf-8"))
        target = plan_obj.target
    else:
        plan_obj = _make_plan(repo_path, target, from_ref, to, schemas=schema, paths=path,
                              with_deps=with_deps)

    report.render_plan(plan_obj, console, ref_names=_decorations(repo_path, plan_obj))

    destructive = [s for s in plan_obj.steps if s.severity is Severity.DESTRUCTIVE]
    if destructive and not allow_destructive:
        err.print(
            "[red]aborting:[/red] plan has destructive steps; pass --allow-destructive "
            "to proceed."
        )
        raise typer.Exit(code=1)

    to_apply = [
        s for s in plan_obj.steps
        if allow_destructive or s.severity is not Severity.DESTRUCTIVE
    ]
    # Statement steps (schema/table/index) run transactionally as one batch; script steps
    # (replaceable objects, raw per-file content) run via the engine's multi-statement runner,
    # after, in order (ADR 0002). Replaceable objects are idempotent, so per-file is safe.
    stmt_steps = [s for s in to_apply if not s.script]
    script_steps = [s for s in to_apply if s.script]
    if not to_apply and not plan_obj.migrations and not plan_obj.baselined:
        console.print("[green]nothing to apply.[/green]")
        return

    project = load_project(repo_path)
    cfg = _resolve_target(project, repo_path, target)
    repo = _open_repo(repo_path, project, cfg=cfg)
    adapter = get_adapter(cfg)
    try:
        _run_hooks(repo, "pre", py_interpreter)
        adapter.ensure_state_table()
        # baseline (bootstrap): record migrations as applied without running them
        for mid in plan_obj.baselined:
            adapter.record_migration(plan_obj.to_ref, mid)
        console.print("[green]✓[/green] Applying changes...")
        # explicit migrations run first — they reshape the schema before reconciliation
        for m in plan_obj.migrations:
            adapter.run_init_script(m.sql)  # engine-aware multi-statement / PL-SQL runner
            adapter.record_migration(plan_obj.to_ref, m.id)
            console.print(f"  [green]✓[/green] migration {m.id}[green]  OK[/green]")
        if stmt_steps:
            adapter.apply([s.sql for s in stmt_steps])
            for s in stmt_steps:
                console.print(f"  [green]✓[/green] {s.title}[green]  OK[/green]")
        for s in script_steps:  # replaceable objects, verbatim per-file
            adapter.run_init_script(s.sql)
            console.print(f"  [green]✓[/green] {s.title}[green]  OK[/green]")
        adapter.record_deploy(plan_obj.to_ref, migration_ids=[])
        _run_hooks(repo, "post", py_interpreter)
    except Exception as exc:  # noqa: BLE001 — surface a concise cause, not a stack trace
        msg = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
        err.print(f"\n[red]apply failed:[/red] {msg}")
        if adapter.transactional_ddl:
            err.print("[dim]the transaction was rolled back — the target is unchanged.[/dim]")
        else:
            err.print("[yellow]DDL auto-commits on this engine — earlier steps may have "
                      "been applied.[/yellow]")
        raise typer.Exit(code=1) from exc
    finally:
        adapter.dispose()
    console.print(
        f"\n[green]✓ Apply complete![/green] {len(stmt_steps) + len(script_steps)} step(s), "
        f"{len(plan_obj.migrations)} migration(s); deployed ref → "
        f"[cyan]{plan_obj.to_ref[:8] if len(plan_obj.to_ref) >= 8 else plan_obj.to_ref}[/cyan]"
    )


@app.command()
def init(
    init_target: str = typer.Option(
        ..., "--init-target",
        help="privileged connection profile (superuser / maintenance DB).",
    ),
    repo_path: Path = typer.Option(Path("."), "--repo"),
    init_dir: str = typer.Option("init", "--dir", help="folder of ordered init SQL scripts."),
) -> None:
    """Run privileged greenfield groundwork (CREATE DATABASE/ROLE/EXTENSION).

    Greenfield only — brownfield (a handed-over database) skips this entirely.
    """
    project = load_project(repo_path)
    repo = _open_repo(repo_path, project)
    scripts = initializer.discover_init_scripts(repo.root, init_dir)
    if not scripts:
        console.print(f"[yellow]no init scripts in {init_dir}/ — nothing to do.[/yellow]")
        return
    adapter = get_adapter(_resolve_target(project, repo_path, init_target))
    try:
        for s in scripts:
            adapter.run_init_script(s.read_text(encoding="utf-8"))
            console.print(f"[green]ran[/green] {init_dir}/{s.name}")
    finally:
        adapter.dispose()
    console.print(f"[green]init complete[/green] — {len(scripts)} script(s).")


@app.command()
def bootstrap(
    to: str = typer.Option("HEAD", "--to"),
    target: str = typer.Option(..., "--target"),
    repo_path: Path = typer.Option(Path("."), "--repo"),
) -> None:
    """Install into an empty database (no baseline — full apply)."""
    plan_obj = _make_plan(repo_path, target, None, to)
    report.render_plan(plan_obj, console, ref_names=_decorations(repo_path, plan_obj))
    console.print("\n[dim]review, then run `dbly apply` to execute.[/dim]")


@app.command()
def baseline(
    to: str = typer.Option("HEAD", "--to", help="git ref the database is already at."),
    target: str = typer.Option(..., "--target"),
    repo_path: Path = typer.Option(Path("."), "--repo"),
) -> None:
    """Record a ref as deployed **without running any SQL** — adopt an existing database.

    For a brownfield / already-deployed target (e.g. rolled out by hand via psql/DataGrip):
    tells dbly's ledger "the database is at <ref>", so subsequent `plan` diffs incrementally
    from here instead of treating the target as empty. Migrations up to <ref> are marked
    applied (recorded, not run). Nothing in the schema is touched.
    """
    project = load_project(repo_path)
    cfg = _resolve_target(project, repo_path, target)
    repo = _open_repo(repo_path, project, cfg=cfg)
    adapter = get_adapter(cfg)
    try:
        ref = repo.resolve_ref(to)
        prev = adapter.get_deployed_ref()
        adapter.ensure_state_table()
        applied = adapter.applied_migrations()
        pending = [mid for mid, _ in repo.migration_files(ref) if mid not in applied]
        for mid in pending:
            adapter.record_migration(ref, mid)  # recorded as applied, never run
        adapter.record_deploy(ref, migration_ids=[])
    finally:
        adapter.dispose()

    names = _decorations(repo_path, Plan(target=target, from_ref=None, to_ref=ref))
    console.print(
        f"[green]✓ baselined[/green] {target} at {report._decorate_ref(ref, names)}"
    )
    if prev:
        console.print(f"[dim]previous deployed ref: {prev[:8]}[/dim]")
    if pending:
        console.print(f"[dim]{len(pending)} migration(s) marked applied (not run)[/dim]")
    console.print("[dim]no SQL was executed — schema untouched.[/dim]")


@app.command()
def status(
    target: str = typer.Option(..., "--target"),
    repo_path: Path = typer.Option(Path("."), "--repo", help="repository root (for ref names)."),
) -> None:
    """Show the deployed ref recorded on the target."""
    cfg = _resolve_target(load_project(repo_path), repo_path, target)
    adapter = get_adapter(cfg)
    try:
        ref = adapter.get_deployed_ref()
    finally:
        adapter.dispose()
    if not ref:
        console.print("[yellow]no deploy recorded — database is unmanaged or empty.[/yellow]")
        return
    try:
        names = Repo(repo_path).ref_names(ref)
    except ValueError:
        names = []  # not a git repo — SHA only
    if names:
        console.print(f"deployed ref: [cyan]{', '.join(names)}[/cyan] [dim]({ref[:8]})[/dim]")
    else:
        console.print(f"deployed ref: [cyan]{ref}[/cyan]")


@app.command()
def check(
    target: str = typer.Option(..., "--target"),
    to: str = typer.Option("HEAD", "--to"),
    repo_path: Path = typer.Option(Path("."), "--repo"),
    orphans: bool = typer.Option(
        False, "--orphans", help="also report objects in the DB but not in the repo."
    ),
    advisory: bool = typer.Option(
        False, "--advisory",
        help="also report procedural definition drift (function/procedure/trigger) — "
             "unreliable across engines, off by default.",
    ),
    show_diff: bool = typer.Option(
        False, "--show-diff",
        help="for changed views/definitions, print a unified diff (live → repo) so you can "
             "tell a real change from parser-normalization noise.",
    ),
    worktree: bool = typer.Option(
        False, "--worktree", "--dirty",
        help="compare the working tree (uncommitted + untracked) against the DB, not a git ref.",
    ),
    schema: Optional[list[str]] = typer.Option(
        None, "--schema", help="limit to these schemas (folder under object_root); repeatable."
    ),
    path: Optional[list[str]] = typer.Option(
        None, "--path", help="limit to this subpath under object_root; repeatable."
    ),
) -> None:
    """Detect drift: compare the desired state at <to> against the live database."""
    project = load_project(repo_path)
    cfg = _resolve_target(project, repo_path, target)
    repo = _open_repo(repo_path, project, schemas=schema, paths=path, cfg=cfg)
    dialect = sqlglot_dialect(detect_dialect(cfg))
    resolved_to = repo.resolve_ref(WORKTREE if worktree else to)
    adapter = get_adapter(cfg)
    try:
        rep = drift.compute_drift(
            repo, adapter, to_ref=resolved_to, dialect=dialect,
            include_orphans=orphans, include_advisory=advisory, include_diff=show_diff,
        )
    finally:
        adapter.dispose()

    report.render_drift(
        rep, console, target=target, ref=resolved_to,
        ref_names=_decorations(repo_path, Plan(target=target, from_ref=None, to_ref=resolved_to)),
        scope=_scope_label(schema, path), show_diff=show_diff, dialect=dialect,
    )
    if not rep.clean:
        raise typer.Exit(code=1)


@app.command()
def export(
    target: str = typer.Option(..., "--target", help="connection profile or named target."),
    dialect: Optional[str] = typer.Option(
        None, "--dialect",
        help="transpile to another engine (postgres|oracle|sqlserver|sqlite). "
             "Default: keep the source engine's dialect.",
    ),
    out: Optional[Path] = typer.Option(None, "--out", help="write the DDL script to a file."),
    schema: Optional[list[str]] = typer.Option(
        None, "--schema", help="limit to these live schemas; repeatable."
    ),
    repo_path: Path = typer.Option(Path("."), "--repo", help="repo root (for dbly.toml targets)."),
) -> None:
    """Export a live database as a DDL script — the reverse of deploy, optionally cross-dialect.

    Tables/views transpile across dialects; procedural objects are emitted verbatim.
    """
    project = load_project(repo_path)
    cfg = _resolve_target(project, repo_path, target)
    source_dialect = sqlglot_dialect(detect_dialect(cfg))
    target_dialect = sqlglot_dialect(dialect) if dialect else None
    if dialect and target_dialect is None:
        err.print(f"[red]unknown --dialect {dialect!r}[/red] (postgres|oracle|sqlserver|sqlite)")
        raise typer.Exit(code=2)

    adapter = get_adapter(cfg)
    try:
        result = export_mod.export_ddl(
            adapter, source_dialect=source_dialect,
            target_dialect=target_dialect, schemas=schema or None,
        )
    finally:
        adapter.dispose()

    if out:
        out.write_text(result.ddl, encoding="utf-8")
        console.print(f"[green]exported[/green] {result.object_count} object(s) → {out}")
    else:
        console.print(result.ddl)
    for w in result.warnings:
        err.print(f"[yellow]![/yellow] {w}")


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
