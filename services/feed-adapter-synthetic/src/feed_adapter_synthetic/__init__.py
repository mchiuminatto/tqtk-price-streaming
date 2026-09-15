"""Statistically-calibrated synthetic tick generator publishing raw ticks to
ticks.raw.synthetic.{symbol}."""

from feed_adapter_synthetic.calibration import SymbolCalibration, compute_calibration
from feed_adapter_synthetic.config import FeedConfig
from feed_adapter_synthetic.generator import TickSink, run_synthetic_feed
from feed_adapter_synthetic.sinks import RedisTickSink
from feed_adapter_synthetic.symbols import discover_symbols, find_data_dir, find_symbol_file

__version__ = "0.1.0"

__all__ = [
    "FeedConfig",
    "RedisTickSink",
    "SymbolCalibration",
    "TickSink",
    "compute_calibration",
    "discover_symbols",
    "find_data_dir",
    "find_symbol_file",
    "run_synthetic_feed",
]
