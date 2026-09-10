"""Verification for task 3.1: `/health` answers immediately, `/ready` only once connected.

The `service-runtime` capability's readiness scenario is the one that matters here - a service
that has started but has not reached Redis is alive and not ready - so the tests drive a real
server over a real socket rather than calling the handler directly. The two endpoints have to be
separable by an orchestrator's probe, which reads a status line, not a body.
"""

from __future__ import annotations

import json
import logging
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from unittest import mock

import pytest
from prometheus_client import CollectorRegistry, Counter

from tqtk_common import Readiness, RuntimeServer
from tqtk_common.server import _Handler


def get(port: int, path: str) -> tuple[int, dict]:
    """A probe's-eye view: the status code and the JSON body, error responses included."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture
def redis_dependent():
    """A service declaring one dependency, serving before it has connected to it."""
    readiness = Readiness("redis")
    # Port 0: the OS picks a free one, so a test run never collides with a real service.
    with RuntimeServer(readiness, host="127.0.0.1", port=0) as server:
        yield server


# --- liveness and readiness are separate answers ----------------------------------------------


def test_health_responds_before_any_dependency_connects(redis_dependent):
    assert get(redis_dependent.port, "/health") == (HTTPStatus.OK, {"status": "alive"})


def test_ready_reports_not_ready_before_any_dependency_connects(redis_dependent):
    status, body = get(redis_dependent.port, "/ready")
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert body == {"ready": False, "dependencies": {"redis": False}}


def test_ready_reports_ready_once_the_dependency_connects(redis_dependent):
    redis_dependent.readiness.mark_connected("redis")

    status, body = get(redis_dependent.port, "/ready")
    assert status == HTTPStatus.OK
    assert body == {"ready": True, "dependencies": {"redis": True}}


def test_health_still_reports_alive_while_not_ready(redis_dependent):
    """The distinction the endpoints exist for: not-ready is not a reason to restart."""
    redis_dependent.readiness.mark_disconnected("redis")

    assert get(redis_dependent.port, "/health")[0] == HTTPStatus.OK
    assert get(redis_dependent.port, "/ready")[0] == HTTPStatus.SERVICE_UNAVAILABLE


def test_ready_falls_back_when_a_connected_dependency_drops(redis_dependent):
    redis_dependent.readiness.mark_connected("redis")
    redis_dependent.readiness.mark_disconnected("redis")

    assert get(redis_dependent.port, "/ready")[0] == HTTPStatus.SERVICE_UNAVAILABLE


def test_ready_needs_every_dependency():
    readiness = Readiness("redis", "postgres")
    with RuntimeServer(readiness, host="127.0.0.1", port=0) as server:
        readiness.mark_connected("redis")
        assert get(server.port, "/ready")[0] == HTTPStatus.SERVICE_UNAVAILABLE

        readiness.mark_connected("postgres")
        assert get(server.port, "/ready")[0] == HTTPStatus.OK


def test_a_service_declaring_no_dependencies_is_ready_at_once():
    """`historical-query-svc` aside, most services have dependencies - but none is not not-ready."""
    with RuntimeServer(Readiness(), host="127.0.0.1", port=0) as server:
        assert get(server.port, "/ready") == (HTTPStatus.OK, {"ready": True, "dependencies": {}})


# --- the readiness state itself ---------------------------------------------------------------


def test_dependencies_start_disconnected():
    assert Readiness("redis").snapshot() == {"redis": False}
    assert not Readiness("redis").is_ready


def test_marking_is_idempotent():
    readiness = Readiness("redis")
    readiness.mark_connected("redis")
    readiness.mark_connected("redis")

    assert readiness.is_ready


def test_an_undeclared_dependency_is_a_typo_not_a_new_dependency():
    readiness = Readiness("redis")

    with pytest.raises(KeyError, match="postgres"):
        readiness.mark_connected("postgres")

    assert readiness.snapshot() == {"redis": False}


def test_declaring_the_same_dependency_twice_is_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        Readiness("redis", "redis")


def test_snapshot_does_not_alias_the_internal_state():
    readiness = Readiness("redis")
    readiness.snapshot()["redis"] = True

    assert not readiness.is_ready


# --- server mechanics -------------------------------------------------------------------------


def test_an_unknown_path_is_a_json_404(redis_dependent):
    status, body = get(redis_dependent.port, "/nope")
    assert status == HTTPStatus.NOT_FOUND
    assert body["path"] == "/nope"


def test_a_query_string_does_not_change_the_route(redis_dependent):
    assert get(redis_dependent.port, "/health?verbose=1")[0] == HTTPStatus.OK


def test_head_returns_the_status_without_a_body(redis_dependent):
    request = urllib.request.Request(
        f"http://127.0.0.1:{redis_dependent.port}/health", method="HEAD"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == HTTPStatus.OK
        assert response.read() == b""
        assert int(response.headers["Content-Length"]) > 0


def test_stop_releases_the_port_and_is_idempotent():
    server = RuntimeServer(Readiness(), host="127.0.0.1", port=0)
    server.start()
    port = server.port
    server.stop()
    server.stop()

    with pytest.raises(RuntimeError, match="not started"):
        _ = server.port
    with pytest.raises(urllib.error.URLError):
        get(port, "/health")


def test_starting_twice_is_rejected(redis_dependent):
    with pytest.raises(RuntimeError, match="already started"):
        redis_dependent.start()


# --- a failure answers, rather than dropping the connection and printing to stderr -------------


class _BrokenCollector:
    """A collector that raises at scrape time - the shape a consumer-lag collector can take."""

    def collect(self):
        raise RuntimeError("collector blew up")


@pytest.fixture
def broken_metrics():
    registry = CollectorRegistry()
    registry.register(_BrokenCollector())
    with RuntimeServer(Readiness(), host="127.0.0.1", port=0, registry=registry) as server:
        yield server


def test_a_failing_collector_answers_500_instead_of_an_empty_socket(broken_metrics, caplog):
    """A scrape is wanted most when something is wrong, so the endpoint has to answer."""
    with caplog.at_level(logging.ERROR):
        status, body = get(broken_metrics.port, "/metrics")

    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert body == {"error": "internal error", "path": "/metrics"}


def test_a_failing_request_is_logged_through_logging_not_stderr(broken_metrics, caplog, capfd):
    """The traceback belongs on the structured stream `configure_logging` sets up."""
    with caplog.at_level(logging.ERROR):
        get(broken_metrics.port, "/metrics")

    assert "runtime request failed" in caplog.text
    assert "collector blew up" in caplog.text
    assert "Traceback" not in capfd.readouterr().err


def test_the_probes_still_answer_while_the_registry_is_broken(broken_metrics):
    """A broken collector must not take liveness and readiness down with it."""
    assert get(broken_metrics.port, "/health") == (HTTPStatus.OK, {"status": "alive"})
    assert get(broken_metrics.port, "/ready")[0] == HTTPStatus.OK


def _wait_until(condition, timeout: float = 5.0) -> None:
    """Poll `condition` until it holds, or `timeout` elapses. Never asserts - the caller does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        time.sleep(0.01)


def _abort(port: int, request: bytes | None) -> None:
    """Connect, optionally send `request`, then RST rather than close cleanly."""
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    if request is not None:
        client.sendall(request)
    # SO_LINGER 0 makes close() send RST, so the server's read or write fails rather than
    # draining into a peer that shut down politely.
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    client.close()


def test_a_client_hanging_up_mid_response_is_not_a_traceback(capfd):
    """A scraper that hits its own timeout is routine traffic, not an error."""
    registry = CollectorRegistry()
    # A body too large to land in one socket buffer, so the write outlives the client.
    for index in range(300):
        counter = Counter(f"filler_{index}_total", "x" * 200, ["a"], registry=registry)
        counter.labels(a="y" * 200).inc()

    baseline = threading.active_count()
    with RuntimeServer(Readiness(), host="127.0.0.1", port=0, registry=registry) as server:
        _abort(server.port, b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n")

        # Wait for the handler thread to appear before waiting for it to go. Reading stderr
        # against a thread that has not run yet finds it empty and passes for the wrong reason -
        # which is how an earlier version of this test missed the bug it exists to catch, on
        # roughly a third of runs. Both waits are relative to a captured baseline rather than a
        # hardcoded count, so an unrelated thread elsewhere in the suite cannot skew them.
        _wait_until(lambda: threading.active_count() > baseline + 1)
        _wait_until(lambda: threading.active_count() <= baseline + 1)

    assert "Traceback" not in capfd.readouterr().err


def test_a_client_aborting_before_it_sends_a_request_is_not_an_error(caplog):
    """The read-side twin of the test above: the same abort, caught in a different place.

    `_send` classifies a disconnect as DEBUG; `handle_error` has to agree, or the severity of a
    port scan depends on whether the server had started writing when the client went away.
    """
    baseline = threading.active_count()
    with (
        RuntimeServer(Readiness(), host="127.0.0.1", port=0) as server,
        caplog.at_level(logging.DEBUG),
    ):
        _abort(server.port, None)  # never sends a byte
        _abort(server.port, b"GET /heal")  # partial request line

        _wait_until(lambda: threading.active_count() <= baseline + 1)

    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert any("client disconnected" in record.message for record in caplog.records)


def test_a_genuine_failure_outside_the_handler_is_still_an_error(caplog):
    """The disconnect exemption must not swallow everything else `handle_error` is there for."""
    baseline = threading.active_count()
    with (
        RuntimeServer(Readiness(), host="127.0.0.1", port=0) as server,
        caplog.at_level(logging.DEBUG),
        mock.patch.object(_Handler, "handle_one_request", side_effect=RuntimeError("boom")),
    ):
        _abort(server.port, b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        _wait_until(lambda: threading.active_count() <= baseline + 1)

    assert any(
        record.levelno == logging.ERROR and "runtime connection failed" in record.message
        for record in caplog.records
    )


# --- an idle connection does not own a thread forever -----------------------------------------


def test_the_handler_declares_a_bounded_idle_timeout():
    """The regression guard for the fix itself.

    The stdlib's default is `None` - no timeout - which combined with HTTP/1.1 keep-alive and a
    thread per connection is what let an idle client pin a thread forever. The two tests below
    exercise the reclaim mechanism at a test-speed value, so this is the one that fails if the
    shipped default goes away.
    """
    assert _Handler.timeout is not None
    assert 0 < _Handler.timeout <= 60


def _threads_held_after_idling(monkeypatch, connect) -> int:
    """Open connections via `connect`, leave them idle, and report the threads still held.

    Patches the timeout down to keep the suite fast; `test_the_handler_declares_a_bounded_idle_
    timeout` is what pins the real value. What this exercises is that the timeout is wired to
    the connection at all - that `_Handler` is where it belongs and `handle_one_request` acts on it.
    """
    monkeypatch.setattr(_Handler, "timeout", 0.3)

    baseline = threading.active_count()
    with RuntimeServer(Readiness(), host="127.0.0.1", port=0) as server:
        clients = [connect(server.port) for _ in range(20)]

        # Every connection is open and left that way, as a keep-alive client leaves it.
        assert threading.active_count() > baseline

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and threading.active_count() > baseline + 1:
            time.sleep(0.05)
        # The serve-loop thread itself remains; the connection threads must not.
        held = threading.active_count() - baseline - 1

        for client in clients:
            client.close()
    return held


def test_an_idle_keepalive_connection_does_not_pin_a_thread(monkeypatch):
    """A client that made its request, read the answer, and kept the connection open."""

    def answered(port: int) -> socket.socket:
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        client.recv(4096)
        return client

    assert _threads_held_after_idling(monkeypatch, answered) == 0


def test_a_connection_that_sends_nothing_is_also_reclaimed(monkeypatch):
    """The slowloris shape: a thread held by a client that never sends a byte."""

    def silent(port: int) -> socket.socket:
        return socket.create_connection(("127.0.0.1", port), timeout=5)

    assert _threads_held_after_idling(monkeypatch, silent) == 0
