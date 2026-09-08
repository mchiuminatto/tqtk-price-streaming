Thre is a missing # Contracts

The pipeline's contract surfaces, defined once and language-neutrally. `libs/tqtk-common` is the
*Python implementation* of what is defined here — not the contract itself. `feed-adapter-dukascopy`
is Java (Phase 2) and cannot import that package, so the authority lives outside both.

```
contracts/
  wire/       Tick and Bar record schemas + sample records   (data-contract, task 2.1)
  storage/    versioned ticks/bars table schemas             (task 2.6 — not yet written)
  tests/      every sample record validates against its schema
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

### Validating a record

```bash
uv run --group dev pytest contracts -q          # schemas and fixtures, language-neutral
uv run --group dev pytest libs/tqtk-common -q   # and what the Python models emit
```

Python: `jsonschema`, `Draft202012Validator`. Java: `networknt/json-schema-validator` or
`everit-org/json-schema` — both read these files as-is, with no generated code to keep in sync.
