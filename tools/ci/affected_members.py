#!/usr/bin/env python3
"""Map a set of changed files onto the workspace members CI must build and test.

The monorepo builds one image per service, so an unrelated service must not
rebuild on an unrelated change (design.md, "Repo layout"). This module is the
single place that decides which members a diff touches; the CI workflow only
shells out to it, so the filtering rules stay unit-testable without pushing a
commit to exercise them.

Usage (changed paths on stdin, one per line, repo-relative):

    git diff --name-only "$BASE" "$HEAD" | python3 tools/ci/affected_members.py

Output is a JSON array of ``{"name", "path", "kind"}`` objects, sorted by name,
suitable for a GitHub Actions matrix.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Workspace member roots, mirroring `[tool.uv.workspace] members` in the root
# pyproject.toml. `kind` drives what CI does with the member: only a service
# owns a Dockerfile and therefore gets an image build.
MEMBER_ROOTS = {"libs": "lib", "services": "service"}

# Paths that change the resolution or the build of every member: the workspace
# definition itself, the lockfile, the shared build context filter, and the CI
# tooling that decides all of this.
GLOBAL_PATHS = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        ".dockerignore",
    }
)
GLOBAL_PREFIXES = (
    ".github/",
    "tools/",
)

# Paths that cannot affect any member's build or tests. Anything NOT listed
# here and not recognised above falls back to "rebuild everything" — an
# unnecessary build is cheap, a skipped one that should have run is not.
IGNORED_PATHS = frozenset(
    {
        ".gitignore",
        "LICENSE",
        "README.md",
    }
)
IGNORED_PREFIXES = (
    ".claude/",
    ".idea/",
    "data/",
    "deploy/",
    "docs/",
    "openspec/",
)


@dataclass(frozen=True, order=True)
class Member:
    name: str
    path: str
    kind: str


def discover_members(repo_root: Path) -> list[Member]:
    """Every uv workspace member present on disk, discovered rather than listed.

    A service added later is picked up by CI without editing this file.
    """
    members: list[Member] = []
    for root, kind in MEMBER_ROOTS.items():
        for candidate in sorted((repo_root / root).glob("*")):
            if (candidate / "pyproject.toml").is_file():
                members.append(
                    Member(name=candidate.name, path=f"{root}/{candidate.name}", kind=kind)
                )
    return sorted(members)


def affected_members(changed_paths: list[str], members: list[Member]) -> list[Member]:
    """The members CI must build and test for this set of changed files."""
    by_path = {member.path: member for member in members}
    services = [member for member in members if member.kind == "service"]

    affected: set[Member] = set()
    for raw in changed_paths:
        path = raw.strip().strip('"')
        if not path:
            continue

        if path in GLOBAL_PATHS or path.startswith(GLOBAL_PREFIXES):
            return sorted(members)

        if path in IGNORED_PATHS or path.startswith(IGNORED_PREFIXES):
            continue

        member = _owning_member(path, by_path)
        if member is None:
            # Unrecognised top-level path: be conservative, not clever.
            return sorted(members)

        affected.add(member)
        if member.kind == "lib":
            # Every service depends on the shared library, so a change to it
            # invalidates all of their tests and images.
            affected.update(services)

    return sorted(affected)


def _owning_member(path: str, by_path: dict[str, Member]) -> Member | None:
    parts = path.split("/")
    if len(parts) < 3 or parts[0] not in MEMBER_ROOTS:
        return None
    return by_path.get(f"{parts[0]}/{parts[1]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root to discover workspace members in.",
    )
    args = parser.parse_args(argv)

    changed = sys.stdin.read().splitlines()
    selected = affected_members(changed, discover_members(args.repo_root))
    json.dump([asdict(member) for member in selected], sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
