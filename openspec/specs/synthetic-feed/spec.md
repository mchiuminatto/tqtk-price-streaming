# synthetic-feed Specification

## Purpose

Generates synthetic tick data for the in-scope symbol set, conforming to the data contract, as the
sole active data source during Phase 1 — statistically calibrated per instrument, each against its
own fitted return, spread, and tick-interval distribution (not one fixed family for every symbol),
per `../../../docs/synthetic-price.md`.

## Requirements

### Requirement: Symbol set
The synthetic feed SHALL generate ticks for exactly the configured symbol set — the 17 symbols
whose sample data is present under `data/*.parquet` (13 FX pairs: 6 majors + 7 crosses; plus 4
non-FX instruments: 2 equity CFDs — AAPLUSUSD, ARKQUSUSD — and 2 index CFDs — USA500IDXUSD,
USATECHIDXUSD) — and no others.

#### Scenario: Adapter starts
- **WHEN** the synthetic feed adapter starts
- **THEN** it publishes ticks tagged `provider="synthetic"` for each of the 17 configured symbols
  and for no symbol outside that set

### Requirement: Per-instrument return distribution
For each configured symbol, the adapter SHALL use that symbol's own fitted return distribution —
a family (e.g. Laplace, Student-t) plus that family's parameters, selected offline by AIC over
several candidate families and documented per symbol in `../../../docs/tick-distributions.md`
— rather than one fixed distribution family shared by every symbol.

#### Scenario: Adapter starts
- **WHEN** the synthetic feed adapter starts for a configured symbol
- **THEN** that symbol's fitted return distribution (family and parameters) is resolved before the
  first tick is published, and no other symbol's parameters are used in its place

### Requirement: Price generation follows a calibrated biased random walk
The next bid for a symbol SHALL be `bid_t+1 = bid_t + r_t+1`, where `r_t+1` is drawn from that
symbol's fitted return distribution, and SHALL be rounded to that symbol's minimum price-change
unit (derived from its sample data) before publication.

#### Scenario: Successive prices are generated
- **WHEN** the synthetic feed generates a sequence of bids for a symbol
- **THEN** each bid equals the previous bid plus a return sampled from that symbol's fitted return
  distribution, rounded to the symbol's minimum price-change unit

### Requirement: Per-instrument spread distribution
For each configured symbol, the adapter SHALL use that symbol's own fitted spread distribution —
a family (e.g. Weibull, Gamma, Log-logistic) plus that family's parameters, selected offline by
AIC over several candidate positive-support families and documented per symbol in
`../../../docs/spread-distributions.md` — to draw a fresh spread for every tick, rather than
publishing a single fixed spread constant for every symbol and every tick.

#### Scenario: A tick is generated
- **WHEN** the synthetic feed generates a tick for a symbol
- **THEN** the published ask equals that tick's bid plus a spread freshly sampled from that
  symbol's fitted spread distribution, rounded to the symbol's minimum price-change unit

### Requirement: Decimal-precision price arithmetic
Every price value and every value derived directly from a price (a return, a spread, the running
walk state, the rounded bid/ask) SHALL be represented as `Decimal`, never `float`, throughout
price calculation, per `../../../docs/synthetic-price.md`. A distribution's own fitted
parameters remain `float` — the sampler is float-only — but a sampled return or spread SHALL be
converted to `Decimal` before being combined with a price. The tick's bid/ask are cast to `float`
only at publication, to satisfy the `Tick` wire contract's `Price` type; no `float` price
arithmetic occurs before that boundary.

#### Scenario: A price is generated
- **WHEN** the synthetic feed computes a bid and ask from a previous bid and sampled return/spread
- **THEN** the previous bid, the new bid, the spread, and the ask are all `Decimal`, and the result
  is cast to `float` only when constructing the published tick

### Requirement: Initial price from sample data
Each symbol's first generated price (`p_0`) SHALL be sourced from that symbol's sample data.

#### Scenario: Adapter starts
- **WHEN** the synthetic feed adapter starts publishing a symbol for the first time in a session
- **THEN** the first published price is the `p_0` derived from that symbol's sample data

### Requirement: Per-instrument tick-interval distribution
For each configured symbol, the adapter SHALL use that symbol's own fitted tick-interval
distribution — a family (e.g. Log-normal, Log-logistic) plus that family's parameters, selected
offline by AIC over several candidate positive-support families and documented per symbol in
`../../../docs/tick-interval-distributions.md` — rather than one fixed distribution family
shared by every symbol.

#### Scenario: Adapter starts
- **WHEN** the synthetic feed adapter starts for a configured symbol
- **THEN** that symbol's fitted tick-interval distribution (family and parameters) is resolved
  before the first tick is published

### Requirement: Configurable tick pacing
The per-symbol publish pacing SHALL be configurable without a code change, as a multiplier applied
to every interval drawn from that symbol's fitted tick-interval distribution: `1.0` publishes at
the distribution's own timescale, and other values speed up or slow down publication
proportionally while preserving its relative timing jitter.

#### Scenario: Tick pacing is reconfigured
- **WHEN** the configured pacing multiplier is changed
- **THEN** the adapter's publish rate changes accordingly on its next start, with no source code
  modification required

### Requirement: Tick pacing follows the calibrated interval distribution
The interval the adapter sleeps between successive ticks for a symbol SHALL be drawn from that
symbol's fitted tick-interval distribution, scaled by the configured pacing multiplier (see
"Configurable tick pacing"); the resulting delay determines when the next tick is generated, while
`recv_ts` itself continues to be stamped from the wall clock at publish time per the `recv_ts`
stamping requirement below.

#### Scenario: Successive ticks are generated
- **WHEN** the synthetic feed generates a sequence of ticks for a symbol
- **THEN** the wait before each tick is sampled from that symbol's fitted tick-interval
  distribution, scaled by the configured pacing multiplier, and `recv_ts` is still stamped from
  the wall clock at publish time

### Requirement: Provider tagging
Every tick generated by the synthetic feed SHALL be tagged `provider="synthetic"`.

#### Scenario: Tick is published
- **WHEN** the synthetic feed generates a tick
- **THEN** the published record's `provider` field is `"synthetic"`

### Requirement: `recv_ts` stamping
The adapter SHALL stamp `recv_ts` once per tick, monotonic per `(provider, symbol)`, per the data
contract.

#### Scenario: Two ticks generated in sequence
- **WHEN** the adapter generates two successive ticks for the same symbol
- **THEN** the second tick's `recv_ts` is greater than or equal to the first tick's `recv_ts`

### Requirement: Session and sequence reset on start
The adapter SHALL assign a new `session_id` and reset `seq` to `0` for each `(provider, symbol)`
at the start of every process run, per the data contract's `seq`/`session_id` semantics.

#### Scenario: Adapter process restarts
- **WHEN** the synthetic feed adapter process is restarted
- **THEN** every `(provider, symbol)` stream it publishes begins a new `session_id` with `seq`
  reset to `0`

### Requirement: Raw-tick-only publication
The adapter SHALL publish only to `ticks.raw.synthetic.{symbol}`. It SHALL NOT compute or publish
any aggregation.

#### Scenario: Adapter emits data
- **WHEN** the synthetic feed adapter emits generated ticks
- **THEN** the only stream it writes to is `ticks.raw.synthetic.{symbol}` for the relevant symbol
