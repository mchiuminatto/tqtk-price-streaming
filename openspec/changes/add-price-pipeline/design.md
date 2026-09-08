## Context

Greenfield project — no existing system, no migration. Phase 1 builds and runs the whole pipeline
end-to-end against a single `synthetic` provider under a <10 ms internal (tick-received to
bar-published) latency budget and a short-term (seconds-to-hours) resiliency target, while staying
structurally ready for a second, Java-based provider (`dukascopy`, Phase 2) without a redesign.
See `proposal.md` for full motivation and scope.

## Goals / Non-Goals

**Goals:**
- Implement the eleven capabilities in `specs/` as a working, deployable pipeline.
- Keep the tick-to-bar critical path measurably under the 10 ms budget.
- Keep the architecture provider-count-agnostic even though only one provider is active.

**Non-Goals:**
- Building `feed-adapter-dukascopy` (Java) — Phase 2, a purely additive deployment later.
- Calibrating synthetic-feed statistical fidelity against the sample data — separate change
  `add-synthetic-feed-fidelity`.
- Any cross-provider consolidated view, LAN authentication, or centralized log aggregation.

## Decisions

### Repo layout: monorepo + shared package, still one image per service
A monorepo (`libs/tqtk-common` + `services/*`) makes a `Tick`/`Bar` contract change one atomic PR
across the library and every consumer, instead of a coordinated multi-repo rollout. **Alternative
rejected**: one repo per service — correct isolation, but every contract change becomes a
multi-repo, multi-PR coordination problem for a project where the contract is still settling.
Deployment independence is preserved separately: each service still builds its own image
(multi-stage build from repo root, or `tqtk-common` built as a wheel and installed per image), and
CI path-filters so only changed services rebuild.

### Data contract defined language-neutrally, `tqtk-common` is one implementation of it
`feed-adapter-dukascopy` will be Java and cannot import a Python package. Defining `Tick`/`Bar`
and the stream-naming convention as a language-neutral spec (not just Python types) means the
Java adapter conforms to the same contract without depending on `tqtk-common`. **Alternative
rejected**: let the Python types be the de facto contract — works until Phase 2, then forces a
retrofit onto a component that was never designed against a neutral spec.

### Persisted schema is a versioned contract, not an implicit shared surface
`historical-query-svc` reads the `ticks` and `bars` tables that `tick-persistence-svc` and
`bar-persistence-svc` own — a CQRS read model, which is a legitimate pattern, but one that was
coupling three services to a schema none of them declared. The schema is therefore promoted to the
same treatment the wire contract already gets: a versioned artifact with an explicit
`schema_version`, DDL ownership following sole-writer ownership (each persistence service applies
its own migrations on startup, rather than platform bootstrap creating both tables), additive-only
evolution, and contract tests on both the writing and reading sides against a shared fixture.
That keeps the read model while making the coupling explicit and independently deployable — a
column added by a writer can no longer silently break the reader, and the two need no coordinated
release. **Alternative rejected**: have the persistence services serve historical queries
themselves, removing the shared table entirely — it puts read load on the write path this design
works to keep clear, and adds a network hop to every historical query for no isolation benefit the
versioned contract doesn't already provide. **Alternative rejected**: leave the schema implicit and
rely on review discipline — the rule that isn't enforced is the one that breaks during the first
refactor under time pressure, which is why the additive-only rule gets a CI check rather than a
paragraph.

### Bid/ask carried as two side-discriminated `Bar` records
A bar is a bid series or an ask series; there is no single "the price" for an FX window. `Bar`
therefore carries `side` (`bid` | `ask`), and every window produces two records identified by
`(provider, symbol, side, timeframe, bar_start_ts)` — the tuple persistence upserts on and the
gateway reconciles on. `tick_count` is identical on the pair, since one tick carries both sides.
The `Tick` surface is unchanged: it already carries `bid` and `ask`, and is the source both bars
are folded from. **Alternative rejected**: one wide record with `bid_open`..`ask_close` — halves
message count, but stops `side` being a subscription and query dimension and forces every
consumer, including charting clients that expect one OHLC per record, to project. **Alternative
rejected**: a third `mid` side — derivable by consumers, and an enum member added later is a
runtime surprise for anything that switches on `side`, so the enum is closed at two.

### `side` lives in the record, not in the stream name or the actor key
Streams stay `bars.{tf}.{provider}.{symbol}` and actors stay keyed
`(provider, symbol, timeframe)`. **Alternative rejected**: `bars.{tf}.{provider}.{symbol}.{side}` —
doubles stream count (104 → 208) and the janitor's trim bookkeeping to serve a filtering need only
external clients have, and they are already filtered at the gateway subscription, not on the raw
stream. **Alternative rejected**: `side` in the actor key — doubles actors and wheel-timer entries
for no isolation, since both sides derive from the same tick and close on the same boundary. One
actor holding both sides also keeps `bar_state:{provider}:{symbol}:{tf}` unchanged, with two O/H/L/C
sets in the checkpoint payload, and lets the pair be published in one atomic bus operation so no
consumer sees a window with one side advanced past the other. That guarantee stops at emission:
a batched read can still split the pair, so consumers pair on the bar's identity rather than
assuming co-delivery.

### Bar sides stored as two typed rows, not one row with a JSONB price document
`bars` gains a `side` column, its key and index become
`(provider, symbol, side, timeframe, bar_start_ts)`, and prices stay `double precision` columns.
**Alternative rejected**: one row per window holding `{bid: {...}, ask: {...}}` as JSONB. It moves
the schema out of `information_schema`, where the whole versioned-storage-contract decision above
just put it — the shared fixture has no columns to `SELECT`, and the additive-only CI check cannot
see a key renamed inside a document, because that is an application-code edit rather than DDL. It
also forfeits Timescale's type-aware compression (Gorilla on float columns, delta-delta on
timestamps and counters, dictionary on the low-cardinality `provider`/`symbol`/`side`/`timeframe`
columns) on a table taking ~2.3M rows/day, in exchange for repeating the JSON key names in every
row. The usual argument for JSONB — that adding a column is slow or political — is one this design
already answered: additive migration owned by the sole writer, verified in CI. **Alternative
rejected**: one row with eight typed price columns — keeps every enforcement property and halves
row count, but the storage shape then stops mirroring the wire shape, adding a fold in
`bar-persistence` and its inverse in `historical-query` for a row rate (~27/s) that is not a
pressure. JSONB stays the right tool for a later `provider_meta` column: sparse, per-provider, and
never filtered on.

### `feed-adapter-*` and `aggregation-svc` stay separate processes
A combined single process would save ~1-2 ms of network-hop latency, comfortably possible today
with only a Python provider active. **Rejected** because Dukascopy's eventual adapter is
Java — combining now would just force a re-split later. Keeping them separate today means adding
`feed-adapter-dukascopy` is purely additive: a new service publishing to a stream
`aggregation-svc` already knows how to discover, with aggregation logic staying in exactly one
place regardless of how many provider languages exist.

### Actor model: one actor per `(provider, symbol, timeframe)`, router does dispatch only
A router reads each symbol's raw-tick stream and fans every tick into 8 in-process timeframe
mailboxes; a wheel timer posts close events into the same mailbox rather than the Redis tick
stream. **Alternative rejected**: a single loop handling all timeframes per tick — simpler, but
couples a heavy 4h/1D bar close to 1s-timeframe tick latency for the same symbol, which the actor
model's cross-timeframe isolation avoids by construction. **Alternative rejected**: encode bar-close
as a special record in the tick stream — would corrupt a stream contract that persistence and the
gateway also consume; the in-process mailbox keeps control messages off the wire entirely.

### Bucketing by `recv_ts` (event time), not arrival order
`recv_ts` is stamped once by the feed adapter and is monotonic per `(provider, symbol)`, so it
reflects true tick timing independent of downstream processing jitter. **Alternative rejected**:
bucket by mailbox arrival order — simpler, but misplaces ticks whenever processing introduces any
reordering or delay, which a grace-delayed close is specifically designed to tolerate.

### Grace-delayed close (50-250 ms, tunable) instead of an immediate boundary close
Closing exactly at `T+tf` would drop any tick still in flight when the boundary is crossed.
Delaying the close event to `T+tf+grace` absorbs that jitter. This adds a close-latency number
separate from the <10 ms tick-to-bar budget; `late_ticks_total` is the tuning signal — start at
the low end and increase only if the metric shows real loss.

### Late ticks: drop-and-count now, defer watermarking to Phase 2
A proper watermark (`max(recv_ts seen) - epsilon`) is the eventual right answer, but is premature
complexity against a single ordered synthetic feed where the late-tick rate should be ~zero — the
metric proves whether that assumption holds. **Deferred, not rejected**: revisit with a real
watermark, and decide on bar revisions vs. drop-and-count, once Dukascopy's less orderly delivery
is active in Phase 2.

### Idle bars carry forward rather than leaving a gap
`Open=High=Low=Close=` previous close, `tick_count=0`, keeps every bar series gapless.
**Alternative rejected**: skip emission for zero-tick windows — simpler for the producer, but
pushes gap-detection and gap-filling onto every downstream consumer (charting, backtesting)
instead of solving it once, centrally.

### Checkpoint on every bar close *and* every ~1 s while forming
The on-close trigger is what makes closed bars exact: the checkpoint's stream ID always sits at
or past the last tick folded into a bar already published as closed, so a crash can never force a
closed bar to be recomputed. **Alternative rejected**: fixed-interval checkpointing only — a crash
immediately after a close could then replay ticks into a bar already emitted as closed, undermining
the immutability `bar-persistence`'s idempotent upsert assumes. The ~1 s interval bounds how much
*forming*-bar progress (which is safely re-derivable — O/H/L are idempotent under replay) is lost.

### Bus retention: consumer-position-driven trim + hard cap + dead-consumer eviction
A flat `MAXLEN` (the original framing) trims by length regardless of consumer-group progress,
silently dropping data an active-but-slow consumer hasn't read yet. The janitor instead trims to
the least-advanced consumer position, so nothing unread is lost under normal operation.
**Two backstops, not one**: the hard cap (`stream_length - N`) bounds Redis memory even if a
consumer is simply slow, firing a Critical alert when it engages; dead-consumer eviction handles
the different failure mode of a consumer that has *stopped* entirely (crashed, OOM-killed), so it
can't block the trim point indefinitely waiting for a recovery that isn't coming.

### Redis durability: AOF (`everysec`) *and* RDB, not either alone
RDB alone loses everything since the last snapshot on a restart; AOF alone carries higher
steady-state write overhead for no restore-speed benefit. Together: AOF bounds loss to ~1 s,
RDB gives a fast full-restore path — the pair matches the "seconds to a few hours" resiliency
target at a cost (disk I/O, dual persistence) that's affordable at this data volume.

### Symbol set sourced from the sample data, not a hardcoded list
The synthetic feed's per-symbol fidelity calibration (a separate change) needs a sample for every
symbol it generates. Tying the deployed symbol set to `data/*.parquet` means the two can never
drift apart — adding a symbol is "add its sample file," not a second config edit that can go out
of sync. **Alternative rejected**: a hardcoded symbol list (the original "6 majors" decision) —
correct until someone adds a symbol in one place and forgets the other.

### Bootstrap snapshot stitched server-side on `streaming-gateway-svc`
The gateway already holds the forming bar in memory for every `(provider, symbol, timeframe)` it
serves, so it's the only component that can produce a consistent seam without an extra round
trip. **Alternative rejected**: a dedicated stitching service — would have to duplicate the
gateway's in-memory forming-bar state or add a network hop to fetch it. **Alternative rejected**:
client-side stitching — pushes `bar_start_ts` reconciliation logic onto every consumer instead of
solving it once.

### `seq`/`session_id`: session-scoped reset, not a persisted global counter
A persisted counter needs a durable write on the tick hot path (latency cost) and reconciliation
logic after a crash. Resetting `seq` to 0 per adapter session and pairing it with a `session_id`
gets the same gap-detection power — a `seq` jump within a session is real loss, a `session_id`
change is an expected restart — without ever writing the counter to disk. **Trade-off accepted**:
`seq` is only dense *within* a session, not globally continuous; consumers must key on
`(provider, symbol, session_id, seq)`, documented in the `data-contract` spec.

### Observability: in-process histograms + pull-based Prometheus, not distributed tracing
The pipeline is shallow (2-3 hops, tick to bar to persistence/gateway), so tracing's main value —
visualizing complex multi-hop call graphs — is low, while span export adds CPU/GC pressure
directly in the hot path this design otherwise keeps free of network calls. `recv_ts`, already in
the Tick payload, lets every hop compute its own latency locally and record it into an in-process
histogram scraped independently by Prometheus — most of the diagnostic value, none of the
critical-path cost. OpenTelemetry can be layered in later per-service if the pipeline grows more
hops; `provider_ts`/`recv_ts` are already there to anchor spans to.

### Alerting via Grafana's built-in engine, not a standalone Alertmanager
Grafana 8+ evaluates rules directly against its Prometheus datasource — real alerting without a
second container. Deferred, purely additive later: a standalone Alertmanager if alert routing
outgrows the built-in engine (e.g. multiple notification channels with routing rules).

## Risks / Trade-offs

- **Aggregation-svc is a single point of failure for bar production across all active
  providers.** → Mitigation: `/health`-based Critical alert plus fast restart via per-actor
  checkpoint recovery; acceptable at Phase-1 scale (documented in `platform-resilience`), shard by
  symbol/provider later only if monitoring shows the need.
- **Redis is a single shared dependency for the bus, checkpoints, and outage buffer — its outage
  stalls the entire pipeline.** → Mitigation: AOF `everysec` + RDB bounds restart data loss to
  ~1 s; accepted and documented rather than engineered around at this scale.
- **Grace-delayed close adds a close-latency number separate from the tick-to-bar budget.** →
  Mitigation: tunable from `late_ticks_total`; start low (50 ms) and raise only if the metric
  shows real late-tick volume.
- **Drop-and-count late-tick handling could silently lose data if the feed becomes less orderly
  (Phase 2 Dukascopy).** → Mitigation: explicitly flagged for revisit with a proper watermark
  before Phase 2 goes live; the metric already exists to detect the condition, so it can't go
  unnoticed in the meantime.
- **The hard-cap backstop trims data a slow-but-alive consumer hasn't read.** → Mitigation: only
  engages past `N` (sized to the resiliency target) and always raises a Critical alert — the loss
  is bounded to that one consumer and never silent.
- **13 symbols (vs. the originally-estimated 6) roughly doubles LAN bar-message volume, and the
  bid/ask split doubles it again to ~6,240 msgs/sec, with the bar share of Redis memory (total
  4.3-5.4 GB/24h before the split) doubling alongside it.** → Mitigation: the streaming-gateway
  slow-consumer conflation policy already absorbs the redundant long-timeframe churn driving that
  volume, and now conflates per side (see `bar-aggregation`/`streaming-gateway` specs); sizing is
  budgeted up front rather than discovered under load. Tick volume and the `ticks` table are
  unaffected — a tick already carried both sides.
- **The bid/ask split doubles persisted bar rows.** Closed bars only:
  `1 + 1/60 + 1/300 + 1/900 + 1/1800 + 1/3600 + 1/14400 + 1/86400 ≈ 1.022` closes/sec per
  (symbol, side), × 13 symbols × 2 sides ≈ **~27 rows/sec ≈ 2.3M rows/day**. → Mitigation: typed
  columns keep Timescale's per-column compression available (the reason the JSONB alternative was
  rejected above); a compression/retention policy is not yet specified and is the lever if storage
  growth becomes the binding constraint.
- **The monorepo couples all six services' source history to one clone.** → Mitigation: each
  service still builds and deploys as an independent image; CI path-filtering keeps unrelated
  services from rebuilding on an unrelated change.
- **Migrations applied on service startup can race when a persistence service scales to more than
  one instance** (both consumer-group scaling requirements permit this). → Mitigation: migrations
  run under a Postgres advisory lock, so exactly one instance migrates and the rest wait and then
  proceed; the additive-only rule bounds the blast radius of a migration running while an older
  instance is still serving.

## Migration Plan

Greenfield — no existing system or data to migrate, no rollback beyond standard Compose teardown.
Build order (each stage independently testable against its spec before the next depends on it):

1. `libs/tqtk-common` — contract types, stream-name builders, consumer-group helpers,
   health/ready/metrics server, structured logging (`data-contract`, `service-runtime`).
2. Platform components up via Docker Compose: Redis (AOF+RDB), PostgreSQL+TimescaleDB
   (server and extension only — no application tables; each persistence service creates its own),
   Prometheus, Grafana (`platform-resilience`).
3. `feed-adapter-synthetic` (`synthetic-feed`) — verify raw ticks land on
   `ticks.raw.synthetic.{symbol}` for all 13 symbols.
4. `aggregation-svc` (`bar-aggregation`) — actor model, router, wheel timer, checkpointing;
   verify `tick_to_bar_latency_ms` stays under budget.
5. `tick-persistence-svc` / `bar-persistence-svc` — each applies its own table's migrations on
   startup; verify sole-writer ownership (of rows and of DDL), storage-contract conformance, and
   `bar-persistence`'s idempotent upsert under a simulated duplicate delivery.
6. `streaming-gateway-svc` (relay + snapshot) and `historical-query-svc` — the latter binding to
   a declared storage `schema_version`, with its read-side contract test green.
7. Bus-retention janitor — verify trim behavior under a simulated slow/dead consumer.
8. Observability wired last, once the services it measures exist: metrics, dashboards, alert
   rules.

## Open Questions

- **Dukascopy WebSocket bridge design** (thin Redis-republishing bridge vs. downstream services
  consuming the WebSocket directly) — explicitly deferred to when Phase 2 work starts; doesn't
  change this change's specs, approach, or tasks.
- **Exact UTC hour ranges for the four session labels** (Asia Pacific, Asia, London, New York) —
  belongs to `add-synthetic-feed-fidelity`, not this change.
