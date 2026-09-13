"""12-factor configuration for `feed-adapter-synthetic`.

Extends `tqtk_common.ServiceConfig` with the two fields this service alone needs: the per-symbol
tick pacing multiplier (the `synthetic-feed` capability's "Configurable tick pacing" requirement)
and the Redis URL it publishes to — Redis is not a universal field on the shared base, since not
every service touches the bus (see `tqtk_common.config`'s module docstring).
"""

from __future__ import annotations

from pydantic import Field

from tqtk_common import ServiceConfig

__all__ = ["FeedConfig"]


class FeedConfig(ServiceConfig):
    service_name: str = "feed-adapter-synthetic"

    # Multiplier against each symbol's sample-derived mean tick interval (see design.md,
    # "Synthetic price/tick-interval generation calibrated from sample data"): 1.0 (default)
    # publishes at the sample's own mean cadence, >1.0 speeds it up, <1.0 slows it down. Positive:
    # a zero or negative multiplier is a misconfiguration, not a valid "publish nothing" mode -
    # stopping the adapter is a deployment action, not a config value.
    tick_rate_per_symbol: float = Field(default=1.0, gt=0)

    redis_url: str = "redis://localhost:6379/0"
