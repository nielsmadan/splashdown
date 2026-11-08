import argparse
import re
import sys
import tomllib
from pathlib import Path

SEMVER_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class WorkflowError(Exception):
    pass


def version(tag: str) -> tuple[int, int, int] | None:
    match = SEMVER_TAG.fullmatch(tag)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def project_metadata(tag: str, path: Path) -> tuple[str, bool]:
    if version(tag) is None:
        raise WorkflowError(f"Invalid release tag: {tag!r}")
    project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
    expected_version = tag.removeprefix("v")
    if project["version"] != expected_version:
        raise WorkflowError(
            f"Tag version {expected_version!r} does not match project version "
            f"{project['version']!r}."
        )
    prerelease = any(
        classifier in project.get("classifiers", [])
        for classifier in (
            "Development Status :: 2 - Pre-Alpha",
            "Development Status :: 3 - Alpha",
            "Development Status :: 4 - Beta",
        )
    )
    return expected_version, prerelease


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    metadata = subparsers.add_parser("project-metadata")
    metadata.add_argument("tag")
    metadata.add_argument("project", type=Path)
    args = parser.parse_args()
    project_version, prerelease = project_metadata(args.tag, args.project)
    print(f"version={project_version}")
    print(f"prerelease={str(prerelease).lower()}")


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, UnicodeError, KeyboardInterrupt) as error:
        print(f"Release workflow stopped: {error}", file=sys.stderr)
        sys.exit(1)
