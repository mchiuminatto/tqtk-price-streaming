"""Verification for tasks 2.6 and 2.7: the shared fixture builds the contracted schema.

The rendering tests run everywhere. The ones marked `integration` build the schema in a real
TimescaleDB and assert what only a database can answer - that a plain `SELECT` of every contracted
column succeeds, that the documented indexes exist, and that the column types and nullability
Postgres reports are the ones the artifact declares. They start a container and skip without one.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tqtk_common.testing import (
    StorageContract,
    create_schema,
    find_contracts_dir,
    load_contract,
    render_ddl,
)

IMAGE = "timescale/timescaledb:2.17.2-pg16"
PASSWORD = "fixture"
TABLES = ("ticks", "bars")

DOCUMENTED_INDEXES = {
    "ticks": ("provider", "symbol", "recv_ts"),
    "bars": ("provider", "symbol", "side", "timeframe", "bar_start_ts"),
}

# What the artifact's declared types must look like once Postgres has them.
PG_TYPE = {
    "text": "text",
    "double precision": "double precision",
    "bigint": "bigint",
    "integer": "integer",
    "boolean": "boolean",
    "timestamptz": "timestamp with time zone",
}


@pytest.fixture(scope="module")
def contracts() -> dict[str, StorageContract]:
    return {table: load_contract(table, 1) for table in TABLES}


# --- loading ------------------------------------------------------------------------------------


def test_the_contracts_directory_is_found_from_the_package():
    assert (find_contracts_dir() / "storage" / "ticks.v1.json").is_file()


def test_a_missing_contracts_directory_raises_rather_than_guessing(tmp_path: Path):
    """A fixture that silently built nothing would make both contract tests pass against nothing."""
    with pytest.raises(FileNotFoundError, match="repository checkout"):
        find_contracts_dir(tmp_path / "nowhere" / "deeper")


def test_an_unknown_version_raises(contracts: dict[str, StorageContract]):
    with pytest.raises(FileNotFoundError, match="schema_version 99"):
        load_contract("ticks", 99)


def test_the_loaded_contract_declares_the_version_asked_for(contracts):
    assert all(contract.schema_version == 1 for contract in contracts.values())


def test_a_contract_mismatching_its_filename_is_refused(tmp_path: Path):
    """The filename carries the lineage; a file disagreeing with it would misdirect a reader."""
    storage = tmp_path / "storage"
    storage.mkdir()
    artifact = json.loads((find_contracts_dir() / "storage" / "ticks.v1.json").read_text())
    artifact["schema_version"] = 2
    (storage / "ticks.v1.json").write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="declares schema_version 2"):
        load_contract("ticks", 1, contracts_dir=tmp_path)


# --- rendering ----------------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_every_contracted_column_appears_in_the_create_table(table: str, contracts):
    contract = contracts[table]
    create_table = render_ddl(contract)[0]
    for column in contract.columns:
        assert f"{column.name} {column.type}" in create_table


@pytest.mark.parametrize("table", TABLES)
def test_nullability_is_rendered(table: str, contracts):
    create_table = render_ddl(contracts[table])[0]
    for column in contracts[table].columns:
        rendered = f"{column.name} {column.type} NOT NULL" in create_table
        assert rendered is not column.nullable


def test_the_bars_upsert_key_is_rendered_as_a_unique_index(contracts):
    statements = render_ddl(contracts["bars"])
    assert any(statement.startswith("CREATE UNIQUE INDEX") for statement in statements)
    assert any(
        "PRIMARY KEY (provider, symbol, side, timeframe, bar_start_ts)" in s for s in statements
    )


def test_ticks_renders_no_unique_index(contracts):
    assert not any("UNIQUE" in statement for statement in render_ddl(contracts["ticks"]))


def test_a_defaulted_column_renders_its_default(contracts):
    """The additive-only rule admits a NOT NULL column that carries a default, so DDL must too."""
    contract = contracts["ticks"]
    with_default = contract.model_copy(
        update={
            "columns": (
                *contract.columns,
                contract.columns[0].model_copy(
                    update={"name": "venue", "nullable": False, "default": "'unknown'"}
                ),
            )
        }
    )
    assert "venue text DEFAULT 'unknown' NOT NULL" in render_ddl(with_default)[0]


@pytest.mark.parametrize("table", TABLES)
def test_the_hypertable_call_can_be_omitted(table: str, contracts):
    """A reader asserting a column exists should not need the TimescaleDB extension."""
    with_ht = render_ddl(contracts[table], hypertable=True)
    without = render_ddl(contracts[table], hypertable=False)
    assert any("create_hypertable" in statement for statement in with_ht)
    assert not any("create_hypertable" in statement for statement in without)
    assert len(with_ht) == len(without) + 1


# --- against a real database ----------------------------------------------------------------------


def _docker_available() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=15, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


requires_docker = pytest.mark.skipif(not _docker_available(), reason="docker is not available")


@pytest.fixture(scope="module")
def connection() -> Iterator[object]:
    psycopg = pytest.importorskip("psycopg")
    container = subprocess.run(
        ["docker", "run", "-d", "--rm", "-e", f"POSTGRES_PASSWORD={PASSWORD}", "-P", IMAGE],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    try:
        port = (
            subprocess.run(
                ["docker", "port", container, "5432/tcp"],
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.strip()
            .rsplit(":", 1)[1]
        )
        dsn = f"host=127.0.0.1 port={port} user=postgres password={PASSWORD} dbname=postgres"
        deadline = time.monotonic() + 90
        while True:
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=2):
                    pass
                conn = psycopg.connect(dsn, connect_timeout=3)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        yield conn
        conn.close()
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


@pytest.fixture(scope="module")
def built(connection, contracts: dict[str, StorageContract]):
    with connection.cursor() as cursor:
        for contract in contracts.values():
            create_schema(cursor, contract)
    return connection


@requires_docker
@pytest.mark.integration
@pytest.mark.parametrize("table", TABLES)
def test_a_plain_select_of_every_contracted_column_succeeds(table: str, built, contracts):
    """Task 2.7's verification: the schema the fixture builds answers the contract's columns."""
    columns = ", ".join(contracts[table].column_names)
    with built.cursor() as cursor:
        cursor.execute(f"SELECT {columns} FROM {table} LIMIT 0")
        assert [description.name for description in cursor.description] == list(
            contracts[table].column_names
        )


@requires_docker
@pytest.mark.integration
@pytest.mark.parametrize("table", TABLES)
def test_the_documented_index_exists_in_the_built_schema(table: str, built, contracts):
    """Task 2.6's verification, against a schema Postgres actually created."""
    with built.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s AND schemaname = 'public'",
            (table,),
        )
        definitions = [row[0] for row in cursor.fetchall()]
    expected = ", ".join(DOCUMENTED_INDEXES[table])
    assert any(f"({expected})" in definition for definition in definitions), definitions


@requires_docker
@pytest.mark.integration
@pytest.mark.parametrize("table", TABLES)
def test_the_column_types_are_the_ones_the_contract_declares(table: str, built, contracts):
    with built.cursor() as cursor:
        cursor.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        observed = {name: (data_type, nullable) for name, data_type, nullable in cursor.fetchall()}
    for column in contracts[table].columns:
        data_type, nullable = observed[column.name]
        assert data_type == PG_TYPE[column.type], column.name
        assert (nullable == "YES") is column.nullable, column.name


@requires_docker
@pytest.mark.integration
@pytest.mark.parametrize("table", TABLES)
def test_the_table_is_a_hypertable_partitioned_on_its_time_column(table: str, built, contracts):
    with built.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM timescaledb_information.dimensions WHERE hypertable_name = %s",
            (table,),
        )
        dimensions = [row[0] for row in cursor.fetchall()]
    assert dimensions == [contracts[table].hypertable.time_column]


@requires_docker
@pytest.mark.integration
def test_the_bars_upsert_key_is_enforced(built):
    """The idempotent upsert needs this: without uniqueness a redelivery duplicates the row."""
    row = (
        "'synthetic', 'EURUSD', 'bid', '1m', '2026-09-04 12:00:00+00', "
        "1.17042, 1.17061, 1.17033, 1.17055, 318, true, '2026-09-04 12:00:59+00'"
    )
    with built.cursor() as cursor:
        cursor.execute(f"INSERT INTO bars VALUES ({row}) ON CONFLICT DO NOTHING")
        cursor.execute(f"INSERT INTO bars VALUES ({row}) ON CONFLICT DO NOTHING")
        cursor.execute("SELECT count(*) FROM bars")
        assert cursor.fetchone()[0] == 1
        # The other side of the same window is a different row, not a conflict.
        cursor.execute(
            f"INSERT INTO bars VALUES ({row.replace(chr(39) + 'bid' + chr(39), chr(39) + 'ask' + chr(39))})"
        )
        cursor.execute("SELECT count(*) FROM bars")
        assert cursor.fetchone()[0] == 2


@requires_docker
@pytest.mark.integration
def test_the_side_check_refuses_a_third_side(built):
    psycopg = pytest.importorskip("psycopg")
    with built.cursor() as cursor, pytest.raises(psycopg.errors.CheckViolation):
        cursor.execute(
            "INSERT INTO bars VALUES ('synthetic', 'EURUSD', 'mid', '1m', "
            "'2026-09-05 12:00:00+00', 1.0, 1.0, 1.0, 1.0, 0, true, '2026-09-05 12:00:00+00')"
        )
