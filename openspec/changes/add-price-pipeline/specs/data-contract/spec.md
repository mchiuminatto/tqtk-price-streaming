  ## Purpose

Defines the pipeline's contract surfaces: the single, language-neutral `Tick`/`Bar` record
schema, Redis stream/key naming, and provider/sequence identity rules that every feed adapter and
every consumer — regardless of implementation language — must conform to, plus the versioned
`ticks`/`bars` storage schema that persistence services own and read-side consumers bind to.

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

### Requirement: Versioned storage schema contract
The `ticks` and `bars` table schemas SHALL be defined as an explicit, versioned contract artifact
carrying a `schema_version`, maintained alongside the language-neutral `Tick`/`Bar` wire schema
rather than existing only as whatever DDL happens to have been applied to the database.

#### Scenario: A service binds to the storage schema
- **WHEN** a service reads from or writes to the `ticks` or `bars` table
- **THEN** the schema it depends on is defined by the versioned storage contract artifact, and the
  `schema_version` it was built against is identifiable

#### Scenario: The storage schema changes
- **WHEN** a column is added to the `ticks` or `bars` table
- **THEN** the storage contract artifact is updated and its `schema_version` is incremented in the
  same change

### Requirement: Table DDL ownership follows sole-writer ownership
The service designated as the sole writer of a table SHALL also own that table's DDL and
migrations. No table's schema SHALL be created or altered by platform bootstrap, by a service that
is not its sole writer, or by any out-of-band manual step.

#### Scenario: A table's schema is created or altered
- **WHEN** the `ticks` or `bars` table is created or its schema is altered
- **THEN** the change was applied by that table's sole-writer service, not by platform bootstrap or
  by any other service

### Requirement: Additive-only schema evolution
Within a released `schema_version` lineage, storage schema changes SHALL be additive only. A
migration SHALL NOT drop a column, rename a column, or narrow a column's type. A newly added
column SHALL be nullable or carry a default, so that a reader built against an earlier
`schema_version` continues to function unchanged.

#### Scenario: An additive migration is applied
- **WHEN** a migration adds a nullable or defaulted column to `ticks` or `bars`
- **THEN** a read-side consumer built against the previous `schema_version` continues to serve
  queries without modification

#### Scenario: A destructive migration is proposed
- **WHEN** a migration would drop a column, rename a column, or narrow a column's type on `ticks`
  or `bars`
- **THEN** it is rejected before merge rather than being applied to the database

### Requirement: Declared read-side consumers
A service that reads a table it does not own SHALL declare, as configuration or code, the
`schema_version` it binds to. Reading a table without a declared version binding SHALL NOT be
permitted.

#### Scenario: A read-side service starts
- **WHEN** a service that reads a table it does not own starts up
- **THEN** the `schema_version` it binds to is declared and observable, not implicit in its queries

#### Scenario: The declared version is no longer available
- **WHEN** a read-side consumer's declared `schema_version` is absent from the database it connects
  to
- **THEN** the service fails its readiness check rather than serving queries against an
  unrecognized schema

### Requirement: Storage contract tests on both sides
Both the owning writer and every declared read-side consumer of a table SHALL have automated tests
asserting conformance to the storage contract — the writer that it produces the contracted schema,
the reader that it consumes it — exercised against a shared fixture derived from the contract
artifact.

#### Scenario: A writer breaks the contract
- **WHEN** a sole-writer service's migrations no longer produce the schema described by its
  declared `schema_version`
- **THEN** its contract test fails

#### Scenario: A reader breaks the contract
- **WHEN** a read-side consumer queries a column or type absent from its declared `schema_version`
- **THEN** its contract test fails against the shared fixture, without requiring a running instance
  of the writing service
