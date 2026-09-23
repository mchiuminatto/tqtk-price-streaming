## Purpose

Holds each instrument's calibration — its fitted return, spread and tick-interval distributions,
its minimum price increment, its quote currency and its initial price — in Redis, so the synthetic
feed resolves reference data from the bus it already uses rather than from a repository checkout.
This capability owns the naming of its own keyspace (`symbology`, `instrument:*`, `calib:*`);
`data-contract`'s stream and key naming covers only the market-data path (`ticks.raw.*`, `bars.*`,
`bar_state:*`), so no key family is owned by two capabilities.

## ADDED Requirements

### Requirement: Calibration keyspace
The store SHALL hold calibration under exactly these keys, keyed by venue symbol (e.g. `EUR/USD`):
- `symbology`: a hash mapping each venue symbol to its file symbol (e.g. `EUR/USD` → `EURUSD`)
- `instrument:<venue>`: a hash with fields `pip_size`, `quote_currency` and `initial_price`
- `calib:<venue>:<quantity>:family`: the fitted distribution family's name
- `calib:<venue>:<quantity>:param_count`: the number of fitted parameters
- `calib:<venue>:<quantity>:param:<n>`: one hash per parameter, `n` counting from `0` in the
  family's fit order, with fields `name`, `value` and `unit`
- `calib:symbols`: a set of every calibrated venue symbol
- `calib:quantities`: a set of every calibrated quantity — `return`, `spread` and `interval`

Numeric values (`pip_size`, `initial_price`, each parameter's `value`) SHALL be stored as decimal
strings. `pip_size` is the instrument's minimum price increment — the smallest price change it is
quoted in (e.g. `0.00001` for `EURUSD`, `0.001` for `USDJPY`).

#### Scenario: A consumer discovers the calibrated instruments
- **WHEN** a consumer reads `calib:symbols` and `symbology`
- **THEN** it obtains every calibrated venue symbol and, for each one, the file symbol used on the
  wire, without reading any other source

#### Scenario: A consumer reads one instrument's calibration
- **WHEN** a consumer reads `instrument:EUR/USD` and, for each quantity in `calib:quantities`, the
  `family`, `param_count` and `param:<n>` keys under `calib:EUR/USD:<quantity>`
- **THEN** it has everything needed to generate `EURUSD` ticks: initial price, minimum price
  increment, and a family plus ordered parameters for each of the three quantities

### Requirement: Stored units tagged per parameter
Every parameter hash SHALL carry a `unit` of exactly one of `quote`, `pip`, `ms` or
`dimensionless`. Location and scale parameters SHALL be stored in their quantity's stored unit —
returns in `quote` (price units of the quote currency), spreads in `pip` (multiples of the
instrument's `pip_size`), tick intervals in `ms` (milliseconds). Shape parameters (e.g. Student-t
`df`, log-normal `sigma`, Weibull, gamma and log-logistic `shape`) SHALL be `dimensionless`.

A reader SHALL convert a parameter to price or seconds according to its `unit` field alone: a
`pip` value multiplied by the instrument's `pip_size`, an `ms` value divided by `1000`, a `quote`
value unchanged, and a `dimensionless` value SHALL NOT be scaled. The conversion SHALL NOT depend
on the family name. Converting a seeded value to stored units and back SHALL reproduce it exactly.

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
The store SHALL be keyed by venue symbol. The file symbol — the form used in stream names and on
the `Tick` wire contract — SHALL be obtained only through the `symbology` hash, never derived from
the venue symbol by string manipulation.

#### Scenario: A consumer publishes for a calibrated instrument
- **WHEN** a consumer generates ticks for venue symbol `EUR/USD`
- **THEN** it publishes them under the file symbol `symbology` maps it to (`EURUSD`)

### Requirement: Seeding tool is the only writer
The keyspace SHALL be written only by the calibration seeding tool. Running the tool SHALL leave
the store holding exactly the tool's calibration set: re-running it with unchanged values SHALL
produce an identical keyspace, and an instrument removed from the tool SHALL no longer appear in
the store after the next run. A reader SHALL never observe a mix of two seeding runs' values.

#### Scenario: The tool is run twice
- **WHEN** the seeding tool is run against a store it has already seeded, with unchanged values
- **THEN** every key under the calibration keyspace holds the same value as after the first run

#### Scenario: An instrument is dropped from the tool
- **WHEN** an instrument is removed from the seeding tool and the tool is re-run
- **THEN** that instrument appears in neither `calib:symbols`, `symbology`, nor any
  `instrument:*` or `calib:*` key

### Requirement: Seeding tool is the single source of truth for seeded values
Every seeded value — each instrument's fitted return, spread and tick-interval distributions,
`pip_size`, `quote_currency`, `initial_price` and venue/file symbology — SHALL be defined in the
seeding tool and nowhere else. The tool SHALL NOT derive any seeded value from sample data at seed
time, and no document in the repository SHALL restate the seeded values.

#### Scenario: The tool seeds without sample data
- **WHEN** the seeding tool runs in an environment with no `data/` directory
- **THEN** it seeds the complete calibration set

#### Scenario: A seeded value changes
- **WHEN** an instrument's calibration needs to change
- **THEN** the change is made in the seeding tool alone, and no document needs a matching edit
