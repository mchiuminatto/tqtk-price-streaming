"""Verification for task 3.2: `/metrics` returns valid Prometheus text format.

"Valid" is not a claim a substring check can make, so every test that says the endpoint is
well-formed hands the body to the client library's own parser - the same code path a scrape is
read by. The empty-registry case is the one the task names: with no custom metrics registered
yet, the endpoint still has to answer, and answer in a format Prometheus accepts.

Each test brings its own `CollectorRegistry`. The default registry is process-global, so metrics
one test declares would otherwise show up in another's scrape, and a re-run in the same process
would collide on the metric name.
"""

from __future__ import annotations

import urllib.request
from http import HTTPStatus

import pytest
from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.parser import text_string_to_metric_families

from tqtk_common import METRICS_CONTENT_TYPE, METRICS_PATH, Readiness, RuntimeServer
from tqtk_common.metrics import default_registry


def scrape(port: int) -> tuple[int, str, str]:
    """A scraper's-eye view: status, declared content type, and the exposition body."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{METRICS_PATH}", timeout=5) as response:
        return response.status, response.headers["Content-Type"], response.read().decode()


def families(body: str) -> dict[str, object]:
    """Parse the body as Prometheus text format, keyed by metric name. Raises if malformed."""
    return {family.name: family for family in text_string_to_metric_families(body)}


@pytest.fixture
def registry() -> CollectorRegistry:
    """An isolated registry - a service's would be the client library's default."""
    return CollectorRegistry()


@pytest.fixture
def server(registry):
    with RuntimeServer(Readiness(), host="127.0.0.1", port=0, registry=registry) as running:
        yield running


# --- the endpoint answers in the format Prometheus reads --------------------------------------


def test_metrics_is_valid_prometheus_text_with_nothing_registered(server):
    """The task's own check: scaffolding is scrapeable before any metric exists."""
    status, content_type, body = scrape(server.port)

    assert status == HTTPStatus.OK
    assert content_type == METRICS_CONTENT_TYPE
    assert families(body) == {}


def test_the_content_type_carries_the_exposition_format_version():
    # Prometheus negotiates on this, so "text/plain" alone would not be enough.
    assert METRICS_CONTENT_TYPE.startswith("text/plain")
    assert "version=" in METRICS_CONTENT_TYPE


def test_a_registered_counter_is_exposed_with_its_value(registry, server):
    late_ticks = Counter(
        "late_ticks_total", "Ticks dropped after their bar closed", ["symbol"], registry=registry
    )
    late_ticks.labels(symbol="EURUSD").inc()
    late_ticks.labels(symbol="EURUSD").inc()

    parsed = families(scrape(server.port)[2])

    assert parsed["late_ticks"].samples[0].labels == {"symbol": "EURUSD"}
    assert parsed["late_ticks"].samples[0].value == 2


def test_a_registered_histogram_is_exposed_with_its_buckets(registry, server):
    """The shape task 6.9 records `tick_to_bar_latency_ms` into."""
    latency = Histogram(
        "tick_to_bar_latency_ms", "Tick recv_ts to bar publish", buckets=[1, 10], registry=registry
    )
    latency.observe(0.5)

    samples = families(scrape(server.port)[2])["tick_to_bar_latency_ms"].samples
    buckets = {s.labels["le"]: s.value for s in samples if s.name.endswith("_bucket")}

    # Cumulative, as the format requires: a 0.5 ms observation falls in every bucket.
    assert buckets == {"1.0": 1, "10.0": 1, "+Inf": 1}
    assert next(s.value for s in samples if s.name.endswith("_count")) == 1


def test_a_scrape_reflects_metrics_recorded_after_the_server_started(registry, server):
    """Rendering reads the registry per request - it is not a snapshot taken at startup."""
    counter = Counter("bars_published_total", "Bars published", registry=registry)

    assert families(scrape(server.port)[2])["bars_published"].samples[0].value == 0

    counter.inc()
    assert families(scrape(server.port)[2])["bars_published"].samples[0].value == 1


# --- how a service gets its metrics onto the endpoint -----------------------------------------


def test_the_server_defaults_to_the_registry_a_bare_counter_lands_in():
    """A service declares `Counter(...)` at module level and it is exposed, with no wiring."""
    assert RuntimeServer(Readiness()).registry is default_registry()


def test_the_default_registry_carries_runtime_metrics_before_any_custom_one():
    """Part of why the default is the default: a service is worth scraping from its first day."""
    names = {metric.name for metric in default_registry().collect()}

    # The interpreter collectors rather than the `process_*` ones - those are Linux-only, and the
    # point here is that the default registry is not empty, not which platform this runs on.
    assert {"python_info", "python_gc_objects_collected"} <= names


# --- server mechanics -------------------------------------------------------------------------


def test_head_on_metrics_returns_the_status_without_a_body(server):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{METRICS_PATH}", method="HEAD")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == HTTPStatus.OK
        assert response.headers["Content-Type"] == METRICS_CONTENT_TYPE
        assert response.read() == b""


def test_a_query_string_does_not_change_the_route(server):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{server.port}{METRICS_PATH}?foo=1", timeout=5
    ) as response:
        assert response.status == HTTPStatus.OK
