# dbly — TODO / Ideen-Backlog

Stand: **0.14.1**. Erledigtes ist unten zusammengefasst; offen sind die neuen, aus
echter Nutzung entstandenen Punkte plus der ursprüngliche Atlas-inspirierte Backlog.

## Als Nächstes (aus realer Nutzung, priorisiert)
- **`dbly baseline`** — deployten Ref im Ledger eintragen *ohne* SQL auszuführen. Für
  Brownfield-/per-psql-deployte DBs, damit `plan` inkrementell diff't statt alles als
  Bootstrap zu listen. → *in Arbeit*
- **Ersetzbare Objekte pro *Datei* anwenden** (statt pro re-gerendertem Statement).
  Erhält `SET search_path` / `ALTER … OWNER` / Kommentare / Datei-Reihenfolge / Overloads
  und behebt die tiefere Ordering-/Faithfulness-Fragilität. Siehe ADR. → *in Arbeit*
- **View-Round-Trip für Oracle/SQL Server** — zurückgestellt (ADR 0001) bis ein
  Live-Ziel zum Verifizieren da ist. PG ist fertig; Oracle/MSSQL nutzen bis dahin den
  strukturellen Fallback (ohne Cast-Elision-Handling).
- **`check`-Performance** — Introspektion bündeln (eine Inventar-/Spalten-Abfrage statt
  N Einzelroundtrips); spürbar bei ~1400 Objekten.
- **Funktion→Funktion-Ordering über PL/pgSQL-Bodies** — sqlglot parst Bodies nicht;
  Aufrufe an der Oberfläche (`EXECUTE FUNCTION`, `FROM func()`) sind erfasst, Body-interne
  nicht. Ging bei `download` nur auf, weil die Overloads in einer Datei stehen.
- Kleinkram: AND/OR-Präzedenz in der `--show-diff`-*Anzeige* (Hash-Vergleich ist korrekt).

## Migrations- & Sicherheits-Analyse (Atlas-Backlog)
- **Migration-Linting / Analyzer**: destruktive/riskante Schritte erkennen und warnen
  bzw. blocken (Atlas `migrate lint`). dbly kennt bisher additiv vs. destruktiv +
  `--allow-destructive`.
- **Custom Schema Rules / Policies** (z. B. „jede Tabelle braucht PK", Namens­konventionen,
  Boolean = NUMBER(1) CHECK (0,-1) erzwingen).
- **Pre-Migration-Checks** (Vorbedingungen prüfen, z. B. Spalte leer vor NOT NULL).
- **PII-/Security-as-Code**: Spalten als sensibel markieren; RLS deklarativ.

## Sichtbarkeit
- **ERD / Schema-Visualisierung** + Auto-Doku aus dem Objektmodell.
- **Schema-Registry** / zentrale „source of truth" mit Versionsvergleich über Umgebungen.
- **Kontinuierliches Drift-Monitoring** (dbly hat `check` on-demand; es fehlt laufendes/
  alarmierendes Monitoring).

## Autoren-Erlebnis
- Zusätzliche **Schema-Sprache (HCL o. ä.)** und/oder **ORM-Loader**.
- **Testing-Framework** für Schema/Objekte (Atlas `atlas test`).
- **Interaktive Migrations / Checkpoints** für lange Migrationsketten.

## Integration
- Breitere **CI-Rezepte** (GitLab, Azure DevOps, CircleCI) — aktuell GH-Actions/Bitbucket.
- **Terraform-Provider / K8s-Operator** (GitOps).
- **Rollout-/Approval-Flow** (Freigabe vor Apply in Prod).

---

## Erledigt

### 0.4.0 – 0.14.1
- **`dbly.toml`-Projektconfig** (`object_root`, `environment`, `[targets]`, `ignore`).
- **Teil-Deploy** `--schema` / `--path`; **`--with-deps`** (fehlende Dependency-Closure,
  stoppt an existierenden Objekten).
- **View-Drift strukturell korrekt** (ADR 0001): Engine-Round-Trip (Wegwerf-TEMP-View) +
  AST-Vergleich (Parens/Qualifizierer/FROM-Join-Flatten, präzedenzsicher). Auf echtem
  Schema **41 → 0** Falschmeldungen.
- **`dbly check --show-diff`** (Unified-Diff je geänderter View/Definition).
- **`dbly export`** — Live-DB → DDL, cross-dialect; Prozedurales verbatim.
- **Struktureller Typ-Vergleich** (kein `INTEGER→INT`/`NUMERIC→DECIMAL`/tz/array/geometry-
  Rauschen mehr), treues DB-Typ-Rendering.
- **Terraform-Style-Ausgabe** für `plan`/`apply`/`check`; **Grants apply-only** in `check`.
- **`CREATE SCHEMA`** für Objekt-Schemas (Greenfield, PG/MSSQL).
- **FK-Tabellen-Ordering**, **Overload-** und **Funktions-Ordering**; Vorab-Warnung bei
  out-of-scope Dependencies; saubere Apply-Fehler (kein Traceback).
- **Quiet by default** (sqlglot/SQLAlchemy-Rauschen; `--debug` schaltet's ein).

### 0.3.0
- **Ref-Dekoration** in `status`/Plan-Header (Tag-/Branch-Namen neben dem SHA).
- **Plan/Check gegen Working-Tree** (`--worktree`/`--dirty`).
- Bug: **Spalten-Typänderung** wird erkannt (`MODIFY`/`ALTER COLUMN`, destructive).
- Bug: **ADD COLUMN**-Severity geklärt (`NOT NULL` ohne Default = korrekt unsafe, mit Hinweis).
- Bug: **`check` meldete vorhandene Objekte als „missing"** (Oracle `default_schema`/Owner).
- Design-Frage **Tabellen topologisch sortieren** — gelöst (FK-Ordering, 0.13.0).
