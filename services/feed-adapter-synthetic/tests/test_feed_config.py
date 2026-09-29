"""Verification for `move-calibration-to-postgres` task 1.2: Redis is the bus only, calibration is
read from PostgreSQL through `postgres_url` plus a password file, and `FeedConfig` still rejects a
mistyped field."""

from __future__ import annotations

from pathlib import Path

import pytest
from feed_adapter_synthetic.config import FeedConfig
from pydantic import ValidationError


def test_service_fields_are_bus_and_calibration_store_connections() -> None:
    assert set(FeedConfig.model_fields) - {"service_name", "runtime_host", "runtime_port"} == {
        "log_level",
        "tick_rate_per_symbol",
        "redis_url",
        "postgres_url",
        "postgres_password_file",
    }


def test_postgres_fields_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TQTK_POSTGRES_URL", raising=False)
    monkeypatch.delenv("TQTK_POSTGRES_PASSWORD_FILE", raising=False)

    config = FeedConfig()

    assert config.postgres_url == "postgresql://tqtk@localhost:5432/tqtk"
    assert config.postgres_password_file is None


def test_postgres_fields_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TQTK_POSTGRES_URL", "postgresql://tqtk@postgres:5432/tqtk")
    monkeypatch.setenv("TQTK_POSTGRES_PASSWORD_FILE", "/run/secrets/postgres_password")

    config = FeedConfig()

    assert config.postgres_url == "postgresql://tqtk@postgres:5432/tqtk"
    assert config.postgres_password_file == Path("/run/secrets/postgres_password")


def test_unknown_keyword_argument_is_rejected() -> None:
    with pytest.raises(ValidationError, match="calibration_redis_url"):
        FeedConfig(calibration_redis_url="redis://elsewhere/0")
