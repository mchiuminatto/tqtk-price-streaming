# deploy

Deployment assets for the tqtk price pipeline.

- `docker-compose.yml` — the stack. Platform components land first (Redis, PostgreSQL/
  TimescaleDB and Prometheus now; Grafana in task 4.4), the services on top of them as each is
  built.
- `redis/redis.conf` — Redis durability configuration, mounted by the Compose file.
- `postgres/initdb/` — database bootstrap: the `timescaledb` extension, and nothing else.
- `prometheus/prometheus.yml` — scrape configuration, one job per service.
- minikube manifests — a later step, per the architecture note.

Run Compose from the repository root, so relative paths and service build contexts resolve.

## Bring-up

```bash
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps      # every component reports (healthy)
```

Every component publishes on loopback only — Redis on `127.0.0.1:6379`, Postgres on
`127.0.0.1:5432`, Prometheus on `127.0.0.1:9090` — for local tooling and tests, not to the LAN. The database is `tqtk`, as user `tqtk`; the password defaults
to `tqtk` and is overridden with `TQTK_POSTGRES_PASSWORD` in the Compose environment.

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
empty string. To confirm both mechanisms are actually writing, `ls /data` in the container shows
`dump.rdb` alongside `appendonlydir/`, and a `docker compose restart redis` leaves keys written
before it in place.

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
