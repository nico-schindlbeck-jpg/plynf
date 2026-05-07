# Gateway Schema Migrations

Versioned SQL migrations applied by
`plinth_gateway.migration_runner.MigrationRunner`. Invoked on startup (when
`Settings.auto_migrate=True`, the default) and via the CLI:

```bash
python -m plinth_gateway migrate              # apply pending
python -m plinth_gateway migrate --status     # show applied + pending
python -m plinth_gateway migrate --to 0003    # apply up to 0003 inclusive
python -m plinth_gateway migrate --create "add foo"   # scaffold new file
```

## Layout

```
migrations/
├── README.md
├── 0001_initial.sql       # tools, audit_events, cache_entries (v0.1)
├── 0002_limits.sql        # agent_limits, rate_limit_snapshots (v0.2)
├── 0003_oauth.sql         # oauth_connections, oauth_states (v0.3)
├── 0004_tenancy.sql       # tenant_id columns (v0.3)
└── (optional) <id>_rollback.sql
```

Each `.sql` file is a forward migration. Rollback is on the v0.6 roadmap.

## Tracking table

The runner manages a `schema_migrations` table created on first invocation:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  id TEXT PRIMARY KEY,                  -- e.g. "0003_oauth"
  applied_at TIMESTAMP NOT NULL,
  duration_ms INTEGER NOT NULL,
  checksum TEXT NOT NULL                -- sha256 of the SQL file content
);
```

## Backwards compatibility

The runner gracefully accepts databases provisioned by the legacy
`CREATE TABLE IF NOT EXISTS` bootstrap (v0.1–v0.4):

1. If `schema_migrations` doesn't exist, it's created.
2. For each migration file in order, the runner inspects whether the tables
   it would create already exist AND whether columns it would ALTER-add
   already exist. If so, the migration is **marked applied** without
   re-running its statements.
3. Partial state heals to the target shape because every CREATE statement
   uses `IF NOT EXISTS`. `ALTER TABLE ... ADD COLUMN` is treated as a
   no-op when the column already exists (the runner swallows SQLite's
   "duplicate column name" error).

## Locking

Migrations execute under an exclusive file-system lock at
`$DATA_DIR/.migration.lock`. Concurrent processes serialise; a
non-blocking acquire returns 409 (HTTP) or exits non-zero (CLI).

## Atomicity

Each migration file runs in **its own transaction**. A SQL syntax error
or constraint violation rolls the file back fully — no half-state. The
`schema_migrations` insert sits inside the same transaction so apply +
record stay in sync.

## Authoring a new migration

```bash
python -m plinth_gateway migrate --create "add widgets"
```

Conventions: prefer `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT
EXISTS`. One logical change per file. Document irreversible removals in a
sibling `_rollback.sql`.
