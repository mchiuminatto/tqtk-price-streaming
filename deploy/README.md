# deploy

Deployment assets for the tqtk price pipeline.

- `docker-compose.yml` — the stack. All four platform components are in it (Redis, PostgreSQL/
  TimescaleDB, Prometheus, Grafana); the services go on top as each is built.
- `docker-compose.lan.yml` — overlay that publishes Grafana beyond loopback, and the only way to
  do so.
- `bootstrap-secrets.sh` — generates the stack's credentials into `secrets/`, once per machine.
- `secrets/` — generated credentials. Git-ignored: nothing in this repository is a working
  password.
- `redis/redis.conf` — Redis durability configuration, mounted by the Compose file.
- `postgres/initdb/` — database bootstrap: the `timescaledb` extension, and nothing else.
- `prometheus/prometheus.yml` — scrape configuration, one job per service.
- `grafana/provisioning/` — datasource (and, from task 12.2, dashboards) provisioned from files.
- minikube manifests — a later step, per the architecture note.

Run Compose from the repository root, so relative paths and service build contexts resolve.

## Bring-up

Once per machine, generate the credentials:

```bash
./deploy/bootstrap-secrets.sh    # idempotent; existing secrets are kept
```

Then:

```bash
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps      # every component reports (healthy)
```

Every component publishes on loopback — Redis on `127.0.0.1:6379`, Postgres on `127.0.0.1:5432`,
Prometheus on `127.0.0.1:9090`, Grafana on `127.0.0.1:3000` — for local tooling and tests, not to
the LAN. The database is `tqtk`, as user `tqtk`.

Grafana is the one that can be widened, being the one a person opens rather than a service connects
to. That goes through an overlay file, not a variable on the base file:

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.lan.yml up -d
```

`TQTK_GRAFANA_BIND` narrows the bind further than `0.0.0.0` if a specific interface is wanted.

## Credentials

Every credential is a file under `deploy/secrets/`, generated per machine, never committed. No
password in this repository is a working password, and none is passed as an environment variable —
`POSTGRES_PASSWORD` and `GF_SECURITY_ADMIN_PASSWORD` in a container's environment are readable for
the life of that container by anyone who can reach the Docker socket:

```bash
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' <container> | grep PASSWORD
```

The Compose file uses each image's own file convention instead — `POSTGRES_PASSWORD_FILE` for
Postgres, `GF_SECURITY_ADMIN_PASSWORD__FILE` (two underscores) for Grafana — so only the path
appears in the environment. Both strip a trailing newline from the file, which is why
`bootstrap-secrets.sh` writes none.

There is no default and no fallback: a missing secret file stops the stack rather than starting it
with a credential everyone can read. Read one when you need it, rather than copying it somewhere:

```bash
cat deploy/secrets/grafana_admin_password   # Grafana sign-in, user `admin`
cat deploy/secrets/postgres_password        # psql, user `tqtk`
```

New secrets are 48 hex characters — `openssl rand -hex 24`, 192 bits. Hex rather than base64 so
the value carries no `+` or `/`: a `/` in the userinfo of a `postgresql://user:pass@host/db` URI
terminates the authority component, and the connection then fails to parse or quietly targets
something else. The script never replaces an existing file, so a machine set up before this change
keeps its base64 value — rotate it deliberately (below) if you want the new alphabet there too.

That is a guardrail, not the fix. Services added in tasks 7.1 and 8.1 should read the file and
pass it to psycopg in keyword form — `host=... password=...` — rather than building a URI at all,
which sidesteps the question of which characters need escaping instead of depending on the answer.

**Rotating.** Delete the file and re-run `bootstrap-secrets.sh` to get a new value. That is only
ever step one. On an already-initialised volume *neither* component picks the new secret up on its
own, because both read it only when they first create their store — so a rotation that stops here
leaves the old credential live while the file says otherwise.

Postgres reads `POSTGRES_PASSWORD_FILE` only in `initdb`, on an empty data directory, so the
database keeps the old password until you change it there as well:

```bash
docker compose -f deploy/docker-compose.yml exec postgres \
  psql -U tqtk -d tqtk -c "ALTER USER tqtk PASSWORD '<the new secret>'"
```

Grafana is the same shape, and restarting it is **not** enough: `GF_SECURITY_ADMIN_PASSWORD__FILE`
is consumed only when Grafana creates its user database on an empty `grafana-data` volume. The
container comes back with the old password still working and no warning from either side.

```bash
docker compose -f deploy/docker-compose.yml exec grafana \
  grafana cli admin reset-admin-password "$(cat deploy/secrets/grafana_admin_password)"
# Admin password changed successfully ✔
```

The value is an argument there, so it is visible in the container's process list for the moment
the command runs — acceptable on a single-host deploy, and the reason not to script this against a
shared machine.

Because the file and the database can disagree, the file is not evidence of what works. Ask:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -u "admin:$(cat deploy/secrets/grafana_admin_password)" http://127.0.0.1:3000/api/org
# 200
```

Recreating a volume with `down -v` is the other route for either component, and discards what is
in it — the price data for Postgres, and for Grafana every dashboard edited in the UI plus all
alert state.

## Teardown

```bash
docker compose -f deploy/docker-compose.yml down    # stop; Redis and Postgres data survive
docker compose -f deploy/docker-compose.yml down -v # also discard both data volumes
```

Full stack bring-up and teardown, once every service is in it, is documented at task 13.4.

## Verifying Redis durability

Redis carries both the bus and the checkpoints, so both AOF (`everysec`) and RDB must be on -
see `redis/redis.conf` for why. After startup:

```bash
docker compose -f deploy/docker-compose.yml exec redis redis-cli CONFIG GET appendonly
# appendonly
# yes
docker compose -f deploy/docker-compose.yml exec redis redis-cli CONFIG GET save
# save
# 900 1 300 100 60 10000
```

A non-empty `save` is what "RDB enabled" means: Redis disables snapshotting by setting it to an
empty string.

To confirm both mechanisms are actually *writing*, do not reach for `ls /data` on a stack that has
just come up. `appendonlydir/` is there from the first write, but `dump.rdb` is not: the earliest
save point is `save 900 1`, so the first snapshot is fifteen minutes away, and its absence before
then is normal rather than a fault. Ask Redis instead of the filesystem:

```bash
R="docker compose -f deploy/docker-compose.yml exec redis redis-cli"

$R INFO persistence | grep rdb_last_save_time     # note this value
# rdb_last_save_time:1789055184

$R BGSAVE SCHEDULE
# Background saving started        <- or "scheduled", if a child was already running

$R INFO persistence | grep -E 'rdb_bgsave_in_progress|rdb_last_save_time|rdb_last_bgsave_status|aof_enabled'
# rdb_bgsave_in_progress:0
# rdb_last_save_time:1789055202     <- moved, so a snapshot really completed
# rdb_last_bgsave_status:ok
# aof_enabled:1
```

**Read the timestamp, not just the status.** `rdb_last_bgsave_status` is `ok` on a server that has
never saved at all — it is the field's initial value, not a result — so on its own it is consistent
with a snapshot that never happened, the same weakness as reading `ls /data`. A `rdb_last_save_time`
that moved is the part that cannot be faked, which is why you note it before and compare after.

**`BGSAVE SCHEDULE`, not a plain `BGSAVE`.** AOF is enabled here, and Redis allows only one child
process at a time: a plain `BGSAVE` issued while an AOF rewrite is in flight is refused outright
with `ERR Another child process is active (AOF?)`. The snapshot then never happens, the timestamp
never moves, and the check looks like a persistence fault when it is ordinary housekeeping.
`SCHEDULE` queues the save for whenever the current child finishes instead of failing.

**Give the fork time.** `rdb_bgsave_in_progress:1` means it is still running — re-run the last
command rather than concluding anything. Snapshot duration scales with the dataset; eight million
keys takes about ninety seconds here. Only once that field reads `0` does an unmoved
`rdb_last_save_time` mean the snapshot failed, and `rdb_last_bgsave_status:err` is what says so.

`rdb_last_bgsave_status` is still the field to read when Redis starts refusing writes for no
obvious reason: it is what latches on a failed snapshot, and what
`stop-writes-on-bgsave-error yes` turns into those refusals.

`dump.rdb` does appear on a clean shutdown, because Redis snapshots on `SIGTERM` when save points
are configured. That is what makes `docker compose restart redis` leave keys written before it in
place.

### Why the healthcheck reads rather than writes

The Compose healthcheck probes with `PING`, and matches the *reply* rather than the exit status —
`redis-cli` exits 0 even on an error reply, so the exit code alone is not a verdict. `PING` is
enough because Redis refuses it in both states a waiting consumer cares about: `-LOADING` while
the AOF or RDB replays, and `-MISCONF` once `stop-writes-on-bgsave-error` has fired. Reads like
`GET` still succeed in the second, so this is not simply "the server is down".

An earlier version probed with a `SET`, on the belief that only a write fails in both. It does
not, and the write was not free: every probe incremented `rdb_changes_since_last_save`, which kept
`save 900 1` permanently satisfied and forked a BGSAVE every 15 minutes on a stack carrying no
traffic at all. Reading costs nothing and detects the same two states.

The one case a write probe would catch and `PING` would not is an OOM refusal under `maxmemory`
with `noeviction` — writes rejected while `PING` still answers. `maxmemory` is deliberately unset
(see `redis/redis.conf`), so that state is unreachable; if it is ever set, revisit the probe at the
same time.

`retries: 5` at `interval: 15s` means Redis is marked unhealthy 75 seconds after it stops
answering. Nothing in the stack fails over on that signal today, so the slower verdict is the
cheaper trade.

## Verifying the database bootstrap

The platform brings up the server and the extension only. `ticks` and `bars` belong to
`tick-persistence-svc` and `bar-persistence-svc`, which apply their own migrations on startup
(tasks 7.1 and 8.1), so on a stack where no service has run yet neither table exists:

```bash
docker compose -f deploy/docker-compose.yml exec postgres psql -U tqtk -d tqtk -c '\dx'
#  timescaledb | 2.17.2 | public | Enables scalable inserts and complex queries for time-series data
docker compose -f deploy/docker-compose.yml exec postgres psql -U tqtk -d tqtk -c '\dt'
# Did not find any relations.
```

A table appearing in `\dt` before any service has started means platform bootstrap has taken
over DDL that a service owns — the implicit shared surface the versioned storage contract exists
to replace.

The `initdb/` scripts run once, against an empty data directory. Re-running them after a schema
change to the bootstrap means `down -v` first; on an existing volume they are skipped silently.

## Verifying the Prometheus scrape configuration

Prometheus scrapes `/metrics` on port 8000 of every service, one job per service, so the
failure-isolation dashboard and the "health failing or unscraped" alert both read
`up{job="<service>"}`. The targets page lists all six from the first start:

```bash
xdg-open http://127.0.0.1:9090/targets                       # or curl the API:
curl -s http://127.0.0.1:9090/api/v1/targets \
  | python3 -c 'import json,sys; [print(t["labels"]["job"], t["health"]) for t in json.load(sys.stdin)["data"]["activeTargets"]]'
```

A target reports down until the service behind it exists and is in this Compose file — which is
the honest answer, not a misconfiguration. All six read `up` once the services are running;
task 13.1 is where that is checked against the real stack.

Editing the scrape config does not need a restart:

```bash
docker compose -f deploy/docker-compose.yml exec prometheus \
  promtool check config /etc/prometheus/prometheus.yml   # validate first
curl -s -X POST http://127.0.0.1:9090/-/reload           # then reload
```

## Verifying Grafana

Grafana is provisioned from `grafana/provisioning/`, so the Prometheus datasource is wired on a
clean volume with nothing to click. Sign in at <http://127.0.0.1:3000> as `admin`, with the
password from `deploy/secrets/grafana_admin_password` — see [Credentials](#credentials), and note
that on a volume where it has been rotated the live one may be the database's rather than the
file's. The datasource is under Connections → Data sources → Prometheus, shown read-only because
the file is its source of truth. **Save & test** there reports "Successfully queried the
Prometheus API".

Both checks without a browser:

```bash
curl -s http://127.0.0.1:3000/api/health
# {"database": "ok", "version": "11.4.0", ...}
curl -s -u "admin:$(cat deploy/secrets/grafana_admin_password)" \
  http://127.0.0.1:3000/api/datasources/uid/tqtk-prometheus/health
# {"message":"Successfully queried the Prometheus API.","status":"OK"}
```

`tqtk-prometheus` is a fixed `uid` on purpose: the task 12.2 dashboards reference it by that
string, and a generated one would break them every time the volume is rebuilt.
