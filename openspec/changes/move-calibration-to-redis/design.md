## Context

See proposal.md — Why. The current shape of `services/feed-adapter-synthetic`:

- `distributions.py` holds `Distribution`, eight `random.Random`-only samplers keyed in
  `_SAMPLERS`, and three private 17-symbol tables (`_RETURN_DISTRIBUTIONS`,
  `_SPREAD_DISTRIBUTIONS`, `_INTERVAL_DISTRIBUTIONS`) reached through three accessors. Parameters
  are in code units: returns and spreads in price units, intervals in seconds.
- `calibration.py` defines the frozen `SymbolCalibration` that `generator._RandomWalk` consumes,
  and `compute_calibration`, which reads each symbol's parquet `Bid` column (via `pyarrow`) for
  `initial_price` and, through `_decimal_places`, `price_increment`.
- `symbols.py` finds the symbol set by walking up to the repository's `data/` directory.
- `__main__._run_service` builds the `Redis` client, awaits `wait_for_redis` (which marks the
  single `redis` readiness dependency), then calls `run_synthetic_feed`, which computes
  calibrations off the event loop with `asyncio.to_thread`.
- `deploy/docker-compose.yml` bind-mounts `../data:/app/data:ro` so that walk-up works in the
  container.

Constraints that shape the approach: the samplers and `SymbolCalibration` are correct and stay
untouched; `tqtk_common.server.Readiness` already accepts several named dependencies; the
`service-runtime` capability requires every *service* to expose `/health`, `/ready` and
`/metrics`; `tools/` is not a workspace member and any change under it already triggers a full CI
rebuild (`tools/ci/affected_members.py`, `GLOBAL_PREFIXES`); the repository has no `conftest.py`,
no mock library and no `fakeredis` — tests hand-roll fakes (`tests/test_sinks.py`,
`tests/test_main.py`).

## Goals / Non-Goals

**Goals:**
- The adapter's only reference-data dependency is Redis; it starts from an image with no `data/`
  mount and no `pyarrow`.
- Seeded values are byte-for-byte today's values, and that is proven by a test, not asserted.
- A misconfigured or unseeded store stops the adapter before it publishes anything, with an error
  that names what is missing.

**Non-Goals:**
- Re-deriving any fit. No `scipy` run; the seeded distributions are today's tables.
- Deleting `data/*.parquet` from the repository — it stays as the input for any future offline
  re-fit; it just leaves every runtime and seed-time path.
- Reloading calibration while the adapter runs. Re-seeding takes effect on the next restart.
- MCP gateway, read-only Redis ACL users, network isolation — Redis runs without a password by
  design (architecture decision #9).
- The other open `add-price-pipeline` tasks.

## Decisions

### Revised: calibration read from Redis at startup, not from the repository checkout
The adapter loads its symbol set and every `SymbolCalibration` from the calibration store once, at
startup, and passes both into `run_synthetic_feed`. This supersedes three `add-price-pipeline`
decisions: "Symbol set sourced from the sample data, not a hardcoded list", the placement half of
"Revised: return/spread/interval fit offline per family" (the static tables no longer live in
`distributions.py`), and "Calibration fitted with `pyarrow.compute`, and concurrently across
symbols" (there is no per-startup fit left to parallelize).

**Historical note:** those decisions optimized for "adding a symbol is one edit" by making the
sample file the source of truth, then accepted a second edit in `distributions.py` once family
selection moved offline. The result was three places per symbol (sample, module, docs). Moving
every value into one seeded store collapses them back to one, and removes the checkout from the
runtime.

**Alternative rejected**: keep reading parquet in the service and move only the distributions to
Redis — leaves `pyarrow` and the 415 MB mount in place for two numbers per symbol.

### The seeding tool holds every seeded value as a literal table
`initial_price`, `pip_size`, `quote_currency`, venue/file symbology and the three fitted
distributions are literal tables in the seeding tool's code. `initial_price` and `pip_size` are
produced once, during implementation, by running today's `compute_calibration` against
`data/*.parquet`, and written into the tool; after that the tool never reads sample data. The
per-symbol fit rationale from `docs/tick-distributions.md`, `docs/spread-distributions.md` and
`docs/tick-interval-distributions.md`, and the grain-inference method from
`docs/minimum-change-position.md`, move into comments beside the tables they explain; the four
docs are then deleted, since each restates seeded values.

**Alternative rejected**: derive `initial_price` and `pip_size` from parquet at seed time — keeps
`pyarrow` and a `data/` mount on the seeder and makes the sample, not the tool, the source of
truth for price metadata. **Alternative rejected**: keep the docs as reference — two
hand-maintained copies of the same parameters drift, and a reader cannot tell which one the running
system uses.

### `initial_price` added to the `instrument:<venue>` hash
The originally proposed keyspace had `pip_size` and `quote_currency` on `instrument:<venue>` but no
home for `p_0`, which today comes from the parquet's first `Bid`. It is added as a third field of
the same hash — an explicit extension of that keyspace, recorded here rather than slipped in.
`pip_size` and `initial_price` come from the same one-time `compute_calibration` run, so they
cannot disagree.

**Alternative rejected**: a separate `calib:<venue>:initial_price` key — splits one instrument's
scalar metadata across two key families for no reader benefit.

### `pip_size` means the instrument's minimum price increment
`pip_size` is the value `SymbolCalibration.price_increment` holds today — the smallest price change
the instrument is quoted in (`0.00001` for `EURUSD`, `0.001` for `USDJPY` and the CFDs) — not the
FX-convention pip (`0.0001` for `EURUSD`). Spread parameters are stored in multiples of it.

**Alternative rejected**: store the FX-convention pip plus a separate increment — two fields for
one number the generator uses, and "pip" has no FX convention for the four CFDs anyway.

### Stored units are tagged per parameter; only `pip` and `ms` are converted
Each parameter hash carries `unit` ∈ {`quote`, `pip`, `ms`, `dimensionless`}. The reader converts
by `unit` alone (`pip` × `pip_size`, `ms` ÷ 1000, `quote` and `dimensionless` unchanged), never by
family name. This works for every family in `_SAMPLERS` because each is linear in `loc` and
`scale`, and every shape parameter is unit-free.

**Alternative rejected**: store everything pre-converted to code units — hides the conversion the
`unit` column makes explicit, and a human reading the store sees `4.92226e-05` where `4.92226`
pips is meaningful.

### Unit conversion is done in `Decimal`, on the stored decimal strings
The seeder writes each value as the shortest decimal string that round-trips its `float`
(`repr`), converting code → stored units in `Decimal`; the reader parses with `Decimal`, converts
stored → code units in `Decimal`, and only then calls `float()`. Because `pip_size` is a power of
ten and `1000` is exact, both directions are exact decimal shifts, so the round trip reproduces
every table value exactly and `SymbolCalibration` equality holds.

**Alternative rejected**: `float` arithmetic — `x / pip_size * pip_size` is not the identity in
binary floating point, which would make the no-behavior-change test either flaky or loosened to a
tolerance that could hide a real unit bug.

### Venue symbols follow the venue's own notation
FX pairs use `BASE/QUOTE` (`EUR/USD`); the CFDs use `AAPL.US/USD`, `ARKQ.US/USD`,
`USA500.IDX/USD` and `USATECH.IDX/USD`. `quote_currency` is the part after `/`. The `symbology`
hash is the only mapping to file symbols (`EURUSD`, `AAPLUSUSD`); the reader never strips
punctuation to derive one, so a future symbol whose file name does not follow that pattern needs
no code change.

**Alternative rejected**: key the store by file symbol — the keyspace would then encode a
file-naming artifact of the sample dump rather than the instrument's identity.

### Fail fast; no fallback
The load validates the whole keyspace before returning and raises on the first problem, naming the
symbol and key (the conditions are listed in the `synthetic-feed` delta). It rejects a family with
no entry in `_SAMPLERS` at load time rather than at first sample. This matches the package's
existing "raise rather than guess" convention (`find_data_dir`, `_lookup`, `_decimal_places`).

**Alternative rejected**: keep the in-module tables as a fallback — reintroduces the drift this
change removes and turns a misconfigured Redis into a silently half-calibrated run.

### `calibration` is a second readiness dependency
`_run_service` constructs `Readiness("redis", "calibration")`. `wait_for_redis` marks `redis` as
today; the calibration load follows and marks `calibration` only on success. `/ready` therefore
flips only after both, and its per-dependency snapshot shows which one is missing — a store problem
reads differently from a connection problem.

**Alternative rejected**: mark ready after Redis alone — `/ready` would report ready for a process
about to exit. **Alternative rejected**: defer marking `redis` until calibration loads — hides
which of the two failed.

### One load in a fixed number of round trips
A new `calibration_store.py` holds `CalibrationStore`, a thin wrapper in the shape of
`sinks.RedisTickSink` (`__slots__`, injected `redis.asyncio.Redis`). Its load issues three
pipelined round trips regardless of symbol count: (1) `calib:symbols`, `calib:quantities`,
`symbology`; (2) every `instrument:<venue>` hash and every `family`/`param_count` key; (3) every
`param:<n>` hash. It returns the file symbols in sorted order and a `dict[str, SymbolCalibration]`
keyed by file symbol.

**Alternative rejected**: one command per key — about 250 sequential round trips at startup for 17
symbols, growing linearly.

### Seeding replaces the keyspace atomically
The seeder scans for existing `calib:*` and `instrument:*` keys plus `symbology`, then deletes them
and writes the full new set inside one `MULTI`/`EXEC` transaction. A concurrent reader sees either
the previous seeding or the new one, never a mix, and an instrument dropped from the tool
disappears from the store.

**Alternative rejected**: overwrite in place — leaves a dropped instrument's keys behind, still
listed nowhere but still readable, and exposes half-written state mid-run.

### The seeder lives under `tools/`, runs as a one-shot Compose service
The seeder is `tools/calibration_seed/` (run as `python -m tools.calibration_seed`, using the
synchronous `redis` client and `TQTK_REDIS_URL`). It is not a `services/*` member: the
`service-runtime` capability would require it to serve `/health`, `/ready` and `/metrics`, which a
run-to-completion job has no use for. Compose runs it as `calibration-seeder` with
`restart: "no"`, depending on Redis being healthy, and `feed-adapter-synthetic` depends on it with
`condition: service_completed_successfully`. Its image is a small Dockerfile beside it, built from
the repo root, installing only `redis`.

**Alternative rejected**: bake the seeder into the adapter's image — couples the writer and the
reader's release cycle and puts seed data inside the service that must not own it.

### Test layout
- The seeder's tests hold the one in-memory Redis fake that supports both the seeder's writes and
  the reader's pipelined reads, and the two cross-cutting tests: the round trip (seed → load equals
  the tool's expected `SymbolCalibration`s) and the unit-discrimination test. They import the
  service's `CalibrationStore`; the seeder's runtime code never does.
- The service's tests use a smaller fake serving a prebuilt keyspace dictionary, injected through
  `_run_service(..., redis_client=...)` as today.
- `tests/test_symbols.py` and the parquet cases in `tests/test_calibration.py` are deleted with the
  code they cover; their equivalence obligation moves to a one-time test (below).
- `tests/test_distributions.py` keeps the statistical sampler tests (`_N = 200_000`,
  `_SEED = 12345`) untouched; the accessor and `registered_symbols` tests go with the accessors.

### No-behavior-change proof is ordered before the removal
Before any file-based code is deleted, a test asserts the seeding tool's tables equal
`compute_calibration(symbol)` (reading `data/`) and the three in-module tables, for all 17 symbols.
Only after it passes is the file-based path removed; the test is then retired with it, leaving the
round-trip test to keep the store faithful to the tool from then on.

## Risks / Trade-offs

- [The store is not seeded when the adapter starts] → Compose orders the seeder before the adapter
  (`service_completed_successfully`); the adapter's error says the store is not seeded and names
  the seeder.
- [The adapter crash-loops under `restart: unless-stopped` while the store is broken] → accepted:
  the loop is visible in `/ready` and the logs, and the alternative (a half-calibrated run) is
  worse.
- [Redis loses the keyspace on restart] → Redis runs with AOF `everysec` and RDB
  (`platform-resilience`), and the seeder re-runs on every `docker compose up`.
- [The seeder image is not built by the per-service CI image job, since `tools/` is not a member]
  → any change under `tools/` already triggers a full rebuild and test run; the end-to-end check
  builds it through Compose.
- [Initial prices are frozen at today's sample] → intended: a refreshed sample no longer shifts
  `p_0` silently; changing it is an edit to the tool.
- [Deleting the four docs loses the fit reasoning] → the reasoning moves into comments beside the
  tables it justifies, in the same change.
- [Re-seeding does not reach a running adapter] → accepted non-goal; restart the adapter.

## Migration Plan

1. Land the seeding tool and its tests while the file-based path still exists; the equivalence
   test proves the tool's tables match today's behavior.
2. Land the Redis-backed read path and the Compose seeder; the adapter now starts only against a
   seeded store.
3. Remove the file-based path, `pyarrow` and the `data/` mount; retire the equivalence test.
4. Delete the four docs and re-point every reference.

Rollback: revert the change. The previous image reads `data/` again once the mount is restored; the
seeded keys are inert to it and can be left in place or removed with the seeder's key patterns.
