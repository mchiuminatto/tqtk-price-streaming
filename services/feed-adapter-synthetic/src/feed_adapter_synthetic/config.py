"""12-factor configuration for `feed-adapter-synthetic`.

Extends `tqtk_common.ServiceConfig` with the fields this service alone needs: the per-symbol tick
pacing multiplier (the `synthetic-feed` capability's "Configurable tick pacing" requirement), the
Redis URL it publishes to, and the PostgreSQL URL and password file it loads calibration from.
Neither connection is a universal field on the shared base, since not every service touches the
bus or the database (see `tqtk_common.config`'s module docstring).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from tqtk_common import ServiceConfig

__all__ = ["FeedConfig"]


class FeedConfig(ServiceConfig):
    service_name: str = "feed-adapter-synthetic"

    # Multiplier against every interval drawn from a symbol's fitted tick-interval distribution
    # (the synthetic-feed spec's "Configurable tick pacing"): 1.0 (default) publishes at the
    # distribution's own timescale, >1.0 speeds it up, <1.0 slows it down. Positive:
    # a zero or negative multiplier is a misconfiguration, not a valid "publish nothing" mode -
    # stopping the adapter is a deployment action, not a config value.
    tick_rate_per_symbol: float = Field(default=1.0, gt=0)

    # The bus ticks are published to. Calibration lives in PostgreSQL, below.
    redis_url: str = "redis://localhost:6379/0"

    # The calibration store, read once at startup. No password in the URL: it comes from
    # `postgres_password_file` (the stack's `_FILE` convention, a Compose secret), whose stripped
    # contents are passed to psycopg separately, so the secret stays out of the environment and
    # out of any logged URL. `None` connects without a password.
    postgres_url: str = "postgresql://tqtk@localhost:5432/tqtk"
    postgres_password_file: Path | None = None
