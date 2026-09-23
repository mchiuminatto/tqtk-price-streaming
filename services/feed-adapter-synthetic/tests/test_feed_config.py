"""Verification for `move-calibration-to-redis` task 2.5: the calibration store adds no config
field - it is the same Redis as the bus - and `FeedConfig` still rejects a mistyped field."""

from __future__ import annotations

import pytest
from feed_adapter_synthetic.config import FeedConfig
from pydantic import ValidationError


def test_one_redis_url_serves_bus_and_calibration_store() -> None:
    assert set(FeedConfig.model_fields) - {"service_name", "runtime_host", "runtime_port"} == {
        "log_level",
        "tick_rate_per_symbol",
        "redis_url",
    }


def test_unknown_keyword_argument_is_rejected() -> None:
    with pytest.raises(ValidationError, match="calibration_redis_url"):
        FeedConfig(calibration_redis_url="redis://elsewhere/0")
