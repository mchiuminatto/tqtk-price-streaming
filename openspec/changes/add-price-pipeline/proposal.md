## Why

We need a running, end-to-end real-time price pipeline: ingest tick prices, stream raw ticks to
external consumers, persist ticks, build live intrabar OHLCV aggregations across 8 timeframes,
serve and store those aggregations, all under a <10 ms internal latency budget with short-term
resiliency. Phase 1 builds and runs the whole pipeline against a single `synthetic` provider (a
configurable-rate Python tick generator); the real `dukascopy` provider is deferred to Phase 2 for
access reasons documented in the architecture note. The architecture and all runtime-semantics
decisions are settled (see `docs/Real-Time-Price-Pipeline-Architecture.md` and
`docs/Architecture-Open-Questions.md`); this change captures them as specs and an implementation
plan.

## What Changes

- **New monorepo layout**: `libs/tqtk-common` (shared package) + `services/*` (one deployable per
  service) + `deploy/` (Docker Compose now, minikube manifests next). One image per service.
- **Language-neutral data contract**: `Tick` and `Bar` schemas, Redis stream / key naming
  (`ticks.raw.{provider}.{symbol}`, `bars.{tf}.{provider}.{symbol}`,
  `bar_state:{provider}:{symbol}:{tf}`), provider tagging on every record, `seq` and dual
  timestamps (`provider_ts`, `recv_ts`).
- **feed-adapter-synthetic**: configurable-rate synthetic tick generation for the 13-symbol set
  (6 majors + 7 crosses — the symbols present in the `data/*.parquet` tick sample are the source of
  truth), provider tagging, `recv_ts` stamping, monotonic `seq`, publish raw ticks only.
- **aggregation-svc**: per-`(provider, symbol, timeframe)` actor model; every tick updates the
  forming bar on all 8 timeframes (1s..1D); `recv_ts` event-time bucketing; time-driven
  grace-delayed bar close via a wheel timer that posts close events into the in-process mailbox;
  gapless series (idle bars carry forward); crash recovery via per-actor checkpoint (O/H/L/C +
  last-consumed stream ID), checkpointed on every bar close and every ~1 s.
- **tick-persistence-svc / bar-persistence-svc**: batch-write to TimescaleDB hypertables; sole
  writers of their tables; bar-persistence idempotent (upsert on
  `(provider, symbol, timeframe, bar_start_ts)`).
- **streaming-gateway-svc**: WebSocket relay of live ticks and bar updates to LAN consumers,
  filterable by provider/symbol/timeframe; slow-consumer policy (conflate latest per key, then
  drop); one-shot bootstrap snapshot endpoint (`N-1` closed bars from historical-query + the 1
  forming bar held in memory, stitched server-side, reconciled on
  `(provider, symbol, timeframe, bar_start_ts)`).
- **historical-query-svc**: stateless REST over the `ticks` and `bars` tables (read-only).
- **bus-retention janitor**: consumer-position-driven `XTRIM` + hard-cap backstop (N ~ a few hours
  of volume, Critical alert) + dead-consumer eviction.
- **Platform resilience**: Redis with both AOF (`everysec`) and RDB; documented recovery-time
  objectives and failure-isolation behavior.
- **Observability**: per-service Prometheus `/metrics`, the `tick_to_bar_latency_ms` SLA histogram,
  consumer-lag and `late_ticks_total` metrics, Grafana dashboards + built-in alerting, structured
  JSON logs to stdout.
- **Service runtime contract**: every service exposes `/health`, `/ready`, `/metrics`; 12-factor
  config; structured logging — provided by `tqtk-common`.

Not in this change: the `dukascopy` (Java) adapter; synthetic-generator statistical fidelity
calibration (separate change `add-synthetic-feed-fidelity`, blocked on sample data); any
cross-provider consolidated view; LAN authentication/access control; centralized log aggregation.

All prior open design questions are now settled (see `docs/Architecture-Open-Questions.md`).

## Capabilities

### New Capabilities

- `data-contract`: language-neutral `Tick`/`Bar` schemas, stream and key naming conventions,
  provider tagging, `provider_ts`/`recv_ts` semantics, and `seq`/`session_id` semantics — `seq` is
  `uint64` monotonic and gapless per `(provider, symbol)`, resets to 0 each adapter session, and is
  paired with a per-session `session_id`; consumers do gap detection on `(session_id, seq)` and key
  dedup/reconciliation on `(provider, symbol, session_id, seq)`. The one authority both
  `tqtk-common` (Python) and the future Java adapter implement.
- `synthetic-feed`: configurable-rate synthetic tick generation, symbol set, provider tagging,
  `recv_ts`/`seq`/`session_id` stamping (new `session_id` and `seq` reset to 0 per process start),
  raw-tick publication.
- `bar-aggregation`: actor model and keying, per-tick intrabar updates across all 8 timeframes,
  `recv_ts` bucketing, grace-delayed time-driven bar close, idle-bar carry-forward, `Open`
  semantics, checkpoint and crash recovery.
- `tick-persistence`: durable batch persistence of raw ticks, sole-writer ownership, consumer-group
  scaling, buffering behavior during a database outage.
- `bar-persistence`: durable batch persistence of closed bars, idempotent upsert keyed on
  `(provider, symbol, timeframe, bar_start_ts)`, buffering during a database outage.
- `streaming-gateway`: WebSocket relay to LAN consumers, subscription filtering, slow-consumer
  policy, LAN-interface binding, plus a one-shot bootstrap snapshot endpoint that stitches the
  `N-1` most recent closed bars with the 1 forming bar (closed tail sourced from `historical-query`).
- `historical-query`: stateless REST read API over `ticks`/`bars`, filterable by provider.
- `bus-retention`: consumer-position-driven stream trimming, hard-cap backstop with alerting,
  dead-consumer eviction.
- `platform-resilience`: Redis durability configuration, recovery-time objectives, per-component
  failure-isolation behavior.
- `observability`: metrics catalog per service, the `tick_to_bar_latency_ms` SLA measurement,
  dashboards, alert rules, structured logging.
- `service-runtime`: the `/health`, `/ready`, `/metrics`, 12-factor config, and structured-logging
  contract every service satisfies via `tqtk-common`.

### Modified Capabilities

None — greenfield project, no existing specs.

## Impact

- **New code**: entire `libs/tqtk-common` package and all six `services/*` services; `deploy/`
  Docker Compose and minikube manifests.
- **New infrastructure**: Redis (bus + checkpoint store, AOF+RDB), PostgreSQL + TimescaleDB,
  Prometheus, Grafana.
- **Dependencies**: Python 3.x, a uv workspace, `redis-py`, an async web framework for
  health/metrics endpoints, `prometheus_client`, a Postgres driver, TimescaleDB extension.
- **APIs introduced**: WebSocket relay (streaming-gateway), REST (historical-query), Prometheus
  `/metrics` on every service.
- **No external-facing auth** at this stage — LAN trust only.
