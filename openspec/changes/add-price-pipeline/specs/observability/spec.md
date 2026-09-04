## Purpose

Gives every service continuous, low-overhead visibility into the internal latency SLA, consumer
health, and failures, without adding a network hop or blocking call to the tick-to-bar critical
path.

## ADDED Requirements

### Requirement: Per-service metrics endpoint
Every service SHALL expose a Prometheus-format `/metrics` endpoint, scraped on a pull basis,
independent of the critical path.

#### Scenario: Prometheus scrapes a service
- **WHEN** Prometheus scrapes a service's `/metrics` endpoint
- **THEN** the scrape succeeds without the service having made any network call on the tick
  processing path to prepare it

### Requirement: `tick_to_bar_latency_ms` SLA measurement
Aggregation SHALL record, for every bar update it publishes, the elapsed time between the
triggering tick's `recv_ts` and the publish, as a histogram labeled by `provider`, `symbol`, and
`timeframe`.

#### Scenario: A bar update is published
- **WHEN** aggregation publishes a bar update in response to a tick
- **THEN** the elapsed time since that tick's `recv_ts` is recorded into the
  `tick_to_bar_latency_ms` histogram, labeled by `provider`, `symbol`, and `timeframe`

### Requirement: Consumer-lag metric
Every consumer-group-based service SHALL expose a consumer-lag metric reflecting how far behind
the stream head its consumption position is.

#### Scenario: A consumer falls behind
- **WHEN** a consumer-group-based service's read position falls behind the stream head
- **THEN** its consumer-lag metric reflects the size of that gap

### Requirement: Late-tick metric
Aggregation SHALL expose `late_ticks_total`, incremented per `(provider, symbol, timeframe)` each
time a tick is dropped for arriving after its bar closed.

#### Scenario: A late tick is dropped
- **WHEN** aggregation drops a tick because its bar has already closed
- **THEN** `late_ticks_total{provider,symbol,timeframe}` is incremented

### Requirement: Structured logging without per-tick volume
Every service SHALL emit structured JSON logs to stdout. INFO level SHALL cover service
lifecycle, connection-state changes, checkpoint writes, and batch-persistence completions.
Per-tick detail SHALL NOT be logged at INFO level.

#### Scenario: A service processes a tick during normal operation
- **WHEN** a service processes an individual tick under normal operation
- **THEN** no INFO-level log line is emitted for that individual tick

### Requirement: Dashboards
Grafana dashboards SHALL cover: critical-path latency (`tick_to_bar_latency_ms` p50/p95/p99 per
`provider`/`symbol`/`timeframe` against the 10 ms budget), pipeline health (consumer lag and feed
connection status), and a per-service up/down failure-isolation view.

#### Scenario: SLA breach is visible
- **WHEN** `tick_to_bar_latency_ms` p99 exceeds the 10 ms budget
- **THEN** the critical-path latency dashboard shows the breach against its reference line

### Requirement: Alert rules
At minimum, the following conditions SHALL raise an alert: `tick_to_bar_latency_ms` p99 > 10 ms
sustained over 1 minute (Warning); unbounded consumer-lag growth on any stream (Critical);
`feed_connection_status == 0` for over 30 seconds (Warning); `aggregation-svc` `/health` failing
or unscraped (Critical).

#### Scenario: Aggregation-svc health check fails
- **WHEN** `aggregation-svc`'s `/health` endpoint fails or goes unscraped
- **THEN** a Critical alert fires

#### Scenario: Consumer lag grows without bound
- **WHEN** consumer lag on any stream grows without bound
- **THEN** a Critical alert fires
