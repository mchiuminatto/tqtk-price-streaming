# Moving the Calibration Store from Redis to PostgreSQL

Status: **proposal** - to be captured as the OpenSpec change `move-calibration-to-postgres`.

## Context

The synthetic feed's calibration is its reference data. For each of the 17 symbols it holds:
- venue/file symbology
- `pip_size`, `quote_currency` and `initial_price`
- fitted return, spread and tick-interval distributions

The archived change `2026-09-24-move-calibration-to-redis` moved it into Redis. Today it works like this:
- **Values:** `deploy/calibration/calibration.redis` is a `redis-cli` command script. It is the single source
  of truth for the values, and its `#` comments carry the per-symbol fit rationale.
- **Seeding:** the one-shot `calibration-seeder` Compose service applies the script through
  `deploy/calibration/seed.sh`.
- **Reading:** `CalibrationStore`
  (`services/feed-adapter-synthetic/src/feed_adapter_synthetic/calibration_store.py`) reads the store at
  startup in three pipelined round trips.
- **Contract:** the `calibration-store` spec (`openspec/specs/calibration-store/spec.md`) defines the keyspace:
  `symbology`, `instrument:<venue>`, `calib:<venue>:<quantity>:{family,param_count,param:<n>}`,
  `calib:symbols` and `calib:quantities`.

## Why

**Future consumers.** Other services and tools will want to query and join calibration with SQL:
- `historical-query-svc`
- dashboards
- offline re-fit tooling

The Redis keyspace suits exactly one reader, the adapter. Getting one distribution out of it takes a walk over
`param_count` and `param:<n>` hashes. PostgreSQL is already the stack's system of record, and SQL is the
interface every other consumer already speaks.

Side benefits:
- **Transactional seeding.** One `psql --single-transaction` run either fully applies or fully rolls back. That
  removes the scratch-DB dry run `seed.sh` does today. The dry run exists only because a command that fails
  inside Redis `MULTI`/`EXEC` doesn't roll back the rest, the leading delete included.
- **Integrity enforced by the database.** Foreign keys, `CHECK`s, a unit enum and a unique file symbol are
  enforced at seed time rather than only in reader code.
- **Exact decimals.** `numeric` keeps the stored scale (`0.00001` stays at exponent −5), and psycopg returns it
  as `Decimal`. The exact stored-to-code unit conversion and the power-of-ten `pip_size` rule keep working
  unchanged.

## Cost

The adapter gains a second infrastructure dependency: PostgreSQL and its password secret. It still needs Redis
for the tick bus. This reverses the Redis change's rationale ("resolve reference data from the bus it already
uses"). The trade is deliberate: one extra startup dependency for the adapter, in exchange for calibration
anyone can query.

Calibration is read once at startup, so the adapter holds no Postgres connection while it runs. A Postgres
outage after startup doesn't affect tick generation.

## Design

### Schema `calibration`, owned by the seeder

The seeder is the only writer, so it owns the DDL. This follows the repo rule that DDL ownership follows
sole-writer ownership (`deploy/postgres/initdb/001-timescaledb.sql`). Every seeding runs one transaction:

```sql
BEGIN;
DROP SCHEMA IF EXISTS calibration CASCADE;
CREATE SCHEMA calibration;
-- CREATE TABLE ... ;
-- INSERT ... ;
COMMIT;
```

The script is the whole truth for both the schema and the data:
- Re-applying it unchanged produces an identical store.
- An instrument removed from the script disappears.
- A reader sees either the previous seeding or the new one, never a mix.

**Rule for consumers:** they only `SELECT`. `CASCADE` would drop any view, foreign key or grant that another
component created in or against this schema. Any grants belong in the seed script itself.

**Alternative rejected:** versioned DDL migrations plus a data-only seed. It is more machinery for a schema
with a single writer that is fully rebuilt on every seeding.

### Tables

These replace the Redis keyspace and become the consumer contract in the `calibration-store` spec.

`calibration.instrument`:

| column | type | constraints |
|---|---|---|
| `venue_symbol` | `text` | primary key, e.g. `EUR/USD` |
| `file_symbol` | `text` | `NOT NULL UNIQUE`, e.g. `EURUSD` |
| `pip_size` | `numeric` | `NOT NULL CHECK (pip_size > 0)` |
| `quote_currency` | `text` | `NOT NULL` |
| `initial_price` | `numeric` | `NOT NULL CHECK (initial_price > 0)` |

`calibration.distribution`:

| column | type | constraints |
|---|---|---|
| `venue_symbol` | `text` | FK → `instrument` `ON DELETE CASCADE` |
| `quantity` | `text` | `CHECK (quantity IN ('return','spread','interval'))` |
| `family` | `text` | `NOT NULL` |
| | | primary key `(venue_symbol, quantity)` |

`calibration.parameter`:

| column | type | constraints |
|---|---|---|
| `venue_symbol`, `quantity` | `text` | FK → `distribution` `ON DELETE CASCADE` |
| `position` | `integer` | `CHECK (position >= 0)`, the family's fit order |
| `name` | `text` | `NOT NULL` |
| `value` | `numeric` | `NOT NULL CHECK (value NOT IN ('NaN','Infinity','-Infinity'))` |
| `unit` | `text` | `CHECK (unit IN ('quote','pip','ms','dimensionless'))` |
| | | primary key `(venue_symbol, quantity, position)` |

The three Redis-only keys go away because the tables make them derivable:
- `param_count`: the number of parameter rows
- `calib:symbols`: the rows in `instrument`
- `calib:quantities`: the `CHECK` on `quantity`

What stays unchanged from the current spec:
- **Units:** each parameter is tagged with a unit and converted by that unit alone:
  - `pip` is multiplied by `pip_size`
  - `ms` is divided by 1000
  - `quote` and `dimensionless` are used as stored
- **Symbology:** the file symbol comes only from `instrument.file_symbol`, never from string manipulation.
- **Source of truth:** the seed script is the only place the values are defined.

Two rules stay in the reader, because they are code knowledge that doesn't belong in the schema:
- `pip_size` must be a power of ten written without trailing zeros.
- Each family's parameter count, names and positive scale/shape values come from
  `distributions.FAMILY_PARAMETERS` and `UNBOUNDED_PARAMETERS`.

### Seeder

`calibration-seeder` keeps its name and its role as a one-shot job:
- **Image:** the stock `timescale/timescaledb` image, pinned to the same digest as the `postgres` service, for
  its `psql`.
- **Ordering:** it has `depends_on: postgres: service_healthy` and `restart: "no"`, and the adapter gates on
  `service_completed_successfully`.
- **Mounts:** `seed.sh` and `calibration.sql`, read-only.
- **What `seed.sh` does:** it reads the password from the mounted `postgres_password` secret and runs:

```sh
psql -h postgres -U tqtk -d tqtk -v ON_ERROR_STOP=1 --single-transaction -f /seed/calibration.sql
```

Any error, whether SQL syntax, a `CHECK` violation or an unreachable host, exits non-zero with the store
unchanged. The scratch-DB dry run and the `(error)` output grepping are deleted.

### Reader

`CalibrationStore` takes a `psycopg.AsyncConnection` and issues one query:

```sql
SELECT i.venue_symbol, i.file_symbol, i.pip_size, i.quote_currency, i.initial_price,
       d.quantity, d.family, p.position, p.name, p.value, p.unit
FROM calibration.instrument i
LEFT JOIN calibration.distribution d USING (venue_symbol)
LEFT JOIN calibration.parameter p USING (venue_symbol, quantity)
ORDER BY i.venue_symbol, d.quantity, p.position;
```

The `LEFT JOIN`s make a missing distribution or parameter show up as `NULL`s, so it gets reported rather than
silently dropped. Parsing moves into a pure function (rows → `dict[str, SymbolCalibration]`), so it can be
tested without a database. Today's validation carries over one-to-one:
- decimal parsing
- `_is_power_of_ten`
- `_to_code_units`
- family, count, name and position checks
- the positive scale/shape check
- the float-range check

Error messages name `calibration.<table>` and the row key instead of a Redis key, for example
`'EUR/USD': calibration.parameter (return, position 1) ...`. Behaviour stays fail-fast with no partial result.
Both an empty `instrument` table and a missing schema (`UndefinedTable`) read as "calibration store is not
seeded" and point at the `calibration-seeder` service.

### Adapter wiring

- **`__main__.py`:**
  - `wait_for_redis` becomes a generic `wait_for(dependency, probe, retry_on)`. It is reused for Postgres,
    retrying on `psycopg.OperationalError`.
  - Readiness becomes `Readiness("redis", "postgres", "calibration")`, so a connection problem and a store
    problem stay distinguishable on `/ready`.
  - The connection is opened for the load and then closed.
- **`config.py`:**
  - adds `postgres_url` (default `postgresql://tqtk@localhost:5432/tqtk`)
  - adds `postgres_password_file: Path | None`, following the stack's `_FILE` convention
  - keeps `redis_url` for the bus only
- **`pyproject.toml`:** adds `psycopg[binary]>=3.2`, the same driver already in the dev group.
- **`docker-compose.yml`:** the adapter gets:
  - `TQTK_POSTGRES_URL=postgresql://tqtk@postgres:5432/tqtk`
  - `TQTK_POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password`
  - `secrets: [postgres_password]`
  - `depends_on: postgres: service_healthy`

### Secret readability

The adapter image runs as uid 1001. A plain Compose `file:` secret is bind-mounted with the host's ownership
and mode, so today's `0600` `postgres_password` would be unreadable in the container. This is the problem
Grafana already hit. The fix is the same one: `deploy/bootstrap-secrets.sh` sets `postgres_password` to
`0644`. The `0700` on `deploy/secrets/` still keeps other host accounts out, for the reason already documented
in that script. Existing machines are repaired by re-running the script, which already fixes the modes of files
that exist.

### Data migration

1. Generate `calibration.sql` from `calibration.redis` once, with a throwaway converter that isn't committed.
   Every `#` fit-rationale comment is carried across as `--` next to the rows it explains. From then on the SQL
   script is maintained by hand.
2. **Prove equivalence before deleting anything.** Load the Redis script through the current reader and the
   SQL script through the new one, and assert that the two `dict[str, SymbolCalibration]` are equal. Record the
   result in the change's design.md, as the Redis change did for its own conversion.
3. Delete `calibration.redis` and the Redis read path.
4. Existing Redis volumes still hold the old keys (`calib:*`, `instrument:*`, `symbology`). They are harmless
   but no capability owns them any more. `deploy/README.md` documents a one-time cleanup.

## Affected files

| Change | Files |
|---|---|
| New | `deploy/calibration/calibration.sql` |
| Rewrite | `deploy/calibration/seed.sh`, `feed_adapter_synthetic/calibration_store.py`, `tests/test_calibration_store.py`, `tests/test_calibration_seeder.py` |
| Delete | `deploy/calibration/calibration.redis` |
| Edit | `feed_adapter_synthetic/__main__.py`, `config.py`, `calibration.py` (docstring), service `pyproject.toml`, `uv.lock`, `deploy/docker-compose.yml`, `deploy/bootstrap-secrets.sh`, `deploy/README.md`, `tests/test_main.py`, `tests/test_feed_config.py` |
| Specs | `calibration-store` (keyspace → tables; seeding semantics), `synthetic-feed` (Postgres source; `postgres` readiness dependency) |

## Verification

- **Unit tests (no Docker):**
  - every current fail-fast case, ported to the pure row parser
  - unit conversion
  - sorted file-symbol keys
  - prices staying on the `pip_size` grid
  - `/ready` with three dependencies, and retrying Postgres until it is up
- **Integration tests (TimescaleDB container, `integration` marker, following `test_storage_fixture.py`):**
  - The real `seed.sh` applies `calibration.sql`, and all 17 instruments load.
  - Re-applying the script is idempotent.
  - Dropping an instrument from the script removes it from the store.
  - A syntax error or a `CHECK` violation fails the seeder and leaves a marker row from the previous seeding
    intact.
  - An unreachable host fails the seeder.
- **Equivalence:** a one-time check that the Redis load equals the Postgres load.
- **Stack:**
  1. Run `docker compose -f deploy/docker-compose.yml up -d --build`.
  2. The seeder exits 0.
  3. The adapter's `/ready` shows `redis`, `postgres` and `calibration` all ready.
  4. `ticks.raw.synthetic.EURUSD` keeps growing.
  5. `SELECT count(*) FROM calibration.instrument` returns 17.

## Out of scope

- Reloading calibration while the adapter runs; a re-seed still takes effect on restart.
- Read-only database roles for consumers. The stack runs as the single `tqtk` user today, and grants would live
  in the seed script when needed.
- Re-fitting any distribution.
