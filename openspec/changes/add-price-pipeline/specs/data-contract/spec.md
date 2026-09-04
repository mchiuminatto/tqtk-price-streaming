## Purpose

Defines the single, language-neutral `Tick`/`Bar` record schema, Redis stream/key naming, and
provider/sequence identity rules that every feed adapter and every consumer — regardless of
implementation language — must conform to.

## ADDED Requirements

### Requirement: Tick record schema
Every tick record SHALL carry `provider`, `symbol`, `provider_ts`, `recv_ts`, `bid`, `ask`,
`session_id`, and `seq`. `bid_size`, `ask_size`, and `provider_symbol` are optional fields.

#### Scenario: Feed adapter publishes a tick
- **WHEN** a feed adapter publishes a tick to its raw-tick stream
- **THEN** the record includes `provider`, `symbol`, `provider_ts`, `recv_ts`, `bid`, `ask`,
  `session_id`, and `seq`

### Requirement: Bar record schema
Every bar record SHALL carry `provider`, `symbol`, `timeframe`, `bar_start_ts`, `open`, `high`,
`low`, `close`, `tick_count`, `is_closed`, and `last_update_ts`.

#### Scenario: Aggregation service publishes a bar update
- **WHEN** a bar update (intrabar or closed) is published to a bar stream
- **THEN** the record includes `provider`, `symbol`, `timeframe`, `bar_start_ts`, `open`, `high`,
  `low`, `close`, `tick_count`, `is_closed`, and `last_update_ts`

### Requirement: Stream and key naming
Raw ticks SHALL be published to `ticks.raw.{provider}.{symbol}`. Bar updates SHALL be published
to `bars.{timeframe}.{provider}.{symbol}`. Aggregation checkpoints SHALL be stored under
`bar_state:{provider}:{symbol}:{timeframe}`.

#### Scenario: A new provider's adapter starts publishing
- **WHEN** a feed adapter for a provider not previously active begins publishing ticks
- **THEN** its ticks appear under `ticks.raw.{provider}.{symbol}` and a consumer that discovers
  raw-tick streams dynamically picks them up without a consumer-side configuration change

### Requirement: Provider tagging
Every `Tick` and `Bar` record SHALL carry a non-empty `provider` field identifying which data
source produced it. Records from different providers SHALL NOT be blended or averaged into a
single consolidated record.

#### Scenario: Two providers are active concurrently
- **WHEN** both `synthetic` and a second provider are actively publishing for the same symbol
- **THEN** each provider's ticks and bars remain tagged and queryable independently, with no
  default view that merges them

### Requirement: Dual timestamp semantics
Every `Tick` record SHALL carry both `provider_ts` (the provider's own timestamp) and `recv_ts`
(stamped once by the feed adapter). `recv_ts` SHALL be monotonic per `(provider, symbol)`.

#### Scenario: Two ticks arrive in sequence for the same symbol
- **WHEN** a feed adapter stamps `recv_ts` on two successive ticks for the same
  `(provider, symbol)`
- **THEN** the second tick's `recv_ts` is greater than or equal to the first tick's `recv_ts`

### Requirement: `seq` and `session_id` semantics
`seq` SHALL be a `uint64` counter, monotonic and gapless per `(provider, symbol)`, and SHALL
reset to `0` at the start of every feed-adapter session for that `(provider, symbol)`. Every tick
SHALL carry a `session_id` identifying the adapter session that produced it.

#### Scenario: Feed adapter restarts
- **WHEN** a feed adapter (re)starts
- **THEN** it assigns a new `session_id` and resets `seq` to `0` for each `(provider, symbol)` it
  publishes

#### Scenario: Consumer detects a genuine gap
- **WHEN** a consumer observes `seq` jump by more than 1 within the same `session_id`
- **THEN** it counts and/or alerts a tick-loss event

#### Scenario: Consumer observes a session change
- **WHEN** a consumer observes `session_id` change on a stream
- **THEN** it treats the discontinuity as an expected adapter restart, not tick loss, and does not
  raise a loss alarm

### Requirement: Dedup and reconciliation key
Consumers that persist or reconcile tick data SHALL key deduplication on
`(provider, symbol, session_id, seq)`, never on `seq` alone.

#### Scenario: Duplicate delivery
- **WHEN** a consumer receives two records with the same `(provider, symbol, session_id, seq)`
  tuple
- **THEN** it treats the second occurrence as a duplicate, not as new data

### Requirement: Language-neutral definition
The `Tick`/`Bar` schema and stream-naming conventions SHALL be defined independently of any
single implementation language, so that adapters written in a language other than the primary
implementation language can conform to the same contract.

#### Scenario: A non-Python adapter conforms
- **WHEN** a feed adapter implemented in a language other than the primary implementation
  language publishes a tick
- **THEN** the record matches the same `Tick` schema and stream-naming contract as adapters
  written in the primary language
