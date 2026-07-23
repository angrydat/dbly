# dbly — TODO / Ideen-Backlog

Kurzliste möglicher Verbesserungen, entstanden aus dem Vergleich mit **Atlas**
(atlasgo.io). Das sind Funktionen, die Atlas (teils nur in der kommerziellen
Pro-Edition) bietet und die dbly aktuell **nicht** hat — als Anregung, nicht als
Roadmap. dbly-Stärken (frei, MIT, Oracle/MSSQL nativ, `.py`-Hooks, `plan --sql`)
bleiben der Ausgangspunkt.

## Migrations- & Sicherheits-Analyse
- **Migration-Linting / Analyzer**: destruktive/riskante Schritte erkennen und
  warnen bzw. blocken (Atlas `migrate lint`). dbly kennt bisher nur additiv vs.
  destruktiv + `--allow-destructive`.
- **Custom Schema Rules / Policies** (z. B. „jede Tabelle braucht PK", Namens-
  konventionen, Boolean = NUMBER(1) CHECK (0,-1) erzwingen).
- **Pre-Migration-Checks** (Vorbedingungen prüfen, z. B. Spalte leer vor NOT NULL).
- **PII-/Security-as-Code**: Spalten als sensibel markieren; RLS deklarativ.

## Sichtbarkeit
- **ERD / Schema-Visualisierung** + Auto-Doku aus dem Objektmodell.
- **Schema-Registry** / zentrale „source of truth" mit Versionsvergleich über
  Umgebungen hinweg.
- **Kontinuierliches Drift-Monitoring**: dbly hat `check` on-demand; es fehlt ein
  laufendes/alarmierendes Monitoring.
- ~~**Ref-Dekoration in `status` / Plan-Header** (Komfort)~~ ✅ **erledigt (0.3.0)**:
  `status` und der Plan-Header zeigen jetzt Tag-/Branch-Namen neben dem SHA
  (`deployed ref: v0.1, main (9ff5e440)`), aufgelöst via `git tag/branch --points-at`.
  Der Ledger speichert weiterhin nur den SHA.

## Autoren-Erlebnis
- Zusätzliche **Schema-Sprache (HCL o. ä.)** und/oder **ORM-Loader** — DB-agnostische
  Definition neben plain SQL.
- **Testing-Framework** für Schema/Objekte (Atlas `atlas test`).
- **Interaktive Migrations / Checkpoints** für lange Migrationsketten.
- ~~**Plan gegen den Working-Tree** (uncommittete Änderungen)~~ ✅ **erledigt (0.3.0)**:
  `dbly plan --worktree` (Alias `--dirty`) und `dbly check --worktree` planen/prüfen
  den Arbeitsstand inkl. ungetrackter neuer Objektdateien. Preview-only — `apply`
  braucht weiterhin einen committeten Ref für den Ledger.

## Integration
- Breitere fertige **CI-Rezepte** (GitLab, Azure DevOps, CircleCI) — aktuell nur
  GH-Actions/Bitbucket-Beispiele.
- **Terraform-Provider / K8s-Operator** (GitOps).
- **Rollout-/Approval-Flow** (Freigabe vor Apply in Prod).

## Bekannte Design-Frage (aus dem MA31-PoC)
- Im Planner werden **Tabellen in Changeset-Reihenfolge** emittiert;
  `topological_order` wird nur auf *replaceable* (Views/Functions) angewandt. Bei
  Inline-FKs zwischen Tabellen ist auf einem frischen Ziel die Anlege-Reihenfolge
  dann nicht FK-sicher — prüfen, ob Tabellen ebenfalls topologisch sortiert werden
  sollten (oder ob der Adapter das per retry-until-stable abfängt).

## Bugs (aus MA31-PoC, Oracle)

- ~~**Spalten-Typänderung wird nicht erkannt.**~~ ✅ **erledigt (0.3.0)**: Der
  Spalten-Diff verglich nur den Namen; `NUMBER` → `NUMBER(10)` blieb unbemerkt. Jetzt
  wird bei geändertem Typ ein `ALTER TABLE … MODIFY`/`ALTER COLUMN`-Schritt erzeugt
  (als `destructive` markiert, nie auto-appliziert, mit Datenkompatibilitäts-Warnung).
  Der Vergleich ist Precision/Scale-bewusst (`NUMBER(10)` ≡ `NUMBER(10,0)`) und
  normalisiert Introspektions-Rauschen (z. B. SQL-Server `COLLATE`) gegen False Positives.
- ~~**ADD COLUMN fälschlich als „destructive" klassifiziert.**~~ ✅ **geklärt (0.3.0)**:
  `TESTFELD` war `NOT NULL` **ohne** Default — die `destructive`-Einstufung ist damit
  korrekt (kann auf einer befüllten Tabelle nicht sicher hinzugefügt werden). Verbessert:
  der Grund (*„NOT NULL without default … — unsafe"*) wird jetzt als Hinweis direkt am
  Plan-Schritt angezeigt, nicht nur im Warnungsblock.
- ~~**`check` meldet vorhandene Objekte als „missing".**~~ ✅ **erledigt (0.3.0)**:
  Ursache war eine asymmetrische Key-Normalisierung — der Oracle-Adapter setzte kein
  `default_schema` und ließ den Owner in `inventory()` weg, sodass die Live-Keys
  (`table:<name>`) nicht zu den Desired-Keys (`table:dbb.<name>`) passten. Der Adapter
  löst `default_schema` jetzt zum verbundenen `USER` auf und führt den Owner mit; beide
  Seiten normalisieren identisch.
