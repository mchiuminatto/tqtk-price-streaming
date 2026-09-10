## 1. Monorepo & shared library scaffolding

- [x] 1.1 Create the monorepo layout (`libs/tqtk-common`, `services/*` for all six services,
      `deploy/`) and a root workspace config (e.g. `uv` workspace `pyproject.toml`); verify the
      workspace resolves (`uv sync` or equivalent succeeds).
- [x] 1.2 Add a per-service Dockerfile skeleton (multi-stage, `tqtk-common` installed as a
      dependency or built wheel) for all six services; verify each builds independently.
- [x] 1.3 Configure CI with per-service path filters; verify a change scoped to one service's
      directory triggers only that service's build/test job.

## 2. Data contract (`data-contract` spec)

- [x] 2.1 Write the language-neutral `Tick`/`Bar` schema (JSON Schema or `.proto`) outside
      `tqtk-common`, as the authority both Python and a future Java adapter implement, with `Bar`
      carrying `side` (`bid` | `ask`, closed enum); verify a sample `Tick` and a sample `Bar` of
      each side validate against it, and a third `side` value is rejected.
- [x] 2.2 Implement `Tick`/`Bar` Python models in `tqtk-common` conforming to the schema; verify a
      unit test round-trips serialize/deserialize without data loss.
- [x] 2.3 Implement stream/key-name builders (`ticks.raw.{provider}.{symbol}`,
      `bars.{tf}.{provider}.{symbol}`, `bar_state:{provider}:{symbol}:{tf}`) in `tqtk-common`;
      verify unit tests cover each naming pattern.
- [x] 2.4 Implement `seq`/`session_id` generation (per-`(provider,symbol)` counter reset to 0 per
      session, paired `session_id`) in `tqtk-common`; verify a unit test confirms reset-on-new-session
      and gapless monotonic increment within a session.
- [x] 2.5 Implement the consumer-side gap-detection/dedup helper keyed on
      `(provider, symbol, session_id, seq)` in `tqtk-common`; verify unit tests cover: an in-session
      gap (flagged), a session change (not flagged as loss), and a duplicate delivery (deduped).
- [x] 2.6 Write the versioned storage schema contract artifact for the `ticks` and `bars` tables
      (columns, types, indexes, `schema_version`), alongside the wire schema from task 2.1, with
      `bars` carrying `side` as a typed column and prices as `double precision` (no JSONB); verify
      a fixture database built from it matches the data model's documented indexes
      (`(provider, symbol, recv_ts)` and `(provider, symbol, side, timeframe, bar_start_ts)`).
- [x] 2.7 Implement the shared storage-contract test fixture in `tqtk-common` — builds a schema at a
      given `schema_version` from the contract artifact, for use by both writer and reader contract
      tests; verify it produces a schema a plain `SELECT` of every contracted column succeeds
      against.
- [x] 2.8 Add a CI check that fails on a destructive migration against `ticks` or `bars` (`DROP`
      COLUMN, `RENAME`, or type-narrowing) within a released `schema_version` lineage; verify it
      passes on an added nullable column and fails on a dropped one.

## 3. Service runtime contract (`service-runtime` spec)

- [x] 3.1 Implement `/health` and `/ready` endpoints in `tqtk-common`'s shared server, with
      `/ready` reflecting dependency-connection state distinct from process liveness; verify a
      service using it responds on `/health` immediately and on `/ready` only once dependencies
      (e.g. Redis) are connected.
- [x] 3.2 Implement the `/metrics` Prometheus-endpoint scaffolding in `tqtk-common`; verify
      `/metrics` returns valid Prometheus text format with no custom metrics registered yet.
- [x] 3.3 Implement a 12-factor (env-var driven) config loader in `tqtk-common`; verify a unit
      test confirms a changed environment variable changes the loaded config with no code change.
- [x] 3.4 Implement structured JSON logging with INFO/DEBUG level separation (lifecycle,
      connection, checkpoint, batch-completion at INFO; per-tick detail at DEBUG only) in
      `tqtk-common`; verify a sample log line parses as JSON and no per-tick line appears at INFO.

## 4. Platform components

- [x] 4.1 Add Redis to Docker Compose with AOF (`everysec`) and RDB both enabled; verify
      `redis-cli CONFIG GET appendonly` and `CONFIG GET save` reflect both enabled after startup.
- [x] 4.2 Add PostgreSQL + TimescaleDB to Docker Compose — server and `timescaledb` extension
      only, with no application tables (each persistence service creates its own on startup, per
      tasks 7.1 and 8.1); verify the extension is available via `\dx` and that `ticks`/`bars` do not
      exist before any service has started.
- [x] 4.3 Add Prometheus to Docker Compose, configured to scrape every service's `/metrics`;
      verify the Prometheus targets page shows all services as up once running.
- [ ] 4.4 Add Grafana to Docker Compose wired to the Prometheus datasource; verify the Grafana UI
      loads and the datasource connection test succeeds.

## 5. feed-adapter-synthetic (`synthetic-feed` spec)

- [ ] 5.1 Implement configurable-rate tick generation for the 13-symbol set (sourced from
      `data/*.parquet` file names); verify a test run publishes ticks only for the configured
      symbols and no others.
- [ ] 5.2 Implement `provider="synthetic"` tagging, `recv_ts` stamping (monotonic per
      `(provider,symbol)`), and `seq`/`session_id` assignment per the data contract; verify
      `recv_ts` is non-decreasing across consecutive ticks per symbol, and a restart produces a
      new `session_id` with `seq` reset to 0.
- [ ] 5.3 Wire tick rate to 12-factor config; verify changing the configured rate changes the
      observed publish rate with no code change.
- [ ] 5.4 Verify the service exposes `/health`, `/ready`, `/metrics` per the service-runtime
      contract.

## 6. aggregation-svc (`bar-aggregation` spec)

- [ ] 6.1 Implement the router/dispatcher that reads each symbol's raw-tick stream and fans every
      tick into that symbol's 8 in-process timeframe mailboxes; verify a test tick reaches all 8
      mailboxes for its symbol.
- [ ] 6.2 Implement the per-`(provider,symbol,timeframe)` actor with `recv_ts`-based event-time
      bucketing, holding both side-bars (the tick's `bid` folding into the `bid` bar, its `ask`
      into the `ask` bar); verify a unit test correctly buckets ticks delivered out of `recv_ts`
      order, and that one tick updates both sides with a single shared `tick_count`.
- [ ] 6.3 Implement the wheel timer posting grace-delayed close events (`T+tf+grace`) into each
      actor's mailbox; verify a test confirms a bar with no boundary tick still closes at
      `T+tf+grace`, and an in-flight tick with `recv_ts < T+tf` arriving before `T+tf+grace` is
      still folded in.
- [ ] 6.4 Implement `Open` semantics per side — the first in-window tick's price *for that side*
      for non-idle bars; `Open=High=Low=Close=` that side's previous `Close` with `tick_count=0`
      for idle bars; verify unit tests for both cases on both sides.
- [ ] 6.5 Implement late-tick drop-and-count (`late_ticks_total{provider,symbol,timeframe}`);
      verify a tick arriving after its bar's close increments the metric and is not folded into
      any bar.
- [ ] 6.6 Implement checkpoint writes (both sides' `O/H/L/C` + the shared `tick_count` +
      `bar_start_ts` + last-consumed stream ID, under the unchanged
      `bar_state:{provider}:{symbol}:{tf}` key) on every bar close and at least every ~1 s while
      forming; verify the checkpoint after a close carries a stream ID at or past the last tick
      folded into that bar, and recovers both sides.
- [ ] 6.7 Implement crash recovery — resume `XREAD` from the checkpointed stream ID on restart;
      verify an integration test that kills and restarts the service mid-stream does not
      reprocess already-checkpointed closed bars.
- [ ] 6.8 Publish bar updates (intrabar and closed) to `bars.{tf}.{provider}.{symbol}` per the
      `Bar` schema, emitting a window's `bid` and `ask` records in a single atomic bus operation;
      verify a consumer receives well-formed records for both update types on both sides, and that
      the pair lands with adjacent stream IDs with no publish visible between them.
- [ ] 6.9 Instrument the `tick_to_bar_latency_ms` histogram at publish time, labeled by
      `provider`/`symbol`/`timeframe`; verify it is observable on `/metrics` and its measured p95
      stays under the 10 ms budget in a local load test.

## 7. tick-persistence-svc (`tick-persistence` spec)

- [ ] 7.1 Implement ownership of the `ticks` table schema: migrations living with this service,
      applied on startup under a Postgres advisory lock before consuming, declaring the
      `schema_version` produced; verify a start against an empty database creates the table and its
      indexes, a start against an up-to-date database applies nothing, and two instances starting
      concurrently migrate exactly once.
- [ ] 7.2 Verify the writer-side storage contract test: the schema the migrations produce matches
      the declared `schema_version` in the contract artifact (task 2.6), using the shared fixture.
- [ ] 7.3 Implement consumer-group-based batch consumption of `ticks.raw.*` across all providers,
      writing to the `ticks` table as sole writer; verify a published tick appears as a row with
      `provider`/`symbol`/timestamps intact.
- [ ] 7.4 Verify horizontal scaling: run two instances against the same streams and confirm each
      tick is persisted exactly once (no duplicate or missing rows).
- [ ] 7.5 Verify outage buffering: stop Postgres, publish ticks, restart Postgres, and confirm
      every tick published during the outage is eventually persisted.

## 8. bar-persistence-svc (`bar-persistence` spec)

- [ ] 8.1 Implement ownership of the `bars` table schema: migrations living with this service,
      applied on startup under a Postgres advisory lock before consuming, declaring the
      `schema_version` produced; verify a start against an empty database creates the table and its
      indexes, a start against an up-to-date database applies nothing, and two instances starting
      concurrently migrate exactly once.
- [ ] 8.2 Verify the writer-side storage contract test: the schema the migrations produce matches
      the declared `schema_version` in the contract artifact (task 2.6), using the shared fixture.
- [ ] 8.3 Implement consumer-group-based batch consumption of `bars.*.*` filtered to
      `is_closed=true`, writing to the `bars` table as sole writer; verify an intrabar update
      never produces a row.
- [ ] 8.4 Implement idempotent upsert keyed on
      `(provider, symbol, side, timeframe, bar_start_ts)`, writing a window's two side-rows in one
      transaction; verify replaying the same closed bar twice results in exactly one row with an
      unchanged `tick_count`, that the two sides occupy distinct rows, and that a failure partway
      through commits neither.
- [ ] 8.5 Verify outage buffering for closed bars, matching task 7.5's pattern.

## 9. streaming-gateway-svc (`streaming-gateway` spec)

- [ ] 9.1 Implement the WebSocket relay of live ticks/bars with `provider`/`symbol`/`timeframe`
      subscription filtering plus an optional `side` filter; verify a subscribed client receives
      only matching updates, and that omitting `side` delivers both sides.
- [ ] 9.2 Implement the slow-consumer policy (conflate-latest-per-key on
      `(provider, symbol, side, timeframe)`, then drop beyond a backlog bound); verify a simulated
      slow client's queue stays bounded, it receives the latest value per key rather than every
      intermediate update, and conflation never drops one side in favour of the other.
- [ ] 9.3 Bind to a LAN-reachable interface (configurable, not localhost-only); verify a
      connection from another host (or network namespace) on the test LAN succeeds.
- [ ] 9.4 Implement `GET /snapshot` with `side` required (`N-1` closed bars from
      `historical-query-svc` + 1 in-memory forming bar, stitched and reconciled on
      `(provider,symbol,side,timeframe,bar_start_ts)`); verify a snapshot response is ordered,
      gapless, single-sided, its newest closed bar time-adjacent to the forming bar, and that a
      request omitting `side` is rejected.
- [ ] 9.5 Verify the snapshot-then-subscribe flow: request a snapshot, then open a subscription,
      and confirm no duplicate or missing update for the bar in progress at connect time.
- [ ] 9.6 Verify no authentication is required to connect or subscribe, per Phase-1 scope.

## 10. historical-query-svc (`historical-query` spec)

- [ ] 10.1 Implement read-only REST endpoints over `ticks`/`bars`, filterable by `provider`,
      `symbol`, `timeframe`, time range, and — for bars — an optional `side`; verify (via code
      review or an integration test) that no write query ever executes, and that omitting `side`
      returns both sides.
- [ ] 10.2 Verify bar endpoints return only `is_closed=true` rows.
- [ ] 10.3 Verify stateless scaling: run two instances behind a load balancer and confirm
      identical responses to the same query regardless of which instance served a prior request.
- [ ] 10.4 Declare the `ticks`/`bars` `schema_version` this service binds to as observable config,
      and apply no migrations from this service; verify startup against a database below the
      declared version fails `/ready` rather than serving queries.
- [ ] 10.5 Verify the read-side storage contract test: every column and type the service's queries
      depend on is present in its declared `schema_version`, run against the shared fixture (task
      2.7) with no persistence service running.
- [ ] 10.6 Verify additive-migration tolerance: apply a migration adding a nullable column to
      `ticks`/`bars` and confirm the service continues serving its existing queries with no
      redeploy.

## 11. bus-retention janitor (`bus-retention` spec)

- [ ] 11.1 Implement consumer-position-driven `XTRIM` (trim to least-advanced consumer-group
      position minus a safety margin); verify a test with multiple consumer groups trims only up
      to the slowest group's position.
- [ ] 11.2 Implement the hard-cap backstop
      (`trim_to = max(min_consumer_position, stream_length - N)`) with a Critical alert on
      engagement; verify a simulated consumer lagging past `N` is trimmed past and an alert fires.
- [ ] 11.3 Implement dead-consumer eviction (no progress for the configured window → evict);
      verify a simulated stalled consumer group is evicted and the trim point advances past it.

## 12. Observability (`observability` spec)

- [ ] 12.1 Instrument the full per-service metrics catalog (`ticks_published_total`,
      `publish_latency_ms`, `feed_connection_status`, `feed_reconnects_total`, `consumer_lag`,
      `checkpoint_duration_ms`, `aggregation_errors_total`, `batch_write_duration_ms`,
      `batch_size`, `write_errors_total`, `connected_clients`, `messages_relayed_total`,
      `slow_consumer_drops_total`, `request_duration_ms`, `request_errors_total`); verify each
      metric appears on its owning service's `/metrics`.
- [ ] 12.2 Build the three Grafana dashboards (critical-path latency, pipeline health,
      failure-isolation view); verify each renders against live data from a running Compose
      stack.
- [ ] 12.3 Configure Grafana's built-in alert rules (latency p99 breach — Warning; unbounded
      consumer lag — Critical; feed disconnect >30s — Warning; aggregation-svc health failure —
      Critical); verify each rule fires under a simulated trigger condition.

## 13. End-to-end integration & deployment

- [ ] 13.1 Bring up the full Docker Compose stack (all six services + Redis + Postgres/Timescale
      + Prometheus + Grafana); verify every service reports healthy on `/health` and `/ready`.
- [ ] 13.2 Run an end-to-end smoke test — synthetic ticks flow through to persisted ticks,
      persisted closed bars, and a live `streaming-gateway` subscription — within the
      `tick_to_bar_latency_ms` budget; verify via the Grafana dashboard and a direct WebSocket
      client check.
- [ ] 13.3 Verify failure-isolation behavior against the `platform-resilience` spec: stop
      `aggregation-svc` and confirm tick ingestion/persistence continue while bar production
      stops; restart it and confirm recovery via checkpoint.
- [ ] 13.4 Document Compose bring-up/teardown steps (`deploy/README` or equivalent); no rollback
      procedure needed beyond standard teardown, per `design.md`'s Migration Plan.
