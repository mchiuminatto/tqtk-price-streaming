"""Statistically-calibrated synthetic tick generator publishing raw ticks to
ticks.raw.synthetic.{symbol}."""

from feed_adapter_synthetic.calibration import SymbolCalibration
from feed_adapter_synthetic.calibration_store import CalibrationError, CalibrationStore
from feed_adapter_synthetic.config import FeedConfig
from feed_adapter_synthetic.generator import TickSink, run_synthetic_feed
from feed_adapter_synthetic.sinks import RedisTickSink

__version__ = "0.1.0"

__all__ = [
    "CalibrationError",
    "CalibrationStore",
    "FeedConfig",
    "RedisTickSink",
    "SymbolCalibration",
    "TickSink",
    "run_synthetic_feed",
]
