"""Verification for task 3.1: `/health` answers immediately, `/ready` only once connected.

The `service-runtime` capability's readiness scenario is the one that matters here - a service
that has started but has not reached Redis is alive and not ready - so the tests drive a real
server over a real socket rather than calling the handler directly. The two endpoints have to be
separable by an orchestrator's probe, which reads a status line, not a body.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from http import HTTPStatus

import pytest

from tqtk_common import Readiness, RuntimeServer


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
