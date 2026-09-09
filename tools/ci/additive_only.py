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

*Immutability* - an edit to a version that has already shipped, or its removal. Found in git, by
comparing the artifacts on disk against the same paths at a base ref. A reader binds to a version
and expects it to mean what it meant when it was released, so v1 is frozen once merged; the way to
change the schema is v2. Free text (descriptions, notes, rationales) is exempt - only the schema
itself is frozen. Deletion is checked from the base ref's side rather than from disk, because a
file that is gone appears in no glob of the working tree.

A column rename shows up as a removal plus an addition, so it needs no separate detection: the
removal half is already a violation. A *table* rename is a file deletion plus an unrelated new
file, which is why deletion is checked at all.

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
from fnmatch import fnmatch
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any, Final

STORAGE_DIRNAME: Final = "storage"

# `storage-contract.schema.json` lives beside the artifacts and is not one; both the on-disk
# scan and the base-ref scan must select by the same pattern or they compare different sets.
ARTIFACT_GLOB: Final = "*.v*.json"

# Free text may change in a released version; the schema may not.
PROSE_KEYS: Final = frozenset({"description", "notes", "rationale", "$schema"})

# The only type changes that keep every existing value readable. Everything else - including any
# float or text change - is a narrowing until someone proves otherwise and adds it here.
SAFE_WIDENINGS: Final = frozenset({("integer", "bigint")})


class CheckCannotRun(Exception):
    """Git could not answer something the check depends on.

    Distinct from a violation, and deliberately not representable as one: a violation means the
    artifacts were compared and found wanting, this means they were never compared at all. The
    two share no exit code either - this is the 2 the module docstring reserves.
    """


def unreadable_at_ref(path: Path, ref: str) -> CheckCannotRun:
    """`path` is listed at `ref`, but its content cannot be read there.

    Not the "nothing to compare" that `at_ref` returns None for elsewhere: the listing already
    proved the file is present at that ref, so failing to read it means this check is broken.
    Skipping it would be the silent pass the whole script exists to prevent.
    """
    return CheckCannotRun(f"{path} is listed at {ref} but cannot be read there")


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
    return sorted((contracts_dir / STORAGE_DIRNAME).glob(ARTIFACT_GLOB))


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


def artifact_paths_at_ref(ref: str, contracts_dir: Path, repo_root: Path) -> set[Path]:
    """The artifacts that existed at `ref`, so a deletion is as visible as an edit.

    Empty when the contracts directory sits outside the repository, which is the case the tests
    use: nothing there was ever released, so nothing there can be deleted. That is the only
    reading of an empty result - a failed listing raises instead, because returning the same
    empty set for "nothing was released" and "git could not tell us" is what let a deleted
    contract pass behind a success line. `contracts_dir` must already be resolved: a relative or
    `..`-bearing path would not be recognised as inside the repository.

    `--full-name` rather than the default: `git ls-tree` reports paths relative to the working
    directory unless asked otherwise, so pinning them to the repository root is what makes
    `repo_root / line` correct no matter where the process happens to be.
    """
    if not contracts_dir.is_relative_to(repo_root):
        return set()
    relative = contracts_dir.relative_to(repo_root) / STORAGE_DIRNAME
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--full-name", "--name-only", ref, "--", relative.as_posix()],
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=False,
    )
    if result.returncode != 0:
        raise CheckCannotRun(
            f"git ls-tree failed for {relative.as_posix()} at {ref}: "
            f"{result.stderr.strip() or 'no error output'}"
        )
    return {
        repo_root / line
        for line in result.stdout.splitlines()
        if fnmatch(PurePosixPath(line).name, ARTIFACT_GLOB)
    }


def check_deletion(released: dict) -> list[Violation]:
    """What a released version may do by disappearing: nothing.

    Returns a list like every other `check_*`, so the caller extends rather than appends and the
    next check added here has one shape to copy.
    """
    return [
        Violation(
            released["table"],
            "released version deleted",
            f"v{released['schema_version']} no longer exists; a released version is frozen, "
            "not removable - supersede it with a new version instead",
        )
    ]


def git_root(start: Path) -> Path | None:
    """The git repository `start` sits in, or None outside one.

    Deliberately not `Path(__file__).parents[2]`: that is where this project's files live, which
    is the repository root only when the tree is checked out on its own. `git show` and
    `git ls-tree` resolve paths from the repository root, so a tree vendored one level down would
    miss every comparison - and the check would print its success line having read nothing.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        cwd=start,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def resolved_dir(value: str) -> Path:
    """An argparse type: a directory as an absolute, normalised path.

    Every git comparison keys on whether a path sits inside the repository and on comparing it
    with what git reports. A relative `--contracts-dir` silently emptied the deletion check, and
    one carrying `..` reported every contract as deleted; both are the same missing `resolve()`.
    """
    return Path(value).resolve()


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
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="main", help="git ref to compare released versions to")
    parser.add_argument(
        "--contracts-dir", type=resolved_dir, default=(project_root / "contracts").resolve()
    )
    args = parser.parse_args(argv)

    paths = artifact_paths(args.contracts_dir)
    if not paths:
        print(f"no storage contracts under {args.contracts_dir}/{STORAGE_DIRNAME}", file=sys.stderr)
        return 2

    violations = check_lineages(lineages(paths))

    # The repository the artifacts live in, which is not necessarily the directory this script
    # sits three levels below - see `git_root`.
    repo_root = git_root(project_root)

    if repo_root is not None and ref_exists(args.base, repo_root):
        try:
            # Listed first: both loops below need to know which artifacts existed at `base`, and
            # it is what separates a file that is new from one that cannot be read.
            released_paths = artifact_paths_at_ref(args.base, args.contracts_dir, repo_root)

            for path in paths:
                committed = at_ref(path, args.base, repo_root)
                if committed is None:
                    if path in released_paths:
                        raise unreadable_at_ref(path, args.base)
                    continue  # Genuinely new since `base`; there is no released version to edit.
                violations.extend(check_immutability(committed, json.loads(path.read_text())))

            for missing in sorted(released_paths - set(paths)):
                released = at_ref(missing, args.base, repo_root)
                if released is None:
                    raise unreadable_at_ref(missing, args.base)
                violations.extend(check_deletion(released))
        except CheckCannotRun as error:
            print(f"error: {error}; the released-version check cannot run", file=sys.stderr)
            return 2
    else:
        reason = (
            f"{project_root} is not inside a git repository"
            if repo_root is None
            else f"base ref {args.base!r} does not resolve"
        )
        print(
            f"warning: {reason}; checked the lineage on disk only, "
            "not whether a released version was edited or deleted",
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
