# Identity Schema Migrations

Versioned SQL migrations applied by
`plinth_identity.migration_runner.MigrationRunner`. Invoked on startup (when
`Settings.auto_migrate=True`, the default) and via the CLI:

```bash
python -m plinth_identity migrate              # apply pending
python -m plinth_identity migrate --status     # show applied + pending
python -m plinth_identity migrate --to 0001    # apply up to 0001 inclusive
python -m plinth_identity migrate --create "add foo"   # scaffold new file
```

## Layout

```
migrations/
├── README.md
├── 0001_initial.sql          # issued_tokens, tenants (v0.3)
├── 0002_signing_keys.sql     # signing_keys (v0.4)
└── (optional) <id>_rollback.sql
```

Each `.sql` file is a forward migration. Rollback support is on the v0.6
roadmap.

## Tracking table

The runner manages a `schema_migrations` table created on first invocation:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  id TEXT PRIMARY KEY,                  -- e.g. "0002_signing_keys"
  applied_at TIMESTAMP NOT NULL,
  duration_ms INTEGER NOT NULL,
  checksum TEXT NOT NULL                -- sha256 of the SQL file content
);
```

## Backwards compatibility

The runner gracefully accepts databases provisioned by the legacy
`init_db` bootstrap (v0.3–v0.4):

1. If `schema_migrations` doesn't exist, it's created.
2. For each migration in order, the runner inspects whether every table
   it would create already exists. If so, the migration is **marked
   applied** without re-running its statements.

## Locking

Migrations execute under an exclusive file-system lock at
`$DATA_DIR/.migration.lock`. Concurrent processes serialise.

## Atomicity

Each migration file runs in its own transaction. A SQL error rolls back
the whole file — no half-state.
