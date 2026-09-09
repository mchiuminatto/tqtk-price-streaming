"""Verification for task 3.4: lines parse as JSON, and high-volume detail stays off INFO.

The second half is the one worth being careful about. "No per-tick line at INFO" cannot be
asserted against a tick here - the services that process ticks arrive in sections 5 and 6 - but
the rule is not really about ticks, it is about the highest-frequency event a service handles.
In this package that is an HTTP request to the runtime endpoints, a few per second forever from
probes and scrapes, and `RuntimeServer` is written to the same rule the tick paths will follow.
So the tests drive real requests and assert the shape: nothing at INFO, everything at DEBUG.
"""

from __future__ import annotations

import json
import logging
import urllib.request

import pytest

from tqtk_common import Readiness, RuntimeServer, ServiceConfig, configure_logging


@pytest.fixture(autouse=True)
def restore_root_logger():
    """Logging config is process-global; leaving a handler behind would follow other tests out."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def logs(capsys):
    """Configure logging as a service would, and read back the lines it wrote to stdout."""

    def configure(level="INFO", service_name="example-svc"):
        configure_logging(ServiceConfig(service_name=service_name, log_level=level))

        def read() -> list[dict]:
            out = capsys.readouterr().out
            return [json.loads(line) for line in out.splitlines() if line]

        return read

    return configure


# --- a line is JSON, with the fields a query needs --------------------------------------------


def test_a_log_line_parses_as_json(logs):
    """The task's own check."""
    read = logs()

    logging.getLogger("tqtk_common.example").info("service started")

    (line,) = read()
    assert line["message"] == "service started"
    assert line["level"] == "INFO"
    assert line["logger"] == "tqtk_common.example"
    assert line["service"] == "example-svc"
    assert line["timestamp"].startswith("20")


def test_extra_fields_are_merged_into_the_line(logs):
    """`RuntimeServer` already logs this way, so the formatter has to carry `extra` through."""
    read = logs()

    logging.getLogger("x").info("dependency connected", extra={"dependency": "redis"})

    (line,) = read()
    assert line["dependency"] == "redis"


def test_printf_style_arguments_are_rendered_into_the_message(logs):
    read = logs()

    logging.getLogger("x").info("bound to %s:%d", "0.0.0.0", 8000)

    assert read()[0]["message"] == "bound to 0.0.0.0:8000"


def test_the_formatters_own_fields_win_a_name_collision(logs):
    """A line whose `level` disagrees with the level it was logged at would mislead a query."""
    read = logs()

    logging.getLogger("x").info("odd", extra={"level": "CRITICAL", "service": "not-this"})

    (line,) = read()
    assert line["level"] == "INFO"
    assert line["service"] == "example-svc"


def test_an_exception_is_carried_as_one_line(logs):
    """A collector splits on newlines, so a traceback has to be a field, not extra lines."""
    read = logs()

    try:
        raise ValueError("redis is unreachable")
    except ValueError:
        logging.getLogger("x").exception("connection failed")

    lines = read()
    assert len(lines) == 1
    assert "ValueError: redis is unreachable" in lines[0]["exception"]
    assert lines[0]["level"] == "ERROR"


def test_an_unserializable_extra_does_not_lose_the_line(logs):
    """A formatter that raises costs the line and prints a traceback from inside `logging`."""
    read = logs()

    logging.getLogger("x").info("checkpoint written", extra={"at": object()})

    assert read()[0]["message"] == "checkpoint written"


def test_logs_go_to_stdout_not_stderr(capsys):
    """`logging` defaults to stderr; the capability puts these on stdout."""
    configure_logging(ServiceConfig(service_name="example-svc"))

    logging.getLogger("x").info("service started")

    captured = capsys.readouterr()
    assert json.loads(captured.out)["message"] == "service started"
    assert captured.err == ""


# --- level separation: the INFO stream stays readable -----------------------------------------


def test_debug_detail_is_absent_at_info(logs):
    read = logs(level="INFO")

    logging.getLogger("x").debug("tick 41.9231/41.9233 seq=7719")
    logging.getLogger("x").info("batch persisted")

    assert [line["message"] for line in read()] == ["batch persisted"]


def test_the_same_detail_is_available_at_debug(logs):
    """One `TQTK_LOG_LEVEL=DEBUG` away, which is what makes the INFO discipline affordable."""
    read = logs(level="DEBUG")

    logging.getLogger("x").debug("tick 41.9231/41.9233 seq=7719")

    assert read()[0]["level"] == "DEBUG"


def test_the_configured_level_comes_from_the_environment(monkeypatch, capsys):
    """The 12-factor loop closed: an operator raises the level without a code change."""
    monkeypatch.setenv("TQTK_LOG_LEVEL", "WARNING")
    configure_logging(ServiceConfig(service_name="example-svc"))

    logging.getLogger("x").info("service started")
    logging.getLogger("x").warning("consumer lag growing")

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert [line["level"] for line in lines] == ["WARNING"]


def test_configuring_twice_does_not_duplicate_every_line(logs):
    logs()
    read = logs()

    logging.getLogger("x").info("service started")

    assert len(read()) == 1


# --- the rule applied to this package's own high-frequency path --------------------------------


@pytest.fixture
def server():
    with RuntimeServer(Readiness(), host="127.0.0.1", port=0) as running:
        yield running


def probe(server) -> None:
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/health", timeout=5) as response:
        response.read()


def test_a_served_request_logs_nothing_at_info(logs, server):
    """The stand-in for a tick: the event this package handles a few times a second, forever."""
    read = logs(level="INFO")

    probe(server)

    assert read() == []


def test_the_same_request_is_visible_at_debug(logs, server):
    read = logs(level="DEBUG")

    probe(server)

    assert any("/health" in line["message"] for line in read())


def test_lifecycle_and_connection_changes_are_the_events_at_info(logs):
    """Two of the four categories the capability names, from the code that exists today."""
    read = logs(level="INFO")

    with RuntimeServer(Readiness("redis"), host="127.0.0.1", port=0) as running:
        running.readiness.mark_connected("redis")

    messages = [line["message"] for line in read()]
    assert "runtime server listening" in messages
    assert "dependency connected" in messages
    assert "runtime server stopped" in messages


def test_a_connection_change_carries_the_dependency_as_a_field(logs):
    read = logs(level="INFO")

    readiness = Readiness("redis")
    with RuntimeServer(readiness, host="127.0.0.1", port=0):
        readiness.mark_connected("redis")

    connected = next(line for line in read() if line["message"] == "dependency connected")
    assert connected["dependency"] == "redis"
    assert connected["connected"] is True
