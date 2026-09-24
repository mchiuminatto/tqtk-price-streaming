#!/bin/sh
# Applies the calibration seed script (calibration.redis, beside this file) to Redis with
# redis-cli. Run by the `calibration-seeder` Compose service from the stock Redis image, with both
# files mounted in; exits non-zero on any failure, and the adapter only starts once this exits 0.
#
# redis-cli does not understand comments, so comment and blank lines are stripped first. With
# commands piped on stdin redis-cli exits 0 even on an error reply or a refused connection (its
# `-e` flag only covers commands given as arguments), so: a `-e PING` first fails fast if Redis is
# unreachable, and each piped run's output is checked - with --no-raw every error reply is marked
# `(error)`, and any one fails the seed.
#
# MULTI/EXEC only discards everything for an error caught while queueing (EXECABORT); a command
# that fails while EXEC runs (an HSET with an odd field/value count, a WRONGTYPE) does not roll back
# the rest, so the script's leading delete would still wipe the previous seeding. So the script is
# dry-run first against SCRATCH_DB, a database reserved for this, and applied to LIVE_DB only if
# that run had no error. The script only touches its own keyspace, which its first command
# empties, so the dry run meets exactly the state the live run will - a rule enforced by
# `test_seed_script_writes_only_keys_its_leading_delete_covers` (feed-adapter-synthetic's
# tests/test_calibration_store.py).
#
# Overridable for tests: SEED_REDIS_HOST (default `redis`, Compose's service name) and SEED_SCRIPT.

set -eo pipefail

host=${SEED_REDIS_HOST:-redis}
script=${SEED_SCRIPT:-/seed/calibration.redis}
LIVE_DB=0
SCRATCH_DB=15

redis-cli -h "$host" -e PING > /dev/null
commands=$(grep -Ev '^[[:space:]]*(#|$)' "$script")

seed() { printf '%s\n' "$commands" | redis-cli -h "$host" -n "$1" --no-raw; }
failed() { printf '%s\n' "$1" | grep -q '(error)'; }
report() { printf '%s\n' "$1" | grep '(error)' >&2; }

dry=$(seed "$SCRATCH_DB")
redis-cli -h "$host" -n "$SCRATCH_DB" FLUSHDB > /dev/null
if failed "$dry"; then
    report "$dry"
    echo "calibration seed failed its dry run; the store keeps its previous seeding" >&2
    exit 1
fi

out=$(seed "$LIVE_DB")
if failed "$out"; then
    report "$out"
    echo "calibration seed failed on the live store after a clean dry run; it may be partly" \
        "seeded - re-run the seeder" >&2
    exit 1
fi
echo "calibration seeded"
