## Context

See proposal.md (Why) and the full design discussion in `docs/Calibration-Store-Postgres.md`. The
current state, from the archived `2026-09-24-move-calibration-to-redis` change:

- **Values.** `deploy/calibration/calibration.redis` is a `redis-cli` command script, and it is the
  only place values are defined. Its `#` comments carry the per-symbol fit rationale.
- **Seeding.** `deploy/calibration/seed.sh` runs in the one-shot `calibration-seeder` service
  (stock `redis:7.4-alpine`). It dry-runs the script on scratch DB 15 and greps `(error)`, because
  a Redis `MULTI`/`EXEC` does not roll back a command that fails while executing.
- **Reading.** `CalibrationStore` (`feed_adapter_synthetic/calibration_store.py`) reads the keyspace
  in three pipelined round trips. It validates fail-fast (decimal parsing, `_is_power_of_ten`,
  `_to_code_units`, family/count/name/position checks against `distributions.FAMILY_PARAMETERS`
  and `UNBOUNDED_PARAMETERS`, a positive scale/shape check, a float-range check) and returns sorted
  file symbols plus a `dict[str, SymbolCalibration]`.
- **Adapter startup.** `__main__._run_service` builds `Readiness("redis", "calibration")`, awaits
  `wait_for_redis` (retries on `RedisError`), then loads calibration.
- **Postgres.** It runs from `timescale/timescaledb:2.17.2-pg16@sha256:4e459e…`. Its password is
  the Compose file secret `postgres_password`, created at mode `0600` by
  `deploy/bootstrap-secrets.sh`. No Python service connects to Postgres yet, so the adapter is the
  first; `psycopg[binary]>=3.2` is only in the workspace dev group today.
- **Test style.** Tests hand-roll their fakes. There is no mock library and no `conftest.py`.
  Integration tests use a TimescaleDB container behind the `integration` marker, following
  `libs/tqtk-common/tests/test_storage_fixture.py`.

## Goals / Non-Goals

**Goals:**
- Loading from Postgres gives `==` the same `dict[str, SymbolCalibration]` that loading from Redis
  gives today, for all 17 instruments. A check proves this before the Redis script is deleted.
- Any seeding failure leaves the previous seeding intact, with no dry-run machinery.
- Validation can be tested without a database: parsing is a pure function over rows.

**Non-Goals:**
- Migrations tooling (Alembic or similar) for the `calibration` schema.
- A long-lived connection pool in the adapter.
- Changing `SymbolCalibration`, the samplers, or anything downstream of the load.
- Cleaning old Redis keys automatically. This is documented, not scripted into the stack.

## Decisions

### The seeder owns the schema's DDL, and re-creates it on every seeding
`calibration.sql` begins `DROP SCHEMA IF EXISTS calibration CASCADE; CREATE SCHEMA calibration;`,
creates the three tables with their constraints, then inserts every row. `psql
--single-transaction` wraps the whole file, so a reader sees the old seeding or the new one. This
follows the repo rule that DDL ownership follows sole-writer ownership
(`deploy/postgres/initdb/001-timescaledb.sql`). The script stays the whole truth for schema and
data, and an instrument removed from it disappears without a delete list.

**Alternative rejected:** versioned DDL migrations plus a data-only seed. That is more machinery for
a single-writer schema that is fully rebuilt on every seeding, and it would still need a
"delete what's not in the script" step.

### The database enforces structure, and the reader enforces code knowledge
Constraints cover what the database can know: primary and foreign keys, `ON DELETE CASCADE`, a
unique `file_symbol`, `CHECK`s on `quantity`, `unit`, `position >= 0`, positive
`pip_size`/`initial_price` and finite `value`. The reader keeps the rules that depend on code:
- `pip_size` is a power of ten written without trailing zeros (`numeric` keeps the scale, so the
  existing `_is_power_of_ten` check works unchanged on the `Decimal` psycopg returns)
- family parameter count, names and order (`FAMILY_PARAMETERS`)
- positive scale/shape (`UNBOUNDED_PARAMETERS`)
- float range

**Alternative rejected:** encoding family parameter lists in a lookup table with foreign keys. That
duplicates `FAMILY_PARAMETERS` in a second place that can drift from the samplers.

### One query with LEFT JOINs, parsed by a pure function
`CalibrationStore` takes an injected `psycopg.AsyncConnection` and runs one query:
`instrument LEFT JOIN distribution LEFT JOIN parameter ORDER BY venue_symbol, quantity, position`.
The `LEFT JOIN`s turn a missing distribution or parameter into `NULL` columns, so it is reported
instead of silently dropped. A module-level `parse_rows(rows) -> dict[str, SymbolCalibration]`
does all validation and conversion. Tests call it with literal row tuples, so every current
fail-fast case ports without a database. Error messages name `calibration.<table>` and the row key,
e.g. `'EUR/USD': calibration.parameter (return, position 1) ...`.

"Not seeded" has two causes that get one message pointing at `calibration-seeder`:
- an empty result
- `psycopg.errors.UndefinedTable` (the schema was never created)

Missing quantities are detected against the fixed set `return`, `spread`, `interval`, which
replaces the old `calib:quantities` set.

**Alternative rejected:** three separate queries, one per table. More round trips, and joining in
Python again.

### Connection opened for the load and then closed; Postgres gets its own readiness entry
`wait_for_redis` becomes a generic `wait_for(dependency, probe, retry_on, ...)`. The Redis call
passes `RedisError`. The Postgres call passes `psycopg.OperationalError`, with a probe that opens
the connection. Readiness becomes `Readiness("redis", "postgres", "calibration")`. After the load,
the connection is closed (`async with`), so a Postgres outage after startup cannot affect
generation. `postgres` stays marked connected after the close. It reports "reached at startup",
matching how the load is a startup-only step.

**Alternative rejected:** a pool, or keeping the connection open for later reloads. Nothing reads
calibration after startup (hot reload is out of scope).

### Password from a file, never from the URL
`FeedConfig` gains `postgres_url: str = "postgresql://tqtk@localhost:5432/tqtk"` and
`postgres_password_file: Path | None = None`. When a file is set, its stripped contents are passed
as psycopg's `password=` keyword. This matches the stack's `_FILE` convention (`POSTGRES_PASSWORD_FILE`,
Grafana's `__FILE`) and keeps the secret out of the environment and out of any logged URL. The
seeder's `seed.sh` does the same for `psql`, exporting `PGPASSWORD` from
`/run/secrets/postgres_password` inside the script only.

### Seeder uses the Postgres image already in the stack
`calibration-seeder` runs `timescale/timescaledb` at the same pinned digest as `postgres`, for its
`psql`. It mounts `seed.sh` and `calibration.sql` read-only and receives `postgres_password` as a
secret. It has `depends_on: postgres: service_healthy` and `restart: "no"`. The adapter keeps
gating on `service_completed_successfully` and adds `depends_on: postgres: service_healthy`.
`seed.sh` is a single command, `psql -h "$host" -U tqtk -d tqtk -v ON_ERROR_STOP=1
--single-transaction -f "$script"`. Its host and script are overridable (`SEED_PG_HOST`,
`SEED_SCRIPT`) for the integration test, as today.

**Alternative rejected:** a smaller `postgres:*-alpine` client image. It adds a second image digest
to pin and track for no gain, since the timescale image is already pulled.

### `postgres_password` becomes `0644`
Compose file secrets are bind-mounted with host ownership and mode, so the adapter (uid 1001)
cannot read a `0600` file. This is the same situation, and the same fix, as `grafana_admin_password`.
The `0700` `deploy/secrets/` directory still keeps other host accounts out. `bootstrap-secrets.sh`
already corrects the mode of existing files, so re-running it repairs existing machines.

### The SQL script is generated once, then hand-maintained
A throwaway converter (not committed) turns `calibration.redis` into `calibration.sql`, carrying
each `#` comment across as `--` beside the rows it explains. After that, the SQL file is edited by
hand, as the Redis script was.

## Risks / Trade-offs

- **The adapter gains a second infrastructure dependency (Postgres + secret).** This reverses the
  Redis change's "resolve reference data from the bus it already uses" rationale. → Accepted
  deliberately in exchange for SQL-queryable calibration. The dependency is startup-only, and
  `/ready` reports it separately.
- **`DROP SCHEMA … CASCADE` destroys any object another component builds against the schema.** →
  The spec forbids consumers creating anything in or against it. Any grants go in the seed script.
- **A reader blocked on the schema lock while a re-seed commits can fail** with "relation does not
  exist", because the old tables' OIDs are gone. → The adapter only reads after the seeder has
  finished (Compose ordering) and retries nothing mid-load. Future ad-hoc consumers re-run their
  query. This is documented in `deploy/README.md`.
- **The conversion could silently change a value.** → The equivalence check (task 4.1) compares the
  full Redis load with the Postgres load before `calibration.redis` is deleted. Its result is
  recorded below.
- **Making `postgres_password` world-readable inside `deploy/secrets/`.** → The directory stays
  `0700`, as reasoned in `bootstrap-secrets.sh` for Grafana.
- **Stale Redis keys on existing volumes.** → They are inert, since no reader remains.
  `deploy/README.md` gives the one-time `redis-cli` cleanup.

## Migration Plan

1. Generate `calibration.sql` and implement the new seeder and reader while `calibration.redis` and
   the Redis reader still exist.
2. Run the equivalence check (Redis load `==` Postgres load for all 17 instruments), and record the
   result under "Equivalence result" below.
3. Delete `calibration.redis` and the Redis read path, then switch Compose.
4. On existing machines: re-run `deploy/bootstrap-secrets.sh` (fixes the secret mode), then
   `docker compose -f deploy/docker-compose.yml up -d --build`. Optionally clean the old Redis keys
   per `deploy/README.md`.

**Rollback:** revert the change's commits. The Redis script and seeder come back, and re-seeding
Redis restores the old path. The `calibration` schema left in Postgres is inert, and
`DROP SCHEMA calibration CASCADE` removes it.

## Equivalence result

_To be filled in during implementation (task 4.1)._
