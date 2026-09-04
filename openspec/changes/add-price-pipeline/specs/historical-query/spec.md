## Purpose

Provides a stateless, read-only REST API over persisted ticks and closed bars, for historical
range queries and backfill, filterable by provider.

## ADDED Requirements

### Requirement: Read-only access
The service SHALL perform no writes to the `ticks` or `bars` tables. It SHALL only read from
them.

#### Scenario: A request is served
- **WHEN** the service handles any request
- **THEN** no row in `ticks` or `bars` is inserted, updated, or deleted as a result

### Requirement: Closed bars only
Bar queries SHALL return only bars where `is_closed = true`.

#### Scenario: A bar query is served
- **WHEN** a client queries historical bars for a `(provider, symbol, timeframe)`
- **THEN** every bar in the response has `is_closed = true`

### Requirement: Filterable by provider
Queries SHALL accept a `provider` filter, restricting results to that provider's data.

#### Scenario: Client filters by provider
- **WHEN** a client queries with a `provider` filter
- **THEN** only rows tagged with that `provider` are returned

### Requirement: Filterable by symbol, timeframe, and time range
Bar queries SHALL accept `symbol`, `timeframe`, and a time-range filter (start/end); tick queries
SHALL accept `symbol` and a time-range filter.

#### Scenario: Client requests a historical range
- **WHEN** a client queries bars for a `(provider, symbol, timeframe)` with a start/end time range
- **THEN** only bars whose `bar_start_ts` falls within that range are returned

### Requirement: Stateless horizontal scaling
Any running instance SHALL be able to serve any request; the service SHALL require no session
affinity or shared in-process state between instances.

#### Scenario: Multiple instances run behind a load balancer
- **WHEN** requests are distributed across multiple running instances
- **THEN** any instance returns the same result for the same query, independent of which prior
  requests it has served
