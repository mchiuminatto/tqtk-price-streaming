## Purpose

Keeps Redis stream memory bounded without silently dropping data an active consumer has not yet
read, while guaranteeing a hard resiliency ceiling and alerting whenever a forced gap occurs.

## ADDED Requirements

### Requirement: Consumer-position-driven trim
A janitor process SHALL periodically compute, for each stream, the least-advanced position across
all of that stream's consumer groups, and SHALL trim only entries older than that position minus
a safety margin.

#### Scenario: All consumers have advanced
- **WHEN** every consumer group on a stream has acknowledged past a given point
- **THEN** the janitor trims entries up to that point, minus the safety margin

#### Scenario: A consumer has not yet read an entry
- **WHEN** an entry has not yet been acknowledged by every active consumer group on its stream
- **THEN** that entry is not trimmed under normal operation (no consumer lagging past the hard
  cap)

### Requirement: Hard-cap backstop
The janitor SHALL enforce a ceiling `trim_to = max(min_consumer_position, stream_length - N)`,
where `N` is sized to the resiliency target. When a consumer group lags more than `N` entries
behind, its unread entries older than the cap SHALL be trimmed anyway.

#### Scenario: A consumer group lags past the hard cap
- **WHEN** a consumer group's position falls more than `N` entries behind the stream head
- **THEN** entries older than `stream_length - N` are trimmed even though that consumer group has
  not acknowledged them, and a Critical alert fires

### Requirement: Dead-consumer eviction
If a consumer group makes no progress for a configured window, the janitor SHALL forcibly evict
that consumer group's registration so it no longer holds back the trim point.

#### Scenario: A consumer group stops making progress
- **WHEN** a consumer group's position has not advanced for the configured dead-consumer window
- **THEN** the janitor evicts that consumer group's registration from the stream, allowing the
  trim point to advance past it

### Requirement: Alerting on forced data loss
Every hard-cap trim event and every dead-consumer eviction SHALL raise a Critical alert.

#### Scenario: A hard-cap trim occurs
- **WHEN** the janitor trims entries a consumer group has not yet acknowledged (hard-cap backstop
  triggered)
- **THEN** a Critical alert fires identifying the affected stream and consumer group
