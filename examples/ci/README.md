# CI: the trunk-based-development gate

These pipelines make *“the trunk is always deployable”* an automatic, enforced fact —
the one guarantee trunk-based development depends on. Drop one into the repo that holds
your database object files (not the dbly repo).

On every change, against a **throwaway database**, the pipeline proves:

| Check | Command | Proves |
|---|---|---|
| **Greenfield** | `dbly apply --to HEAD` on an empty DB | the trunk builds a correct database from scratch |
| **Drift** | `dbly check --to HEAD` | desired state == the live DB after apply |
| **Upgrade** (PRs) | `dbly apply --to origin/main` → `dbly plan/apply --from origin/main --to HEAD` | the path from the released baseline applies cleanly and stays additive |
| **Behaviour** | `dbression run ./tests` | no regressions in schema or business logic |

Together: `dbly` deploys, `dbression` verifies → a green gate that keeps the trunk
releasable. Developers integrate early and often; deployment to customers stays a separate,
scheduled act (`dbly apply --to <release-tag>` in the maintenance window, or
`dbly plan --sql` for a hand-deploy through a VPN).

## Notes

- **No secrets in the repo.** CI starts an ephemeral Postgres and writes the
  `connection.properties` at runtime. For real targets, use repository/pipeline secrets and
  `${ENV}` placeholders in the profile.
- **One profile, two tools.** dbly reads it via `--target`; dbression discovers it next to
  the suite. The extra `environment=` key is ignored by dbression.
- **The release artifact** (`dbly plan --sql`) is only an accurate *diff* when generated
  against a copy of production (read access). Against an empty CI DB it is the full bootstrap
  script — fine for a fresh customer, not for an upgrade.
- Postgres is dbly’s reference engine here; the same pattern works for SQL Server / Oracle by
  swapping the service image, the `environment=` value, and installing the matching extra
  (`uv tool install "dbly[mssql]"` / `"dbly[oracle]"`).

Files: [`bitbucket-pipelines.yml`](bitbucket-pipelines.yml) · [`github-actions.yml`](github-actions.yml)
