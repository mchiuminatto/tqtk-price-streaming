"""Verification for task 1.3: a change scoped to one service selects only that service."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.ci.affected_members import affected_members, discover_members

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "tools" / "ci" / "affected_members.py"


@pytest.fixture(scope="module")
def members():
    return discover_members(REPO_ROOT)


def names(selected) -> list[str]:
    return [member.name for member in selected]


def test_discovers_every_workspace_member(members):
    assert names(members) == [
        "aggregation-svc",
        "bar-persistence-svc",
        "feed-adapter-synthetic",
        "historical-query-svc",
        "streaming-gateway-svc",
        "tick-persistence-svc",
        "tqtk-common",
    ]


def test_service_source_change_selects_only_that_service(members):
    selected = affected_members(["services/aggregation-svc/src/aggregation_svc/actor.py"], members)
    assert names(selected) == ["aggregation-svc"]


def test_service_dockerfile_change_selects_only_that_service(members):
    selected = affected_members(["services/aggregation-svc/Dockerfile"], members)
    assert names(selected) == ["aggregation-svc"]


def test_two_service_changes_select_exactly_those_two(members):
    selected = affected_members(
        [
            "services/aggregation-svc/src/aggregation_svc/actor.py",
            "services/historical-query-svc/tests/test_api.py",
        ],
        members,
    )
    assert names(selected) == ["aggregation-svc", "historical-query-svc"]


def test_shared_library_change_selects_every_member(members):
    selected = affected_members(["libs/tqtk-common/src/tqtk_common/tick.py"], members)
    assert names(selected) == names(sorted(members))


def test_workspace_and_lockfile_changes_select_every_member(members):
    for path in ("pyproject.toml", "uv.lock", ".dockerignore", ".github/workflows/ci.yml"):
        assert names(affected_members([path], members)) == names(sorted(members)), path


def test_ci_tooling_change_selects_every_member(members):
    selected = affected_members(["tools/ci/affected_members.py"], members)
    assert names(selected) == names(sorted(members))


def test_documentation_and_planning_changes_select_nothing(members):
    selected = affected_members(
        [
            "docs/Real-Time-Price-Pipeline-Architecture.md",
            "openspec/changes/add-price-pipeline/tasks.md",
            "deploy/README.md",
            "README.md",
            "data/EURUSD.parquet",
        ],
        members,
    )
    assert selected == []


def test_unrecognised_path_falls_back_to_every_member(members):
    selected = affected_members(["Makefile"], members)
    assert names(selected) == names(sorted(members))


def test_blank_lines_are_ignored(members):
    assert affected_members(["", "   "], members) == []


def test_kind_distinguishes_services_from_the_library(members):
    by_name = {member.name: member for member in members}
    assert by_name["tqtk-common"].kind == "lib"
    assert by_name["aggregation-svc"].kind == "service"
    assert by_name["aggregation-svc"].path == "services/aggregation-svc"


def test_cli_emits_a_json_matrix_on_stdout():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="services/aggregation-svc/src/aggregation_svc/actor.py\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [
        {
            "name": "aggregation-svc",
            "path": "services/aggregation-svc",
            "kind": "service",
        }
    ]
