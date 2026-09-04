## Purpose

Durably persists every closed bar into the system of record, idempotently, tolerating temporary
database outages without losing bars within the retention window.

## ADDED Requirements

### Requirement: Sole writer of the bars table
The bar-persistence service SHALL be the only writer of the `bars` table.

#### Scenario: A bar row is written
- **WHEN** a row is written to the `bars` table
- **THEN** it was written by the bar-persistence service, not by any other component

### Requirement: Closed bars only
The service SHALL persist only bar records where `is_closed = true`. It SHALL NOT write a row
for an intrabar (forming) update.

#### Scenario: An intrabar update is published
- **WHEN** a bar update with `is_closed = false` is published
- **THEN** the bar-persistence service does not write a row for it

### Requirement: Idempotent upsert
The service SHALL upsert bar rows keyed on `(provider, symbol, timeframe, bar_start_ts)`, so a
repeated delivery of the same closed bar overwrites the existing row rather than creating a
duplicate.

#### Scenario: The same closed bar is delivered twice
- **WHEN** the same closed bar (identified by `provider`, `symbol`, `timeframe`, `bar_start_ts`)
  is delivered more than once — e.g. re-emitted after a crash recovery
- **THEN** the stored row is overwritten in place, and no duplicate row or doubled `tick_count`
  results

### Requirement: Consumer-group horizontal scaling
The service SHALL scale horizontally via Redis consumer groups such that each closed bar is
persisted exactly once (per the idempotent-upsert guarantee) across the fleet.

#### Scenario: Multiple instances run concurrently
- **WHEN** more than one bar-persistence instance is running against the same streams
- **THEN** no closed bar is lost and none produces more than one final row

### Requirement: Outage buffering
While the database is unavailable, the service SHALL stop acknowledging consumed stream entries
so that unpersisted closed bars remain on the bus, bounded by the retention/trim policy, rather
than being lost.

#### Scenario: Database becomes unavailable
- **WHEN** the database is unavailable for a period within the resiliency target
- **THEN** closed bars published during that period remain retrievable from the bus and are
  persisted once the database recovers

### Requirement: Live-pipeline isolation
The bar-persistence service being unavailable SHALL NOT affect live tick ingestion or
aggregation.

#### Scenario: Bar-persistence service is down
- **WHEN** the bar-persistence service is down
- **THEN** feed adapters and aggregation continue operating unaffected
