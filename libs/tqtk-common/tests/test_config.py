"""Verification for task 3.3: a changed environment variable changes the loaded config.

That is the `service-runtime` capability's externalized-configuration scenario, and the reason
these tests set variables through `monkeypatch` rather than constructing with keyword arguments:
passing a value in Python would prove the field exists, not that an operator can reach it. Every
test here goes through the environment, the way a deployment does.
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from tqtk_common import DEFAULT_HOST, DEFAULT_PORT, ENV_PREFIX, ServiceConfig


class ExampleConfig(ServiceConfig):
    """A service's own config: its name as the default, plus the fields it alone needs."""

    service_name: str = "example-svc"
    tick_rate_per_symbol: float = 10.0


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No `TQTK_*` from the developer's own shell, so a default under test is really a default."""
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)


# --- the contract: the environment drives the config ------------------------------------------


def test_a_changed_environment_variable_changes_the_loaded_config(monkeypatch):
    """The task's own check, and the capability's scenario."""
    assert ExampleConfig().log_level == "INFO"

    monkeypatch.setenv("TQTK_LOG_LEVEL", "DEBUG")

    assert ExampleConfig().log_level == "DEBUG"


def test_every_field_is_reachable_from_the_environment(monkeypatch):
    monkeypatch.setenv("TQTK_SERVICE_NAME", "renamed-svc")
    monkeypatch.setenv("TQTK_RUNTIME_HOST", "127.0.0.1")
    monkeypatch.setenv("TQTK_RUNTIME_PORT", "9100")
    monkeypatch.setenv("TQTK_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("TQTK_TICK_RATE_PER_SYMBOL", "250.5")

    config = ExampleConfig()

    assert config.service_name == "renamed-svc"
    assert config.runtime_host == "127.0.0.1"
    assert config.runtime_port == 9100
    assert config.log_level == "WARNING"
    assert config.tick_rate_per_symbol == 250.5


def test_values_are_coerced_to_the_declared_type(monkeypatch):
    """The environment is all strings; a service should get its `int` and `float`."""
    monkeypatch.setenv("TQTK_RUNTIME_PORT", "9100")
    monkeypatch.setenv("TQTK_TICK_RATE_PER_SYMBOL", "0.5")

    config = ExampleConfig()

    assert isinstance(config.runtime_port, int)
    assert isinstance(config.tick_rate_per_symbol, float)


def test_the_environment_is_read_per_construction_not_per_import(monkeypatch):
    """Import order must not decide what a service is configured with."""
    monkeypatch.setenv("TQTK_LOG_LEVEL", "ERROR")
    assert ExampleConfig().log_level == "ERROR"

    monkeypatch.setenv("TQTK_LOG_LEVEL", "CRITICAL")
    assert ExampleConfig().log_level == "CRITICAL"


def test_defaults_apply_when_nothing_is_set():
    config = ExampleConfig()

    assert config.runtime_host == DEFAULT_HOST
    assert config.runtime_port == DEFAULT_PORT
    assert config.log_level == "INFO"
    assert config.service_name == "example-svc"


def test_the_prefix_scopes_which_variables_are_read(monkeypatch):
    """`LOG_LEVEL` belongs to whatever else is in the image; only the prefixed name is ours."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("RUNTIME_PORT", "1")

    config = ExampleConfig()

    assert config.log_level == "INFO"
    assert config.runtime_port == DEFAULT_PORT


def test_variable_names_are_case_insensitive(monkeypatch):
    """As the environment itself is, in practice, for anything hand-edited into a Compose file."""
    monkeypatch.setenv("tqtk_log_level", "DEBUG")

    assert ExampleConfig().log_level == "DEBUG"


# --- invalid config stops the service at startup, where someone is watching -------------------


def test_an_unparseable_value_fails_at_construction(monkeypatch):
    monkeypatch.setenv("TQTK_RUNTIME_PORT", "not-a-port")

    with pytest.raises(ValidationError, match="runtime_port"):
        ExampleConfig()


def test_an_out_of_range_port_fails_at_construction(monkeypatch):
    monkeypatch.setenv("TQTK_RUNTIME_PORT", "70000")

    with pytest.raises(ValidationError, match="runtime_port"):
        ExampleConfig()


def test_an_unknown_log_level_fails_rather_than_logging_nothing(monkeypatch):
    monkeypatch.setenv("TQTK_LOG_LEVEL", "INFOO")

    with pytest.raises(ValidationError, match="log_level"):
        ExampleConfig()


def test_a_service_name_must_not_be_blank(monkeypatch):
    """Asserted on the subclass on purpose: supplying a default redeclares the field and drops
    any `Field()` constraint on it, so the check has to be a validator to survive."""
    monkeypatch.setenv("TQTK_SERVICE_NAME", "   ")

    with pytest.raises(ValidationError, match="service_name"):
        ExampleConfig()


def test_the_base_requires_a_service_name():
    """A service that has not said what it is has nothing to label its logs and metrics with."""
    with pytest.raises(ValidationError, match="service_name"):
        ServiceConfig()


# --- config does not change under the running process ------------------------------------------


def test_config_is_frozen():
    config = ExampleConfig()

    with pytest.raises(ValidationError):
        config.log_level = "DEBUG"


def test_a_later_environment_change_does_not_affect_a_loaded_config(monkeypatch):
    """12-factor puts a restart between a changed variable and a changed process."""
    config = ExampleConfig()
    monkeypatch.setenv("TQTK_LOG_LEVEL", "DEBUG")

    assert config.log_level == "INFO"


def test_an_unknown_keyword_argument_is_rejected():
    """`extra="forbid"` catches a typo in code. It cannot catch one in a variable name - only
    declared fields are ever read from the environment, so `TQTK_LOG_LEVL` is simply unseen."""
    with pytest.raises(ValidationError):
        ExampleConfig(log_levl="DEBUG")


def test_an_unknown_prefixed_variable_is_ignored(monkeypatch):
    """Deliberate: one Compose env block serves services that declare different fields."""
    monkeypatch.setenv("TQTK_SOMETHING_ANOTHER_SERVICE_NEEDS", "1")

    assert ExampleConfig().log_level == "INFO"
