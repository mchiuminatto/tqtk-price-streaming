## Purpose

Defines the common runtime contract every service in the pipeline satisfies — health, readiness,
and metrics endpoints, externalized configuration, and structured logging — so each service
doesn't reimplement infrastructure concerns independently.

## ADDED Requirements

### Requirement: Health endpoint
Every service SHALL expose a `/health` endpoint reporting whether its process is alive.

#### Scenario: Process is running
- **WHEN** a service's process is running and its `/health` endpoint is queried
- **THEN** it responds indicating the process is alive

### Requirement: Readiness endpoint
Every service SHALL expose a `/ready` endpoint reporting whether it is ready to serve traffic or
consume work, distinct from liveness.

#### Scenario: Service has not finished connecting to dependencies
- **WHEN** a service has started but has not yet finished connecting to its required
  dependencies (e.g. Redis)
- **THEN** `/ready` reports not-ready while `/health` still reports the process alive

### Requirement: Metrics endpoint
Every service SHALL expose a Prometheus-format `/metrics` endpoint (content defined by the
observability capability).

#### Scenario: Metrics endpoint is queried
- **WHEN** `/metrics` is queried on any service
- **THEN** it returns Prometheus-format metric output

### Requirement: Externalized configuration
Every service's configuration SHALL be supplied via environment variables or external config,
following 12-factor conventions, with no code change required to alter it.

#### Scenario: A configuration value is changed
- **WHEN** an operator changes a service's environment/config value and restarts it
- **THEN** the new value takes effect without any source code modification

### Requirement: Structured logging
Every service SHALL emit structured JSON logs to stdout.

#### Scenario: A service logs an event
- **WHEN** a service emits a log line
- **THEN** the line is structured JSON written to stdout
