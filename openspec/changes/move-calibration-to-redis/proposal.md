## Why

`feed-adapter-synthetic` resolves everything it needs to generate ticks from a repository
checkout: the symbol set by globbing `data/*.parquet`, the fitted return/spread/interval
distributions from three tables typed into `distributions.py`, and each symbol's initial price and
price grain by re-reading its parquet sample on every startup. That ties the service to a 415 MB
`data/` bind-mount and a `pyarrow` runtime dependency, and it spreads one symbol's reference data
across a sample file, a module and four markdown documents that can drift apart (TPS-24, subtask
of TPS-22). The adapter already connects to Redis; making Redis its only reference-data source
removes the checkout dependency entirely.

## What Changes

- **New calibration keyspace in Redis**: a `symbology` hash (venue symbol → file symbol), an
  `instrument:<venue>` hash per symbol (`pip_size`, `quote_currency`, `initial_price`), and per
  quantity a `calib:<venue>:<quantity>:family`, `calib:<venue>:<quantity>:param_count` and
  `calib:<venue>:<quantity>:param:<n>` hashes carrying `name`/`value`/`unit`, indexed by the
  `calib:symbols` and `calib:quantities` sets.
- **Stored units tagged per parameter**: intervals stored in milliseconds, spreads in pips,
  returns in quote units; only `pip`/`ms`-tagged parameters are converted on read, and
  `dimensionless` shape parameters are never scaled.
- **New seed script `deploy/calibration/calibration.redis`**: the single source of truth for every
  seeded value — the three fitted distributions and per-instrument initial price, pip size and
  quote currency — as plain Redis commands with values in stored units, not in any code. Applied
  with `redis-cli` by a separate one-shot seeder container (the stock Redis image, script mounted
  in) as one atomic, idempotent transaction; the only writer of the keyspace.
- **feed-adapter-synthetic reads calibration from Redis at startup** in one batched load, after
  Redis is reachable and before the first tick. **BREAKING** (deployment): the adapter no longer
  starts without a seeded store — missing or incomplete calibration for any discovered symbol
  fails startup, with no fallback to in-module tables.
- **`/ready` gains a `calibration` dependency** alongside `redis`, so a store problem is
  distinguishable from a connection problem.
- **Removed from the service**: the three distribution tables and their accessors,
  `compute_calibration`, `_decimal_places`, `symbols.py`'s parquet discovery, the `pyarrow`
  dependency and the `../data:/app/data:ro` bind-mount.
- **Removed from `docs/`**: `tick-distributions.md`, `spread-distributions.md`,
  `tick-interval-distributions.md` and `minimum-change-position.md` — each restates seeded values.
  Their per-symbol fit rationale and the grain-inference method move into comments beside the
  values in the seed script; every reference to them is re-pointed.

## Capabilities

### New Capabilities
- `calibration-store`: the Redis keyspace holding per-instrument calibration, its naming (owned by
  this capability, not `data-contract`), its stored-unit conventions, its discovery sets, and the
  seed script that is its only writer and the single source of truth for its values.

### Modified Capabilities
- `synthetic-feed`: symbol set, the three per-instrument distributions, the initial price and the
  minimum price-change unit are sourced from `calibration-store` instead of `data/*.parquet` and
  in-module tables; adds a fail-fast requirement for missing or incomplete calibration.

## Impact

- **Code**: `services/feed-adapter-synthetic` — new `calibration_store.py`; `distributions.py`
  keeps only `Distribution` and the samplers; `calibration.py` keeps only `SymbolCalibration`;
  `symbols.py` is removed; `generator.py`, `__main__.py`, `__init__.py`, `config.py` change.
  No seeding code: the seed values are a Redis command script.
- **Dependencies**: `pyarrow` leaves the service; `redis>=5.0` is already declared.
- **Deployment**: `deploy/docker-compose.yml` loses the `data/` mount and gains a one-shot
  `calibration-seeder` service (stock `redis:7.4-alpine`, seed script mounted) that the adapter
  waits on; a seeded store becomes a prerequisite for a running stack.
- **Docs and specs**: the three `docs/*-distributions.md` files and
  `docs/minimum-change-position.md` deleted; references in `docs/synthetic-price.md`, module
  docstrings, tests, and `add-price-pipeline`'s `design.md`/`tasks.md` re-pointed to the seed
  script.
- **Behavior**: none intended — the seeded values are today's tables and today's sample-derived
  initial prices and price grains, so generated ticks are statistically unchanged.
