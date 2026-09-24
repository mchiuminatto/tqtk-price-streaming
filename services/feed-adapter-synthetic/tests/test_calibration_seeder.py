"""Verification for the `calibration-store` spec's seeding requirement, against a real Redis: the
seeder (`deploy/calibration/seed.sh`, the exact file the `calibration-seeder` Compose service
runs) seeds a clean script, and a script that fails - whether rejected while queueing (EXECABORT)
or only while EXEC runs - fails the seeder and leaves the live store unchanged.

The second case is the one MULTI/EXEC alone does not cover (a command failing inside EXEC does not
roll back the rest, including the leading delete); `seed.sh`'s dry run on a scratch database is
what makes it pass. Each test starts containers and skips without Docker.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

IMAGE = "redis:7.4-alpine"
_DEPLOY = Path(__file__).resolve().parents[3] / "deploy" / "calibration"
_SEED_SH = _DEPLOY / "seed.sh"
_SEED_SCRIPT = _DEPLOY / "calibration.redis"
_SCRATCH_DB = "15"

# One line of the real script, and two ways to break it.
_EURUSD_LINE = "HSET instrument:EUR/USD pip_size 0.00001 quote_currency USD initial_price 1.15922"
_QUANTITIES_LINE = "SADD calib:quantities return spread interval"


# Generous: `docker run` may pull the image first. A stuck daemon then fails the test instead of
# hanging the CI job until its own time limit.
_DOCKER_TIMEOUT_SECONDS = 120
_DOCKER_INFO_TIMEOUT_SECONDS = 15


def _docker(
    *args: str, check: bool = True, timeout: float = _DOCKER_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=timeout
    )


class _Redis:
    """A throwaway Redis on its own Docker network, reachable there as `redis`."""

    def __init__(self, network: str, container: str) -> None:
        self.network = network
        self.container = container

    def cli(self, *args: str, db: str = "0") -> str:
        return _docker("exec", self.container, "redis-cli", "-n", db, *args).stdout.strip()

    def seed(self, script: Path, host: str = "redis") -> subprocess.CompletedProcess[str]:
        """Run `seed.sh` the way Compose does: stock image, both files mounted read-only."""
        return _docker(
            "run", "--rm", "--network", self.network,
            "-e", f"SEED_REDIS_HOST={host}",
            "-v", f"{_SEED_SH}:/seed/seed.sh:ro",
            "-v", f"{script}:/seed/calibration.redis:ro",
            IMAGE, "sh", "/seed/seed.sh",
            check=False,
        )  # fmt: skip


@pytest.fixture(scope="module")
def redis() -> Iterator[_Redis]:
    # Checked here rather than in a `skipif`, so collecting the suite never shells out to Docker.
    try:
        available = (
            _docker("info", check=False, timeout=_DOCKER_INFO_TIMEOUT_SECONDS).returncode == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        available = False
    if not available:
        pytest.skip("docker is not available")

    suffix = uuid.uuid4().hex[:8]
    network = _docker("network", "create", f"seeder-test-{suffix}").stdout.strip()
    container = ""
    try:
        container = _docker(
            "run", "-d", "--rm", "--network", network, "--network-alias", "redis", IMAGE
        ).stdout.strip()
        client = _Redis(network, container)
        deadline = time.monotonic() + 30
        while _docker("exec", container, "redis-cli", "PING", check=False).stdout.strip() != "PONG":
            if time.monotonic() > deadline:
                raise TimeoutError("redis did not answer PING")
            time.sleep(0.2)
        yield client
    finally:
        if container:
            _docker("rm", "-f", container, check=False)
        _docker("network", "rm", network, check=False)


@pytest.fixture
def seeded(redis: _Redis) -> _Redis:
    """A store holding the committed script's seeding plus a planted `calib:marker` key - so a
    later failed seed that wiped the store would show as the marker being gone."""
    redis.cli("FLUSHALL")
    assert redis.seed(_SEED_SCRIPT).returncode == 0
    redis.cli("SET", "calib:marker", "previous")
    return redis


def _broken(tmp_path: Path, line: str, replacement: str) -> Path:
    text = _SEED_SCRIPT.read_text()
    assert text.count(line) == 1, line  # the seed script changed under this test
    path = tmp_path / "calibration.redis"
    path.write_text(text.replace(line, replacement))
    return path


def test_a_clean_script_seeds_the_live_store_and_leaves_the_scratch_db_empty(seeded: _Redis):
    result = seeded.seed(_SEED_SCRIPT)

    assert result.returncode == 0, result.stderr
    assert "calibration seeded" in result.stdout
    assert seeded.cli("SCARD", "calib:symbols") == "17"
    assert seeded.cli("EXISTS", "calib:marker") == "0"  # the leading delete ran
    assert seeded.cli("DBSIZE", db=_SCRATCH_DB) == "0"


def test_a_command_failing_inside_exec_leaves_the_live_store_unchanged(
    seeded: _Redis, tmp_path: Path
):
    # Queues fine (HSET's arity is "at least 4"), fails only when EXEC runs it.
    script = _broken(tmp_path, _EURUSD_LINE, _EURUSD_LINE.rsplit(" ", 1)[0])

    result = seeded.seed(script)

    assert result.returncode != 0
    assert "wrong number of arguments for 'hset'" in result.stderr
    assert "failed its dry run" in result.stderr
    assert seeded.cli("GET", "calib:marker") == "previous"
    assert seeded.cli("HLEN", "instrument:EUR/USD") == "3"
    assert seeded.cli("DBSIZE", db=_SCRATCH_DB) == "0"


def test_a_command_rejected_while_queueing_leaves_the_live_store_unchanged(
    seeded: _Redis, tmp_path: Path
):
    script = _broken(tmp_path, _QUANTITIES_LINE, _QUANTITIES_LINE.replace("SADD", "SADDX", 1))

    result = seeded.seed(script)

    assert result.returncode != 0
    assert "EXECABORT" in result.stderr
    assert seeded.cli("GET", "calib:marker") == "previous"
    assert seeded.cli("SCARD", "calib:quantities") == "3"
    assert seeded.cli("DBSIZE", db=_SCRATCH_DB) == "0"


def test_an_unreachable_redis_fails_the_seed(redis: _Redis):
    result = redis.seed(_SEED_SCRIPT, host="no-such-redis")

    assert result.returncode != 0
