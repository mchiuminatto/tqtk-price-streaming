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

### Requirement: Filterable by symbol, side, timeframe, and time range
Bar queries SHALL accept `symbol`, `timeframe`, an optional `side`, and a time-range filter
(start/end); tick queries SHALL accept `symbol` and a time-range filter. Omitting `side` SHALL
return both sides rather than defaulting to one.

#### Scenario: Client requests a historical range
- **WHEN** a client queries bars for a `(provider, symbol, timeframe)` with a start/end time range
- **THEN** only bars whose `bar_start_ts` falls within that range are returned

#### Scenario: Client filters by side
- **WHEN** a client queries bars with `side = bid`
- **THEN** only `bid` rows are returned, and a query omitting `side` returns both sides

### Requirement: Stateless horizontal scaling
Any running instance SHALL be able to serve any request; the service SHALL require no session
affinity or shared in-process state between instances.

#### Scenario: Multiple instances run behind a load balancer
- **WHEN** requests are distributed across multiple running instances
- **THEN** any instance returns the same result for the same query, independent of which prior
  requests it has served

### Requirement: Declared read-side consumer of the storage contract
The service SHALL declare the `ticks` and `bars` `schema_version` it binds to, per the
`data-contract` capability's declared-read-side-consumer rule. It SHALL NOT own, create, or alter
either table's schema, and it SHALL NOT depend on schema details absent from its declared version.

#### Scenario: The service starts
- **WHEN** the historical-query service starts
- **THEN** the `schema_version` it binds to for `ticks` and `bars` is declared and observable, and
  no migration is applied by this service

#### Scenario: The declared version is unavailable
- **WHEN** the database the service connects to is not at, or has not reached, its declared
  `schema_version`
- **THEN** the service fails its readiness check rather than serving queries against an
  unrecognized schema

#### Scenario: The writing services add a column
- **WHEN** a persistence service applies an additive migration introducing a new column
- **THEN** the historical-query service continues serving its existing queries unchanged, with no
  coordinated redeploy required

#### Scenario: Contract conformance is verified
- **WHEN** the service's read-side contract test runs
- **THEN** it asserts every column and type its queries depend on is present in its declared
  `schema_version`, using the shared fixture and without a running persistence service
