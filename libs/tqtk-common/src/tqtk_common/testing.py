"""The shared storage-contract fixture: a schema built from `contracts/storage/`.

Both sides of the `ticks`/`bars` coupling test against this. The owning service asserts that the
schema its migrations produce matches the contract it declares; `historical-query-svc` asserts that
every column and type its queries touch exists in the `schema_version` it binds to - and does so
without a running persistence service, because the schema comes from the artifact rather than from
whoever happens to have migrated the database.

Rendering is pure string generation, and applying it takes any DB-API cursor, so `tqtk-common`
needs no Postgres driver of its own. The caller brings its own connection.

This module ships with the package so services can import it, but it reads `contracts/`, which is
deliberately not inside any wheel - so it works from a repo checkout, where tests run, and not
from inside a service image, where they do not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final, Protocol, Self

from pydantic import BaseModel, ConfigDict

__all__ = [
    "Column",
    "Index",
    "StorageContract",
    "create_schema",
    "find_contracts_dir",
    "load_contract",
    "render_ddl",
]

STORAGE_DIRNAME: Final = "storage"


class Cursor(Protocol):
    """Just enough of DB-API to apply DDL, so no driver is imported here."""

    def execute(self, statement: str, /) -> Any: ...


class _Artifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class Column(_Artifact):
    name: str
    type: str
    nullable: bool
    wire_field: str | None = None


class Index(_Artifact):
    name: str
    columns: tuple[str, ...]
    unique: bool


class Check(_Artifact):
    name: str
    expression: str


class Hypertable(_Artifact):
    time_column: str
    chunk_interval: str


class StorageContract(_Artifact):
    """One table's contract, at one `schema_version`."""

    table: str
    schema_version: int
    owner: str
    hypertable: Hypertable
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...] | None = None
    indexes: tuple[Index, ...]
    checks: tuple[Check, ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @classmethod
    def from_dict(cls, artifact: dict[str, Any]) -> Self:
        return cls.model_validate(artifact)


def find_contracts_dir(start: Path | None = None) -> Path:
    """The repository's `contracts/` directory, found by walking up from `start`.

    Raises rather than guessing: a fixture that silently built an empty schema would make both
    contract tests pass against nothing.
    """
    for candidate in (start or Path(__file__).resolve()).parents:
        contracts = candidate / "contracts"
        if (contracts / STORAGE_DIRNAME).is_dir():
            return contracts
    raise FileNotFoundError(
        f"no contracts/{STORAGE_DIRNAME} directory above {start or __file__}; "
        "the storage fixture runs from a repository checkout, not from an installed wheel"
    )


def load_contract(
    table: str, schema_version: int, *, contracts_dir: Path | None = None
) -> StorageContract:
    """Load one table's contract at an explicit `schema_version`.

    The version is required, not inferred: a test that binds to a version is the whole point of
    the artifact, and picking "the newest on disk" would let a reader silently follow a writer
    into a schema it was never built against.
    """
    directory = contracts_dir or find_contracts_dir()
    path = directory / STORAGE_DIRNAME / f"{table}.v{schema_version}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no contract for {table} at schema_version {schema_version}: {path}"
        )
    contract = StorageContract.from_dict(json.loads(path.read_text()))
    if contract.schema_version != schema_version:
        raise ValueError(
            f"{path.name} declares schema_version {contract.schema_version}, not {schema_version}"
        )
    return contract


def render_ddl(contract: StorageContract, *, hypertable: bool = True) -> list[str]:
    """The statements that build this contract's schema, in application order.

    `hypertable=False` omits the `create_hypertable` call, for a database without the TimescaleDB
    extension. The read-side contract test needs the table's shape, not its time partitioning, and
    should not require an extension to assert that a column it queries exists.
    """
    statements = [_create_table(contract)]
    if hypertable:
        statements.append(_create_hypertable(contract))
    statements.extend(_create_index(contract, index) for index in contract.indexes)
    return statements


def create_schema(cursor: Cursor, contract: StorageContract, *, hypertable: bool = True) -> None:
    """Apply `render_ddl` through a caller-supplied DB-API cursor."""
    for statement in render_ddl(contract, hypertable=hypertable):
        cursor.execute(statement)


def _create_table(contract: StorageContract) -> str:
    definitions = [
        f"    {column.name} {column.type}{'' if column.nullable else ' NOT NULL'}"
        for column in contract.columns
    ]
    if contract.primary_key is not None:
        definitions.append(
            f"    CONSTRAINT {contract.table}_pkey PRIMARY KEY ({', '.join(contract.primary_key)})"
        )
    definitions.extend(
        f"    CONSTRAINT {check.name} CHECK ({check.expression})" for check in contract.checks
    )
    body = ",\n".join(definitions)
    return f"CREATE TABLE IF NOT EXISTS {contract.table} (\n{body}\n)"


def _create_hypertable(contract: StorageContract) -> str:
    return (
        f"SELECT create_hypertable('{contract.table}', "
        f"by_range('{contract.hypertable.time_column}', "
        f"INTERVAL '{contract.hypertable.chunk_interval}'), if_not_exists => TRUE)"
    )


def _create_index(contract: StorageContract, index: Index) -> str:
    unique = "UNIQUE " if index.unique else ""
    return (
        f"CREATE {unique}INDEX IF NOT EXISTS {index.name} "
        f"ON {contract.table} ({', '.join(index.columns)})"
    )
