## Purpose

Maintains live OHLCV bar state on both price sides across all 8 timeframes for every active
`(provider, symbol)` pair, publishing per-tick intrabar updates and time-driven bar closes within
the internal latency budget, with per-actor crash recovery.

## ADDED Requirements

### Requirement: Actor isolation
The system SHALL maintain one independent actor per `(provider, symbol, timeframe)`. That actor
SHALL hold both of its window's side-bars, since both derive from the same tick and close on the
same boundary; `side` SHALL NOT be part of the actor key. Processing for one timeframe of a symbol
SHALL NOT block or delay processing for another timeframe of the same symbol.

#### Scenario: A long-timeframe close overlaps a short-timeframe tick
- **WHEN** a 1D bar close is being processed for a symbol
- **THEN** concurrent 1s bar updates for the same symbol are not delayed by it

### Requirement: Per-tick intrabar update across all timeframes and both sides
Every tick SHALL update the forming bar's Open/High/Low/Close on all 8 configured timeframes
(1s, 1m, 5m, 15m, 30m, 1h, 4h, 1D), on both sides: the tick's `bid` folds into the `bid` bar and
its `ask` into the `ask` bar.

#### Scenario: A tick arrives
- **WHEN** a tick arrives for an active `(provider, symbol)`
- **THEN** the forming `bid` and `ask` bars for each of the 8 timeframes are updated and a bar
  update is published for each side of each timeframe

### Requirement: Paired side emission
The two side-records for one `(provider, symbol, timeframe, bar_start_ts)` SHALL be published in a
single atomic bus operation, so no consumer can observe one side of a window advanced past the
other. Ordering guarantees SHALL NOT extend to consumption: a consumer reading in batches may
receive the two records in different reads, and pairs them on the bar's identity per the
`data-contract` capability.

#### Scenario: A bar update is emitted
- **WHEN** an update (intrabar or close) is emitted for a bar window
- **THEN** its `bid` and `ask` records are published in one atomic operation, never as two
  separately-visible publishes

#### Scenario: A consumer reads a partial batch
- **WHEN** a consumer's batched read returns one side's record without the other
- **THEN** the missing side is not treated as a gap or a loss event; the consumer pairs on
  `(provider, symbol, timeframe, bar_start_ts)` across reads

### Requirement: Event-time bucketing
A tick SHALL be assigned to a bar window using its `recv_ts`, not its arrival order into
processing.

#### Scenario: Ticks are folded into a bar regardless of arrival order
- **WHEN** two ticks for the same symbol have `recv_ts` values that both fall within a single bar
  window `[T, T+tf)`, but arrive at the aggregation stage out of that order
- **THEN** both ticks are folded into the bar for window `[T, T+tf)`

### Requirement: Time-driven bar close
A bar for window `[T, T+tf)` SHALL close at `T + tf + grace` regardless of whether a tick arrives
at the boundary, where `grace` is a small fixed delay.

#### Scenario: No tick arrives at the boundary
- **WHEN** no tick arrives at a timeframe boundary
- **THEN** the bar for the window ending at that boundary still closes at `T + tf + grace`

#### Scenario: A late-arriving in-flight tick is still admitted
- **WHEN** a tick with `recv_ts < T + tf` arrives after `T + tf` but before `T + tf + grace`
- **THEN** it is folded into the bar for window `[T, T+tf)` before that bar closes

### Requirement: Late-tick handling
A tick whose `recv_ts` belongs to a bar window that has already closed SHALL be dropped and
counted.

#### Scenario: A tick arrives after its bar has closed
- **WHEN** a tick arrives with `recv_ts` before the close boundary of an already-closed bar for
  its `(provider, symbol, timeframe)`
- **THEN** the tick is dropped and `late_ticks_total{provider,symbol,timeframe}` is incremented

### Requirement: Idle-bar carry-forward
A bar window that closes with zero ticks received SHALL still be emitted, keeping the series
gapless.

#### Scenario: A bar window has no ticks
- **WHEN** a bar window closes having received zero ticks
- **THEN** each side's emitted bar has `Open = High = Low = Close` equal to that same side's
  previous bar `Close`, with `tick_count = 0` and `is_closed = true`

### Requirement: `Open` semantics for non-idle bars
For a bar window that receives at least one tick, each side's `Open` SHALL be that side's price on
the first tick whose `recv_ts` falls in that window.

#### Scenario: First tick of a new bar window
- **WHEN** the first tick with `recv_ts` in a new bar window arrives
- **THEN** the `bid` bar's `Open` is set to that tick's `bid` and the `ask` bar's `Open` to its
  `ask`, each overwriting any provisional `Open` previously held for that window

### Requirement: Checkpoint content and cadence
Each actor SHALL checkpoint both sides' forming `Open/High/Low/Close`, the shared `tick_count`,
`bar_start_ts`, and the last-consumed tick stream ID, under its single unchanged
`bar_state:{provider}:{symbol}:{timeframe}` key: immediately on every bar close, and at least once
per second while a bar is forming.

#### Scenario: A bar closes
- **WHEN** an actor closes a bar
- **THEN** its checkpoint is written before or together with the closed-bar publish, with a
  stream ID at or past the last tick folded into that bar

#### Scenario: A bar is still forming after one second
- **WHEN** one second elapses while a bar is still forming
- **THEN** the actor's checkpoint is refreshed with both sides' current `Open/High/Low/Close`, the
  `tick_count`, and the last-consumed stream ID

### Requirement: Crash recovery
On restart, an actor SHALL resume consuming ticks from exactly the stream ID recorded in its
last checkpoint.

#### Scenario: An actor restarts after a crash
- **WHEN** an actor process restarts after a crash
- **THEN** it resumes reading ticks from the stream ID stored in its most recent checkpoint

### Requirement: Internal latency budget
The elapsed time from a tick's `recv_ts` to the corresponding bar update being published SHALL be
measurable, and SHALL average under 10 ms under normal operating conditions.

#### Scenario: A bar update is published
- **WHEN** a bar update (intrabar or closed) is published as a result of a tick
- **THEN** the elapsed time since that tick's `recv_ts` is recorded and observable via the
  `tick_to_bar_latency_ms` metric
