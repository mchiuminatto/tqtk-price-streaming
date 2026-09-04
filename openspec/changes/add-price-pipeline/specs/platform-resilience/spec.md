## Purpose

Defines Redis durability configuration and per-component failure-isolation guarantees so the
pipeline recovers from crashes and restarts within a bounded window, with each component's outage
blast radius explicitly documented.

## ADDED Requirements

### Requirement: Redis durability configuration
Redis SHALL run with both AOF persistence (`everysec` fsync policy) and RDB snapshotting enabled.

#### Scenario: Redis process restarts
- **WHEN** the Redis process restarts (not merely a downtime, an actual process restart)
- **THEN** checkpoints and unpersisted ticks/bars written before the restart are recovered from
  AOF/RDB rather than lost

### Requirement: Recovery-time objective
The system SHALL recover from a crash or disconnect of any single component within seconds to at
most a few hours.

#### Scenario: A stateful component crashes and restarts
- **WHEN** a component holding in-memory or checkpointed state crashes and is restarted
- **THEN** it resumes correct operation within the resiliency target, without manual data repair

### Requirement: Aggregation-svc failure isolation
When `aggregation-svc` is unavailable, raw tick capture and tick persistence SHALL continue
operating; only new bar production SHALL stop.

#### Scenario: Aggregation-svc goes down
- **WHEN** `aggregation-svc` is unavailable
- **THEN** feed adapters continue publishing ticks and tick-persistence continues writing them,
  while no new bar updates are produced until it recovers

### Requirement: Persistence-service failure isolation
When `tick-persistence-svc` or `bar-persistence-svc` is unavailable, live ingestion and
aggregation SHALL continue operating unaffected; durability SHALL resume once the service
recovers.

#### Scenario: A persistence service goes down
- **WHEN** `tick-persistence-svc` or `bar-persistence-svc` is unavailable
- **THEN** live tick ingestion and bar aggregation continue unaffected, and buffered data is
  persisted once the service recovers

### Requirement: Streaming-gateway failure isolation
When `streaming-gateway-svc` is unavailable, LAN consumers SHALL lose the live feed, but
ingestion, aggregation, persistence, and the database SHALL be unaffected.

#### Scenario: Streaming-gateway-svc goes down
- **WHEN** `streaming-gateway-svc` is unavailable
- **THEN** LAN consumers cannot receive live updates, while ingestion, aggregation, and
  persistence continue unaffected

### Requirement: Redis as a shared platform dependency
Redis SHALL be documented as a single point of failure: when Redis is unavailable, the entire
pipeline SHALL stall, since it is the shared bus and checkpoint store for every service.

#### Scenario: Redis is unavailable
- **WHEN** Redis is unavailable
- **THEN** no service can publish, consume, or checkpoint, and the pipeline stalls until Redis
  recovers
