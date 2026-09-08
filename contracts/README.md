Thre is a missing # Contracts

The pipeline's contract surfaces, defined once and language-neutrally. `libs/tqtk-common` is the
*Python implementation* of what is defined here — not the contract itself. `feed-adapter-dukascopy`
is Java (Phase 2) and cannot import that package, so the authority lives outside both.

```
contracts/
  wire/       Tick and Bar record schemas + sample records   (data-contract, task 2.1)
  storage/    versioned ticks/bars table contracts            (task 2.6)
  tests/      the fixtures validate, and the two surfaces agree
```

## Wire contract (`wire/`)

JSON Schema, draft 2020-12. Each file is self-contained — no `$ref` across files — so an
implementer in any language can consume exactly one file per record type.

| File | Defines |
|---|---|
| `wire/tick.schema.json` | `Tick`, published to `ticks.raw.{provider}.{symbol}` |
| `wire/bar.schema.json` | `Bar`, published to `bars.{timeframe}.{provider}.{symbol}` — one record per price side |
| `wire/examples/*.sample.json` | Records that must validate; the fixture set both languages test against |

### Conventions the schemas assume

- **Timestamps** are integer **microseconds since the Unix epoch, UTC** — never strings, never
  local time. Microseconds because it is exactly PostgreSQL `timestamptz` resolution, so the wire
  and storage surfaces round-trip without a precision decision hiding between them; integers
  because parsing a timestamp string is work on a path budgeted at <10 ms end to end.
- **Prices and sizes** are JSON numbers (IEEE-754 double). The Dukascopy tick sample is `float64`
  at five decimals, comfortably inside double's exact range.
- **`provider` and `symbol` appear verbatim in stream and key names**, so both patterns exclude the
  `.` and `:` characters used as separators. The deployed symbol set comes from `data/*.parquet`,
  not from this contract.
- **A bar window produces two records**, `side = "bid"` and `side = "ask"`, built from that side's
  price on each contributing tick. `side` is a field, never a stream-name segment: one bar stream
  carries both. The enum is closed at two — a mid series is derived by consumers, never published.
  The pair is published in one atomic bus operation, but a batched read may still split it, so
  consumers pair on `(provider, symbol, timeframe, bar_start_ts)`.
- **Unknown fields are permitted** (`additionalProperties: true`). A producer may publish a field
  before every consumer has adopted it — the same forward-compatibility rule the storage contract's
  additive-only evolution gives readers. Consumers ignore what they do not recognize.
- **Scope is the logical record**: field names, types, and ranges. How a record is framed onto a
  Redis stream entry is the implementations' concern, not this artifact's.

### Invariants JSON Schema cannot express

Documented here and asserted by the implementations and their tests, not by validation. In
Python that is `tqtk_common.records`, which raises on construction; a test there also asserts its
models declare exactly these schemas' fields, so the two copies cannot drift apart silently:

- `ask >= bid`.
- `high >= max(open, close)`, `low <= min(open, close)`, `high >= low`.
- `bar_start_ts` is aligned to its `timeframe` boundary; `last_update_ts >= bar_start_ts`.
- A window's two side-records carry the same `tick_count`, since every tick supplies both a bid and
  an ask.
- `recv_ts` is monotonic non-decreasing per `(provider, symbol)`.
- `seq` is gapless and monotonic per `(provider, symbol)` *within one `session_id`*, and resets to
  0 on each new session. Dedup and reconciliation key on `(provider, symbol, session_id, seq)` —
  never on `seq` alone.

### Versioning

The contract version is the `v1` segment of each schema's `$id`. A change that an existing
consumer could not survive — a removed or renamed field, a narrowed type, a changed unit — is a new
`$id` version, not an edit in place. Additive changes (a new optional field) keep `v1` and are made
in one PR across this directory and every implementation of it.

## Storage contract (`storage/`)

The second contract surface: the `ticks` and `bars` table schemas, each an artifact carrying its
own `schema_version` rather than existing only as whatever DDL happened to be applied.

| File | Defines |
|---|---|
| `storage/ticks.v1.json` | the `ticks` table, owned by `tick-persistence-svc` |
| `storage/bars.v1.json` | the `bars` table, owned by `bar-persistence-svc` |
| `storage/storage-contract.schema.json` | the shape those two are written in |

- **Each table versions independently.** Two owners, two release cadences; `schema_version` is an
  integer bumped in the same change as any column addition, and the filename carries it.
- **One file per version, and a released one is frozen.** `ticks.v1.json` and a later
  `ticks.v2.json` coexist, because a reader binds to a version and expects it to keep meaning what
  it meant when it shipped. Changing the schema means writing the next version, never editing the
  last; only the prose in a released file may change.
- **Additive only within a lineage, enforced in CI.** No `DROP`, no `RENAME`, no type-narrowing,
  no tightening a column to `NOT NULL`; a new column is nullable or carries a `default`, so a
  reader built against an earlier version keeps working. `tools/ci/additive_only.py` fails the
  build on any of those, and on an edit to an already-released version. Run it locally with
  `uv run python tools/ci/additive_only.py --base main`.
- **DDL ownership follows sole-writer ownership.** The owning service applies its own migrations on
  startup, under an advisory lock. Nothing is created by platform bootstrap.
- **Every column carries its `wire_field`**, so a test can hold the storage and wire surfaces
  together instead of trusting that both were updated.
- **Prices are `double precision`, timestamps are `timestamptz`, and no column is JSONB** — a price
  document would hide the schema from `information_schema`, from the shared fixture, and from
  Timescale's per-column compression.

### Validating a record

```bash
uv run --group dev pytest contracts -q          # schemas and fixtures, language-neutral
uv run --group dev pytest libs/tqtk-common -q   # and what the Python models emit
```

Python: `jsonschema`, `Draft202012Validator`. Java: `networknt/json-schema-validator` or
`everit-org/json-schema` — both read these files as-is, with no generated code to keep in sync.
