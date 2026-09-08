# Real-Time Price Pipeline: Microservice Architecture (Multi-Provider)

## Objective (as given)
- Consume tick price from a price provider — from multiple price providers concurrently
- Stream raw ticks as-is for external consumers
- Store raw ticks in a database
- Build price aggregations in real time (1s, 1m, 5m, 15m, 30m, 1h, 4h, 1D) — **live intrabar updates, not just bar-close events**
- Make aggregations available to external consumers
- Store aggregations for historical data
- Low latency: <10ms for aggregated price
- Short-term resiliency
- Modeled as an independently-deployable microservice architecture
- Every tick and bar tagged with the provider it came from

## Decisions (resolved)

1. **Providers, phased.** `synthetic` (a configurable-rate Python tick generator) is the only **active** provider — build and run the whole pipeline against it now. `dukascopy` (real market data) is **deferred to Phase 2**: Dukascopy's JForex-API is officially **Java-only** (no Python bindings), and the only officially-supported language-agnostic path — Dukascopy's FIX 4.4 API — requires a **$100,000 minimum deposit** or institutional "External Service Provider" approval, which isn't available now. Revisit when ready to build the Java/JForex component (or if FIX API eligibility changes).
2. **Symbol set**: 13 pairs — the symbols present in the delivered tick-sample data (`data/*.parquet`) are the source of truth. 6 majors (EURUSD, GBPUSD, AUDUSD, USDCAD, USDJPY, USDCHF) + 7 crosses (AUDJPY, EURGBP, EURJPY, GBPJPY, NZDJPY, NZDUSD, USDCNH) — for whichever provider is active (synthetic now; Dukascopy when added).
3. **No cross-provider consolidated view.** Confirmed — per-provider tagging only, no blending, even once a second provider is active. Averaging "real" vs. "synthetic" (or two real providers with different microstructure) would be meaningless; any future consolidated view is a distinct, explicitly-labeled derived service, not a default behavior.
4. **External consumers**: same LAN, separate host from the pipeline itself.
5. **Aggregated-price SLA = live intrabar updates.** Every tick updates the currently-forming bar's O/H/L/C, not just the bar-close event.
6. **Deployment**: Docker Compose now, Kubernetes via minikube as the next target.
7. **Phase 1 scope**: the architecture stays multi-provider-ready (provider tagging, dynamic stream discovery, per-(provider,symbol) keys) end-to-end, but only the `synthetic` provider is built and deployed today. Adding `dukascopy` later is a new `feed-adapter-dukascopy` deployment — no redesign of `aggregation-svc` or anything downstream.
8. **Synthetic generator fidelity**: the generator must approximate Dukascopy's real tick-rate and statistical properties, not just produce a naive random walk — it's the sole active data source for an extended period, and Phase 1 results need to be meaningfully comparable once Phase 2 (real data) arrives.
9. **LAN gateway auth**: LAN-only network trust is sufficient for this stage — no authentication/access-control layer required yet. Revisit if the trust boundary changes (e.g., more hosts join the LAN).
10. **aggregation-svc scaling**: confirmed — starts as a single shared instance across the active provider(s) and all 13 symbols.
11. **Dukascopy (Phase 2) approach**: will be implemented as a Java component exposing prices over a **WebSocket** interface, built later. Implementation detail deferred until Phase 2 starts: whether a thin bridge republishes the WebSocket feed into Redis (preserving the existing `ticks.raw.dukascopy.{symbol}` contract) or downstream services consume the WebSocket directly.

## Scope and assumptions

1. **10ms budget = internal processing latency** (tick received → aggregate published on the bus), not wall-clock to a remote consumer.
2. **Feed ingestion and aggregation are separate services.** This was a deliberate single-process design originally (lower latency); it's split now for two independent reasons: (a) provider isolation — Dukascopy, when it's built, will very likely be Java, and Python components shouldn't need to embed or link against it; (b) one shared, provider-agnostic aggregation implementation rather than duplicating aggregation logic per provider. The latency cost is small relative to the 10ms budget (quantified below), so this holds even in Phase 1 with a single Python-only provider — it avoids a second re-architecture when Dukascopy is added.
3. **Phase 1 = synthetic only.** Real Dukascopy ingestion is deferred pending the Java/JForex build (or a change in FIX API eligibility). The pipeline is designed, built, and run end-to-end against `synthetic` data until then.
4. **"Short-term resiliency"** = recover from crashes/disconnects in seconds to at most a few hours.
5. **Streaming bus: Redis Streams. Storage: PostgreSQL + TimescaleDB.**

## Service catalog

| Service | Responsibility | Data owned | Exposes / Publishes | Consumes | Scaling |
|---|---|---|---|---|---|
| **feed-adapter-synthetic** | Generate configurable-rate synthetic ticks; tag `provider="synthetic"`; publish raw ticks only | None | Redis stream `ticks.raw.synthetic.{symbol}` | Internal generator, no external dependency | One instance |
| **feed-adapter-dukascopy** *(Phase 2 — planned, not built)* | Connect to Dukascopy (JForex, Java); normalize + tag `provider="dukascopy"`; publish raw ticks only | None | Redis stream `ticks.raw.dukascopy.{symbol}` | Dukascopy feed (external) | One instance (single provider session) |
| **aggregation-svc** | Discover and consume raw-tick streams across **all active** providers; maintain live OHLCV bar state per `(provider, symbol, timeframe)` on both price sides (`bid`/`ask`); checkpoint for crash recovery | None persistent (in-memory + Redis checkpoint) | Redis streams `bars.{tf}.{provider}.{symbol}` (both sides, `side` in the record); checkpoint key `bar_state:{provider}:{symbol}:{tf}` | Redis stream `ticks.raw.*` (all providers, dynamically discovered) | Single shared instance to start; shardable by symbol or provider later if measurement shows it's needed |
| **tick-persistence-svc** | Discover and consume raw-tick streams across all providers, batch-write durably | `ticks` table — sole writer of both its rows and its DDL/migrations | — | Redis streams `ticks.raw.*` | Horizontal via consumer groups |
| **bar-persistence-svc** | Discover and consume closed-bar streams across all providers, batch-write durably | `bars` table — sole writer of both its rows and its DDL/migrations | — | Redis streams `bars.*.*`, closed only | Horizontal via consumer groups |
| **streaming-gateway-svc** | Relay live ticks/bars to LAN consumers over WebSocket, filterable by provider/symbol/timeframe and optionally side; slow-consumer policy | None | WebSocket API | Redis streams (all providers) | Horizontal; sticky routing beyond 1 instance (not yet) |
| **historical-query-svc** | Stateless REST for historical queries/backfill, filterable by provider and, for bars, side | None — declared read-side consumer bound to a storage `schema_version` | REST API | PostgreSQL (read-only) | Horizontal, stateless |
| **edge-gateway** *(not in scope — deferred with decision #9)* | Would be a single LAN-facing entrypoint (TLS, routing, rate limiting) if the trust boundary ever changes; not built now | None | HTTPS / WSS | streaming-gateway-svc, historical-query-svc | Horizontal, stateless |

Platform components: Redis (bus + checkpoint store), PostgreSQL + TimescaleDB (system of record — the server and extension only; the `ticks` and `bars` tables are created by their owning services' migrations, not by platform bootstrap).

## High-level architecture

```
      Dukascopy (Phase 2 — planned,        Synthetic Generator (ACTIVE,
      Java/JForex, not yet built)          internal dev/testing source)
             |  (not wired in yet)                v
             v                          ======================
   - - - - - - - - - - - - - -          feed-adapter-synthetic [Python]
   feed-adapter-dukascopy [Java]        critical path — ACTIVE
   PLANNED / NOT BUILT                    1. generate tick (configurable rate)
     1. connect (JForex API)               2. tag provider=synthetic
     2. tag provider=dukascopy              3. XADD ticks.raw.synthetic.*
     3. XADD ticks.raw.dukascopy.*        ======================
   - - - - - - - - - - - - - -                     |
             : (planned)                            |
              \                                     v
               \.......................> +
                                          |
         Redis  [PLATFORM: bus + checkpoint store]  <---+
         ticks.raw.{provider}.{symbol}                  |
         bars.{tf}.{provider}.{symbol}                  | XADD bars.*
         bar_state:{provider}:{symbol}                  | (per provider,symbol,tf)
             |                                           |
             | XREADGROUP ticks.raw.* (active providers) |
             v                                           |
         aggregation-svc  [SERVICE, shared]  -------------+
         critical path (2-hop: ~1-3ms typical)
           1. XREADGROUP ticks.raw.* (dynamic discovery)
           2. update bar state (provider,symbol,tf) -- 1s..1D, parallel
           3. XADD bars.{tf}.{provider}.{symbol}  (live intrabar + closed)
           4. ~1s checkpoint -> bar_state:{provider}:{symbol}

           [ back to Redis, which also branches to: ]
                 /                  |                    \
                v                   v                      v
   tick-persistence-svc    bar-persistence-svc      streaming-gateway-svc
   owns: ticks table        owns: bars table          WS relay -> LAN
             \                    /                            |
              v                  v                              |
          PostgreSQL + TimescaleDB  [PLATFORM: system of record] |
                        |                                        |
                        | read-only, cross-service                |
                        v                                        |
              historical-query-svc  [SERVICE]                    |
                        \                                        |
                         v                                       v
                        edge-gateway  [deferred — decision #9; not in Phase 1]
                                     |
                                     v
                    External Consumers (same LAN, separate host)
```

## Why feed-adapter and aggregation-svc stay split

The previous version of this design combined feed handling and aggregation into one process to avoid a network hop on the <10ms critical path — correct *given* both were in the same language/process. That assumption held for exactly as long as Dukascopy looked like it might be built in Python. It isn't: JForex-API is officially Java-only, with no free Python-compatible alternative. So the split stays, for a forward-looking reason rather than a today reason:

**Today**, with only `synthetic` (Python) active, a combined single-process design would technically be possible and would save the ~1-2ms of network-hop latency. **But** Dukascopy's eventual return is very likely a Java component again — nothing about deferring it changes that constraint. Keeping `feed-adapter-{provider}` and `aggregation-svc` separate now means:

1. Adding `feed-adapter-dukascopy` later is purely additive — a new Java service that publishes to a Redis stream `aggregation-svc` already knows how to discover. No rework of aggregation logic, no re-architecture.
2. Aggregation logic exists in exactly one place, in one language, regardless of how many providers (or what languages their adapters use) exist.
3. The latency cost is small relative to budget either way (quantified below) — paying it now to avoid paying a redesign cost later is the better trade at this stage.

**The honest trade-off, unchanged from before**: `aggregation-svc` is a single point of failure for bar production across all active providers. Today that only means synthetic bars stop if it crashes (since Dukascopy isn't active yet) — but the failure-isolation profile widens back out once a second provider is added. Starting as one shared instance is still the right call at this scale; shard later if monitoring says so.

## Data model

```
Tick:  { provider, symbol, provider_symbol?, provider_ts, recv_ts, bid, ask,
         bid_size?, ask_size?, session_id, seq }
Bar:   { provider, symbol, side, timeframe, bar_start_ts, open, high, low, close,
         tick_count, is_closed, last_update_ts }        side = "bid" | "ask"
```

Redis streams: `ticks.raw.{provider}.{symbol}`, `bars.{timeframe}.{provider}.{symbol}`. Postgres/TimescaleDB: `ticks` hypertable indexed on `(provider, symbol, ts)`; `bars` hypertable indexed on `(provider, symbol, side, timeframe, bar_start_ts)`, prices as typed `double precision` columns.

**The bid/ask side dimension.** A bar is a bid series or an ask series — there is no single "the price" for an FX window — so every window produces **two** records, `side = "bid"` and `side = "ask"`, built from that side's price on each contributing tick, with identical `tick_count`. `side` is a field of the record, deliberately **not** a segment of the stream name: bar streams carry both sides (stream count stays 104, and the trim policy and checkpoint keys are untouched), while the aggregation actor stays keyed `(provider, symbol, timeframe)` and holds both side-bars, publishing the pair in one atomic bus operation. There is no `mid` side — consumers derive it. The split doubles LAN bar-message volume to **~6,240 msgs/sec** and persisted bar rows to **~27/sec (~2.3M/day)**; ticks are unaffected, since a `Tick` already carried both sides.

### Two contract surfaces, both versioned

The `Tick`/`Bar` records above are the **wire contract** — defined language-neutrally in JSON Schema (draft 2020-12, under `contracts/wire/`) so a future Java adapter conforms without importing the Python package. JSON Schema rather than `.proto` because the bus carries Redis field maps, not a binary protocol: protobuf's codegen would buy little and would add `protoc` to both the Python and the Java build. The `ticks`/`bars` table schema is a **second contract surface**, and gets the same treatment rather than being left as whatever DDL happens to have been applied:

- **Versioned artifact.** The table schema carries an explicit `schema_version` and lives beside the wire schema, not only in the database.
- **DDL ownership follows sole-writer ownership.** `tick-persistence-svc` owns the `ticks` schema; `bar-persistence-svc` owns `bars`. Each applies its own migrations on startup, under a Postgres advisory lock so concurrent instances migrate exactly once. Nothing is created by platform bootstrap or by an out-of-band manual step.
- **Additive-only evolution.** Within a released version lineage: no `DROP`, no `RENAME`, no type-narrowing; new columns are nullable or defaulted. Enforced by a CI check, not by review discipline.
- **Declared read-side consumers.** `historical-query-svc` reads tables it does not own — a CQRS read model, which is a legitimate pattern. It binds to a named `schema_version` and fails its readiness check rather than serving queries against an unrecognized schema.
- **Contract tests on both sides.** The writer asserts it produces the contracted schema; the reader asserts every column and type its queries touch is present in the version it declares. Both run against a shared fixture built from the contract artifact, so the reader's test needs no running persistence service.

**Why this matters:** without it, a column rename in `tick-persistence-svc` silently breaks `historical-query-svc` at runtime, with no test to catch it and no safe deploy order between them — the shared-database coupling that would otherwise disqualify `historical-query-svc` as an independently deployable service. **The alternative rejected** was having the persistence services serve historical queries themselves: it puts read load on the write path this design works to keep clear, and adds a hop to every historical query for no isolation the versioned contract doesn't already give.

`seq` is `uint64`, monotonic and gapless **per `(provider, symbol)`**, and **resets to 0 on every feed-adapter (re)start**. `session_id` identifies one adapter session for that stream, so consumers do gap detection on `(session_id, seq)` — a `seq` jump within a session is real tick loss; a `session_id` change is an expected restart, not loss. Dedup/reconciliation keys on `(provider, symbol, session_id, seq)`. See [`Architecture-Open-Questions.md`](./Architecture-Open-Questions.md) §#4.

## Latency budget (internal, critical path — unchanged by provider count)

| Stage | Order of magnitude |
|---|---|
| feed-adapter: parse/normalize/tag tick | ~0.05–0.2 ms |
| feed-adapter: publish to Redis (`XADD`) | ~0.1–0.5 ms |
| aggregation-svc: notice new entry (`XREADGROUP`, blocking) | ~0.1–1 ms (least certain figure — benchmark once built) |
| aggregation-svc: update bar state, keyed by (provider,symbol,tf) | ~0.01–0.05 ms |
| aggregation-svc: publish bar update (`XADD`) | ~0.1–0.5 ms |
| **Total** | **roughly 1–3 ms typical** — comfortably under 10ms. Applies the same whether one provider or several are active; instrument end-to-end (`recv_ts` at feed-adapter → bar-publish timestamp at aggregation-svc) from day one rather than trusting this estimate. |

This budget covers tick-received → bar-published-to-Redis. Delivery from streaming-gateway-svc to a LAN consumer is a separate hop again (below).

## LAN consumer delivery

External consumers are same-LAN, separate host — not localhost, not open internet. Consequence: streaming-gateway-svc and historical-query-svc (or edge-gateway, if used) need to bind to a LAN-reachable interface rather than `127.0.0.1`. Per decision #9, LAN network trust is sufficient at this stage — no authentication or access-control layer (API key, LAN-scoped auth) is added now; revisit if the trust boundary changes. LAN round-trip latency is typically sub-millisecond to a few ms depending on network quality — small compared to the internal budget, but it's a genuinely separate number from the internal <10ms figure, not part of it.

## Redis memory sizing (concrete, Phase 1: 13 symbols × 1 active provider)

13 `(provider, symbol)` combinations (`synthetic` only, for now). At a representative ~30 ticks/sec/pair (combined bid/ask, retail-feed order of magnitude) × 13 ≈ 390 ticks/sec total, ~100–150 bytes/stream-entry:

| Retention window | Approx. memory |
|---|---|
| 4–6 hours | roughly 700MB–1.1GB |
| 24 hours | roughly 4.3–5.4GB |

This **roughly doubles** once `dukascopy` is added as a second active provider (26 combinations: ~1.4–2.2GB for 4–6h, ~8.7–10.8GB for 24h) — worth budgeting Redis memory for that from the start even though it isn't needed yet. The synthetic provider's rate is fully controllable — dial it down for routine testing, or up to stress-test the aggregation-svc hop specifically.

## Failure isolation (blast radius per service, Phase 1)

| Component down | Blast radius |
|---|---|
| feed-adapter-synthetic | Only synthetic ticks stop — currently the *only* active feed, so this stops all live ingestion until it recovers. (Once `dukascopy` is added, this row reverts to isolated-per-provider, as previously designed.) |
| **aggregation-svc** | **No new bars** — raw tick capture and persistence continue independently, but live/historical bar production stops until it recovers |
| streaming-gateway-svc | LAN consumers lose the live feed; ingestion, aggregation, persistence, DB unaffected |
| historical-query-svc | REST/backfill unavailable; live pipeline unaffected |
| tick-persistence-svc | Tick durability paused, buffered up to MAXLEN; live pipeline unaffected |
| bar-persistence-svc | Bar durability paused, same buffering; live pipeline unaffected |
| edge-gateway | LAN access blocked at the edge; internal pipeline keeps running |
| Redis | Everything stalls — shared platform dependency |
| Postgres | Writes buffer in Redis; historical-query-svc degrades; live pipeline unaffected |
| Prometheus / Grafana | No dashboards, metrics history, or alerts — you're flying blind until it's back, but the live pipeline itself is unaffected (metrics are pulled, not pushed, so a scraper outage can't block a service) |

Note the honest cost of Phase 1 being single-provider: `feed-adapter-synthetic` going down currently takes out *all* live data, not just one provider's slice — a natural, temporary consequence of there being only one active feed, not a design flaw.

## Observability

### Design principle: instrument the critical path without joining it

The <10ms budget rules out anything that adds a network hop or blocking call inside the tick→bar path. That rules out synchronous distributed-tracing span export on every tick, and it rules out logging every tick at INFO. Instead: `recv_ts` is already carried in the Tick payload (data model above), so per-hop latency is computed locally — `now() - recv_ts` — and recorded into an **in-process** Prometheus histogram, which is an in-memory counter increment, not a network call. Prometheus then scrapes each service's `/metrics` endpoint on its own schedule, fully decoupled from the critical path. This replaces the "least certain figure — benchmark once built" caveats in the latency budget table above with real, continuous p50/p95/p99 numbers.

### Metrics (Prometheus, pull-based via `/metrics` — already planned per service in the deployment model)

| Service | Key metrics |
|---|---|
| **feed-adapter-{provider}** | `ticks_published_total` (counter, by provider/symbol); `publish_latency_ms` (histogram, recv_ts → XADD complete); `feed_connection_status` (gauge, 0/1); `feed_reconnects_total` (counter) |
| **aggregation-svc** | **`tick_to_bar_latency_ms`** (histogram, `now() − recv_ts` at bar XADD — the direct <10ms SLA measurement, by provider/symbol/timeframe; no `side` label, since a window's two side-records publish in one operation); `ticks_consumed_total`; `consumer_lag` (gauge, from stream length vs. last-delivered ID); `checkpoint_duration_ms`; `aggregation_errors_total` |
| **tick-persistence-svc / bar-persistence-svc** | `batch_write_duration_ms` (histogram); `batch_size` (histogram); `write_errors_total`; `consumer_lag` |
| **streaming-gateway-svc** | `connected_clients` (gauge); `messages_relayed_total`; `slow_consumer_drops_total` |
| **historical-query-svc** | `request_duration_ms` (histogram); `request_errors_total` |

**Redis/Postgres internals** (memory, stream lengths, table/hypertable sizes) are checked manually via `redis-cli` / `psql` for now rather than dashboarded — `redis_exporter`/`postgres_exporter` are purely additive later if that stops being enough.

**Correlation**: `(provider, symbol, session_id, seq)` — already in the Tick schema — is the correlation key across logs and metrics when tracing one specific bad event by hand, without needing a tracing backend.

### Logs (structured, not centralized)

JSON-structured logs. INFO level for service lifecycle, connection state changes, checkpoint writes, and batch-persistence completions — not per-tick (that volume would itself become a latency/resource risk). Tick-level detail is available at DEBUG, off by default. No centralized log aggregation for now — logs go to stdout, captured automatically by Docker/Kubernetes, and read with `docker compose logs -f <service>` (or `kubectl logs` later). Add Loki only if this stops being enough — e.g. once searching across services or retaining logs longer than local rotation allows actually matters.

### Dashboards (Grafana)

1. **Critical path latency** — `tick_to_bar_latency_ms` p50/p95/p99 per (provider, symbol, timeframe), with a reference line at the 10ms budget.
2. **Pipeline health** — consumer lag across all Redis-consuming services, feed connection status per provider.
3. **Failure isolation view** — per-service up/down, using Prometheus's own scrape-target `up` metric (no extra infrastructure needed) — mirrors the failure-isolation table above.

### Alerting (Grafana's built-in alerting — no separate Alertmanager container)

Grafana 8+ ships a built-in alerting engine that evaluates rules directly against its Prometheus datasource; a single-instance setup doesn't need a standalone Alertmanager to get real alerting. Rules fire and show in the Grafana UI regardless of whether a notification channel (email/webhook) is configured — enough to notice on your own check-ins; wiring a notification channel is a config change any time, not an architecture one.

| Condition | Severity | Rationale |
|---|---|---|
| `tick_to_bar_latency_ms` p99 > 10ms, sustained >1min | Warning | Direct SLA breach signal |
| Consumer lag growing unbounded on any stream | Critical | Indicates a consumer (often `aggregation-svc`, the documented SPOF) is falling behind or down |
| `feed_connection_status == 0` for >30s | Warning | Feed adapter disconnected from its provider |
| `aggregation-svc` `/health` failing or unscraped | Critical | Matches its documented single-point-of-failure status |

### Why not full distributed tracing (Jaeger/Tempo/OpenTelemetry spans)

Worth addressing directly since it's the obvious alternative. The pipeline is shallow — 2–3 hops from tick to bar to persistence/gateway — so the diagnostic value distributed tracing adds (visualizing complex multi-hop call graphs) is low, while span creation and export, even batched, adds CPU and GC pressure directly in the hot path this design has otherwise kept clear of network calls. Embedding timestamps in the message payload and computing latency via local histograms gets most of the same diagnostic value — where time is being spent, per stage — without that cost. If the pipeline grows more hops or more complex fan-out later, OpenTelemetry can be layered in incrementally per-service without changing the data model, since `provider_ts`/`recv_ts` are already there to anchor spans to.

### Observability platform components

Deliberately minimal — 2 containers, not 6. This is the low-complexity option: real dashboards and real alerting for the one metric that matters (`tick_to_bar_latency_ms`), without deploying anything the project doesn't yet need.

| Component | Role | Scaling |
|---|---|---|
| **Prometheus** | Scrapes `/metrics` from every service; stores time series | Single instance at this scale |
| **Grafana** | Dashboards over Prometheus, plus built-in alerting (no separate Alertmanager) | Single instance |

**Deferred, purely additive later**: `redis_exporter`/`postgres_exporter` (if Redis/Postgres internals need dashboarding), Loki + Promtail (if logs need centralizing/searching across services), Alertmanager (if alert routing outgrows Grafana's built-in engine — e.g. multiple notification channels with routing rules). None of these require restructuring anything already built — they're additions, not migrations.

## Deployment model

- **feed-adapter-synthetic**: Python, own Dockerfile, configurable tick-rate and symbol set via env/config.
- **aggregation-svc, tick-persistence-svc, bar-persistence-svc, streaming-gateway-svc, historical-query-svc**: Python, own Dockerfile each, own 12-factor config, own `/health`, `/ready`, `/metrics`. The two persistence services additionally carry their own table's migrations and apply them on startup before consuming; `historical-query-svc` applies none and declares the `schema_version` it binds to.
- **feed-adapter-dukascopy (Phase 2, not built yet)**: Java, own Dockerfile (JRE base image + built JAR), own config (JForex credentials, symbol map) via secret/config, not baked into the image. Needs its own build pipeline (Maven/Gradle), separate from the Python services' — set this up as a distinct CI job only once Phase 2 actually starts, so it isn't sitting idle in CI today.
- **Now (single host)**: Docker Compose — six Python services + `redis` + `postgres` (TimescaleDB image), optionally `edge-gateway`, plus `prometheus` and `grafana`. No Java in Compose yet.
- **Next: minikube (local Kubernetes)**. Since minikube is single-node, most services run as 1-replica Deployments to start — this is a good exercise for writing real K8s manifests (Deployment + ClusterIP Service + ConfigMap/Secret per service) without yet needing multi-node concerns like `streaming-gateway-svc` sticky routing. Prometheus and Grafana map to their own lightweight Helm charts (`prometheus-community/prometheus`, `grafana/grafana`) rather than the full `kube-prometheus-stack` bundle, keeping only what's actually deployed.
- **CI/CD**: independent per service; adding `dukascopy` later is a new feed-adapter deployment (plus its own Java build pipeline), not a change to `aggregation-svc` or anything downstream.

## Open items

- **Dukascopy WebSocket bridge design** (Phase 2, non-blocking): whether the Java component's WebSocket feed is republished into Redis by a thin bridge (keeping today's `ticks.raw.dukascopy.{symbol}` contract intact) or consumed directly by downstream services — to be decided when Phase 2 work starts.
