"""Render plans for humans (rich) and for machines (YAML manifest, CONCEPT.md §7).

The plan artifact is a vanilla-SQL bundle plus a YAML manifest carrying only what SQL can't
express: ordering, severity, source provenance and warnings.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from dbly.model import ObjectId, ObjectKind, Plan, Severity, Step


def render_plan(plan: Plan, console: Console) -> None:
    console.print(
        f"[bold]Plan[/bold] for [cyan]{plan.target}[/cyan]  "
        f"{plan.from_ref or '∅'} → {plan.to_ref}"
    )
    if not plan.steps and not plan.warnings:
        console.print("[green]nothing to do — target is up to date[/green]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("severity")
    table.add_column("kind")
    table.add_column("step")
    for i, step in enumerate(plan.steps, 1):
        sev_style = "red" if step.severity is Severity.DESTRUCTIVE else "green"
        table.add_row(
            str(i),
            f"[{sev_style}]{step.severity.value}[/{sev_style}]",
            step.kind.value,
            step.title,
        )
    console.print(table)

    if plan.warnings:
        console.print("\n[bold yellow]Warnings[/bold yellow]")
        for w in plan.warnings:
            console.print(f"  [yellow]![/yellow] {w}")

    if plan.has_destructive:
        console.print(
            "\n[red bold]Plan contains destructive steps[/red bold] — "
            "they require [bold]--allow-destructive[/bold] to apply."
        )


def plan_to_yaml(plan: Plan) -> str:
    doc = {
        "target": plan.target,
        "from_ref": plan.from_ref,
        "to_ref": plan.to_ref,
        "warnings": plan.warnings,
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
