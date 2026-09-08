#!/usr/bin/env python3
"""Reject a destructive change to the `ticks` or `bars` storage contract before it merges.

Within a released `schema_version` lineage the schema evolves additively only: no column dropped,
none renamed, none narrowed, and a new one nullable or defaulted, so that a reader built against an
earlier version keeps working (`data-contract`, "Additive-only schema evolution"). The rule that
isn't enforced is the one that breaks during the first refactor under time pressure, which is why
it gets a check rather than a paragraph.

Two things can go wrong, and they need different evidence:

*Lineage* - a new version of a table that removes or narrows something the previous version had.
Found on disk, by comparing consecutive `{table}.v{n}.json` files.

*Immutability* - an edit to a version that has already shipped. Found in git, by comparing each
artifact against the same file at a base ref. A reader binds to a version and expects it to mean
what it meant when it was released, so v1 is frozen once merged; the way to change the schema is
v2. Free text (descriptions, notes, rationales) is exempt - only the schema itself is frozen.

A rename shows up as a removal plus an addition, so it needs no separate detection: the removal
half is already a violation.

This guards the artifacts rather than the SQL migrations that tasks 7.1 and 8.1 will write. That
is not a gap: those services' contract tests assert their migrations produce the schema their
declared version describes, so a migration cannot drop a column without either failing that test
or dropping it here first.

Usage:

    python3 tools/ci/additive_only.py [--base <ref>] [--contracts-dir <path>]

Exits 0 when clean, 1 on a violation, 2 when the check could not run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

STORAGE_DIRNAME: Final = "storage"

# Free text may change in a released version; the schema may not.
PROSE_KEYS: Final = frozenset({"description", "notes", "rationale", "$schema"})

# The only type changes that keep every existing value readable. Everything else - including any
# float or text change - is a narrowing until someone proves otherwise and adds it here.
SAFE_WIDENINGS: Final = frozenset({("integer", "bigint")})


@dataclass(frozen=True, order=True)
class Violation:
    table: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.table}: {self.rule} - {self.detail}"


def strip_prose(value: Any) -> Any:
    """The schema-bearing part of an artifact, with human text removed."""
    if isinstance(value, dict):
        return {k: strip_prose(v) for k, v in value.items() if k not in PROSE_KEYS}
    if isinstance(value, list):
        return [strip_prose(item) for item in value]
    return value


def columns_by_name(artifact: dict) -> dict[str, dict]:
    return {column["name"]: column for column in artifact["columns"]}


def check_lineage(older: dict, newer: dict) -> list[Violation]:
    """What one version of a table may do to the version before it."""
    table = newer["table"]
    violations: list[Violation] = []
    before, after = columns_by_name(older), columns_by_name(newer)

    for name in sorted(before.keys() - after.keys()):
        violations.append(
            Violation(table, "column dropped", f"{name!r} is absent; a rename counts as a drop")
        )

    for name in sorted(before.keys() & after.keys()):
        old_column, new_column = before[name], after[name]
        if old_column["type"] != new_column["type"]:
            change = (old_column["type"], new_column["type"])
            if change not in SAFE_WIDENINGS:
                violations.append(
                    Violation(
                        table,
                        "type narrowed",
                        f"{name!r} changes {change[0]!r} -> {change[1]!r}",
                    )
                )
        if old_column["nullable"] and not new_column["nullable"]:
            violations.append(
                Violation(
                    table,
                    "nullability tightened",
                    f"{name!r} becomes NOT NULL, rejecting rows an earlier writer could produce",
                )
            )

    for name in sorted(after.keys() - before.keys()):
        column = after[name]
        if not column["nullable"] and "default" not in column:
            violations.append(
                Violation(
                    table,
                    "column added without a default",
                    f"{name!r} is NOT NULL with no default, so an earlier reader's writes fail",
                )
            )

    if newer["schema_version"] <= older["schema_version"]:
        violations.append(
            Violation(
                table,
                "version not incremented",
                f"v{newer['schema_version']} does not follow v{older['schema_version']}",
            )
        )
    return violations


def check_immutability(committed: dict, current: dict) -> list[Violation]:
    """What a released version may do to itself: nothing, beyond its prose."""
    if strip_prose(committed) == strip_prose(current):
        return []
    return [
        Violation(
            current["table"],
            "released version edited",
            f"v{current['schema_version']} changed after release; add a new version instead",
        )
    ]


def artifact_paths(contracts_dir: Path) -> list[Path]:
    return sorted((contracts_dir / STORAGE_DIRNAME).glob("*.v*.json"))


def lineages(paths: list[Path]) -> dict[str, dict[int, dict]]:
    """Every table's versions, keyed by table then version."""
    found: dict[str, dict[int, dict]] = {}
    for path in paths:
        artifact = json.loads(path.read_text())
        found.setdefault(artifact["table"], {})[artifact["schema_version"]] = artifact
    return found


def check_lineages(by_table: dict[str, dict[int, dict]]) -> list[Violation]:
    violations: list[Violation] = []
    for versions in by_table.values():
        for older, newer in pairwise(sorted(versions)):
            violations.extend(check_lineage(versions[older], versions[newer]))
    return violations


def at_ref(path: Path, ref: str, repo_root: Path) -> dict | None:
    """The artifact as of `ref`, or None if git cannot show it there.

    None covers three cases that all mean "nothing to compare": a new version file, a path git
    does not track, and a contracts directory outside the repository, which the tests use.
    """
    if not path.is_relative_to(repo_root):
        return None
    relative = path.relative_to(repo_root)
    result = subprocess.run(
        ["git", "show", f"{ref}:{relative.as_posix()}"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=False,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def ref_exists(ref: str, repo_root: Path) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True,
            cwd=repo_root,
            check=False,
        ).returncode
        == 0
    )


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="main", help="git ref to compare released versions to")
    parser.add_argument("--contracts-dir", type=Path, default=repo_root / "contracts")
    args = parser.parse_args(argv)

    paths = artifact_paths(args.contracts_dir)
    if not paths:
        print(f"no storage contracts under {args.contracts_dir}/{STORAGE_DIRNAME}", file=sys.stderr)
        return 2

    violations = check_lineages(lineages(paths))

    if ref_exists(args.base, repo_root):
        for path in paths:
            committed = at_ref(path, args.base, repo_root)
            if committed is not None:
                violations.extend(check_immutability(committed, json.loads(path.read_text())))
    else:
        print(
            f"warning: base ref {args.base!r} does not resolve; checked the lineage on disk only, "
            "not whether a released version was edited",
            file=sys.stderr,
        )

    if violations:
        print("Destructive change to a storage contract:", file=sys.stderr)
        for violation in sorted(violations):
            print(f"  {violation}", file=sys.stderr)
        return 1

    print(f"{len(paths)} storage contract(s) evolve additively")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
