"""`python -m tools.calibration_seed`: seed the calibration store once and exit.

Reads the Redis URL from `TQTK_REDIS_URL` (the variable every service uses), defaulting to the
same local URL as `FeedConfig`. Exits 0 once the keyspace is written, 1 with a one-line message if
Redis cannot be reached - a run-to-completion job, so Compose can gate the adapter on its success.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence

from redis import Redis
from redis.exceptions import RedisError

from .keyspace import SYMBOLS_KEY, SeedClient, seed

__all__ = ["DEFAULT_REDIS_URL", "REDIS_URL_VARIABLE", "main"]

REDIS_URL_VARIABLE = "TQTK_REDIS_URL"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"

_log = logging.getLogger("calibration_seed")


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
    connect: Callable[[str], SeedClient] = Redis.from_url,
) -> int:
    del argv  # no options; kept so the signature matches a console entrypoint
    url = environ.get(REDIS_URL_VARIABLE, DEFAULT_REDIS_URL)
    try:
        keyspace = seed(connect(url))
    except RedisError as exc:
        print(f"calibration seed failed: cannot write to Redis at {url}: {exc}", file=sys.stderr)
        return 1
    _log.info("seeded %d instruments into %s", len(keyspace.sets[SYMBOLS_KEY]), url)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main(sys.argv[1:]))
