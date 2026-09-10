# deploy

Deployment assets for the tqtk price pipeline.

- `docker-compose.yml` — the stack. Platform components land first (Redis now; PostgreSQL/
  TimescaleDB, Prometheus and Grafana in tasks 4.2-4.4), the services on top of them as each is
  built.
- `redis/redis.conf` — Redis durability configuration, mounted by the Compose file.
- minikube manifests — a later step, per the architecture note.

Run Compose from the repository root, so relative paths and service build contexts resolve.

## Bring-up

```bash
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps      # redis reports (healthy)
```

## Teardown

```bash
docker compose -f deploy/docker-compose.yml down    # stop; Redis data survives
docker compose -f deploy/docker-compose.yml down -v # also discard the redis-data volume
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
