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

**Rotating.** Delete the file and re-run `bootstrap-secrets.sh`. For Grafana that is enough —
restart it and the new password applies. For Postgres it is not: `POSTGRES_PASSWORD_FILE` is read
only by `initdb`, on an empty data directory, so on an existing volume the database keeps the old
password until you change it in the database as well:

```bash
docker compose -f deploy/docker-compose.yml exec postgres \
  psql -U tqtk -d tqtk -c "ALTER USER tqtk PASSWORD '<the new secret>'"
```

Recreating the volume with `down -v` is the other way, and discards the data.

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

## Verifying Grafana

Grafana is provisioned from `grafana/provisioning/`, so the Prometheus datasource is wired on a
clean volume with nothing to click. Sign in at <http://127.0.0.1:3000> (`admin` / `admin`, or
`TQTK_GRAFANA_PASSWORD` if the LAN overlay is in use); the datasource is under Connections → Data
sources → Prometheus, shown read-only because the file is its source of truth. **Save & test**
there reports "Successfully queried the Prometheus API".

Both checks without a browser:

```bash
curl -s http://127.0.0.1:3000/api/health
# {"database": "ok", "version": "11.4.0", ...}
curl -s -u admin:admin http://127.0.0.1:3000/api/datasources/uid/tqtk-prometheus/health
# {"message":"Successfully queried the Prometheus API.","status":"OK"}
```

`tqtk-prometheus` is a fixed `uid` on purpose: the task 12.2 dashboards reference it by that
string, and a generated one would break them every time the volume is rebuilt.
