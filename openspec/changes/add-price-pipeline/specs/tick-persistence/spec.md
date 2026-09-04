## Purpose

Durably persists every raw tick from every active provider into the system of record, tolerating
temporary database outages without losing ticks within the retention window.

## ADDED Requirements

### Requirement: Sole writer of the ticks table
The tick-persistence service SHALL be the only writer of the `ticks` table.

#### Scenario: A tick row is written
- **WHEN** a row is written to the `ticks` table
- **THEN** it was written by the tick-persistence service, not by any other component

### Requirement: Durable batch persistence
The service SHALL consume raw-tick streams across all active providers and durably write them, in
batches, to the `ticks` table, preserving `provider`, `symbol`, and timestamps.

#### Scenario: A tick is published
- **WHEN** a tick is published to a raw-tick stream
- **THEN** it eventually appears as a row in the `ticks` table with its `provider`, `symbol`, and
  timestamp fields preserved

### Requirement: Consumer-group horizontal scaling
The service SHALL scale horizontally via Redis consumer groups such that each tick is persisted
exactly once across the fleet, regardless of how many instances are running.

#### Scenario: Multiple instances run concurrently
- **WHEN** more than one tick-persistence instance is running against the same streams
- **THEN** each tick is persisted exactly once, not duplicated and not skipped

### Requirement: Outage buffering
While the database is unavailable, the service SHALL stop acknowledging consumed stream entries
so that unpersisted ticks remain on the bus, bounded by the retention/trim policy, rather than
being lost.

#### Scenario: Database becomes unavailable
- **WHEN** the database is unavailable for a period within the resiliency target
- **THEN** ticks published during that period remain retrievable from the bus and are persisted
  once the database recovers

### Requirement: Live-pipeline isolation
The tick-persistence service being unavailable SHALL NOT affect live tick ingestion or
aggregation.

#### Scenario: Tick-persistence service is down
- **WHEN** the tick-persistence service is down
- **THEN** feed adapters continue publishing and aggregation continues producing bar updates
  unaffected
