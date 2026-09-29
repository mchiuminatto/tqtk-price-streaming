## Why

The synthetic feed's calibration (per-instrument symbology, `pip_size`, `quote_currency`,
`initial_price` and the fitted return, spread and tick-interval distributions) lives in a Redis
keyspace shaped for exactly one reader, the adapter. Other consumers, such as `historical-query-svc`,
dashboards and offline re-fit tooling, will want to query and join it with SQL. PostgreSQL is
already the stack's system of record. Moving calibration there also buys transactional seeding,
which removes `seed.sh`'s scratch-DB dry run, plus integrity the database enforces and exact
`numeric` decimals. Design rationale: `docs/Calibration-Store-Postgres.md`.

## What Changes

- **Calibration moves to a PostgreSQL schema `calibration`**, with three tables:
  - `instrument`: venue symbol, unique file symbol, `pip_size`, `quote_currency`, `initial_price`
  - `distribution`: one family per `(venue_symbol, quantity)`
  - `parameter`: name, value and unit per position in the family's fit order

  Foreign keys, `CHECK`s on quantity, unit and positivity, and `numeric` values replace the Redis
  keyspace. The derivable keys `param_count`, `calib:symbols` and `calib:quantities` go away.
  **BREAKING** for any reader of the Redis keyspace; the adapter is the only one today.
- **New seed script `deploy/calibration/calibration.sql`** replaces `calibration.redis` as the
  single source of truth. It drops and recreates the `calibration` schema and inserts every value,
  and carries the per-symbol fit-rationale comments across. The seeder owns the DDL because it is
  the only writer.
- **`calibration-seeder` applies the script with `psql --single-transaction -v ON_ERROR_STOP=1`**
  from the stock `timescale/timescaledb` image, pinned to the `postgres` service's digest. Any
  failure rolls back to the previous seeding and exits non-zero. The scratch-DB dry run and the
  `(error)` output grepping are removed.
- **`feed-adapter-synthetic` reads calibration from PostgreSQL** with one query at startup, through
  a pure rows → `dict[str, SymbolCalibration]` parser that keeps today's validation. It opens the
  connection for the load only, then closes it. It still uses Redis for the tick bus.
  **BREAKING** (deployment): the adapter now needs PostgreSQL and the `postgres_password` secret to
  start.
- **`/ready` gains a `postgres` dependency**, giving `redis`, `postgres` and `calibration`, so a
  Postgres connection problem stays distinguishable from a store problem.
- **`postgres_password` becomes mode `0644`** in `deploy/bootstrap-secrets.sh`, so the adapter's uid
  1001 can read it. This is the same fix Grafana's secret already has.
- **Removed:** `deploy/calibration/calibration.redis`, the Redis calibration read path, and the
  seeder's Redis dependency. `deploy/README.md` documents a one-time cleanup of the old `calib:*`,
  `instrument:*` and `symbology` keys left on existing Redis volumes.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `calibration-store`: the store moves from a Redis keyspace to the `calibration` PostgreSQL schema.
  Its table contract replaces the key contract. Seeding becomes a single `psql` transaction that
  owns the schema's DDL. Consumers may only `SELECT`. The unit tagging, symbology and
  single-source-of-truth rules carry over with Postgres wording.
- `synthetic-feed`: the symbol set and calibration come from the `calibration` tables instead of
  `calib:symbols`/`symbology`. The fail-fast conditions name tables and rows instead of keys, and a
  missing schema also counts as unseeded. `/ready` reports `postgres` as a separate dependency.

## Impact

- **Code:** `services/feed-adapter-synthetic`
  - `calibration_store.py` rewritten for `psycopg.AsyncConnection`
  - `__main__.py`: generic `wait_for`, a Postgres probe, three-dependency readiness
  - `config.py`: `postgres_url`, `postgres_password_file`
  - `calibration.py`: docstring
  - tests rewritten or extended: `test_calibration_store.py`, `test_calibration_seeder.py`,
    `test_main.py`, `test_feed_config.py`
- **Dependencies:** the service gains `psycopg[binary]>=3.2` (already in the workspace dev group);
  `uv.lock` is refreshed.
- **Deployment:** in `deploy/docker-compose.yml`, the seeder switches image and mounts and depends on
  `postgres`. The adapter gains `TQTK_POSTGRES_URL`, `TQTK_POSTGRES_PASSWORD_FILE`, the
  `postgres_password` secret and a `depends_on: postgres`. `deploy/bootstrap-secrets.sh` and
  `deploy/README.md` are edited.
- **Behavior:** none intended for generated ticks. A one-time equivalence check proves the Postgres
  load equals the Redis load for all 17 instruments before the Redis script is deleted.
- **Out of scope:** hot reload of calibration, read-only roles for consumers, and re-fitting any
  distribution.
