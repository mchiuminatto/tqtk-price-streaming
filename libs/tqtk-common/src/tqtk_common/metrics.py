"""The `/metrics` scaffolding: a registry to record into and its Prometheus rendering.

The observability capability puts the whole latency story on a pull: `recv_ts` is already in the
Tick payload, so every hop computes `now() - recv_ts` locally and records it into an in-process
histogram - an in-memory increment, no network call - which Prometheus then scrapes on its own
schedule. This module is the scraped half of that arrangement; the metrics themselves land here
as later tasks instrument the services (`tick_to_bar_latency_ms` in 6.9, `late_ticks_total` in
6.5, consumer lag in the consumer-group services).

Two decisions worth naming, because both are about what a service has to do to participate:

Metrics go in the client library's default registry rather than one this package owns. A service
declares `Counter("late_ticks_total", ...)` at import time and it is exposed - no registration
call to forget, and no second registry to keep in sync. That default also carries the process and
interpreter collectors (`process_resident_memory_bytes`, `python_gc_*`), which are the metrics
worth having before any custom one exists. Tests pass their own `CollectorRegistry` instead, so
one test's counters can't leak into another's scrape.

Rendering is a read of in-memory values, so it runs on the runtime server's thread with no
handoff to the service's event loop. A scrape therefore cannot contend with the tick path, and
still answers when that path is the thing that has stalled - which is when a scrape matters most.
"""

from __future__ import annotations

from typing import Final

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest

__all__ = [
    "METRICS_CONTENT_TYPE",
    "METRICS_PATH",
    "default_registry",
    "render",
]

METRICS_PATH: Final = "/metrics"

# The exposition format's own content type, version parameter included - Prometheus negotiates on
# it, so it is the library's constant rather than a bare "text/plain".
METRICS_CONTENT_TYPE: Final[str] = CONTENT_TYPE_LATEST


def default_registry() -> CollectorRegistry:
    """The registry a bare `Counter(...)`/`Histogram(...)` registers itself into."""
    return REGISTRY


def render(registry: CollectorRegistry) -> bytes:
    """Render `registry` as Prometheus text exposition, ready to write to a scrape."""
    return generate_latest(registry)
