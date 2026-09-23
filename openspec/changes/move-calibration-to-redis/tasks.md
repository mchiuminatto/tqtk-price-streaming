## 1. Seeding tool (`calibration-store` spec)

Superseded by section 6: the Python tool built here was replaced by a Redis command script.

- [x] 1.1 Create `tools/calibration_seed/` with the venue/file symbology table, the per-instrument
      `pip_size`/`quote_currency`/`initial_price` table (values produced once by running today's
      `compute_calibration` over `data/*.parquet`), and the three fitted distribution tables in
      code units, each parameter named in its family's fit order; verify the tool imports with no
      `pyarrow` installed and that `ruff` passes.
- [x] 1.2 Move the per-symbol fit rationale from `docs/tick-distributions.md`,
      `docs/spread-distributions.md`, `docs/tick-interval-distributions.md` and the
      grain-inference method from `docs/minimum-change-position.md` into comments beside the
      tables they explain; verify every family choice in the three distribution docs has a
      matching comment in the tool.
- [x] 1.3 Add a one-time equivalence test asserting, for all 17 symbols, that the tool's
      `initial_price` and `pip_size` equal `compute_calibration(symbol)`'s and its three
      distributions equal the in-module tables; verify it passes against the current file-based
      code.
- [x] 1.4 Implement code-unit → stored-unit conversion in `Decimal` (spread `loc`/`scale` ÷
      `pip_size` → `pip`, interval `loc`/`scale` × 1000 → `ms`, returns → `quote`, shape
      parameters → `dimensionless`) and write values as decimal strings; verify a unit test covers
      each unit tag, including a shape parameter left unscaled.
- [x] 1.5 Implement the seeder: scan existing `calib:*`, `instrument:*` and `symbology` keys, then
      delete them and write the full keyspace (`symbology`, `instrument:<venue>`,
      `calib:<venue>:<quantity>:family`/`param_count`/`param:<n>`, `calib:symbols`,
      `calib:quantities`) in one `MULTI`/`EXEC`, reading `TQTK_REDIS_URL`; add the in-memory Redis
      fake to the tool's tests; verify tests show the exact keyspace written, that a second run
      leaves it identical, and that an instrument removed from the tables disappears from every
      key.
- [x] 1.6 Add `tools/calibration_seed/__main__.py` so `python -m tools.calibration_seed` seeds
      and exits `0`, exiting non-zero with a clear message when Redis is unreachable; verify both
      exit paths with a test.

## 2. Redis-backed read path (`calibration-store` and `synthetic-feed` specs)

- [x] 2.1 Add `calibration_store.py` with `CalibrationStore` (injected `redis.asyncio.Redis`,
      `__slots__`, shaped like `sinks.RedisTickSink`) whose load runs three pipelined round trips
      and returns the sorted file symbols plus a `dict[str, SymbolCalibration]`, converting
      stored → code units in `Decimal` by `unit` alone; verify with a fake that counts round trips
      that the count is three for any number of symbols.
- [x] 2.2 Make the load fail fast: empty or absent `calib:symbols`, a symbol missing from
      `symbology`, a missing `instrument:<venue>` field, a missing quantity in `calib:quantities`,
      a missing `family`, `param_count` disagreeing with the `param:<n>` hashes present, a
      parameter missing `name`/`value`/`unit`, an unknown `unit`, or a family absent from
      `_SAMPLERS` each raise an error naming the symbol and key; verify one test per condition.
- [x] 2.3 Add the round-trip test in the tool's suite: seed into the fake, load through
      `CalibrationStore`, and assert the result equals the tool's expected `SymbolCalibration`s
      for all 17 symbols with exact `==`; add a discrimination test proving a `dimensionless`
      parameter is not scaled while `pip`/`ms` `loc`/`scale` are; verify both pass.
- [x] 2.4 Wire `__main__._run_service`: `Readiness("redis", "calibration")`, load calibration
      after `wait_for_redis`, mark `calibration` only on success, and pass `symbols=` and
      `calibrations=` into `run_synthetic_feed`; verify with `_FakeRedisClient`-style tests that
      `/ready` is not-ready with `redis` connected and `calibration` not, that it becomes ready
      after a successful load, and that an incomplete store publishes no ticks and raises.
- [x] 2.5 Add any new `FeedConfig` field the store needs (e.g. a calibration Redis URL if it is
      not the bus) or record that none is needed; verify `FeedConfig` still rejects an unknown
      keyword argument. (None needed: the store is the bus, so `redis_url` serves both. An
      unknown `TQTK_*` variable is deliberately ignored, per `tqtk_common.config`.)

## 3. Deployment (`calibration-store` spec)

3.1's custom seeder image is superseded by 6.3 (the stock Redis image).

- [x] 3.1 Add `tools/calibration_seed/Dockerfile` (built from the repo root, installing only
      `redis`) and a `calibration-seeder` Compose service with `restart: "no"`, depending on Redis
      being healthy; verify `docker compose build calibration-seeder` succeeds.
- [x] 3.2 Make `feed-adapter-synthetic` depend on `calibration-seeder` with
      `condition: service_completed_successfully`, and remove its `../data:/app/data:ro` mount and
      the comment about `discover_symbols()`; verify `docker compose config` shows the dependency
      and no `data` volume on the adapter.

## 4. Removing the file-based path (`synthetic-feed` spec)

- [x] 4.1 Remove the three distribution tables, their accessors, `_lookup` and
      `registered_symbols` from `distributions.py`, keeping `Distribution`, `_SAMPLERS` and the
      samplers; delete the accessor tests and keep the statistical sampler tests unchanged; verify
      `pytest services/feed-adapter-synthetic` passes.
- [x] 4.2 Reduce `calibration.py` to `SymbolCalibration`, delete `symbols.py`, drop
      `_compute_calibrations`, the `data_dir` parameter and the `Path` import from `generator.py`
      (making `calibrations` required), and update `__init__.py`'s exports; delete
      `tests/test_symbols.py`, the parquet cases in `tests/test_calibration.py` and the one-time
      equivalence test from 1.3; verify `pytest` at the repo root passes.
- [x] 4.3 Drop `pyarrow>=17.0` from the service's `pyproject.toml` and refresh `uv.lock`; verify
      `grep -rn "pyarrow" services/feed-adapter-synthetic/` returns nothing.
- [x] 4.4 Delete `docs/tick-distributions.md`, `docs/spread-distributions.md`,
      `docs/tick-interval-distributions.md` and `docs/minimum-change-position.md`, and re-point
      every reference to the seeding tool: `docs/synthetic-price.md:27,37,50,58`, the module
      docstrings of `generator.py`, `calibration.py` and `distributions.py`,
      `openspec/changes/add-price-pipeline/design.md:237,256` and `tasks.md:98-100`; verify a
      search for the four file names finds only the TPS-24 planning documents, this change's
      artifacts, and `openspec/specs/synthetic-feed/spec.md` (rewritten when this change is
      archived).

## 5. Verification

- [x] 5.1 Run `openspec validate move-calibration-to-redis --strict`; verify it reports the change
      valid.
- [x] 5.2 Run `pytest` at the repo root and `ruff check .`; verify both pass.
- [x] 5.3 Run `docker compose up` in `deploy/`; verify the seeder exits `0`, the adapter's `/ready`
      flips only after both `redis` and `calibration` are connected, ticks land on
      `ticks.raw.synthetic.{symbol}` for exactly the seeded symbol set, and the adapter runs with
      no `data/` mount.
- [x] 5.4 Stop the stack, flush Redis, and start only Redis and the adapter; verify the adapter
      publishes nothing and exits with an error stating the store is not seeded. (Run against a
      throwaway Redis on an isolated network instead of flushing the stack's Redis, whose streams
      and checkpoints it would have destroyed.)

## 6. Seed values as a redis-cli script (`calibration-store` spec)

- [x] 6.1 Generate `deploy/calibration/calibration.redis` once from the Python tables: one
      `MULTI`/`EXEC` whose first queued command is an `EVAL` deleting `calib:*`, `instrument:*` and
      `symbology`, then `SET`/`HSET`/`SADD` with values in stored units, carrying the tables'
      rationale as `#` comments; verify applying it with `redis-cli` to a real Redis writes the
      same key count `build_keyspace()` produces, and a re-run removes a planted stale key while
      keeping unrelated keys.
- [x] 6.2 Add seed-script tests to `tests/test_calibration_store.py` (one transaction, first
      deletes, then only writes; loads cleanly for 17 symbols; units per parameter; plain decimals)
      plus a one-time key-by-key equivalence with the Python tables; verify all pass.
- [x] 6.3 Switch `calibration-seeder` to `redis:7.4-alpine` with the script mounted read-only and
      an entrypoint that strips comments and blank lines, pipes to `redis-cli -h redis --no-raw`,
      and fails on a refused `redis-cli -e PING` or any `(error)` reply (piped `redis-cli` exits 0
      even on errors, so `-e` alone is not enough); verify `docker compose config` shows it, the
      adapter still waits on it, and a good seed exits 0 while a malformed script or a refused
      connection exits 1.
- [x] 6.4 Delete `tools/calibration_seed/`, retire the equivalence test, and re-point every
      reference to the seed script (service docstrings and seeder hint, `docs/synthetic-price.md`,
      `add-price-pipeline` artifacts, this change's proposal/spec/design); verify `pytest` and
      `ruff` pass and `tools/calibration_seed` appears only in this change's history.
- [x] 6.5 Run `calibration-seeder` and `feed-adapter-synthetic` with Compose; verify the seeder
      exits `0`, `/ready` shows `redis` and `calibration` connected, and all 17
      `ticks.raw.synthetic.*` streams receive new ticks.
- [x] 6.6 On the stack's Redis, plant `calib:ZZZ/USD:return:family` and re-run the seeder; verify
      the key is gone, `calib:symbols` is unchanged, and non-calibration keys are untouched.
- [x] 6.7 On a throwaway Redis on an isolated network, apply a copy of the script with one
      malformed command; verify the seeder exits non-zero, no calibration key exists, and the
      adapter started against it exits stating the store is not seeded.
