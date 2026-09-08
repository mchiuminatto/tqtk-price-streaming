"""Verification for task 2.8: a destructive storage-contract change is rejected before merge.

The two cases the task names - an added nullable column passes, a dropped column fails - are the
first two tests. The rest cover the other shapes the rule forbids, and the second failure mode:
editing a version that has already shipped, rather than writing a bad new one.

Artifacts are built inline rather than read from `contracts/`, so these stay tests of the rule and
do not fail every time the real schema legitimately grows.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools.ci.additive_only import (
    check_immutability,
    check_lineage,
    check_lineages,
    lineages,
    main,
    strip_prose,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "tools" / "ci" / "additive_only.py"


def column(name: str, type_: str = "text", *, nullable: bool = True, **extra: Any) -> dict:
    return {"name": name, "type": type_, "nullable": nullable, **extra}


def artifact(version: int = 1, *, table: str = "ticks", columns: list[dict] | None = None) -> dict:
    return {
        "table": table,
        "schema_version": version,
        "owner": "tick-persistence-svc",
        "description": "prose that may change freely",
        "hypertable": {"time_column": "recv_ts", "chunk_interval": "1 day", "rationale": "prose"},
        "columns": columns
        if columns is not None
        else [column("provider", nullable=False), column("recv_ts", "timestamptz", nullable=False)],
        "primary_key": None,
        "indexes": [{"name": "ticks_idx", "columns": ["provider", "recv_ts"], "unique": False}],
    }


def rules(violations) -> list[str]:
    return [violation.rule for violation in violations]


# --- the two cases the task names --------------------------------------------------------------


def test_an_added_nullable_column_passes():
    before = artifact(1)
    after = artifact(2, columns=[*before["columns"], column("venue")])
    assert check_lineage(before, after) == []


def test_a_dropped_column_fails():
    before = artifact(1)
    after = artifact(2, columns=before["columns"][:1])
    assert rules(check_lineage(before, after)) == ["column dropped"]


# --- the other destructive shapes ----------------------------------------------------------------


def test_a_renamed_column_fails_as_a_drop():
    """A rename is a removal plus an addition; the removal half is already the violation."""
    before = artifact(1)
    after = artifact(2, columns=[column("provider_name", nullable=False), before["columns"][1]])
    assert "column dropped" in rules(check_lineage(before, after))


@pytest.mark.parametrize(
    ("old_type", "new_type"),
    [
        ("bigint", "integer"),
        ("double precision", "integer"),
        ("text", "integer"),
        ("timestamptz", "text"),
    ],
)
def test_a_narrowed_type_fails(old_type: str, new_type: str):
    before = artifact(1, columns=[column("seq", old_type)])
    after = artifact(2, columns=[column("seq", new_type)])
    assert rules(check_lineage(before, after)) == ["type narrowed"]


def test_a_widening_to_bigint_is_allowed():
    """The one change that keeps every existing value readable."""
    before = artifact(1, columns=[column("seq", "integer")])
    after = artifact(2, columns=[column("seq", "bigint")])
    assert check_lineage(before, after) == []


def test_making_an_existing_column_not_null_fails():
    """It rejects rows an earlier writer was entitled to produce."""
    before = artifact(1, columns=[column("venue")])
    after = artifact(2, columns=[column("venue", nullable=False)])
    assert rules(check_lineage(before, after)) == ["nullability tightened"]


def test_a_new_not_null_column_without_a_default_fails():
    before = artifact(1)
    after = artifact(2, columns=[*before["columns"], column("venue", nullable=False)])
    assert rules(check_lineage(before, after)) == ["column added without a default"]


def test_a_new_not_null_column_with_a_default_passes():
    """The contract permits nullable *or* defaulted; the check must not be stricter than that."""
    before = artifact(1)
    after = artifact(
        2, columns=[*before["columns"], column("venue", nullable=False, default="'unknown'")]
    )
    assert check_lineage(before, after) == []


def test_a_version_that_does_not_advance_fails():
    assert "version not incremented" in rules(check_lineage(artifact(2), artifact(2)))


def test_several_violations_are_all_reported():
    """One run should show every problem, not stop at the first."""
    before = artifact(1, columns=[column("a"), column("b", "bigint")])
    after = artifact(2, columns=[column("b", "integer"), column("c", nullable=False)])
    assert sorted(rules(check_lineage(before, after))) == [
        "column added without a default",
        "column dropped",
        "type narrowed",
    ]


# --- a released version is frozen ------------------------------------------------------------------


def test_editing_a_released_version_fails():
    released = artifact(1)
    edited = artifact(1, columns=released["columns"][:1])
    assert rules(check_immutability(released, edited)) == ["released version edited"]


def test_adding_a_column_to_a_released_version_fails():
    """Even an additive edit: a reader bound to v1 expects v1 to keep meaning what it meant."""
    released = artifact(1)
    edited = artifact(1, columns=[*released["columns"], column("venue")])
    assert rules(check_immutability(released, edited)) == ["released version edited"]


def test_rewriting_prose_in_a_released_version_is_allowed():
    released = artifact(1)
    reworded = artifact(1)
    reworded["description"] = "a clearer explanation"
    reworded["hypertable"]["rationale"] = "a better reason"
    reworded["columns"][0]["description"] = "newly documented"
    assert check_immutability(released, reworded) == []


def test_strip_prose_leaves_the_schema_alone():
    stripped = strip_prose(artifact(1))
    assert "description" not in stripped
    assert "rationale" not in stripped["hypertable"]
    assert stripped["columns"] == artifact(1)["columns"]
    assert stripped["schema_version"] == 1


# --- lineages across several tables and versions ------------------------------------------------


def test_each_table_is_a_separate_lineage():
    """bars v2 dropping a column must not be excused by ticks v2 being fine, or vice versa."""
    by_table = {
        "ticks": {1: artifact(1), 2: artifact(2, columns=[*artifact(1)["columns"], column("v")])},
        "bars": {
            1: artifact(1, table="bars"),
            2: artifact(2, table="bars", columns=artifact(1)["columns"][:1]),
        },
    }
    violations = check_lineages(by_table)
    assert [(v.table, v.rule) for v in violations] == [("bars", "column dropped")]


def test_versions_are_compared_in_order_not_in_file_order():
    by_table = {"ticks": {3: artifact(3, columns=[column("a")]), 1: artifact(1)}}
    # v1 -> v3 drops both original columns; ordering by version is what makes that visible.
    assert "column dropped" in rules(check_lineages(by_table))


def test_a_single_version_has_nothing_to_compare():
    assert check_lineages({"ticks": {1: artifact(1)}}) == []


def test_lineages_groups_files_by_table_and_version(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "ticks.v1.json").write_text(json.dumps(artifact(1)))
    (storage / "ticks.v2.json").write_text(json.dumps(artifact(2)))
    (storage / "bars.v1.json").write_text(json.dumps(artifact(1, table="bars")))
    grouped = lineages(sorted(storage.glob("*.v*.json")))
    assert sorted(grouped) == ["bars", "ticks"]
    assert sorted(grouped["ticks"]) == [1, 2]


# --- the check as CI runs it -----------------------------------------------------------------------


def test_the_real_contracts_pass(capsys):
    """The repository's own artifacts must satisfy the rule they are guarded by."""
    assert main(["--base", "HEAD"]) == 0


def test_a_missing_contracts_directory_is_an_error_not_a_pass(tmp_path: Path):
    """An empty glob must never read as 'nothing destructive found'."""
    assert main(["--contracts-dir", str(tmp_path)]) == 2


def test_an_unresolvable_base_still_checks_the_lineage(tmp_path: Path, capsys):
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "ticks.v1.json").write_text(json.dumps(artifact(1)))
    (storage / "ticks.v2.json").write_text(json.dumps(artifact(2, columns=[column("provider")])))
    assert main(["--base", "no-such-ref", "--contracts-dir", str(tmp_path)]) == 1
    assert "does not resolve" in capsys.readouterr().err


def test_the_script_exits_non_zero_on_a_violation(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "ticks.v1.json").write_text(json.dumps(artifact(1)))
    (storage / "ticks.v2.json").write_text(json.dumps(artifact(2, columns=[column("provider")])))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD", "--contracts-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "column dropped" in result.stderr
