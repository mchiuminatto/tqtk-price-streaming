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
`/metrics`; the stack's `redis:7.4-alpine` image ships `redis-cli`, `sh` and `grep` - but
`redis-cli` exits 0 for commands piped on stdin even on an error reply or a refused connection
(its `-e` flag only covers commands given as arguments); the repository has no `conftest.py`,
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

### Revised: seed values are a redis-cli command script, not code
Every seeded value — symbology, `pip_size`, `quote_currency`, `initial_price` and the three fitted
distributions — lives in `deploy/calibration/calibration.redis`: plain Redis commands (`SET`,
`HSET`, `SADD`) with values already in stored units, applied by `redis-cli`. There is no seeding
code. The per-symbol fit rationale formerly in `docs/tick-distributions.md`,
`docs/spread-distributions.md` and `docs/tick-interval-distributions.md`, and the grain-inference
method from `docs/minimum-change-position.md`, are `#` comments beside the commands they explain;
the four docs are deleted, since each restates seeded values.

**Historical note:** the first implementation held the values as literal Python tables in a
`tools/calibration_seed` package, converted to stored units and written by a Python seeder image.
That kept data in code. The script was generated once from those tables (so its content is
identical, checked key by key before the package was deleted) and is hand-maintained from then on.

**Alternative rejected**: Python tables plus a seeding program — data in code, and a program and
image to maintain for what is a fixed list of writes. **Alternative rejected**: derive
`initial_price` and `pip_size` from parquet at seed time — keeps `pyarrow` and a `data/` mount on
the seeder and makes the sample, not the script, the source of truth for price metadata.
**Alternative rejected**: keep the docs as reference — two hand-maintained copies of the same
parameters drift, and a reader cannot tell which one the running system uses.

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
The script stores every number as a plain positional decimal string (never scientific notation);
the reader parses it with `Decimal`, converts stored → code units in `Decimal` (`pip` × `pip_size`,
`ms` shifted three places), and only then calls `float()`. Because `pip_size` is a power of ten and
the millisecond shift is exact, the conversion is an exact decimal shift, so the loaded parameters
are exactly the fitted values the script was generated from.

**Alternative rejected**: `float` arithmetic — `x * pip_size` in binary floating point can land
one ulp off the fitted value, which would make an equality check against the fits flaky or
loosened to a tolerance that could hide a real unit bug.

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
The script is one `MULTI`/`EXEC` transaction whose first queued command is an `EVAL` that deletes
every key matching `calib:*`, `instrument:*` and `symbology`; the writes follow. A concurrent
reader sees either the previous seeding or the new one, never a mix, and an instrument removed
from the script disappears from the store.

`MULTI`/`EXEC` alone does not keep a broken script from damaging the store: a command rejected
while queueing (unknown command, too few arguments) aborts the whole transaction (`EXECABORT`),
but one that fails while `EXEC` runs - an `HSET` with an odd field/value count, a `WRONGTYPE` - does
not roll back the others, so the leading delete would still wipe the previous seeding. The seeder
therefore dry-runs the script against DB 15, a scratch logical database reserved for it, flushes
that database, and applies the script to the live DB 0 only if the dry run had no error. The dry
run is faithful because the script writes only inside its own keyspace, which its first command
empties: it meets exactly the state the live run will.

**Alternative rejected**: overwrite in place — leaves a removed instrument's keys behind, still
readable, and exposes half-written state mid-run. **Alternative rejected**: `redis-cli --scan` then
`DEL` from the seeder's shell — the delete would not be atomic with the writes.

### The seeder is the stock Redis image, run as a one-shot Compose service
Compose runs `calibration-seeder` from `redis:7.4-alpine` — the image the stack's Redis already
uses — with the script and its runner, `deploy/calibration/seed.sh`, bind-mounted read-only; the
entrypoint only runs `seed.sh`, kept as a file rather than inline YAML so its integration test
runs exactly what Compose does. It strips comment and blank lines (`redis-cli` does not understand
comments) and pipes the rest to `redis-cli -h redis --no-raw`. Because piped `redis-cli` exits 0
regardless, `seed.sh` checks instead: a `redis-cli -e PING`
first fails fast on an unreachable Redis, and any `(error)` in a piped run's output - `EXECABORT`
from a command rejected while queueing, or an error inside `EXEC` - fails the seed: the dry run's
before the live store is touched, the live run's (which a clean dry run should make impossible)
after. It has `restart: "no"` and waits for Redis to be healthy, and `feed-adapter-synthetic` depends on it with
`condition: service_completed_successfully`. It is a container separate from the adapter, and not a
`services/*` member: the `service-runtime` capability would require runtime endpoints a
run-to-completion job has no use for.

**Alternative rejected**: a custom seeder image — nothing to build when the stock image already has
the only program needed, and the data stays a mounted file rather than an image layer.
**Alternative rejected**: bake the seeding into the adapter's image — couples writer and reader and
puts seed data inside the service that must not own it.

### Test layout
- The service's `tests/test_calibration_seeder.py` (marked `integration`; starts a Redis container
  and skips without Docker) runs `seed.sh` in the stock image against a real Redis: a clean script
  seeds DB 0 and leaves DB 15 empty, a command failing inside `EXEC` and one rejected while
  queueing each fail the seeder with a planted key and the previous seeding intact, and an
  unreachable Redis fails it.
- The service's `tests/test_calibration_store.py` covers the reader with a fake serving a prebuilt
  keyspace dictionary, and also parses the committed seed script the way the seeder feeds it to
  `redis-cli` (comments and blank lines stripped, each line split like a shell): it checks the
  file is one transaction that first deletes the previous seeding and otherwise only writes, that
  the resulting keyspace loads cleanly for all 17 symbols (the loader's validation is the
  completeness check - it checks each family's parameter count, names and positive
  scales/shapes, not just that keys exist), that every `loc`/`scale` carries its quantity's unit and every other
  parameter is `dimensionless`, and that numbers are plain decimals.
- `_run_service` tests inject a fake `CalibrationStore`, as they already inject `RuntimeServer`.
- `tests/test_symbols.py` and the parquet cases in `tests/test_calibration.py` are deleted with the
  code they cover.
- `tests/test_distributions.py` keeps the statistical sampler tests (`_N = 200_000`,
  `_SEED = 12345`) untouched; the accessor and `registered_symbols` tests go with the accessors.
- Real `redis-cli` behaviour — quoting of the `EVAL`, transaction abort, stale-key removal — is
  checked end to end against a real Redis (see tasks).

### No-behavior-change proof is ordered before each removal
Before the file-based code was deleted, a test asserted the seeded values equal
`compute_calibration(symbol)` (reading `data/`) and the three in-module tables, for all 17 symbols.
Before the Python seeding tables were deleted, a test asserted the script's keyspace equals the one
those tables produced, key by key. Each test was retired with the code it compared against; from
then on the seed-script tests keep the script loadable and correctly tagged.

## Risks / Trade-offs

- [The store is not seeded when the adapter starts] → Compose orders the seeder before the adapter
  (`service_completed_successfully`); the adapter's error says the store is not seeded and names
  the seeder.
- [The adapter crash-loops under `restart: unless-stopped` while the store is broken] → accepted:
  the loop is visible in `/ready` and the logs, and the alternative (a half-calibrated run) is
  worse.
- [Redis loses the keyspace on restart] → Redis runs with AOF `everysec` and RDB
  (`platform-resilience`), and the seeder re-runs on every `docker compose up`.
- [A hand edit to the script breaks it] → the dry run on scratch DB 15 fails and so does the
  seeder, before the live store is touched, so it keeps its previous seeding and the adapter does
  not start; the seed-script
  tests catch a missing key, wrong unit or scientific notation before merge.
- [Initial prices are frozen at today's sample] → intended: a refreshed sample no longer shifts
  `p_0` silently; changing it is an edit to the script.
- [Deleting the four docs loses the fit reasoning] → the reasoning moves into comments beside the
  values it justifies in the seed script, in the same change.
- [Re-seeding does not reach a running adapter] → accepted non-goal; restart the adapter.

## Migration Plan

1. Land the seeded values and their tests while the file-based path still exists; the equivalence
   test proves they match today's behavior.
2. Land the Redis-backed read path and the Compose seeder; the adapter now starts only against a
   seeded store.
3. Remove the file-based path, `pyarrow` and the `data/` mount; retire the equivalence test.
4. Delete the four docs and re-point every reference.

Rollback: revert the change. The previous image reads `data/` again once the mount is restored; the
seeded keys are inert to it and can be left in place or removed with the script's delete patterns.
