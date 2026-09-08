"""Verification for task 2.6: the versioned storage contract for `ticks` and `bars`.

These checks are language-neutral, like the wire ones beside them: they read the artifacts and the
schemas, and import nothing from the Python implementation. What they cannot do is build a
database - that is the shared fixture of task 2.7, which is where the documented indexes are
asserted against a schema Postgres actually created.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

CONTRACTS_DIR = Path(__file__).resolve().parents[1]
STORAGE_DIR = CONTRACTS_DIR / "storage"
WIRE_DIR = CONTRACTS_DIR / "wire"

META_SCHEMA_PATH = STORAGE_DIR / "storage-contract.schema.json"
ARTIFACT_PATHS = sorted(STORAGE_DIR.glob("*.v*.json"))

# The access paths the data model documents, and the reason this artifact exists.
DOCUMENTED_INDEXES = {
    "ticks": ["provider", "symbol", "recv_ts"],
    "bars": ["provider", "symbol", "side", "timeframe", "bar_start_ts"],
}

# Which wire record each table stores, for holding the two contract surfaces together.
WIRE_RECORD = {"ticks": "tick", "bars": "bar"}


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def artifacts() -> dict[str, dict]:
    return {load(path)["table"]: load(path) for path in ARTIFACT_PATHS}


ARTIFACTS = artifacts()


def wire_fields(record: str) -> set[str]:
    return set(load(WIRE_DIR / f"{record}.schema.json")["properties"])


# --- the artifacts exist and are well formed --------------------------------------------------


def test_both_tables_are_contracted():
    assert set(ARTIFACTS) == {"ticks", "bars"}


def test_the_meta_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(load(META_SCHEMA_PATH))


@pytest.mark.parametrize("path", ARTIFACT_PATHS, ids=lambda path: path.name)
def test_each_artifact_conforms_to_the_meta_schema(path: Path):
    Draft202012Validator(load(META_SCHEMA_PATH)).validate(load(path))


@pytest.mark.parametrize("path", ARTIFACT_PATHS, ids=lambda path: path.name)
def test_the_filename_carries_the_declared_version(path: Path):
    """`bars.v1.json` is version 1 of `bars`: the lineage is legible without opening the file."""
    table, version = path.name.split(".")[:2]
    artifact = load(path)
    assert artifact["table"] == table
    assert f"v{artifact['schema_version']}" == version


def test_each_table_versions_independently():
    """Two owners, two release cadences: the versions are not required to move together."""
    assert {name: artifact["schema_version"] for name, artifact in ARTIFACTS.items()} == {
        "ticks": 1,
        "bars": 1,
    }


def test_ddl_ownership_follows_sole_writer_ownership():
    assert ARTIFACTS["ticks"]["owner"] == "tick-persistence-svc"
    assert ARTIFACTS["bars"]["owner"] == "bar-persistence-svc"


# --- the documented indexes ---------------------------------------------------------------------


@pytest.mark.parametrize(("table", "columns"), DOCUMENTED_INDEXES.items())
def test_the_documented_index_is_declared(table: str, columns: list[str]):
    declared = [index["columns"] for index in ARTIFACTS[table]["indexes"]]
    assert columns in declared


def test_the_bars_index_is_the_upsert_key_and_is_unique():
    """Idempotent upsert needs the conflict target to be unique, or a redelivery duplicates."""
    artifact = ARTIFACTS["bars"]
    index = next(i for i in artifact["indexes"] if i["columns"] == DOCUMENTED_INDEXES["bars"])
    assert index["unique"]
    assert artifact["primary_key"] == DOCUMENTED_INDEXES["bars"]


def test_ticks_declares_no_unique_index():
    """Its dedup key omits the partitioning column, so Timescale could not enforce it anyway."""
    assert ARTIFACTS["ticks"]["primary_key"] is None
    assert not any(index["unique"] for index in ARTIFACTS["ticks"]["indexes"])


@pytest.mark.parametrize("table", sorted(ARTIFACTS))
def test_every_unique_index_contains_the_partitioning_column(table: str):
    """A TimescaleDB rule, not a preference: a unique index must include the time dimension."""
    artifact = ARTIFACTS[table]
    time_column = artifact["hypertable"]["time_column"]
    for index in artifact["indexes"]:
        if index["unique"]:
            assert time_column in index["columns"], index["name"]
    if artifact["primary_key"] is not None:
        assert time_column in artifact["primary_key"]


@pytest.mark.parametrize("table", sorted(ARTIFACTS))
def test_every_index_and_key_names_columns_that_exist(table: str):
    artifact = ARTIFACTS[table]
    columns = {column["name"] for column in artifact["columns"]}
    assert artifact["hypertable"]["time_column"] in columns
    for index in artifact["indexes"]:
        assert set(index["columns"]) <= columns, index["name"]
    if artifact["primary_key"] is not None:
        assert set(artifact["primary_key"]) <= columns


# --- the storage surface against the wire surface -----------------------------------------------


@pytest.mark.parametrize(("table", "record"), WIRE_RECORD.items())
def test_every_wire_field_is_stored(table: str, record: str):
    """A field on the bus that no column holds would be silently dropped at persistence."""
    stored = {
        column["wire_field"] for column in ARTIFACTS[table]["columns"] if "wire_field" in column
    }
    assert wire_fields(record) == stored


@pytest.mark.parametrize(("table", "record"), WIRE_RECORD.items())
def test_optionality_matches_the_wire_contract(table: str, record: str):
    """An optional wire field needs a nullable column; a required one must not be nullable."""
    required = set(load(WIRE_DIR / f"{record}.schema.json")["required"])
    for column in ARTIFACTS[table]["columns"]:
        if "wire_field" not in column:
            continue
        assert column["nullable"] == (column["wire_field"] not in required), column["name"]


# --- the shape the design decided on --------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(ARTIFACTS))
def test_no_column_is_a_json_document(table: str):
    """Prices are typed columns: the rejected JSONB alternative would hide them from the
    shared fixture, from the destructive-migration check, and from per-column compression."""
    types = {column["type"] for column in ARTIFACTS[table]["columns"]}
    assert not {"json", "jsonb"} & types


def test_prices_are_double_precision():
    price_columns = {"bid", "ask"} | {"open", "high", "low", "close"}
    for artifact in ARTIFACTS.values():
        for column in artifact["columns"]:
            if column["name"] in price_columns:
                assert column["type"] == "double precision", column["name"]


def test_timestamps_are_timestamptz():
    for artifact in ARTIFACTS.values():
        for column in artifact["columns"]:
            if column["name"].endswith("_ts"):
                assert column["type"] == "timestamptz", column["name"]


def test_the_side_column_is_constrained_to_the_closed_enum():
    checks = {check["name"]: check["expression"] for check in ARTIFACTS["bars"]["checks"]}
    assert checks["bars_side_is_bid_or_ask"] == "side IN ('bid', 'ask')"


def test_the_timeframe_check_lists_every_contracted_timeframe():
    contracted = load(WIRE_DIR / "bar.schema.json")["properties"]["timeframe"]["enum"]
    checks = {check["name"]: check["expression"] for check in ARTIFACTS["bars"]["checks"]}
    expression = checks["bars_timeframe_is_contracted"]
    assert all(f"'{timeframe}'" in expression for timeframe in contracted)
