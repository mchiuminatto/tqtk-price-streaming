## MODIFIED Requirements

### Requirement: Symbol set
The synthetic feed SHALL generate ticks for exactly the configured symbol set, and no others. The
configured symbol set is the `file_symbol` of every row in the calibration store's
`calibration.instrument` table. The symbol set SHALL NOT be derived from sample data or a
repository checkout. (At the time of writing the store is seeded with 17 symbols: 13 FX pairs,
6 majors and 7 crosses, plus 2 equity CFDs, AAPLUSUSD and ARKQUSUSD, and 2 index CFDs,
USA500IDXUSD and USATECHIDXUSD. This list is illustrative; the store is authoritative.)

#### Scenario: Adapter starts
- **WHEN** the synthetic feed adapter starts
- **THEN** it publishes ticks tagged `provider="synthetic"` for each instrument in the calibration
  store's `instrument` table and for no symbol outside that set

#### Scenario: The seeded symbol set changes
- **WHEN** the calibration store is re-seeded with a different set of instruments and the adapter
  is restarted
- **THEN** the adapter publishes for exactly the new set, with no change to the adapter's code,
  configuration or image

### Requirement: Fail fast on missing or incomplete calibration
The adapter SHALL refuse to start generating when the calibration store has no instruments or no
`calibration` schema.

It SHALL also refuse when any instrument lacks a complete distribution for any of the `return`,
`spread` and `interval` quantities. That covers:
- a missing `distribution` row
- a distribution with no `parameter` rows
- a parameter row missing `name`, `value` or `unit`
- an unrecognized `unit`
- a family the adapter cannot sample

It SHALL also refuse when any of the calibration could not be generated from:
- a parameter count other than the family's own
- a gap in the parameter positions
- a parameter whose `name` is not the one the family takes at that position
- a scale or shape parameter that is not positive
- a value outside the range a `float` can represent
- a `pip_size` that is not a positive power of ten written without trailing zeros
- an `initial_price` that is not positive

Every such check SHALL happen before generation starts, so a calibration that loads is one the
adapter can sample. The adapter SHALL NOT fall back to any other source of calibration, and SHALL
NOT start generating for a subset of the instruments. The failure SHALL name the symbol and the
table and row (quantity and position, where applicable) that is missing or invalid.

The adapter SHALL read calibration once, at startup, and SHALL NOT hold a PostgreSQL connection
while generating, so a PostgreSQL outage after startup does not affect tick generation.

`/ready` SHALL report not-ready until calibration has loaded successfully. It SHALL report the
Redis connection, the PostgreSQL connection and the calibration load as three separate
dependencies, so a store problem is distinguishable from either connection problem.

#### Scenario: A symbol's calibration is incomplete
- **WHEN** the adapter starts and one instrument has no `distribution` row for its `spread`
  quantity
- **THEN** the adapter publishes no ticks for any symbol and exits with an error naming that
  symbol and the missing `calibration.distribution` row

#### Scenario: A symbol's calibration cannot be sampled
- **WHEN** the adapter starts and one symbol's `return` distribution is `laplace` with a `scale`
  parameter of `0`, or with three parameter rows
- **THEN** the adapter publishes no ticks for any symbol, `/ready` never reports the calibration
  dependency connected, and it exits with an error naming that symbol and the offending
  `calibration.parameter` row

#### Scenario: The store has not been seeded
- **WHEN** the adapter starts against a PostgreSQL database with no `calibration` schema, or with
  an empty `calibration.instrument` table
- **THEN** the adapter publishes no ticks and exits with an error stating that the calibration
  store is not seeded and naming the `calibration-seeder` service

#### Scenario: PostgreSQL is not yet reachable
- **WHEN** the adapter starts while PostgreSQL is not accepting connections
- **THEN** it keeps retrying the connection, `/ready` reports the PostgreSQL dependency not
  connected, and it loads calibration once the connection succeeds

#### Scenario: Redis is reachable but calibration has not loaded
- **WHEN** the adapter has connected to Redis and PostgreSQL but has not yet loaded calibration
- **THEN** `/ready` reports not-ready, with the Redis and PostgreSQL dependencies connected and the
  calibration dependency not connected

#### Scenario: PostgreSQL goes down after startup
- **WHEN** PostgreSQL becomes unreachable after the adapter has loaded calibration
- **THEN** the adapter keeps publishing ticks for every symbol
