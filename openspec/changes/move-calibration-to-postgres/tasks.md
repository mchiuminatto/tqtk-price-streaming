## 1. Dependencies and configuration (`synthetic-feed` spec)

- [ ] 1.1 Add `psycopg[binary]>=3.2` to `services/feed-adapter-synthetic/pyproject.toml` and refresh
      `uv.lock`. Verify that `uv sync` succeeds and that `python -c "import psycopg"` works in the
      service's environment.
- [ ] 1.2 Add `postgres_url` (default `postgresql://tqtk@localhost:5432/tqtk`) and
      `postgres_password_file: Path | None` to `FeedConfig`. Update the module docstring and the
      `redis_url` comment, since Redis is now the bus only. Verify in `tests/test_feed_config.py`
      that both fields read from `TQTK_POSTGRES_URL` / `TQTK_POSTGRES_PASSWORD_FILE` and keep their
      defaults when unset.

## 2. SQL seed script and seeder (`calibration-store` spec)

- [ ] 2.1 Generate `deploy/calibration/calibration.sql` from `calibration.redis` with a throwaway,
      uncommitted converter. The script `DROP SCHEMA IF EXISTS calibration CASCADE`, creates the
      schema and the three tables with every constraint from design.md, and inserts all 17
      instruments. Every `#` fit-rationale comment is carried across as `--` beside the rows it
      explains. Verify that the number of `--` rationale comments matches the number of `#` ones,
      and that `psql -v ON_ERROR_STOP=1 --single-transaction -f` applies it cleanly to a scratch
      TimescaleDB container.
- [ ] 2.2 Rewrite `deploy/calibration/seed.sh` as a single `psql -h "$host" -U tqtk -d tqtk -v
      ON_ERROR_STOP=1 --single-transaction -f "$script"`. It exports `PGPASSWORD` from the mounted
      `postgres_password` secret, and the host, script and secret path can be overridden for tests.
      Delete the scratch-DB dry run and `(error)` grepping, and rewrite the header comment. Verify
      that `sh -n seed.sh` passes and that the script contains no `redis-cli`.
- [ ] 2.3 Rewrite `tests/test_calibration_seeder.py` against a TimescaleDB container under the
      `integration` marker, following `libs/tqtk-common/tests/test_storage_fixture.py`, and run the
      real `seed.sh`. It must cover each case below, and all of them must pass with
      `pytest -m integration`:
      - a clean apply loads 17 instruments
      - re-applying the script is idempotent (identical rows)
      - an instrument dropped from the script disappears from all three tables
      - a syntax error leaves a marker row from the previous seeding intact and exits non-zero
      - a `CHECK` violation (e.g. unit `bp`, `initial_price` 0) does the same
      - an unreachable host exits non-zero

## 3. Dependency wait (`synthetic-feed` spec)

- [ ] 3.1 Generalise `wait_for_redis` into `wait_for(dependency, probe, retry_on, ...)` and use it
      for Redis (`RedisError`) and for Postgres (`psycopg.OperationalError`, with a probe that
      opens the connection). Verify in `tests/test_main.py` that Postgres is retried until its
      probe succeeds, and that `stop` ends the wait.

## 4. Postgres calibration store and cut-over (`calibration-store` and `synthetic-feed` specs)

- [ ] 4.1 Replace the Redis calibration read path with Postgres, in this order:
      1. Add a pure `parse_rows(rows) -> dict[str, SymbolCalibration]` over the
         `instrument ⟕ distribution ⟕ parameter` row shape. Port the decimal parsing,
         `_is_power_of_ten`, `_to_code_units`, the family/count/name/position checks, the positive
         scale/shape check and the float-range check. Detect a missing quantity against the fixed
         set `return`/`spread`/`interval`, and treat `NULL` distribution or parameter columns (from
         the `LEFT JOIN`) as missing rows. Error messages name the symbol plus
         `calibration.<table>` and the row key.
      2. Add the Postgres `CalibrationStore` load, which takes an injected
         `psycopg.AsyncConnection` and runs the one `LEFT JOIN … ORDER BY venue_symbol, quantity,
         position` query from design.md. An empty result and `psycopg.errors.UndefinedTable` both
         raise "calibration store is not seeded", naming the `calibration-seeder` service.
      3. Equivalence check: load `calibration.redis` through the Redis reader and `calibration.sql`
         through the Postgres reader, and assert the two `dict[str, SymbolCalibration]` are `==`
         for all 17 instruments. Record the command and result under "Equivalence result" in this
         change's `design.md`.
      4. Wire `_run_service` with `Readiness("redis", "postgres", "calibration")`. Connect using
         `postgres_url` plus the password read from `postgres_password_file`, load calibration,
         then close the connection before generation starts. Mark `calibration` only on success.
      5. Only after step 3 passes, delete `deploy/calibration/calibration.redis`, the Redis
         `CalibrationStore` and its Redis-only helpers and tests, including the "leading delete
         covers every key" test.

      Verify all of the following:
      - every current fail-fast test in `tests/test_calibration_store.py` is ported to literal row
        tuples and passes, plus a gap in `position` and a distribution with no parameter rows
      - with a hand-rolled fake connection, the query runs once, rows go through `parse_rows`, and
        both not-seeded causes give the "not seeded" message
      - the equivalence check passes and its result is recorded
      - in `tests/test_main.py`, `/ready` is not-ready with `redis` and `postgres` connected but
        `calibration` not, and becomes ready after a load; an incomplete store publishes no ticks
        and raises; the connection is closed before the first tick
      - the service suite passes, and `grep -rn "calib:\|symbology\b\|calibration\.redis"` over
        `services/` and `deploy/` finds nothing
- [ ] 4.2 Verify the conversion and grid behaviour on `parse_rows`: unit conversion (`pip`, `ms`,
      `quote`, and `dimensionless` left unscaled), exact `Decimal` results such as spread `scale`
      `4.92226` pip → `0.0000492226`, sorted file-symbol keys, and generated prices staying on the
      `pip_size` grid for every accepted `pip_size` form. All of these must pass without a
      database.
- [ ] 4.3 Add an integration test that seeds `calibration.sql` and loads it through the Postgres
      `CalibrationStore`. Verify that it returns 17 symbols and that `pip_size` comes back as the
      exact stored `Decimal`, e.g. `Decimal("0.00001")` with exponent −5.
- [ ] 4.4 Update the `__main__.py`, `calibration.py` and `calibration_store.py` docstrings to
      describe the Postgres source and the three readiness dependencies. Verify that `ruff check`
      and `ruff format --check` pass for the service.

## 5. Removing Redis remnants (`calibration-store` and `synthetic-feed` specs)

- [ ] 5.1 Re-point the remaining references to the Redis store in `distributions.py`,
      `generator.py`, `docs/synthetic-price.md`, and `openspec/changes/add-price-pipeline/`
      (`design.md`, `tasks.md`, `specs/synthetic-feed/spec.md`) to `calibration.sql` and the
      `calibration` schema. Verify that the grep from task 4.1 over `docs/`, `services/` and
      `openspec/changes/add-price-pipeline/` finds no stale reference.
- [ ] 5.2 Rewrite the `calibration-store` main spec's `## Purpose`
      (`openspec/specs/calibration-store/spec.md`), which still says Redis and keyspace, at archive
      time. Verify that `openspec validate --specs --strict` passes after archiving.

## 6. Deployment (`calibration-store` and `synthetic-feed` specs)

- [ ] 6.1 Switch `calibration-seeder` in `deploy/docker-compose.yml` to the `timescale/timescaledb`
      image at the `postgres` service's pinned digest. Mount `seed.sh` and `calibration.sql`
      read-only, add `secrets: [postgres_password]` and `depends_on: postgres: service_healthy`,
      keep `restart: "no"`, and update its comments. Verify with `docker compose -f
      deploy/docker-compose.yml config` that the seeder has no Redis dependency and uses the same
      digest as `postgres`.
- [ ] 6.2 Give `feed-adapter-synthetic` `TQTK_POSTGRES_URL=postgresql://tqtk@postgres:5432/tqtk`,
      `TQTK_POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password`,
      `secrets: [postgres_password]` and `depends_on: postgres: service_healthy`. Keep its
      `calibration-seeder` gate. Verify that `docker compose config` shows all four.
- [ ] 6.3 Set `postgres_password` to `0644` in `deploy/bootstrap-secrets.sh`, extending the
      Grafana mode comment to cover uid 1001. Verify that running the script on a machine with an
      existing `0600` file leaves it `0644` and `deploy/secrets/` still `0700`.
- [ ] 6.4 Update `deploy/README.md` with the following:
      - the Postgres-backed store and the seeder's transactional semantics
      - that consumers only `SELECT`
      - the need to re-run `bootstrap-secrets.sh` on existing machines
      - a one-time `redis-cli` cleanup of the old `calib:*`, `instrument:*` and `symbology` keys

      Verify that the documented cleanup command runs cleanly against a Redis that holds those keys.

## 7. Stack verification

- [ ] 7.1 Run `docker compose -f deploy/docker-compose.yml up -d --build` and confirm each of the
      following:
      - `calibration-seeder` exits 0
      - the adapter's `/ready` shows `redis`, `postgres` and `calibration` all ready
      - `ticks.raw.synthetic.EURUSD` keeps growing
      - `SELECT count(*) FROM calibration.instrument` returns 17
- [ ] 7.2 Stop `postgres` while the stack is running. Verify that the adapter keeps publishing to
      `ticks.raw.synthetic.EURUSD`, then restart `postgres`.
