"""Structured JSON logging to stdout, configured once at service startup.

The `observability` capability asks for two things at once, and they pull in opposite directions.
Logs must be structured JSON so a line can be queried by field rather than grepped. And INFO must
stay readable - service lifecycle, connection-state changes, checkpoint writes, batch-persistence
completions - which means per-tick detail never appears there. At ~3,120 ticks/sec a single INFO
line per tick would bury the four events an operator actually watches for, and cost real time on
the path this design keeps free of I/O.

Only the first half is something a library can implement. The second is a rule about call sites,
so what this module does is make the rule cheap to follow: `logging`'s own levels do the
separation, and a service logs high-volume detail through `logger.debug`, which costs almost
nothing when DEBUG is off and is one `TQTK_LOG_LEVEL=DEBUG` away when someone is debugging.
`RuntimeServer` is already written this way - lifecycle and connection transitions at INFO, its
per-request line at DEBUG - and the tests assert that shape on it, since it is the one
high-frequency path that exists in this package today.

Fields a service passes as `extra={...}` are merged into the line at the top level, so
`dependency` is queried as `dependency` and not `context.dependency`. The formatter's own fields
win a name collision: a line whose `level` disagrees with the level it was logged at is worse
than a dropped field, and the reserved names are few and generic enough that a real collision
means the call site should pick a better key.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

from tqtk_common.config import ServiceConfig

__all__ = [
    "JSONFormatter",
    "configure_logging",
]

# Set on the handler this module installs, so configuring twice replaces its own handler rather
# than stacking a second one that duplicates every line.
_HANDLER_TAG: Final = "_tqtk_runtime_handler"

# What `logging` itself puts on a record. Anything else a record carries arrived through `extra`,
# which is how a caller's fields are told apart from the machinery's. Derived from a real record
# rather than hardcoded, so a new attribute in a future Python does not start showing up as a
# caller's field. `message` and `asctime` are added by formatters, never by the caller.
_STANDARD_ATTRS: Final = frozenset(
    logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__
) | {"message", "asctime"}


class JSONFormatter(logging.Formatter):
    """Renders a record as one JSON object per line, `extra` fields included."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRS and not key.startswith("_")
        }

        # Written after the extras so the formatter's own fields survive a name collision.
        payload.update(
            timestamp=datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            level=record.levelname,
            logger=record.name,
            service=self._service_name,
            message=record.getMessage(),
        )

        if record.exc_info:
            # The traceback as one string field: a log line stays one line, which is what a
            # collector splitting on newlines needs it to be.
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # `default=str` because a formatter must never be the thing that raises: a caller passing
        # a Decimal or a datetime in `extra` gets it stringified, not a lost line and a traceback
        # on stderr from inside `logging`.
        return json.dumps(payload, default=str)


def configure_logging(config: ServiceConfig) -> None:
    """Point the root logger at stdout with JSON formatting, at the configured level.

    Called once at startup, before anything worth logging happens. Safe to call again - the
    handler it installs replaces the one from a previous call rather than adding to it.
    """
    root = logging.getLogger()

    for existing in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(existing)
        existing.close()

    # stdout, not `logging`'s default of stderr: the capability puts logs on stdout, and a
    # container runtime treats the two streams differently.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(config.service_name))
    setattr(handler, _HANDLER_TAG, True)

    root.addHandler(handler)
    # On the root logger as well as the handler: the level a `logger.debug` call checks before
    # building its message is the logger's, and that cheap check is what keeps per-tick detail
    # from costing anything while DEBUG is off.
    root.setLevel(config.log_level)
