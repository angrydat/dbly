"""Render plans for humans (rich) and for machines (YAML manifest, CONCEPT.md §7).

The plan artifact is a vanilla-SQL bundle plus a YAML manifest carrying only what SQL can't
express: ordering, severity, source provenance and warnings.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from rich.console import Console

from dbly.model import Migration, ObjectId, ObjectKind, Plan, Severity, Step

if TYPE_CHECKING:
    from dbly.drift import DriftReport


def _decorate_ref(ref: str | None, ref_names: dict[str, str] | None) -> str:
    """Render a ref for the plan header: git-style ``<names> (<short-sha>)`` when known."""
    if not ref:
        return "∅"
    if ref == "WORKTREE":
        return "working tree"
    if ref_names and ref_names.get(ref):
        return f"{ref_names[ref]} ({ref[:8]})"
    return ref


_VERBS = ("create", "add", "modify", "apply", "drop", "alter")


def _step_row(step: Step) -> tuple[str, str, str, str, str]:
    """Decompose a step into (marker, style, action, kind, target) for a Terraform-style row."""
    low = step.title.lower()
    if "drop" in low or "delete" in low:
        marker, style = "-", "red"
    elif "modify" in low or "alter" in low:
        marker, style = "~", "yellow"
    elif step.severity is Severity.DESTRUCTIVE:
        marker, style = "!", "red"
    else:
        marker, style = "+", "green"

    action = next((v for v in _VERBS if low.startswith(v)), step.title.split()[0])
    action = "replace" if action == "apply" else action     # replaceable objects re-apply

    # target: the object; for column-level steps the column lives in the title, not object_id.
    kind = step.kind.value
    if "column" in low:
        kind = "column"                                      # ALTER targets a table, but show "column"
        toks = step.title.split()
        target = toks[toks.index("column") + 1] if "column" in toks else str(step.object_id or "")
    else:
        target = str(step.object_id) if step.object_id else step.title.split()[-1]
    return marker, style, action, kind, target


def render_plan(
    plan: Plan, console: Console, *, ref_names: dict[str, str] | None = None
) -> None:
    console.print(
        f"[bold]Plan[/bold] for [cyan]{plan.target}[/cyan]  "
        f"{_decorate_ref(plan.from_ref, ref_names)} → {_decorate_ref(plan.to_ref, ref_names)}"
    )
    if not plan.steps and not plan.warnings and not plan.migrations and not plan.baselined:
        console.print("[green]✓ nothing to do — target is up to date[/green]")
        return

    for m in plan.migrations:
        console.print(f"[magenta]→ migration[/magenta] run  {m.id}")
    if plan.baselined:
        console.print(
            f"[dim]migration baseline (recorded, not run): {', '.join(plan.baselined)}[/dim]"
        )

    if plan.steps:
        n_destroy = sum(1 for s in plan.steps if "drop" in s.title.lower())
        console.print(
            f"\n[bold]Plan:[/bold] {len(plan.steps)} to change, {n_destroy} to destroy.\n"
        )
        rows = [_step_row(s) for s in plan.steps]
        w_action = max((len(r[2]) for r in rows), default=6)
        w_kind = max((len(r[3]) for r in rows), default=8)
        for (marker, style, action, kind, target), step in zip(rows, plan.steps):
            console.print(
                f"  [{style}]{marker}[/{style}] [{style}]{action:<{w_action}}[/{style}]  "
                f"[dim]{kind:<{w_kind}}[/dim]  {target}"
            )
            if step.note:  # surface *why* a step is flagged (e.g. NOT NULL without default)
                console.print(f"      [dim]↳ {step.note}[/dim]")

    if plan.warnings:
        console.print("\n[bold yellow]Warnings[/bold yellow]")
        for w in plan.warnings:
            console.print(f"  [yellow]![/yellow] {w}")

    if plan.has_destructive:
        console.print(
            "\n[red bold]Plan contains destructive steps[/red bold] — "
            "they require [bold]--allow-destructive[/bold] to apply."
        )


def render_drift(
    rep: DriftReport,
    console: Console,
    *,
    target: str,
    ref: str,
    ref_names: dict[str, str] | None = None,
    scope: str | None = None,
) -> None:
    """Render a drift report with explicit direction, grouping and counts (CONCEPT.md §9).

    The old output was one flat line per item and never made clear *which way* a difference
    ran. Here each group states the direction in its heading, and column drift uses ``+`` (in
    the repo, missing from the DB) vs. ``−`` (in the DB, not in the repo).
    """
    head = f"[bold]Drift[/bold] for [cyan]{target}[/cyan]  {_decorate_ref(ref, ref_names)}"
    if scope:
        head += f"  [dim](scope: {scope})[/dim]"
    console.print(head)

    if rep.clean and not rep.unreadable:
        console.print("[green]✓ no drift — the database matches the desired state.[/green]")
        return

    n_add = sum(len(cd.added) for cd in rep.columns)
    n_del = sum(len(cd.removed) for cd in rep.columns)
    tally = [
        f"[yellow]{len(rep.missing)}[/yellow] to create" if rep.missing else None,
        f"[red]{len(rep.orphaned)}[/red] orphaned" if rep.orphaned else None,
        f"[yellow]{len(rep.columns)}[/yellow] tables with column drift"
        f" ([green]+{n_add}[/green]/[red]−{n_del}[/red])" if rep.columns else None,
        f"[yellow]{len(rep.definitions)}[/yellow] changed views" if rep.definitions else None,
        f"[dim]{len(rep.advisory)} advisory[/dim]" if rep.advisory else None,
        f"[dim]{len(rep.unreadable)} unreadable[/dim]" if rep.unreadable else None,
    ]
    shown = [t for t in tally if t]
    console.print(("  " + "  ·  ".join(shown) if shown else "  [green]in sync[/green]") + "\n")

    def _section(title: str, style: str, rows: list[str]) -> None:
        if not rows:
            return
        console.print(f"[{style} bold]{title}[/{style} bold]  [dim]({len(rows)})[/dim]")
        for r in rows:
            console.print(f"  {r}")
        console.print()

    _section(
        "Only in the repo — will be created on apply", "yellow",
        [f"{k.value:9} {oid}" for k, oid in rep.missing],
    )
    _section(
        "Only in the database — not in the repo", "red",
        [f"{k.value:9} {oid}" for k, oid in rep.orphaned],
    )
    if rep.columns:
        console.print(f"[yellow bold]Column drift[/yellow bold]  [dim]({len(rep.columns)})[/dim]")
        console.print("  [dim](+ in repo, missing from DB · − in DB, not in repo)[/dim]")
        for cd in rep.columns:
            console.print(f"  {cd.table}")
            if cd.added:
                console.print(f"    [green]+ {', '.join(cd.added)}[/green]")
            if cd.removed:
                console.print(f"    [red]− {', '.join(cd.removed)}[/red]")
        console.print()
    _section(
        "View definition differs", "yellow",
        [f"{k.value:9} {oid}" for k, oid in rep.definitions],
    )
    _section(
        "Procedural definition differs — advisory (unreliable across engines)", "dim",
        [f"{k.value:9} {oid}" for k, oid in rep.advisory],
    )
    _section(
        "Could not introspect — advisory", "dim",
        [f"{k.value:9} {oid}" for k, oid in rep.unreadable],
    )


def plan_to_sql(plan: Plan, *, state_ddl: str | None = None, record_sql: str | None = None) -> str:
    """Render the plan as a single, ordered vanilla-SQL script (CONCEPT.md §7).

    Self-contained for a hand-run on a system without dbly: ledger DDL up front, each step
    annotated with its severity/source, the deploy recorded at the end. Destructive steps
    are included but loudly marked — the human reviewing the script is the gate.
    """
    out: list[str] = [
        "-- dbly deployment script — review before running.",
        f"-- target: {plan.target}",
        f"-- refs:   {plan.from_ref or '<empty>'} -> {plan.to_ref}",
        "-- run by hand via psql / sqlplus / sqlcmd. Wrap in a transaction on",
        "-- transactional-DDL engines (e.g. Postgres) if you want all-or-nothing.",
    ]
    if plan.has_destructive:
        out.append("-- WARNING: contains DESTRUCTIVE steps (marked !! below).")
    for w in plan.warnings:
        out.append(f"--   ! {w}")
    out.append("")

    if state_ddl:
        out += ["-- ledger table (no-op if it already exists)", state_ddl, ""]

    safe_ref = plan.to_ref.replace("'", "''")
    for m in plan.migrations:
        mid = m.id.replace("'", "''")
        out.append(f"-- migration (run-once): {m.id}")
        body = m.sql.rstrip()
        out.append(body if body.endswith(";") else body + ";")
        out.append(
            "INSERT INTO dbly_state (deployed_sha, migration_id) "
            f"VALUES ('{safe_ref}', '{mid}');"
        )
        out.append("")
    for mid in plan.baselined:
        safe_mid = mid.replace("'", "''")
        out.append(f"-- migration baseline (recorded, not run): {mid}")
        out.append(
            "INSERT INTO dbly_state (deployed_sha, migration_id) "
            f"VALUES ('{safe_ref}', '{safe_mid}');"
        )
        out.append("")

    for i, step in enumerate(plan.steps, 1):
        mark = " !! DESTRUCTIVE" if step.severity is Severity.DESTRUCTIVE else ""
        out.append(f"-- [{i}] {step.severity.value}{mark}: {step.title}")
        if step.source_file:
            out.append(f"--     source: {step.source_file}")
        if step.note:
            out.append(f"--     note: {step.note}")
        sql = step.sql.rstrip()
        out.append(sql if sql.endswith(";") else sql + ";")
        out.append("")

    if record_sql:
        out += ["-- record the deploy in the dbly ledger", record_sql, ""]
    return "\n".join(out)


def plan_to_yaml(plan: Plan) -> str:
    doc = {
        "target": plan.target,
        "from_ref": plan.from_ref,
        "to_ref": plan.to_ref,
        "warnings": plan.warnings,
        "migrations": [
            {"id": m.id, "source_file": str(m.source_file), "sql": m.sql}
            for m in plan.migrations
        ],
        "baselined": plan.baselined,
        "steps": [
            {
                "title": s.title,
                "object": str(s.object_id) if s.object_id else None,
                "kind": s.kind.value,
                "severity": s.severity.value,
                "source_file": str(s.source_file) if s.source_file else None,
                "note": s.note,
                "sql": s.sql,
            }
            for s in plan.steps
        ],
    }
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def plan_from_yaml(text: str) -> Plan:
    doc = yaml.safe_load(text)
    plan = Plan(target=doc["target"], from_ref=doc.get("from_ref"), to_ref=doc["to_ref"])
    plan.warnings = list(doc.get("warnings") or [])
    plan.baselined = list(doc.get("baselined") or [])
    plan.migrations = [
        Migration(m["id"], m["sql"], Path(m["source_file"]))
        for m in (doc.get("migrations") or [])
    ]
    for s in doc.get("steps") or []:
        obj = s.get("object")
        oid = None
        if obj:
            schema, _, name = obj.rpartition(".")
            oid = ObjectId(schema=schema or None, name=name)
        plan.steps.append(
            Step(
                title=s["title"],
                object_id=oid,
                kind=ObjectKind(s["kind"]),
                severity=Severity(s["severity"]),
                sql=s["sql"],
                source_file=Path(s["source_file"]) if s.get("source_file") else None,
                note=s.get("note"),
            )
        )
    return plan
