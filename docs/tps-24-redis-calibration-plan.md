# TPS-24: move distributions + instrument data to Redis, via OpenSpec

## Context

`feed-adapter-synthetic` currently resolves everything it needs to generate ticks from files in a
repository checkout:

- `distributions.py` holds three hardcoded 17-symbol tables (`_RETURN_DISTRIBUTIONS`,
  `_SPREAD_DISTRIBUTIONS`, `_INTERVAL_DISTRIBUTIONS`) — the frozen output of an offline `scipy` MLE
  fit, typed into the module.
- `symbols.py` discovers the symbol set by walking up to the repo's `data/` dir and globbing
  `*.parquet` filename prefixes.
- `calibration.py` re-reads each symbol's parquet sample on every startup to derive
  `initial_price` (first `Bid`) and `price_increment` (pip grain, via `_decimal_places`).

That ties the service to a repository checkout and a 415 MB `data/` bind-mount, and it means adding
a symbol is two edits in two places that can drift. TPS-24 ("Refactor solution to read distributions
and instrument data from redis", subtask of TPS-22) moves all three to Redis so the adapter's only
reference-data dependency is the bus it already connects to.

The intended outcome: the service reads its symbol set, its fitted distributions, and its
per-instrument price metadata from Redis at startup; `pyarrow` and the `data/` mount leave the
runtime path entirely; a seeding tool populates the keyspace so the stack is still runnable end to
end.

Note the Jira ticket itself has an empty description — the requirements below come from the code,
this repo's existing `synthetic-feed` spec, and decisions taken in planning.

### Decisions taken

| Question | Decision |
|---|---|
| Scope | Everything: distributions, instrument data (`initial_price`/`price_increment`), **and** symbol discovery. Parquet/`pyarrow` leave the runtime path. |
| Seeding | In scope — this change ships a loader tool. |
| Missing/partial data | **Fail fast at startup.** No fallback to the in-module tables. |
| Keyspace & units | Adopt the calibration-store keyspace and stored-unit conventions defined below. |
| Source of truth for seeded values | The seeding tool **only**. Distributions, instrument metadata and initial prices live in its code and nowhere else; `docs/tick-distributions.md`, `docs/spread-distributions.md` and `docs/tick-interval-distributions.md` are deleted once the tool lands. |
| Key-naming ownership | `calibration-store` owns its own keyspace, stated in its `## Purpose`. `data-contract`'s `Stream and key naming` is **not** extended. |
| OpenSpec route | `/opsx:sync` `add-price-pipeline` first, then a **new change** with a proper `MODIFIED` delta. |

---

## Step 0 — materialize main specs

```
/opsx:sync add-price-pipeline
```

`openspec/specs/` is currently **empty**; all eleven capability specs exist only as delta specs
inside the still-active `add-price-pipeline` change (27/71 tasks done). Without this step there is
no `synthetic-feed` main spec to write a `MODIFIED` delta against.

Accepted trade-off: this materializes main specs covering the 44 tasks not yet built. Per the skill
table in `Capturing-Answers-in-OpenSpec.md`, `sync` leaves `add-price-pipeline` **active**, so its
remaining tasks keep their home.

Sanity-check after: `openspec/specs/synthetic-feed/spec.md` should exist with an
`# synthetic-feed Specification` H1 and a single `## Requirements` section.

## Step 1 — propose the change

```
/opsx:propose move-calibration-to-redis
```

Change IDs here are plain kebab-case verb-subject (`add-price-pipeline`), **not** Jira-prefixed —
the `TPS-24` prefix belongs in commit subjects only (`TPS-23: hide per-symbol distribution tables
behind accessors (#10)`).

Artifact dependency order for the `spec-driven` schema is
`proposal.md` → `specs/*/spec.md` → `design.md` → `tasks.md`.

### `proposal.md`

Follow `add-price-pipeline/proposal.md`'s headings exactly: `## Why` → `## What Changes` →
`## Capabilities` (`### New Capabilities` / `### Modified Capabilities`) → `## Impact`. Hard-wrap
~100 cols, bolded lead-ins on bullets.

- New capability: `calibration-store` — the Redis keyspace, its stored-unit conventions, discovery
  sets, and the seeding tool.
- Modified capability: `synthetic-feed` — its sourcing requirements change from files to the store.
- Impact: `pyarrow` drops out of `feed-adapter-synthetic`'s runtime deps; the `../data:/app/data:ro`
  bind-mount at `deploy/docker-compose.yml:139-144` goes away; a new seeding step becomes a
  prerequisite for a working stack.

### `specs/calibration-store/spec.md` — `## ADDED Requirements`

New capability, so it opens with `## Purpose` (that seeds the main spec on the next sync). The
purpose SHALL state that this capability owns the naming of its own keyspace — `symbology`,
`instrument:*`, `calib:*` — so that `data-contract`'s `Stream and key naming` keeps owning only the
market-data path (`ticks.raw.*`, `bars.*`, `bar_state:*`) and no key family is owned by two
capabilities. Cover:

- **Keyspace**: `symbology` hash (venue → file symbol),
  `instrument:<venue>` hash, `calib:<venue>:<quantity>:family`,
  `calib:<venue>:<quantity>:param_count`, `calib:<venue>:<quantity>:param:<n>` hashes carrying
  `name`/`value`/`unit`, plus `SADD calib:symbols` and `SADD calib:quantities` as the discovery
  index.
- **Stored units differ from code units** — intervals stored in **ms**, spread in **pips**, returns
  in **quote units** (no conversion). Conversion applies **only** to parameters whose `unit` is
  `pip` or `ms`; a `dimensionless` parameter (every shape parameter: `df`, `sigma`, Weibull/gamma/
  log-logistic `shape`) SHALL NOT be scaled. This is family-agnostic — every family in
  `_SAMPLERS` is linear in `loc` and `scale` — so the reader keys off the `unit` field, never off
  the family name.
- **Venue vs. file symbology** — the store is keyed by venue symbol (`EUR/USD`); the wire contract
  and code use file symbols (`EURUSD`). The `symbology` hash is the only mapping.
- **Seeding is reproducible** — the tool is the only writer; re-running it is idempotent.
- **Single source of truth** — the seeding tool is the only place the seeded values (fitted
  distributions, instrument metadata, initial prices) are defined. No document restates them.

### `specs/synthetic-feed/spec.md` — `## MODIFIED Requirements`

⚠️ A `MODIFIED` block must carry the **whole** requirement — body plus every scenario that survives.
`openspec validate` and `openspec archive` both reject one that drops a scenario the main spec still
has. Six requirements change (current text in
`openspec/changes/add-price-pipeline/specs/synthetic-feed/spec.md`):

| Requirement | Change |
|---|---|
| `Symbol set` | Source becomes the store's `calib:symbols` discovery set mapped through `symbology`, not "the 17 symbols whose sample data is present under `data/*.parquet`". Drop the parenthetical symbol inventory or restate it as illustrative. |
| `Per-instrument return distribution` | Resolved from the store; replace the `tick-distributions.md` citation with one to the seeding tool (the file is deleted in this change). |
| `Per-instrument spread distribution` | Same, replacing the `spread-distributions.md` citation. |
| `Per-instrument tick-interval distribution` | Same, replacing the `tick-interval-distributions.md` citation. |
| `Initial price from sample data` | Rename to sourcing from the store (a `RENAMED` block plus a `MODIFIED` body, or fold into one modified requirement — pick one and keep it consistent). |
| `Price generation follows a calibrated biased random walk` | Its "minimum price-change unit (derived from its sample data)" clause now comes from the store. |

Plus `## ADDED Requirements` on `synthetic-feed` for **fail-fast**: the adapter SHALL refuse to
start when the store has no entry, or an incomplete entry, for a symbol in the discovery set —
matching the package's existing "raise rather than guess" convention (`find_data_dir`,
`_lookup`, `_decimal_places` all do this).

Requirement style, copied exactly from the existing deltas: `### Requirement: <Sentence case>` with
**no blank line** before its body; **SHALL**/**SHALL NOT** (never MUST, despite what
`Capturing-Answers-in-OpenSpec.md` says); `#### Scenario: <present-tense event>` then lowercase
`- **WHEN**` / `- **THEN**` bullets with no trailing period, continuation lines indented 2 spaces.
Relative doc links from a delta spec are five levels up (`../../../../../docs/synthetic-price.md`).

Key-naming ownership (decided): `data-contract`'s `Requirement: Stream and key naming` covers only
`ticks.raw.*`, `bars.*` and `bar_state:*`, and stays that way — no `data-contract` delta in this
change. `calibration-store`'s `## Purpose` states that it owns its own keyspace (`symbology`,
`instrument:*`, `calib:*`), so each key family has exactly one owning capability.

### `design.md`

Match the house style: `## Context` → `## Goals / Non-Goals` → `## Decisions` (one `###` per
decision, each with an explicit `**Alternative rejected**:` clause) → `## Risks / Trade-offs` →
`## Migration Plan` → `## Open Questions`.

The load-bearing decision is a third revision of the calibration-sourcing question, so follow the
established `### Revised:` idiom — `add-price-pipeline/design.md:231-265` already contains
`### Revised: return/spread/interval fit offline per family, not derived online as N(μ,σ)`, which
itself revised an earlier decision without deleting it. Decisions to record:

1. **Revised: calibration read from Redis at startup, not from the repository checkout.** Supersedes
   both "Symbol set sourced from the sample data, not a hardcoded list" and the offline-fit
   revision's static-table conclusion. Keep a `Historical note:` paragraph rather than editing
   those sections.
2. **Stored units are tagged per parameter, and only `pip`/`ms` parameters are converted.** The
   alternative rejected — storing everything pre-converted to code units — loses the ability to
   re-seed against a different pip grain and hides the conversion the `unit` column makes explicit.
3. **Fail fast on missing or partial calibration**, no fallback to the in-module tables.
   Alternative rejected: keeping the tables as a fallback — reintroduces exactly the drift
   `distributions.py`'s docstring warns about and turns a misconfigured Redis into a silent
   half-calibrated run.
4. **The seeding tool is the single source of truth for every seeded value.** The fitted tables,
   instrument metadata and initial prices move into it, not out of the repo, which is what makes
   "no behavior change" verifiable (see Verification). The three `docs/*-distributions.md` files
   are deleted once the tool lands. Alternative rejected: keeping them as reference documentation —
   two hand-maintained copies of the same parameters drift, and a reader cannot tell which one the
   running system uses.
5. **`pyarrow` stays a dependency of the seeding tool only**, not of the service.

#### Open question to resolve in `design.md`

**The keyspace has no field for `p_0`.** It defines `instrument:<venue>` with `pip_size` and
`quote_currency`, but `initial_price` currently comes from the parquet's first `Bid`
(`calibration.py:96`). Since parquet is leaving the runtime, the keyspace needs one more field —
propose `initial_price` on the `instrument:<venue>` hash, seeded from the sample. Flag this
explicitly as an extension to the keyspace rather than slipping it in, and note that `pip_size` must
agree with the grain `_decimal_places` infers from the same sample — the seeding tool derives both
from one read, so they cannot disagree by construction.

### `tasks.md`

Format: `## N. <Area> (\`<capability>\` spec)` sections; items are
`- [ ] N.M <imperative>; verify <observable check>`, continuation lines indented **6 spaces**.

Both halves are load-bearing: a task is done only when the implementation *and* its stated
verification exist. Pre-existing code that already satisfies the "implement" clause does not close
the task while its "verify" clause has no coverage.

Suggested sections: (1) keyspace + seeding tool, (2) Redis-backed read side, (3) wiring and
deployment, (4) removing the file-based path. Section 4 ends with deleting the three
`docs/*-distributions.md` files and re-pointing every reference to them (see "Removing the
distribution docs") — ordered last, so it only happens after the seeding tool and its no-behavior-
change test exist.

## Step 2 — implement

```
/opsx:apply move-calibration-to-redis
```

Planning only until this point — `propose`/`update` must not touch code.

### Files to change

- **`distributions.py`** — keeps `Distribution`, `_SAMPLERS` and all eight samplers (they are
  correct and stay untouched). The three tables and the three module-level accessors go; the
  accessors' error strings (`"no fitted {quantity} distribution for symbol {symbol!r}"`) are
  asserted verbatim by `tests/test_distributions.py:129-139`, so decide deliberately whether to
  preserve or update them.
- **New `calibration_store.py`** — a thin injected-client wrapper in the exact shape of
  `sinks.py:18-27` (`__slots__`, `Redis` in `__init__`, `redis.asyncio` only). Owns the venue↔file
  mapping, the `unit`-driven conversion, and one `async` load that returns
  `dict[str, SymbolCalibration]`. A single batched load (pipeline/`MGET`) beats ~250 round trips.
- **`calibration.py`** — `SymbolCalibration` (the frozen dataclass) stays as-is; it is the contract
  `generator._RandomWalk` consumes. `compute_calibration`, `_decimal_places`, `_MAX_DECIMAL_PLACES`
  and the `pyarrow` imports move to the seeding tool.
- **`symbols.py`** — `find_data_dir`/`find_symbol_file`/`discover_symbols` move to the seeding tool
  (which still needs them to locate samples). `__init__.py` re-exports all three plus
  `compute_calibration`, so it needs updating in step with them.
- **`generator.py`** — drop `_compute_calibrations`, the `data_dir` parameter, and the `Path`
  import; `calibrations` becomes required rather than defaulted. The `asyncio.to_thread` comment at
  `:123-130` describes parquet cost that no longer exists.
- **`__main__.py:76-93`** — the natural seam. `_run_service` already builds the `Redis` client and
  awaits `wait_for_redis` before generating; load calibrations from the store immediately after
  readiness is marked, then pass both `symbols=` and `calibrations=` into `run_synthetic_feed`.
  Fail-fast lands here, after `/ready` has reported the connection, so a store problem is
  distinguishable from a connection problem.
- **`config.py`** — add any new field (key prefix, or a separate calibration Redis URL if the store
  is not the bus) to `FeedConfig`. `ServiceConfig` sets `env_prefix = "TQTK_"`, `frozen=True`,
  `extra="forbid"`, so a field named `x` is `TQTK_X`.
- **`pyproject.toml`** — drop `pyarrow>=17.0` from the service; `redis>=5.0` is already declared.
- **`deploy/docker-compose.yml`** — remove the `../data:/app/data:ro` mount and the `:139-144`
  comment about `discover_symbols()` walking up; add the seeding step. Redis runs without a
  password by design (architecture decision #9), so no credential work.

### Seeding tool

Under `tools/` (already in the root `testpaths = ["contracts","libs","services","tools"]`, so its
tests are collected automatically). It owns the moved parquet code and the three fitted tables, and
writes the calibration-store keyspace — converting **code units → stored units** on the way in (the inverse of
what the reader does), which is what makes the round-trip test below meaningful.

It is the **only** place the seeded values are defined: fitted distributions, instrument metadata
(`pip_size`, `quote_currency`) and initial prices. The per-symbol fit rationale that today lives
only in the markdown files (why each family won) moves into comments next to the tables it
explains, so deleting the docs loses no reasoning.

### Removing the distribution docs

Once the seeding tool and its tests land, delete `docs/tick-distributions.md`,
`docs/spread-distributions.md` and `docs/tick-interval-distributions.md`, and re-point every
reference to the seeding tool:

- `docs/synthetic-price.md:27,37,50` — the three "described in this document" pointers.
- `generator.py:10-14` and `calibration.py:6-7` module docstrings. (`distributions.py`'s citations
  go with the tables and accessors that carry them.)
- `openspec/changes/add-price-pipeline/design.md:237,256` and `tasks.md:98-100`.
- The `synthetic-feed` spec citations, via the `MODIFIED` deltas above.

### Tests

There is **no `conftest.py` anywhere in this repo** and no mock library or `fakeredis` — externals
are hand-rolled fakes: `tests/test_sinks.py:15-23` `_FakeRedis` (records `xadd`),
`tests/test_main.py:21-35` `_FakeRedisClient` (`ping` fails N times then succeeds), injected via
`_run_service(..., redis_client=...)`. Follow that pattern; add a fake that serves the calibration
keyspace.

Two existing tests have no injection seam and will need one:

- `tests/test_symbols.py:31-32` asserts `discover_symbols() == EXPECTED_SYMBOLS` against the **real**
  repo `data/` directory — it moves with the tool.
- `tests/test_distributions.py:120-126` couples `registered_symbols()` to `discover_symbols()` —
  both sides of that coupling are changing.

The statistical sampler tests (`test_distributions.py:20-108`, `_N = 200_000`, `_SEED = 12345`,
moments vs. textbook values at `rel=0.02..0.05`) stay untouched — the samplers don't change.

## Verification

1. `openspec validate --change move-calibration-to-redis` — catches a `MODIFIED` block that dropped
   a surviving scenario, the most likely spec error here.
2. **No-behavior-change check.** Seed from the current tables, read back through the store, and
   assert the resulting `SymbolCalibration`s equal what `compute_calibration` produces today for all
   17 symbols. `SymbolCalibration` and `Distribution` are both frozen dataclasses, so `==` is
   meaningful, and `tests/test_calibration.py:82-84,165,214-216` already assert equality this way.
3. **Unit round-trip.** code units → stored units → code units is identity, and a discrimination
   test asserting a `dimensionless` shape parameter is *not* scaled while `pip`/`ms` `loc`/`scale`
   are — the one bug this keyspace is shaped to expose.
4. **Fail-fast.** A symbol in `calib:symbols` with a missing `family` key, and one with
   `param_count` disagreeing with the `param:<n>` hashes present, each raise at startup rather than
   starting a partially-calibrated run.
5. `pytest` at the repo root (collects `contracts`, `libs`, `services`, `tools`) and `ruff`
   (`line-length = 100`).
6. **End to end**: `docker compose up` in `deploy/`, run the seeder, confirm
   `/ready` flips only after Redis *and* the calibration load succeed, and that ticks land on
   `ticks.raw.synthetic.{symbol}` for the seeded symbol set and no other. Then confirm the service
   image no longer needs the `data/` mount — start it with the mount removed.
7. `grep -rn "pyarrow" services/feed-adapter-synthetic/` returns nothing outside tests.
8. The three `docs/*-distributions.md` files are gone, and
   `grep -rn "tick-distributions\|spread-distributions\|tick-interval-distributions" --exclude-dir=.git .`
   finds nothing outside this plan.

## Not in this change

- Re-deriving the fits. The seeded values are the existing tables; no new `scipy` run.
- Stubbing `distributions.py`'s samplers — they stay as they are.
- MCP gateway, read-only Redis ACL users, network isolation.
- The other 44 open `add-price-pipeline` tasks.
