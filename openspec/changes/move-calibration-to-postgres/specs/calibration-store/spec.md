## ADDED Requirements

### Requirement: Calibration schema
The store SHALL hold calibration in the PostgreSQL schema `calibration`, in exactly these tables,
keyed by venue symbol (e.g. `EUR/USD`):
- `calibration.instrument`: one row per calibrated instrument, with columns `venue_symbol`
  (primary key), `file_symbol` (not null, unique; e.g. `EURUSD`), `pip_size`, `quote_currency` and
  `initial_price` (all not null)
- `calibration.distribution`: one row per instrument and quantity, with columns `venue_symbol`
  (referencing `instrument`), `quantity` (one of `return`, `spread`, `interval`) and `family`
  (not null), with primary key `(venue_symbol, quantity)`
- `calibration.parameter`: one row per fitted parameter, with columns `venue_symbol` and
  `quantity` (together referencing `distribution`), `position` (counting from `0` in the family's
  fit order), `name`, `value` and `unit` (all not null), with primary key
  `(venue_symbol, quantity, position)`

Numeric values (`pip_size`, `initial_price`, each parameter's `value`) SHALL be stored as exact
decimals (`numeric`) that keep their written scale, and SHALL be finite. `pip_size` is the
instrument's minimum price increment, the smallest price change it is quoted in (e.g. `0.00001`
for `EURUSD`, `0.001` for `USDJPY`). It SHALL be positive, and a power of ten written without
trailing zeros. `initial_price` SHALL be positive. The database SHALL reject, at seed time, a
seeding that breaks any of these: a non-positive `pip_size` or `initial_price`, a quantity or unit
outside its allowed set, a non-finite value, a duplicate file symbol, a negative `position`, or a
distribution or parameter row with no parent row. The calibrated instruments are the rows of
`instrument`, and a distribution's parameter count is the number of its `parameter` rows.

#### Scenario: A consumer discovers the calibrated instruments
- **WHEN** a consumer selects `venue_symbol` and `file_symbol` from `calibration.instrument`
- **THEN** it obtains every calibrated venue symbol and, for each one, the file symbol used on the
  wire, without reading any other source

#### Scenario: A consumer reads one instrument's calibration
- **WHEN** a consumer selects the `instrument` row for `EUR/USD` and its `distribution` and
  `parameter` rows, ordered by `quantity` and `position`
- **THEN** it has everything needed to generate `EURUSD` ticks: initial price, minimum price
  increment, and a family plus ordered parameters for each of the three quantities

#### Scenario: A stored decimal keeps its scale
- **WHEN** a consumer reads the `pip_size` of an instrument seeded with `0.00001`
- **THEN** it receives the exact decimal `0.00001`, not a binary floating-point approximation and
  not a value with a different number of decimal places

#### Scenario: A seeding violates an integrity rule
- **WHEN** the seed script inserts a parameter with `unit` `bp`, or an instrument with
  `initial_price` `0`
- **THEN** the database rejects the seeding and the store keeps its previous contents

## MODIFIED Requirements

### Requirement: Stored units tagged per parameter
Every `parameter` row SHALL carry a `unit` of exactly one of `quote`, `pip`, `ms` or
`dimensionless`. Location and scale parameters SHALL be stored in their quantity's stored unit:
returns in `quote` (price units of the quote currency), spreads in `pip` (multiples of the
instrument's `pip_size`), tick intervals in `ms` (milliseconds). Shape parameters (e.g. Student-t
`df`, log-normal `sigma`, Weibull, gamma and log-logistic `shape`) SHALL be `dimensionless`.

A reader SHALL convert a parameter to price or seconds according to its `unit` alone:
- a `pip` value is multiplied by the instrument's `pip_size`
- an `ms` value is divided by `1000`
- a `quote` value is used unchanged
- a `dimensionless` value SHALL NOT be scaled

The conversion SHALL NOT depend on the family name. It SHALL be exact decimal arithmetic on the
stored decimal, introducing no rounding before the final value is produced.

#### Scenario: A spread parameter is read
- **WHEN** a reader reads a spread `scale` stored as `4.92226` with `unit=pip` for an instrument
  whose `pip_size` is `0.00001`
- **THEN** the price-unit value it uses is `0.0000492226`

#### Scenario: A shape parameter is read
- **WHEN** a reader reads a spread `shape` stored with `unit=dimensionless`
- **THEN** it uses the stored value unchanged, even though the other parameters of the same
  distribution are converted from pips

#### Scenario: A tick-interval parameter is read
- **WHEN** a reader reads a tick-interval `scale` stored as `466.108` with `unit=ms`
- **THEN** the value it uses is `0.466108` seconds

### Requirement: Venue and file symbology
The store SHALL be keyed by venue symbol. The file symbol, the form used in stream names and on
the `Tick` wire contract, SHALL be obtained only from `calibration.instrument.file_symbol`, never
derived from the venue symbol by string manipulation.

#### Scenario: A consumer publishes for a calibrated instrument
- **WHEN** a consumer generates ticks for venue symbol `EUR/USD`
- **THEN** it publishes them under the file symbol that `EUR/USD`'s `instrument` row holds
  (`EURUSD`)

### Requirement: Seed script is the only writer
The `calibration` schema, both its definition and its data, SHALL be written only by applying the
calibration seed script, a SQL file, with `psql` from a dedicated one-shot seeder container
separate from the adapter. Each application SHALL be one database transaction that drops the
schema, recreates it and inserts the full calibration set. Applying the script SHALL leave the
store holding exactly the script's calibration set:
- re-applying it unchanged SHALL produce an identical store
- an instrument removed from the script SHALL no longer appear in the store afterwards

A reader SHALL never observe a mix of two seedings' values. A script that fails to apply for any reason SHALL leave the store unchanged and make the
seeding fail, so the adapter does not start on it. Reasons include a syntax error, a violated
constraint, or an unreachable database. Other components SHALL only read from the schema: they
SHALL NOT create tables, views, foreign keys or grants in it or against it, because each seeding
drops them.

#### Scenario: The script is applied twice
- **WHEN** the seed script is applied to a store it has already seeded, unchanged
- **THEN** every table in the `calibration` schema holds the same rows as after the first run

#### Scenario: An instrument is dropped from the script
- **WHEN** an instrument's rows are removed from the seed script and the script is re-applied
- **THEN** that instrument appears in none of `calibration.instrument`,
  `calibration.distribution` or `calibration.parameter`

#### Scenario: The script contains a malformed command
- **WHEN** a seed script containing a malformed SQL statement is applied to a seeded store
- **THEN** no calibration row changes, the seeder exits with a failure, and the adapter is not
  started against the result

#### Scenario: The script contains a command that fails only when executed
- **WHEN** a seed script whose statements all parse but one violates a `CHECK`, unique or foreign
  key constraint is applied to a seeded store
- **THEN** no calibration row changes, the seeder exits with a failure, and the adapter is not
  started against the result

#### Scenario: The database is unreachable
- **WHEN** the seeder runs while PostgreSQL cannot be reached
- **THEN** the seeder exits with a failure and the adapter is not started

### Requirement: Seed script is the single source of truth for seeded values
Every seeded value SHALL be defined in the seed script, as SQL carrying values in stored units, and
nowhere else: not in program code, and not derived from sample data at seed time. The seeded values
are each instrument's fitted return, spread and tick-interval distributions, its `pip_size`,
`quote_currency` and `initial_price`, and its venue/file symbology. No document in the repository
SHALL restate the seeded values.

#### Scenario: The seeder runs without program code or sample data
- **WHEN** the seeder container applies the seed script
- **THEN** it seeds the complete calibration set using only `psql` and the script, with no
  application code and no `data/` directory

#### Scenario: A seeded value changes
- **WHEN** an instrument's calibration needs to change
- **THEN** the change is made in the seed script alone, and neither code nor any document needs a
  matching edit

## REMOVED Requirements

### Requirement: Calibration keyspace
**Reason**: The store moves from Redis to the PostgreSQL `calibration` schema, so SQL consumers can
query and join it. The keys `param_count`, `calib:symbols` and `calib:quantities` become derivable
from the tables.
**Migration**: Read `calibration.instrument`, `calibration.distribution` and
`calibration.parameter` instead (see "Calibration schema"). Existing Redis volumes still hold the
old `symbology`, `instrument:*` and `calib:*` keys. They are inert, and `deploy/README.md`
documents a one-time cleanup.
