# Implementation plan — Redis-backed calibration authority behind an MCP gateway

**Date:** 2026-09-23
**Split:** Part A happens in `mchiuminatto/tqtk-price-streaming`; Part B happens in this task repo.

---

## Design in one picture

```
                internal: true
   ┌────────────────────────────────┐
   │  redis        (calibration)    │   ← no route from the agent
   │  redis-seed   (one-shot)       │
   └──────────────┬─────────────────┘
                  │  backend
        ┌─────────┴─────────┐
        │   redis-mcp       │  redis-mcp-server + supergateway → streamable-http
        └─────────┬─────────┘
                  │  agent
              ┌───┴───┐
              │ main  │  agent container: /workspace + /data (CSV samples)
              └───────┘
```

**You fit the distributions offline, as the author, on the full 8.5M rows.** The results are seeded
into Redis. The agent retrieves them through MCP and bakes them into `distributions.py`.

This is not a shortcut — it is what the project already documents. `distributions.py`'s own docstring
calls the tables "the executable form of" the calibration notes, deliberately baked in so the feed
needs no `scipy`/`numpy` at runtime. Querying a calibration store and encoding the result is the
real workflow.

**What the agent actually does:**
1. Discover symbols from `/data/*.csv` (local).
2. Query Redis through MCP for each symbol's three fitted distributions.
3. Reconcile venue symbology against file symbols via the alias map.
4. Convert stored units to code units — **only for `loc` and `scale`, never for shape parameters**.
5. Implement eleven samplers using `random.Random` alone, plus the three accessors and
   `registered_symbols`.

---

## Part A — work in `tqtk-price-streaming` (upstream)

Do this first, commit, then re-clone into `environment/workspace/` and record the **new** SHA.

### A1. Restore the stub state of `distributions.py`

`services/feed-adapter-synthetic/src/feed_adapter_synthetic/distributions.py` should ship with the
five `NotImplementedError` stubs. Fix the docstrings while you are there:

- Remove the three `docs/tick-distributions.md` / `spread-distributions.md` /
  `tick-interval-distributions.md` citations — those files must not exist at the pinned SHA (see R2).
- **Change "fitted log-return distribution" to consecutive `Bid` differences.** `generator.py:98-105`
  does `self._bid += Decimal(str(drawn_return))`, so "log-return" is simply wrong and is the single
  most expensive wrong word in the task.
- Point at "the calibration store" generically — no key names, no schema, no MCP hints.

### A2. Clear the 18 dangling references

Same three filenames are cited across `calibration.py:6-7`, `generator.py:10-14`,
`docs/synthetic-price.md:27,37,50`, and `openspec/changes/add-price-pipeline/{design.md,spec.md,tasks.md}`.
Re-point them at the calibration store or drop the citation. Leaving them sends the agent hunting for
files that do not exist and burns its budget — a fairness problem, not tidiness.

### A3. Decide what the repo says about the calibration store

The openspec change docs should describe calibration parameters being published to Redis for the
adapter. This matters for realism *and* for provenance (R1): the feature should read as real project
work, because it is.

### A4. Shrink the sample CSVs (optional but recommended)

They are no longer the fitting source — you fit offline from the full data. In the task they serve
only symbol discovery and `calibration.py`'s `price_increment` inference, which needs only enough
rows to expose the pip grain. A few thousand rows per symbol takes 415MB → ~5MB with zero loss where
it matters.

**Copy lines verbatim.** Do not round-trip through pandas/pyarrow: `_decimal_places` infers
`price_increment` by round-tripping `Bid`, so reformatting `1.15926` silently changes the inferred
increment and breaks `test_calibration.py`.

### A5. Re-clone into the task repo

```sh
mkdir -p environment/workspace
git -C environment/workspace init
git -C environment/workspace remote add origin https://github.com/mchiuminatto/tqtk-price-streaming.git
git -C environment/workspace fetch --depth 1 origin <NEW_FULL_40_CHAR_SHA>
git -C environment/workspace checkout --detach FETCH_HEAD
rm -rf environment/workspace/.git
```

No `.git` may remain in `environment/workspace`. Update the `Makefile` target and record the new SHA
in `comments.md`.

---

## Part B — work in the task repo

### B1. Offline fit derivation (author-only, outside the task tree)

`rv submit` ships essentially the whole task directory — `rv/submission.py:26` drops only
`node_modules`, root `AGENTS.md`, and `submission.zip`. There is no ignore mechanism, so this lives
in a sibling:

```
/home/mchiuminatto/work/m1-synth-price-feed-authoring/
  derive_fits.py      # scipy MLE + AIC over the FULL untrimmed CSVs
  verify_fits.py      # independent re-implementation, no scipy.stats.fit
  emit_seed.py        # writes environment/redis/seed.redis
```

**Protocol to implement in `derive_fits.py`:**

| item | value |
|---|---|
| returns | `Bid[t] − Bid[t−1]`, quote units, all rows |
| spread | `Ask[t] − Bid[t]` |
| interval | `time_art[t] − time_art[t−1]`, seconds |
| returns candidates | `normal, student_t, laplace, logistic, cauchy` |
| spread/interval candidates | `expon, gamma, lognormal, weibull_min, loglogistic` |
| `loc` pinning (positive-support only) | `min(x)·(1−1e-4)` if `min(x)>0`, else `min(x)−1e-4` |
| ranking | `AIC = 2k − 2·logL`, lower wins; `k` counts only *freely estimated* params |
| tie-break | within a chosen margin, prefer the earlier candidate in the documented order |

Measured runtime for reference: the largest symbol (USATECHIDXUSD, 1.73M rows) takes **103.5s** for
all 15 fits — `student_t` 44.3s, `loglogistic` 17.5s, `weibull_min` 10.1s, the rest near zero.
The full 8.5M-row job is roughly **8–9 minutes**.

**Record the ΔAIC margins.** Full-data minimums from the previous analysis: returns 3,395 (EURUSD),
spread 9,457 (ARKQUSUSD), interval **115 (AUDJPY)** — the only genuinely close call. Choose the
tie-break margin so every one of the 51 cells sits well clear of it.

**`verify_fits.py` is not optional** — see R14. Implement the same protocol with a different
estimator path (`math`/`statistics` only, no `scipy.optimize`): Laplace → median + MAD; normal →
mean/std; lognormal with pinned `loc` → mean/std of `log(x−loc)`; gamma → digamma Minka update;
Weibull → Newton on the shape equation; log-logistic → golden-section on the profile likelihood;
Student-t → EM. **Gate: family agreement on all 51 cells, parameters within 2%.**

### B2. Redis keyspace

Venue symbology in Redis, file symbology on disk. Units differ from code units.

```redis
# --- symbology: venue → file ---
HSET symbology EUR/USD EURUSD
HSET symbology AAPL.US AAPLUSUSD
...

# --- instrument reference ---
HSET instrument:EUR/USD pip_size 0.0001 quote_currency USD

# --- one key per fit ---
SET  calib:EUR/USD:spread:family "weibull_min"
SET  calib:EUR/USD:spread:param_count 3

# --- one hash per parameter, ordinal-indexed, unit-tagged ---
HSET calib:EUR/USD:spread:param:0 name shape value 1.17157     unit dimensionless
HSET calib:EUR/USD:spread:param:1 name loc   value 9.99852     unit pip
HSET calib:EUR/USD:spread:param:2 name scale value 34.4205     unit pip

# --- discovery index, so the keyspace is findable ---
SADD calib:symbols EUR/USD AAPL.US ...
SADD calib:quantities return spread interval
```

**The `unit` field is the whole trap, and it is fair because it is data, not folklore.** Pip
conversion applies to `loc` and `scale`; shape parameters are dimensionless and must not be scaled.
An agent that multiplies all three has ignored a column it queried. Same for `interval`, stored in
milliseconds.

Seed intervals in **ms** and spreads in **pips**; returns stay in quote units (no conversion) so not
every quantity carries the same trick.

`emit_seed.py` generates `environment/redis/seed.redis` from `derive_fits.py`'s output, so the seed
is reproducible and never hand-typed.

### B3. Compose wiring

Copy the first-party template at
`rv/realms/mcp/examples/redis-deployment-policy/environment/` — it is exactly this shape and is
known to pass `rv check`.

```yaml
services:
  main:
    depends_on: { redis-mcp: { condition: service_healthy } }
    networks: [agent]
    environment:
      NO_PROXY: localhost,127.0.0.1,::1,redis,redis-seed,redis-mcp
      no_proxy: localhost,127.0.0.1,::1,redis,redis-seed,redis-mcp
      # ...keep the whole Netleash proxy/CA block and the ca.crt volume unchanged
  redis:
    image: redis:8.2.3-alpine@sha256:08ad0b...
    networks: [backend]
    healthcheck: { test: ["CMD","redis-cli","ping"], interval: 2s, retries: 20 }
  redis-seed:
    image: redis:8.2.3-alpine@sha256:08ad0b...
    depends_on: { redis: { condition: service_healthy } }
    networks: [backend]
    volumes: [ ./redis/seed.redis:/seed.redis:ro ]
    command: ["sh","-c","redis-cli -h redis < /seed.redis"]
  redis-mcp:
    build: { context: ./redis-mcp }
    depends_on: { redis-seed: { condition: service_completed_successfully } }
    networks: [agent, backend]
    healthcheck: { test: ["CMD","node","-e","fetch('http://127.0.0.1:8000/healthz')..."], retries: 30 }
networks:
  agent:
  backend: { internal: true }
```

`environment/redis-mcp/Dockerfile` — no server code, two pinned tools:

```dockerfile
FROM node:22-bookworm-slim@sha256:6c7479...
COPY --from=ghcr.io/astral-sh/uv:0.11.30@sha256:93b61e... /uv /uvx /bin/
ENV PATH="/root/.local/bin:${PATH}"
RUN uv tool install --managed-python --python 3.12 redis-mcp-server==0.5.0
RUN npm install --global supergateway@3.4.3
ENTRYPOINT ["supergateway"]
CMD ["--stdio", "redis-mcp-server --url redis://mcp:<readonly-pass>@redis:6379/0", \
     "--outputTransport", "streamableHttp", "--stateful", \
     "--port", "8000", "--healthEndpoint", "/healthz", "--logLevel", "none"]
```

Use a **read-only Redis ACL user** for the MCP connection, as the example does — it enforces
read-only authority at the server rather than by convention.

### B4. `task.toml`

```toml
[[environment.mcp_servers]]
name = "calibration"
transport = "streamable-http"
url = "http://redis-mcp:8000/mcp"
```

Drop the `filesystem` stdio entry. The name must match `instruction.md` exactly. Keep
`network_mode = "public"`.

### B5. Agent image

`environment/Dockerfile`: **remove** `numpy`/`scipy` if you had planned them — the agent no longer
fits anything. **Remove** the Node/npm layers and the `npx` warm-up, now that `filesystem` is gone.
Keep `pytest`, `pyarrow`, `pydantic`, `pydantic-settings`, `prometheus-client`, `redis`. Keep the
`# === BEGIN realm base ===` block byte-for-byte, and `COPY data/ /data/` plus
`COPY workspace/ /workspace/` as explicit paths.

### B6. Tests

The existing `tests/test_outputs.py` structure is largely **correct again** — you control the
expected values, so exact grading is fair. Changes:

- Keep the textbook family-moment tests; extend to `logistic`, `expon`, `cauchy` (the last by median
  and IQR only — mean and variance do not exist).
- Keep `test_unknown_family_raises`, the three accessor error-message tests,
  `test_registered_symbols_matches_the_sample_data_exactly`, and
  `test_return_distributions_are_not_one_hardcoded_value` (extend to all three quantities).
- Update `_EXPECTED_*` tables to the **converted, code-unit** values — pips → quote units,
  ms → seconds — sourced from `verify_fits.py`, not `derive_fits.py` (see R14).
- Keep `rel=1e-2`; it absorbs seed rounding without admitting a wrong fit.
- **Add a unit-conversion discrimination test**: for at least one symbol per affected quantity,
  assert the shape parameter is *unscaled*. An agent that multiplied everything by `pip_size`
  fails distinctly from one that converted nothing.

`tests/test.sh` — reduce to exit-status grading: write zero reward, run pytest, write full reward
only on exit 0, reward on every path. Drop the JUnit XML emit/parse and the exact test-count check.

### B7. `instruction.md`

Context → data at `/data` (one spelling) → what to build, naming the dataclass fields **`family`** and
**`params`** (the current `parameters` fails every test on a literal reading) → the **`calibration`**
MCP server as the source of the fitted tables → definition of done → **anti-cheat note, verbatim,
last** (see R6). Do not name keys, units, or the alias table — those are what MCP is for.

---

## Validation sequence

1. Re-clone the workspace at the new SHA; confirm no `.git` under `environment/workspace`.
2. Run `derive_fits.py`, then `verify_fits.py`; require 51/51 family agreement.
3. `emit_seed.py` → `environment/redis/seed.redis`.
4. `docker compose up`; confirm `redis-seed` exits 0 and `redis-mcp` reports healthy.
5. **Prove isolation**: from inside `main`, `redis-cli -h redis ping` and a Python `redis` connect
   must both fail. If either succeeds, `backend` is not internal and the whole design collapses.
6. **Prove the MCP path**: hand-solve one symbol end to end through the MCP tools only — list the
   keyspace, read a fit, reconcile the alias, convert units. Seeded data and a passing oracle do not
   prove retrievability.
7. `rv check` — zero failures.
8. `rv oracle` — 1.0 from clean state.
9. **Prove the oracle is not hollow**: untouched starter scores 0; one family flipped to its
   runner-up scores 0; one `scale` × 1.3 scores 0; a table built by scaling shape parameters scores 0.
10. `rv probe`, all four modes.
11. Calibration: 3 completed Opus 4.8 + 3 completed GPT-5.6 Sol, ≤2 genuine passes, every trajectory
    inspected, each failure attributed to MCP-dependent work.
12. Fill `comments.md`; housekeeping; `rv submit`.

---

## Risks to the task requirements

### R1 — Provenance: you own the repository *(highest)*

The rules demand "a real problem source … supported by stable reviewer-accessible evidence" and call
out **"Inventing the problem — the defect or feature exists only in the task author's narrative."**
You are the repo owner about to modify upstream specifically to enable an eval. A reviewer can read
that as invented.

What makes it defensible, and what you must do:

- **The repo must be public.** MIT is confirmed (`LICENSE`, "Copyright (c) 2026 Marcello
  Chiuminatto"), but permissive licensing is not the same as public. Verify before building.
- **Cite the genuine evidence.** `openspec/changes/add-price-pipeline/tasks.md` records tasks 5.1–5.x
  for `feed-adapter-synthetic` as real planned work, and `docs/synthetic-price.md` specifies the
  model. That is reviewer-inspectable proof this feature is real project work, not narrative.
- **Do not make the upstream commit task-shaped.** A commit that removes an implementation and leaves
  stubs reads as manufactured. Prefer a history where the calibration-store integration is genuine
  forward work.
- **`comments.md` must be candid**: repo URL, new 40-char SHA, MIT, the openspec URL as problem
  source, honest contamination analysis, and what was materially adapted.

### R2 — Contamination: the answer may be public *(critical, easy to miss)*

At commit `dabbe9b` this repo's history contained `docs/tick-distributions.md`,
`spread-distributions.md` and `tick-interval-distributions.md` — **the complete fitted tables**, plus
a `distributions.py` implementing every sampler. If any of that is reachable in the public repo at or
near the pinned SHA, the task's hidden authority is published on GitHub.

Required:
- The three calibration notes **must not exist** at the pinned SHA.
- A fully implemented `distributions.py` must not be reachable at the pinned SHA.
- Ideally the seeded values are a **fresh derivation** under the new protocol (defined epsilon,
  stated `k`, no 200k subsample), so they do not match anything previously published.
- State this analysis explicitly in `comments.md`. The agent has no egress, but contamination is a
  review criterion independent of runtime reachability.

### R3 — Difficulty attribution

`GET`-ing 51 fits is easy; the samplers are hard but equally hard without MCP — which the rules call
"merely SWE-bench-style, does not qualify." Your two mechanisms carry this weight: symbol
reconciliation and the unit/shape-parameter trap. Both must be graded and must fail visibly. If
calibration rollouts fail on sampler mathematics rather than on retrieval and conversion, the task
does not clear the bar.

### R4 — Aliasing collides with `registered_symbols()`

`test_registered_symbols_matches_the_sample_data_exactly` compares against `discover_symbols()`,
which reads **file** symbols (`EURUSD`). Redis holds **venue** symbols (`EUR/USD`). So
`registered_symbols()` must return file symbols, and the alias map must cover every discovered symbol
exactly — no orphans in either direction. Seed a couple of venue symbols that are *not* in `/data`
(instruments not yet onboarded) so the intersection is a real operation rather than an identity.

### R5 — `pip_size` can contradict `price_increment`

`calibration._decimal_places` already infers the increment from the CSVs. If Redis's `pip_size`
disagrees, the workspace contradicts the authority. Seed `pip_size` to exactly match what
`_decimal_places` infers, and assert it during validation.

### R6 — The anti-cheat note is mandatory

`rv/checks/common.py:36` (`InstructionHasAntiCheatNote`) requires the marker
*"Retrieving the answer from an external source is a failed attempt"*; `mcp` is not in
`_ANTI_CHEAT_EXEMPT`. `rv/checks/util.py:54` strips it before the tone judges run, so it costs
nothing stylistically. **Keep it verbatim, last.** (`docs/readiness-review.md` says to delete it —
that advice is wrong; fix or delete that file.)

### R7 — Tool-call budget

51 fits × (family + param_count + 3 param hashes) ≈ 250 MCP round trips, plus symbology and
instrument lookups. If the Redis MCP server has no bulk/scan capability this may exceed a reasonable
budget. Check its tool surface early; if it is thin, flatten to one hash per fit rather than one key
per parameter.

### R8 — Backend isolation is the whole design

If `backend` is not `internal: true`, or `main` is accidentally attached to it, the agent reaches
Redis directly and MCP becomes optional again. Validation step 5 exists for exactly this. Note the
agent image ships the Python `redis` package — harmless when there is no route, but it means the only
thing standing between the agent and the authority is the network topology.

### R9 — `registered_symbols` must be the three-way intersection

Symbols with only some quantities fitted must be excluded. Seed at least one such partial symbol so
the intersection is exercised, not assumed.

### R10 — Seed determinism and resettability

`rv oracle` runs from a clean state repeatedly. The seed must be idempotent and produce identical
state every time — no timestamps, no randomness, no `INCR`. Prefer explicit `SET`/`HSET` over
anything order-dependent.

### R11 — Every image digest-pinned

`redis`, `node`, `uv`, and the base all need `@sha256:` on `FROM` and `COPY --from`. Resolve them
with `docker buildx imagetools inspect`. Compose build contexts and bind mounts must stay inside
`environment/`.

### R12 — `rv submit` ships everything

`derive_fits.py`, `verify_fits.py`, `emit_seed.py` and the full-size original CSVs must live outside
the task tree. Also remove `.idea/`, `.pytest_cache/`, `tests/.pytest_cache/`, `docs/rework-plan.md`,
`docs/readiness-review.md`, `docs/harness-runbook.md`, and this file before submitting.

### R13 — `.dockerignore` bug in the workspace

`environment/workspace/.dockerignore` was edited from `data/` to `../data/`. Docker resolves ignore
patterns from the context root, so `../` can never match and the rule is a silent no-op. Revert it
upstream.

### R14 — Fixture/oracle coupling

The verifier's expected tables and `solution/distributions.py` would both trace to `derive_fits.py`.
Break it: the **tests** take their values from `verify_fits.py`, the **golden** from `derive_fits.py`.
The oracle is then downstream of, and checked against, the fixture. Skipping `verify_fits.py` is the
most likely single cause of a review rejection on this design.

### R15 — Unit conversion must be unambiguous

`pip` and `dimensionless` must be the only unit values, spelled identically everywhere, with the
conversion direction obvious. If an agent can reasonably read "value is in pips" as "already
converted", the test is unfair rather than discriminating. Seed one worked cross-check the agent can
verify against — e.g. an instrument whose `loc` in pips and in quote units are both recoverable.
