# Workspace Schema Migrations

This directory holds versioned forward migrations applied by
`plinth_workspace.migration_runner.MigrationRunner`. The runner is invoked on
service startup (when `Settings.auto_migrate` is `True`, the default) and is
also reachable via the CLI:

```bash
python -m plinth_workspace migrate              # apply pending
python -m plinth_workspace migrate --status     # show applied + pending
python -m plinth_workspace migrate --to 0003    # apply up to 0003 inclusive
python -m plinth_workspace migrate --create "add foo"   # scaffold new file
```

## Layout

```
migrations/
├── README.md
├── 0001_initial.sql
├── 0002_channels.sql
├── 0003_workflows.sql
├── 0004_tenancy.sql
├── 0005_retention.sql
└── (optional) <id>_rollback.sql
```

Each `.sql` file is a forward migration. Rollback files are optional and
documentary in v0.5 (see `<id>_rollback.sql`); automated rollback is on the
v0.6 roadmap (`migrate --to <older_id>` will execute them in reverse).

## Tracking table

The runner manages a `schema_migrations` table created on first invocation:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  id TEXT PRIMARY KEY,                  -- e.g. "0003_workflows"
  applied_at TIMESTAMP NOT NULL,
  duration_ms INTEGER NOT NULL,
  checksum TEXT NOT NULL                -- sha256 of the SQL file content
);
```

This bootstrap table itself is **not** captured as a migration file — it's
the runner's metadata, created automatically.

## Backwards compatibility

The runner gracefully accepts databases provisioned by the legacy
`CREATE TABLE IF NOT EXISTS` bootstrap (v0.1–v0.4):

1. If `schema_migrations` doesn't exist, it's created.
2. For each migration file in order, the runner inspects whether the tables
   it would create already exist. If so, the migration is **marked applied**
   without re-running its statements.
3. If a migration is partially-already-applied (some tables exist, some
   don't), it executes — the SQL is built from idempotent
   `CREATE ... IF NOT EXISTS` so partial state heals to the target shape.

## Locking

Migrations execute under an advisory lock so concurrent runners don't race:

* **SQLite**: an exclusive file-system lock at `$DATA_DIR/.migration.lock`.
* **Postgres**: `pg_advisory_lock(<service-specific id>)`.

If the lock is held, the runner returns 409 (HTTP) or exits with a non-zero
status (CLI).

## Atomicity

Each migration file runs in **its own transaction**. A SQL syntax error or
constraint violation rolls the file back fully — no half-state. Tracking
table writes happen inside the same transaction so applied-state and schema
shape stay in sync.

## Checksums

Every applied migration records the sha256 of its on-disk content. On
subsequent runs the runner compares — a mismatch means someone changed
history, which is a startup error in production. Tests can call
`MigrationRunner.verify_checksums()` to surface mismatches.

## Authoring a new migration

```bash
python -m plinth_workspace migrate --create "add widgets"
```

Drops a file `migrations/000N_add_widgets.sql` with a header. Edit, then run
`migrate` to apply.

Conventions:

* Prefer `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`.
* Keep one logical change per file. Split `0006_add_widgets.sql` and
  `0007_add_widget_indices.sql` rather than batching unrelated work.
* Document irreversible schema removals in a sibling `_rollback.sql`.
