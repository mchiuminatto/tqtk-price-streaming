## Purpose

Relays live ticks and bar updates to LAN consumers over WebSocket, and provides a one-shot
bootstrap snapshot so a newly connecting consumer can reconstruct recent bar history without a
separate historical query.

## ADDED Requirements

### Requirement: WebSocket relay of live updates
The gateway SHALL relay published ticks and bar updates (including intrabar updates) to
subscribed LAN consumers over WebSocket.

#### Scenario: A tick or bar update is published
- **WHEN** a tick or bar update is published on the bus
- **THEN** every subscribed consumer whose filter matches the update's `provider`/`symbol`/
  `side`/`timeframe` receives it over its WebSocket connection

### Requirement: Subscription filtering
A consumer SHALL be able to subscribe with a filter on `provider`, `symbol`, `timeframe`, and
optionally `side`, and SHALL receive only messages matching that filter. Omitting `side` SHALL
deliver both sides rather than defaulting to one.

#### Scenario: Consumer subscribes with a filter
- **WHEN** a consumer subscribes specifying `provider`, `symbol`, and `timeframe`
- **THEN** it receives only updates matching that `(provider, symbol, timeframe)` combination, on
  both sides

#### Scenario: Consumer subscribes to one side
- **WHEN** a consumer subscribes specifying `side = bid`
- **THEN** it receives only `bid` bar updates for its `(provider, symbol, timeframe)` filter

### Requirement: Slow-consumer policy
When a consumer falls behind, the gateway SHALL conflate queued updates to the latest value per
`(provider, symbol, side, timeframe)` key, and SHALL drop further backlog beyond a bound rather
than growing the queue unboundedly or blocking the fast path. `side` SHALL be part of the
conflation key, so conflation never collapses a window's two sides into one another.

#### Scenario: A consumer falls behind
- **WHEN** a consumer's outbound queue cannot keep up with the publish rate
- **THEN** queued updates for the same key are conflated to the latest value, and updates beyond
  the configured backlog bound are dropped for that consumer rather than delaying other consumers

#### Scenario: Both sides are queued for one window
- **WHEN** a slow consumer has both the `bid` and `ask` update for one bar window queued
- **THEN** conflation keeps the latest update for each side, never discarding one side in favour
  of the other

### Requirement: LAN-reachable binding
The gateway SHALL bind to a LAN-reachable network interface, not only to localhost.

#### Scenario: Gateway starts
- **WHEN** the gateway starts
- **THEN** it accepts connections from other hosts on the same LAN, not only from `127.0.0.1`

### Requirement: Bootstrap snapshot endpoint
The gateway SHALL expose a read endpoint that, given `provider`, `symbol`, `side`, `timeframe`,
and `count = N`, returns one ordered, gapless list: the `N-1` most recent closed bars followed by
the 1 currently-forming bar. `side` SHALL be required, since a snapshot is one series: a consumer
wanting both sides requests two snapshots.

#### Scenario: Consumer requests a snapshot
- **WHEN** a consumer requests a snapshot for `(provider, symbol, side, timeframe, count=N)`
- **THEN** it receives that side's `N-1` most recent closed bars followed by its 1
  currently-forming bar, in ascending time order

#### Scenario: Consumer omits the side
- **WHEN** a snapshot is requested without a `side`
- **THEN** the request is rejected rather than served with an implicit side or with two
  interleaved series

### Requirement: Snapshot seam continuity
The newest closed bar in a snapshot response and the forming bar SHALL be time-adjacent:
`newest_closed.bar_start_ts + timeframe == forming.bar_start_ts`. If a bar closes between the
historical read and the in-memory read during snapshot composition, the gateway SHALL reconcile
on `(provider, symbol, side, timeframe, bar_start_ts)`, keeping the closed version.

#### Scenario: A bar closes mid-snapshot
- **WHEN** a bar closes between the gateway's historical-tail read and its in-memory forming-bar
  read while composing a snapshot
- **THEN** the response is reconciled on `(provider, symbol, side, timeframe, bar_start_ts)` so it
  contains no gap and no duplicate, keeping the closed version

### Requirement: Snapshot is a one-shot read
A snapshot response SHALL reflect state at the time of the request only. Live updates after the
snapshot SHALL be delivered solely through the consumer's separately-opened subscription.

#### Scenario: Consumer requests a snapshot then subscribes
- **WHEN** a consumer requests a snapshot and then opens a live subscription
- **THEN** the snapshot response does not update after being returned, and further updates arrive
  only through the subscription

### Requirement: No authentication in Phase 1
The gateway SHALL NOT require credentials to connect or subscribe. Any host on the same LAN
SHALL be able to connect.

#### Scenario: A LAN host connects
- **WHEN** a host on the same LAN opens a WebSocket connection to the gateway
- **THEN** the connection is accepted without presenting credentials
