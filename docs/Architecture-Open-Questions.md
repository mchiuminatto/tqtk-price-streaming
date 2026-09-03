# Architecture: Open Questions & Runtime-Semantics Gaps

Companion to [`Real-Time-Price-Pipeline-Architecture.md`](./Real-Time-Price-Pipeline-Architecture.md).
Exploration notes — none of these contradict the architecture; they sit *under* it.
Most are cases where the doc states one line but the reality is an unmade design decision.

Status: notes for later digging. Not decisions.

---

## Thread A — the intrabar firehose on long timeframes

"Every tick updates the currently-forming bar's O/H/L/C" is applied uniformly to all 8 timeframes.

```
1 tick on EURUSD  -->  update 1s, 1m, 5m, 15m, 30m, 1h, 4h, 1D  -->  8x XADD

At ~30 ticks/s/pair:
  bars.1D.synthetic.EURUSD  gets ~30 updates/sec
  ... of a bar that closes once per day
```

For a 1D or 4h bar, ~29 of every 30 updates change nothing a consumer cares about (occasionally a
new high/low). A LAN consumer subscribed to all TFs for 6 symbols takes ~1,440 bar msgs/sec, almost
all redundant long-TF churn.

Questions:
- Is uniform per-tick intrabar update the actual requirement, or is the requirement "the forming bar
  is never more than X stale"?
- Would per-timeframe update cadence work better — 1s/1m every tick, 1h+ coalesced to ~1/sec or
  on-O/H/L/C-change-only? `last_update_ts` is already in the Bar model, so consumers can tell.
- Does this belong in `aggregation-svc` (emit less) or `streaming-gateway-svc` (conflate per slow
  consumer)? Emitting less upstream also shrinks `bars.*` stream memory. `bar-persistence` only wants
  closed bars, so it's really a gateway/consumer concern.

Answer:

What: Stream every tick, no matter the timeframe.
Why: Because price feed consumers needs to take decisions on the latest price available. For a breakout strategy
you need to know if the latest price available is above a price level more than five pips.
Trade-off: Required accuracy is at the cost of higher traffic.

---

## Thread B — who closes a bar when ticks go quiet?

`aggregation-svc` consumes via blocking `XREADGROUP`. No tick -> no wakeup. But a 1m bar must close
at :00 whether or not a tick arrived at :00.

```
  09:00:59.980  tick  -> updates 09:00 bar
  09:01:00.000  <bar 09:00 should close, bar 09:01 should open>
  09:01:03.500  next tick arrives -> only NOW does svc wake up
                -> 09:00 bar closed 3.5s late; any consumer waiting on
                   bars.1m close event was blind for 3.5s
```

Synthetic at 30 ticks/s masks this — gaps are tiny. But the generator is meant to model real FX
microstructure, which has genuine quiet spells (rollover, thin sessions), and Phase 2 Dukascopy
definitely does. Needs a time-driven close path (wheel timer / periodic internal tick independent of
data flow) — worth stating explicitly rather than discovering it under real data.

Related: does an idle bar with zero ticks emit at all? (a "no trades this minute" bar with
O=H=L=C=last close, or a gap in the series?)

Answer:

**Execution model.** One actor per `(provider, symbol, timeframe)` — 48 actors for the synthetic
provider (6 symbols x 8 timeframes), 96 once Dukascopy is added. Each actor owns exactly one forming
bar, its own FIFO mailbox, and its own checkpoint. Cross-timeframe isolation: a heavy 4h/1D bar
close never sits in front of a 1s tick for the same symbol.

- A **router / dispatcher** reads each symbol's Redis stream (`ticks.raw.{provider}.{symbol}`) and
  fans every tick into that symbol's 8 in-process timeframe mailboxes. The router does dispatch only
  — no decode/compute beyond what routing needs — so it never becomes the bottleneck.
- A **wheel timer** posts a "close bar" event into each actor's **in-process mailbox** at the period
  boundary. The close event never goes into the Redis tick stream — that stream is a contract
  consumed by persistence and the gateway, and control messages would corrupt it.
- Each actor drains one merged, ordered sequence from its mailbox — `tick, tick, close, tick, ...` —
  and is the single writer to its own bar state. No lock.

Rationale for keeping the timer out of the data-flow path: opening the next bar must not block tick
reception, or the first tick's timestamp/price for the new bar could be delayed and skewed. The
socket reader only enqueues; all bar-state mutation happens inside the actor.

**Bucketing = event time (`recv_ts`).** `feed-adapter` stamps `recv_ts` once, monotonic per
`(provider, symbol)`. Each actor assigns a tick to a bar by `recv_ts`, not by arrival order into the
mailbox. Both `provider_ts` and `recv_ts` stay in the Tick record. (This also settles smaller-item
#3 — timestamp authority.)

**Closing = grace-delayed timer.** The wheel timer enqueues the close event for bar `[T, T+tf)` at
`T + tf + grace`, where `grace` is a small fixed delay (start at 50-250 ms, tune from the
`late_ticks_total` metric). This lets in-flight ticks with `recv_ts < T+tf` drain into the mailbox
before the bar is finalized. Bar-close latency is therefore `~= grace` — a separate number from the
<10 ms tick -> bar-publish budget, which is unaffected.

**Late ticks (recv_ts belongs to an already-closed bar).** Phase 1: **drop and increment
`late_ticks_total{provider,symbol,timeframe}`**. On a LAN with a single ordered feed-adapter and
sub-ms XADD this should be ~zero; the metric proves it. Phase 2 (real Dukascopy): revisit with a
proper watermark (`max(recv_ts seen) - epsilon`) instead of a fixed grace, and decide then whether
late ticks warrant bar revisions (re-emit with `is_closed = true` + a revision counter) or stay
drop-and-count.

**`Open` semantics (B2 — settled).** Split rule, matching standard market-data behavior:
- **Non-idle bar**: `Open` = the price of the first tick with `recv_ts` in `[T, T+tf)`. The closer
  may write a provisional `Open` when it opens the bar at `T`; the first real tick **overwrites**
  it. `High`/`Low`/`Close` then track normally.
- **Idle bar** (zero ticks in the period): `Open = High = Low = Close = previous bar's Close`,
  carried forward, `tick_count = 0`, `is_closed = true`. Series stays gapless.

One requirement statement: *"`Open` is the first tick with `recv_ts` in the bar window; if the
window has zero ticks, `Open` = the previous bar's `Close` (and `High = Low = Close = Open`)."*
Consequence: non-idle bars can gap away from the prior close (normal for FX around thin liquidity);
this is intended, not a bug.


---

## Thread C — Redis trimming vs. the resiliency promise

The doc says persistence services buffer "up to MAXLEN" while Postgres is down (target: seconds to a
few hours). But Redis Streams `MAXLEN` trims by length regardless of consumer-group progress — there
is no native "don't trim past the slowest consumer."

```
  Postgres down 2h.  bar-persistence not acking.
  Meanwhile aggregation-svc keeps XADD-ing at full rate.
  XADD ... MAXLEN ~ N   -->  oldest entries trimmed while still unacked
                        -->  silent gap in the bars table when PG recovers
```

"Resiliency for a few hours" implies one of:
1. MAXLEN sized for worst-case few-hours volume, and Redis memory budgeted for it. (The sizing table
   is framed as a *retention window*, not as an *outage buffer* — same numbers, different intent.)
2. A trim strategy driven by `min(consumer group positions)` rather than a fixed length.
3. Accept bounded data loss on long outages — and say so explicitly.

Which is the intent?

Answer:

**Primary: consumer-position-driven trim (Option 2).** A small janitor task periodically walks
every consumer group on each stream, finds the least-advanced position, and `XTRIM`s only up to that
ID minus a safety margin. Anything an active consumer has not yet read is never trimmed — this is
the correct fix for the silent-gap problem.

**Backstop A — hard cap + alert.** The janitor also enforces a ceiling:
`trim_to = max(min_consumer_position, stream_length - N)`, with `N` sized to the resiliency target
(a few hours of worst-case volume — ~500 MB/provider for ~6 h per the sizing table, comfortably
affordable). If a consumer lags past `N`, its unread entries *are* trimmed (that consumer takes a
data gap) so Redis memory stays bounded, and a **Critical alert** fires. This keeps one wedged
consumer from growing a stream until Redis OOMs and stalls the whole platform.

**Backstop C — dead-consumer eviction.** If a consumer group makes no progress for X minutes
(crashed, OOM-killed, bad deploy — not merely slow), the janitor forcibly drops that consumer group
so it stops holding back the trim point. On recovery that service cold-catches-up from Postgres or
accepts the gap. This handles a truly dead consumer automatically, before the hard cap even has to
engage.

Net blast radius of any single stuck/dead consumer: bounded to that consumer's own data gap, never
the platform. Alert fires in every case.



---

## Thread D — recovery idempotency

Checkpoint every ~1s to `bar_state:{provider}:{symbol}`. Crash -> lose up to 1s of state -> rebuild
by reprocessing ticks. But:

- `tick_count` on a Bar is **not** idempotent under reprocessing (O/H/L are — first/max/min; close
  re-settles fine).
- What does the checkpoint actually store? Bar O/H/L/C *and* the last-consumed stream ID? If only bar
  state, recovery can't know where to resume. If stream ID too, the consumer-group PEL (pending
  entries) and the checkpoint can disagree by up to 1s.
- Bars already `XADD`'d but not yet checkpointed get re-emitted on recovery — is `bars.{tf}`
  consumer-side dedup expected (keyed by `bar_start_ts` + `last_update_ts`)?

Not hard, but "checkpoint for crash recovery" is one line covering a fair amount of subtlety.


Answer:

**Checkpoint contents.** Per actor (`bar_state:{provider}:{symbol}:{timeframe}`), the checkpoint
stores the forming bar's `O/H/L/C` + `tick_count` + `bar_start_ts` **and** the last-consumed tick
stream ID. On restart, each actor loads its checkpoint and resumes `XREAD` from exactly that ID.

**Checkpoint cadence.** Two triggers:
1. **On every bar close** — immediately, before/with the closed-bar `XADD`. This makes closed bars
   exact: the checkpoint's stream ID always sits at or past the last tick folded into a published
   closed bar, so a crash can never force a closed bar to be recomputed from replayed ticks.
2. **Every ~1 s** for the still-forming bar, to bound how much intrabar progress is lost.

**What can still replay after a crash.** Only the *forming* bar's transient intrabar updates
(ticks since the last ~1 s checkpoint). `O/H/L` are idempotent under replay (first/max/min);
`tick_count` on the forming bar may be re-counted for those few ticks, which self-corrects as the
bar fills and is anyway superseded when the bar closes and re-checkpoints.

**Consumer-side dedup (belt-and-suspenders).** `bar-persistence-svc` (and any other `bars.*`
consumer that stores) keys on `(provider, symbol, timeframe, bar_start_ts)` and treats a repeat as
an upsert/overwrite, not an insert — so a duplicate closed-bar emission right after a crash never
produces a duplicate row or a doubled `tick_count`. Persistence is idempotent regardless of what
the producer does.

---

## Thread E — "synthetic must approximate Dukascopy fidelity" is a project inside the project

Decision #8 is load-bearing: synthetic is the *only* data source for an extended period, and Phase 1
conclusions have to survive contact with Phase 2 real data. But "approximate real tick-rate and
statistical properties" isn't pinned to anything testable.

Open questions:
- Which properties matter? Tick inter-arrival distribution, spread dynamics, volatility clustering,
  session/time-of-day effects, jump behavior, weekend gaps?
- Calibrated against what? There's no Dukascopy data in hand (that's the whole Phase 2 problem) — so
  is fidelity calibrated against public FX stylized facts / literature, or a one-time historical
  sample pulled another way?
- How do you *know* it's good enough — is there an acceptance check, or is it "looks plausible"?

Might deserve its own change proposal, separate from the pipeline.


Answer. 

I will provide, for each currency pair, a sample of one day of tick data so the properties can be derived
from that data.

---

## Thread F — Redis durability

Redis holds three distinct things: the bus, the checkpoints, and (per Thread C) the outage buffer.
"Redis down -> everything stalls" is acknowledged, but a Redis *restart* (not just downtime) with
default persistence loses checkpoints and unpersisted ticks/bars — which undercuts the
seconds-to-hours resiliency goal.

Is AOF (`everysec`) assumed? RDB only? One line to state, and it changes the recovery story
materially.


Answer: 

Both: AOF and RDB.


---

## Smaller things worth a sentence each

| # | Gap |
|---|---|
| 1 | **Repo/build structure** — SETTLED: monorepo + shared internal package (see below). |
| 2 | **Live/historical stitching** — SETTLED: one-shot bootstrap snapshot endpoint on `streaming-gateway-svc` composes `N-1` closed + 1 forming; consumer then subscribes for forming-bar updates (see below). |
| 3 | **Bucketing timestamp** — SETTLED in Thread B: bucket by `recv_ts` (event time); late ticks past `grace` are dropped + counted in Phase 1, watermark + possible bar revisions in Phase 2. |
| 4 | **`seq` scope & reset** — SETTLED: `uint64` monotonic per `(provider, symbol)`, starts at 0 each adapter session, paired with a `session_id`; gap detection on `(session_id, seq)` (see below). |

### #1 — Repo / build structure (settled)

**Monorepo with a shared internal package.**

```
tqtk-price-streaming/
|-- libs/
|   `-- tqtk-common/           installable pkg: Tick/Bar models, stream-name builders,
|       `-- tqtk_common/       redis consumer-group helpers, 12-factor config,
|                              health/ready/metrics server, Prometheus helpers,
|                              structured JSON logging
|-- services/
|   |-- feed-adapter-synthetic/   each: pyproject.toml (deps: tqtk-common),
|   |-- aggregation-svc/           Dockerfile, src/
|   |-- tick-persistence-svc/
|   |-- bar-persistence-svc/
|   |-- streaming-gateway-svc/
|   `-- historical-query-svc/
|-- deploy/
|   |-- docker-compose.yml
|   `-- k8s/                   minikube manifests
`-- pyproject.toml             uv workspace (or hatch / poetry)
```

- One clone; a contract change (e.g. a `Tick` field) is one atomic PR across lib + all consumers.
- Still builds **one image per service** — "independently deployable" is about deployment, not repo
  layout. Docker: multi-stage build with repo root as context, or build `tqtk-common` as a wheel and
  `pip install` it per service image.
- CI: path filters so only changed services rebuild.
- `tqtk-common` holds **boring infrastructure + the contract only** — no business logic. Aggregation
  logic leaking into the shared lib is a smell.

**Language-neutral contract.** `feed-adapter-dukascopy` is Java (Phase 2) and cannot import
`tqtk-common`. So `Tick`/`Bar` and the stream-name conventions must also exist as a
language-neutral definition (JSON Schema or `.proto` + a written spec). `tqtk-common` is the *Python
implementation* of that contract, not the contract itself. Captured as `specs/data-contract/spec.md`.

### #2 — Live/historical stitching (settled)

**One-shot bootstrap snapshot endpoint on `streaming-gateway-svc`; server-side stitch; no third
service.**

- On startup, a consumer calls a single read endpoint on `streaming-gateway-svc` (e.g.
  `GET /snapshot?provider=&symbol=&timeframe=&count=N`). It returns one ordered, gapless list:
  the `N-1` most recent **closed** bars followed by the **1 currently-forming** bar.
- The gateway composes the response. It already holds the forming bar in memory for that
  `(provider, symbol, timeframe)`; it fetches the closed tail from `historical-query-svc` (or a
  direct read-only `bars` query). The client does **no** stitching.
- After the snapshot, the consumer opens the normal `streaming-gateway-svc` subscription and
  receives live updates to the forming bar plus the roll events (forming bar closes, next bar
  opens). The snapshot is consumed once, at connect time; everything after is the live stream.
- `historical-query-svc` stays a pure REST-over-Postgres read API — **closed bars only**, unchanged.
  `streaming-gateway-svc` gains one read dependency on it, used solely for the snapshot's closed
  tail. The stateful component (the gateway, which owns the forming bar) owns the stitch.

**Continuity at the seam.** The newest closed bar in the snapshot and the forming bar must be
adjacent in time: `newest_closed.bar_start_ts + timeframe == forming.bar_start_ts`. If a bar closes
between the history read and the in-memory read, the gateway reconciles on
`(provider, symbol, timeframe, bar_start_ts)` — dedup, keep the closed version — before returning.

**Not in scope.** Snapshot for raw ticks (only bars); pagination / arbitrary historical ranges
(that's `historical-query-svc`'s REST API directly); resumable snapshots after a client
disconnect (client re-snapshots on reconnect).

### #4 — `seq` scope & reset (settled)

**`seq` is `uint64`, monotonic and gapless per `(provider, symbol)`, starting at 0 on each
adapter session. Every tick also carries a `session_id` for that stream. Gap detection is defined
on the pair `(session_id, seq)`.**

- **Scope — per `(provider, symbol)`.** Independent counter per symbol stream, consistent with the
  one-stream-per-`(provider,symbol)` layout and the per-`(provider,symbol)` monotonic `recv_ts`
  (Thread B). A consumer of `ticks.raw.{provider}.{symbol}` sees a clean `0, 1, 2, …`.
- **Reset — to 0 on every adapter (re)start.** No persisted counter, so a crash can never desync
  it. The counter lives only in adapter memory.
- **`session_id`** — assigned once per adapter session per `(provider, symbol)` (adapter boot UUID,
  or a monotonic boot counter persisted cheaply off the hot path). Included in every Tick record
  alongside `provider_ts` / `recv_ts` / `seq`.
- **Consumer gap-detection rule:**
  - Same `session_id`, `seq` jumped by more than 1 → **genuine tick loss**; count it / alert.
  - `session_id` changed → **adapter restarted**; expected discontinuity, *not* loss. The consumer
    re-syncs from Postgres (or the gateway snapshot) and resumes; it must not raise a loss alarm.
  - Same `session_id`, `seq` went backwards or repeated → duplicate delivery; dedup on
    `(provider, symbol, session_id, seq)`.

**Consequence.** `seq` is not globally continuous across the lifetime of a `(provider, symbol)` —
it is only dense *within* a session. Any consumer that persists or reconciles on sequence must key
on `(provider, symbol, session_id, seq)`, never `seq` alone. This is the deliberate trade for a
hot path with no persisted-counter write and unambiguous restart semantics.

**Phase 2 note.** Real Dukascopy may expose its own provider-side sequence or gap signal; if so,
carry it as a separate field and keep `(session_id, seq)` as the transport-level check.

---

## Status

| Thread | State |
|---|---|
| #1 repo/build structure | Settled: monorepo + `tqtk-common` shared package; one image per service; language-neutral data contract. |
| A — intrabar cadence | Settled: every tick updates the forming bar on all 8 timeframes. |
| B — bar close / bucketing / Open | Settled: per-`(provider,symbol,timeframe)` actor, wheel timer posts close events into the in-process mailbox, `recv_ts` event-time bucketing, grace-delayed close (50-250 ms), late ticks dropped + counted (Phase 1), idle bars carry-forward, non-idle `Open` = first tick in window. |
| C — stream trimming | Settled: consumer-position-driven trim + hard-cap backstop (N ~ few hours, Critical alert) + dead-consumer eviction. |
| D — recovery | Settled: checkpoint stores O/H/L/C + last-consumed stream ID per actor; checkpoint on every bar close plus every ~1 s; `bar-persistence` dedups on `(provider,symbol,timeframe,bar_start_ts)`. |
| E — synthetic fidelity | Approach settled: derive targets from a supplied 1-day tick sample per pair. Blocked on the sample files. Own change (`add-synthetic-feed-fidelity`). |
| F — Redis durability | Settled: AOF + RDB both on. |
| #2 live/historical stitching | Settled: one-shot bootstrap snapshot endpoint on `streaming-gateway-svc` (`N-1` closed + 1 forming, server-side stitch); consumer then subscribes for forming-bar updates. |
| #3 timestamp authority | Settled by Thread B (`recv_ts`). |
| #4 `seq` scope & reset | Settled: `uint64` monotonic per `(provider, symbol)`, resets to 0 each adapter session, paired with `session_id`; gap detection on `(session_id, seq)`; consumers key on `(provider, symbol, session_id, seq)`. |

All threads settled. Thread E's approach is agreed but blocked on the per-pair 1-day tick samples.

Ready to capture into OpenSpec: `add-price-pipeline` (A/B/C/D/F + smaller-items #2 and #4 + tick
schema) and `add-synthetic-feed-fidelity` (E). See [`Capturing-Answers-in-OpenSpec.md`](./Capturing-Answers-in-OpenSpec.md).
