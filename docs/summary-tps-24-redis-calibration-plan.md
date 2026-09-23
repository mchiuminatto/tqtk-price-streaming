# Summary: TPS-24 Redis calibration plan

Summary of [`tps-24-redis-calibration-plan.md`](./tps-24-redis-calibration-plan.md).

## Goal

Today `feed-adapter-synthetic` gets everything it needs from the repository checkout:

- The fitted distributions are tables typed into `distributions.py`.
- The symbol list comes from globbing `data/*.parquet`.
- `initial_price` and `price_increment` are recomputed from those parquet files on every startup.

The plan moves all three into Redis, so the only reference-data dependency left is the bus the
service already uses. `pyarrow` and the 415 MB `data/` mount stop being needed at runtime, and a
seeding tool fills Redis so the stack still runs end to end.

## Key decisions

- **Scope:** distributions, instrument data and symbol discovery all move.
- **Missing or partial data:** the service refuses to start. There is no fallback to the old
  tables.
- **Source of truth:** the seeding tool holds every seeded value, and nothing else does.
  `docs/tick-distributions.md`, `docs/spread-distributions.md` and
  `docs/tick-interval-distributions.md` are deleted once the tool lands.
- **Key naming:** the new `calibration-store` capability owns its own keys. `data-contract` keeps
  only the market-data keys (`ticks.raw.*`, `bars.*`, `bar_state:*`).

## Workflow

1. **Step 0:** `/opsx:sync add-price-pipeline`. The main specs folder is empty, so there is nothing
   to modify yet without this. The existing change stays active for its 44 open tasks.
2. **Step 1:** `/opsx:propose move-calibration-to-redis`, which writes the plan files:
   - **New `calibration-store` spec:**
     - **Keys:** a `symbology` hash mapping `EUR/USD` to `EURUSD`, an `instrument:<venue>` hash,
       `calib:<venue>:<quantity>:*` keys for each fit, and `calib:symbols` / `calib:quantities`
       sets for discovery.
     - **Units:** values are stored in ms, pips or quote units. Only parameters tagged `pip` or
       `ms` are converted when read; shape parameters are never scaled. The reader decides this
       from each parameter's unit tag, not from the distribution type.
     - **Seeding:** re-running the tool is safe and gives the same result.
   - **Changes to the `synthetic-feed` spec:** requirements for the symbol list, the three
     distributions, the initial price and the price grain now point at Redis, plus a new
     fail-fast requirement.
   - **`design.md`:** records the decisions above as a third revision of how calibration is
     sourced, keeping the earlier decisions as history.
   - **`tasks.md`:** four sections: keys and seeding tool, reading from Redis, wiring and
     deployment, then removing the file-based path. Deleting the three docs is the last task.
3. **Step 2:** `/opsx:apply`, which changes the code:
   - **Service:** a new `calibration_store.py` reads everything in one batched load. The service
     loads calibration at startup, right after it reports it's ready.
   - **Seeding tool:** the parquet code and tables move out of the service into it, under
     `tools/`.
   - **Dependencies and deployment:** `pyarrow` and the `data/` mount are removed.
   - **Docs:** every reference to the three deleted docs is re-pointed.
   - **Tests:** follow the repo's pattern of hand-written fakes.

## Verification

- `openspec validate` passes.
- **No behavior change:** seeding and then reading back gives the same `SymbolCalibration` as today
  for all 17 symbols.
- **Unit round-trip:** converting to stored units and back gives the original values, and a test
  confirms shape parameters are not scaled.
- **Fail-fast:** incomplete data in Redis stops startup.
- **Tests and lint:** `pytest` and `ruff` pass.
- **End to end:** the stack runs in Docker without the `data/` mount.
- **Clean-up:** no `pyarrow` remains in the service, and no references to the deleted docs remain.

## Out of scope

Re-fitting the distributions, the MCP gateway and Redis access controls, and the other 44
`add-price-pipeline` tasks.

## Loose ends in the plan

- The synthetic-feed section says "five requirements change", but its table lists six.
- Where `initial_price` is stored is still open. The plan proposes adding it to the
  `instrument:<venue>` hash.
