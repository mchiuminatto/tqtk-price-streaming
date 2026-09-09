"""The 12-factor config base every service's own config extends.

The `service-runtime` capability requires configuration to come from the environment, with no
code change needed to alter it. That is the whole contract, and it shapes what this module is:
a `BaseSettings` subclass holding the fields every service has, which each service subclasses to
add its own.

    class FeedConfig(ServiceConfig):
        service_name: str = "feed-adapter-synthetic"
        tick_rate_per_symbol: float = 10.0   # TQTK_TICK_RATE_PER_SYMBOL

    config = FeedConfig()   # reads the environment at construction

Only genuinely universal fields live here. Redis is not among them, close as it comes - the
`historical-query-svc` reads Postgres and never touches the bus, and a base class that forces
every service to carry a connection string for something it does not use is how a shared config
turns into a junk drawer. Services declare what they connect to.

Three decisions:

`TQTK_` prefixes every variable. A container's environment is shared with whatever else the image
carries, and `PORT` or `HOST` unprefixed is a name half the ecosystem claims.

Config is frozen. It is read once at startup, so a restart is what applies a change - which is
what 12-factor asks for, and what makes the running process's behaviour match the environment it
was started with rather than some later in-process mutation.

An unknown `TQTK_*` variable is ignored rather than rejected. It is tempting to fail on one,
since an operator who mistypes `TQTK_LOG_LEVL` gets silence and the default - a real failure of
"changing the variable changes the config". But a Compose stack usually hands the same env block
to several services, each declaring different fields, and rejecting unknown names would mean
every service crashing on every variable meant for a sibling. The typo is the lesser cost.
(Note that `extra="forbid"` below does not do this job either: pydantic-settings only ever
surfaces variables matching a declared field, so unknown ones never reach validation. It bites
on keyword arguments, which is where a typo in a test would otherwise pass unnoticed.)
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tqtk_common.server import DEFAULT_HOST, DEFAULT_PORT

__all__ = [
    "ENV_PREFIX",
    "LogLevel",
    "ServiceConfig",
]

ENV_PREFIX: Final = "TQTK_"

# The levels `logging` itself accepts. Spelled as a Literal so `TQTK_LOG_LEVEL=INFOO` fails at
# startup, where an operator is watching, rather than resolving to a level nothing logs at.
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ServiceConfig(BaseSettings):
    """Configuration common to every service, read from `TQTK_*` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        frozen=True,
        extra="forbid",
        # No `env_file`: config comes from the environment. A file baked into an image is the
        # thing 12-factor is drawing a line against, and python-dotenv arrives here only as a
        # dependency of pydantic-settings, not as a source we read.
    )

    # No default. Every log line and dashboard panel is grouped by it, so a service that has not
    # said what it is should not start; each service's subclass supplies its own name as the
    # default, leaving the variable free to distinguish two instances of one service.
    service_name: str

    runtime_host: str = DEFAULT_HOST
    # 0 is allowed and means "any free port" - what the tests bind on. The upper bound is the
    # protocol's, and catching a 70000 here beats an OSError from deep inside `start`.
    runtime_port: int = Field(default=DEFAULT_PORT, ge=0, le=65535)

    log_level: LogLevel = "INFO"

    @field_validator("service_name")
    @classmethod
    def _reject_blank_service_name(cls, name: str) -> str:
        # A validator rather than `Field(min_length=1)`: a subclass supplying its own default
        # redeclares the field and drops any constraint attached to it, which is exactly what
        # every service here does. Validators are collected across the hierarchy by field name,
        # so this one still runs on the subclass's field.
        if not name.strip():
            raise ValueError("must not be blank")
        return name
