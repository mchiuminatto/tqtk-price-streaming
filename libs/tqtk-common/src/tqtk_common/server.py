"""The shared `/health`, `/ready` and `/metrics` HTTP server every service runs.

The `service-runtime` capability draws one line and this module exists to hold it: `/health`
answers "is this process alive", `/ready` answers "has it finished connecting to what it needs".
A service that has started but has not reached Redis yet is alive and not ready, and an
orchestrator reads those two answers differently - restart the container on the first, withhold
traffic on the second. Collapsing them would turn a transient reconnect into a restart loop.

Readiness is therefore declared, not discovered: a service names its dependencies when it builds
`Readiness`, and each one starts disconnected. Registering them lazily as they connect would make
an empty set indistinguishable from a fully-connected one, so the window this endpoint exists to
report - startup, before anything is connected - is exactly the window that would read as ready.

The server is the standard library's, on a daemon thread, rather than an async framework sharing
the service's event loop. Two reasons: the pipeline holds a <10 ms tick-to-bar budget, and probe
and scrape traffic that never touches the loop cannot contend with it - including when the loop is
the thing that has stalled, which is the moment a probe most needs to answer. And `tqtk-common` is
a dependency of all six services, so a web framework here is a framework in every image.

The same server carries `/metrics`, for the same reason: a scrape is the one request that has
to answer while the event loop is busy or wedged, since that is when the numbers are needed.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final, Self
from urllib.parse import urlsplit

from prometheus_client import CollectorRegistry

from tqtk_common.metrics import METRICS_CONTENT_TYPE, METRICS_PATH, default_registry, render

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HEALTH_PATH",
    "READY_PATH",
    "Readiness",
    "RuntimeServer",
]

_log = logging.getLogger(__name__)

# Matches the port every service's Dockerfile exposes.
DEFAULT_PORT: Final = 8000

# Probes and scrapes arrive from outside the container, so binding loopback would make the
# endpoints unreachable by the only callers they have.
DEFAULT_HOST: Final = "0.0.0.0"  # bind-all is deliberate, not an oversight

HEALTH_PATH: Final = "/health"
READY_PATH: Final = "/ready"

_JSON_CONTENT_TYPE: Final = "application/json"

# What a route resolves to: a status, a rendered body, and the type that body is in.
_Response = tuple[HTTPStatus, bytes, str]

_JOIN_TIMEOUT_SECONDS: Final = 5.0

# How long `stop` can wait on the serve loop to notice it. The stdlib's 0.5 s default is
# half a second added to every shutdown, including a container's termination grace period.
_POLL_INTERVAL_SECONDS: Final = 0.05

# How long a connection may sit idle before the server reclaims it. HTTP/1.1 keeps a connection
# open after the response and `ThreadingHTTPServer` gives every connection a thread, so without
# this an idle client holds a thread for as long as it likes and nothing bounds how many. Well
# above any real probe or scrape interval, so it only ever fires on a connection that has gone
# quiet - including one that never sent a byte.
_IDLE_CONNECTION_TIMEOUT_SECONDS: Final = 10.0

# What a client hanging up raises, wherever it lands. A scraper that hit its own timeout is
# routine traffic rather than an error, and it is the same event whether it aborted before the
# request was parsed or midway through the response - so both sites read it from here rather
# than each keeping their own list to drift apart.
_CLIENT_DISCONNECT_ERRORS: Final = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class Readiness:
    """The connection state of one service's dependencies, as `/ready` reports it.

    Mutated from the service's event loop and read from the server thread, so every access is
    under a lock. Names are fixed at construction: `mark_connected` on an undeclared name is a
    typo, and raising beats silently tracking a dependency nothing waits on.
    """

    __slots__ = ("_connected", "_lock")

    def __init__(self, *dependencies: str) -> None:
        if len(set(dependencies)) != len(dependencies):
            raise ValueError(f"duplicate dependency name in {dependencies}")
        self._lock = threading.Lock()
        self._connected = dict.fromkeys(dependencies, False)

    def mark_connected(self, dependency: str) -> None:
        """Record that `dependency` is connected. Idempotent."""
        self._set(dependency, connected=True)

    def mark_disconnected(self, dependency: str) -> None:
        """Record that `dependency` is no longer connected. Idempotent."""
        self._set(dependency, connected=False)

    def _set(self, dependency: str, *, connected: bool) -> None:
        with self._lock:
            if dependency not in self._connected:
                raise KeyError(
                    f"{dependency!r} is not a declared dependency; "
                    f"declared: {sorted(self._connected)}"
                )
            changed = self._connected[dependency] != connected
            self._connected[dependency] = connected
        # Connection-state changes are INFO per the observability capability - the transition
        # only, so a flapping dependency logs its flaps and a steady one stays quiet.
        if changed:
            _log.info(
                "dependency %s",
                "connected" if connected else "disconnected",
                extra={"dependency": dependency, "connected": connected},
            )

    @property
    def is_ready(self) -> bool:
        """Whether every declared dependency is connected. A service declaring none is ready."""
        with self._lock:
            return all(self._connected.values())

    def snapshot(self) -> dict[str, bool]:
        """The per-dependency state, read atomically so `/ready` can't report a torn view."""
        with self._lock:
            return dict(self._connected)


class _RuntimeHTTPServer(ThreadingHTTPServer):
    """A `ThreadingHTTPServer` carrying the state its handlers report on."""

    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], readiness: Readiness, registry: CollectorRegistry
    ) -> None:
        super().__init__(address, _Handler)
        self.readiness = readiness
        self.registry = registry

    def handle_error(self, request: object, client_address: object) -> None:
        # socketserver's default prints a traceback straight to stderr, unstructured - the one
        # channel `configure_logging` exists to empty. `_Handler` answers its own failures; this
        # is the backstop for anything raised outside it.
        if isinstance(sys.exc_info()[1], _CLIENT_DISCONNECT_ERRORS):
            # The client aborted before there was a response to write, so `_send` never saw it.
            # No more an error here than there: an aborting probe or a port scan is not an event
            # worth waking anyone for, and ERROR is what an operator alerts on.
            _log.debug("client disconnected", extra={"client": str(client_address)})
            return
        _log.exception("runtime connection failed", extra={"client": str(client_address)})


def _json(status: HTTPStatus, payload: dict[str, Any]) -> _Response:
    return status, json.dumps(payload).encode() + b"\n", _JSON_CONTENT_TYPE


class _Handler(BaseHTTPRequestHandler):
    """Routes the runtime endpoints. The probes answer JSON, `/metrics` its own text format."""

    server: _RuntimeHTTPServer  # narrowed from BaseServer for the readiness and registry
    protocol_version = "HTTP/1.1"
    server_version = "tqtk-runtime/1.0"

    # The stdlib's default is None - no timeout at all, so a kept-alive connection pins its
    # thread until the client chooses to close. `handle_one_request` turns the TimeoutError this
    # produces into a close, and logs it through `log_error` at DEBUG like any other request line.
    timeout = _IDLE_CONNECTION_TIMEOUT_SECONDS

    def do_GET(self) -> None:  # the stdlib's dispatch name, not ours to rename
        self._respond(with_body=True)

    def do_HEAD(self) -> None:  # the stdlib's dispatch name, not ours to rename
        # Some probes send HEAD; the base class does not derive it from do_GET.
        self._respond(with_body=False)

    def _respond(self, *, with_body: bool) -> None:
        """Route and write, answering a failure in either rather than raising into socketserver."""
        try:
            response = self._route()
        except Exception:  # noqa: BLE001 - a request boundary catches everything by definition
            # Letting this propagate would close the socket with no status line written - the
            # client sees an empty response, and the reason goes to stderr as a traceback. A
            # scrape is wanted most when something is wrong, so the endpoint answers instead.
            _log.exception("runtime request failed", extra={"path": self.path})
            response = _json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error", "path": self.path}
            )
        self._send(*response, with_body=with_body)

    def _route(self) -> _Response:
        path = urlsplit(self.path).path
        if path == HEALTH_PATH:
            # Reached only by a process that is running and whose server thread is serving,
            # which is the whole of what liveness claims.
            return _json(HTTPStatus.OK, {"status": "alive"})
        if path == READY_PATH:
            dependencies = self.server.readiness.snapshot()
            ready = all(dependencies.values())
            # 503 rather than 200-with-a-flag: an orchestrator's probe reads the status line.
            status = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
            return _json(status, {"ready": ready, "dependencies": dependencies})
        if path == METRICS_PATH:
            # Reading in-memory values on this thread - the scrape never reaches the event loop.
            return HTTPStatus.OK, render(self.server.registry), METRICS_CONTENT_TYPE
        return _json(HTTPStatus.NOT_FOUND, {"error": "not found", "path": path})

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, *, with_body: bool) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            # Sent on HEAD too: it describes the body a GET would return.
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if with_body:
                self.wfile.write(body)
        except _CLIENT_DISCONNECT_ERRORS:
            # A scraper that hit its own timeout and hung up. Routine rather than an error, so
            # DEBUG with the per-request lines - and handled here so it is not a traceback.
            _log.debug("client disconnected before the response was written")
            self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        # The stdlib writes a line per request to stderr, unstructured. Probes and scrapes are a
        # few per second forever, so they belong at DEBUG, through `configure_logging` - not on
        # a second, plaintext channel.
        _log.debug("%s %s", self.address_string(), format % args)


class RuntimeServer:
    """The runtime endpoints, served on a daemon thread for the life of the service.

    Start it before connecting anything: that is what makes the not-ready window observable
    rather than a gap where nothing answers at all.

    `registry` defaults to the client library's, the one a module-level `Counter(...)` lands in,
    so a service instruments itself without handing anything to this class. Tests pass their own.
    """

    __slots__ = ("_address", "_http", "_readiness", "_registry", "_thread")

    def __init__(
        self,
        readiness: Readiness,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        registry: CollectorRegistry | None = None,
    ) -> None:
        self._readiness = readiness
        self._registry = default_registry() if registry is None else registry
        self._address = (host, port)
        self._http: _RuntimeHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def readiness(self) -> Readiness:
        return self._readiness

    @property
    def registry(self) -> CollectorRegistry:
        """The registry `/metrics` renders - a service's own metrics go here."""
        return self._registry

    @property
    def port(self) -> int:
        """The bound port - the resolved one when constructed with port 0, as tests do."""
        if self._http is None:
            raise RuntimeError("server is not started")
        return self._http.server_address[1]

    def start(self) -> None:
        if self._http is not None:
            raise RuntimeError("server is already started")
        # Binding here rather than on the thread means a port clash raises out of `start`, to the
        # caller that can fail the startup, instead of killing a thread nobody is watching.
        self._http = _RuntimeHTTPServer(self._address, self._readiness, self._registry)
        self._thread = threading.Thread(
            target=self._http.serve_forever,
            args=(_POLL_INTERVAL_SECONDS,),
            name="tqtk-runtime-server",
            daemon=True,
        )
        self._thread.start()
        _log.info("runtime server listening", extra={"port": self.port})

    def stop(self) -> None:
        """Stop serving and release the port. Idempotent."""
        if self._http is None:
            return
        http, thread = self._http, self._thread
        self._http, self._thread = None, None
        http.shutdown()
        http.server_close()
        if thread is not None:
            thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        _log.info("runtime server stopped")

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
