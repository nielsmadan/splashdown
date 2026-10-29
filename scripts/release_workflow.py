import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
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


def previous_tag(current: str, tags: Sequence[str]) -> str | None:
    current_version = version(current)
    if current_version is None:
        raise WorkflowError(f"Invalid release tag: {current!r}")
    candidates = [
        (parsed, tag)
        for tag in tags
        if (parsed := version(tag)) is not None and parsed < current_version
    ]
    return max(candidates)[1] if candidates else None


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise WorkflowError(f"{' '.join(args)} failed ({result.returncode}). {detail}")
    return result


def validate_tag(tag: str, event_sha: str | None = None) -> str:
    if version(tag) is None:
        raise WorkflowError(f"Invalid release tag: {tag!r}")
    ref = f"refs/tags/{tag}"
    object_type = command("git", "cat-file", "-t", ref).stdout.strip()
    if object_type != "tag":
        raise WorkflowError(f"Release ref {tag} must be an annotated tag, got {object_type!r}.")
    release_commit = command("git", "rev-parse", f"{ref}^{{commit}}").stdout.strip()
    if event_sha is not None:
        event_commit = command("git", "rev-parse", f"{event_sha}^{{commit}}").stdout.strip()
        if release_commit != event_commit:
            raise WorkflowError(
                f"Release event SHA {event_sha} does not identify {tag} commit {release_commit}."
            )
    origin_main = command("git", "rev-parse", "refs/remotes/origin/main^{commit}").stdout.strip()
    ancestry = command(
        "git", "merge-base", "--is-ancestor", release_commit, origin_main, check=False
    )
    if ancestry.returncode != 0:
        raise WorkflowError(
            f"Release commit {release_commit} is not contained in fetched origin/main "
            f"({origin_main})."
        )
    return release_commit


def verify_remote_tag(tag: str, target: str) -> None:
    if version(tag) is None:
        raise WorkflowError(f"Invalid release tag: {tag!r}")
    ref = f"refs/tags/{tag}"
    peeled = f"{ref}^{{}}"
    refs = command("git", "ls-remote", "--exit-code", "--tags", "origin", ref, peeled)
    if f"{target}\t{peeled}" not in refs.stdout.splitlines():
        raise WorkflowError(
            f"Remote release tag {tag} must be annotated and identify verified commit {target}."
        )


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected_assets(assets: Sequence[str]) -> dict[str, tuple[Path, int, str]]:
    expected: dict[str, tuple[Path, int, str]] = {}
    for value in assets:
        path = Path(value)
        if not path.is_file():
            raise WorkflowError(f"Release asset does not exist: {path}")
        if path.name in expected:
            raise WorkflowError(f"Release asset names must be unique: {path.name}")
        expected[path.name] = (path, path.stat().st_size, f"sha256:{_sha256(path)}")
    return expected


def _release(tag: str) -> dict[str, object] | None:
    fields = "tagName,name,body,isDraft,isPrerelease,isImmutable,assets"
    result = command("gh", "release", "view", tag, "--json", fields, check=False)
    if result.returncode == 0:
        try:
            release = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise WorkflowError(f"GitHub returned invalid release metadata for {tag}.") from error
        if not isinstance(release, dict):
            raise WorkflowError(f"GitHub returned invalid release metadata for {tag}.")
        return release
    if result.returncode == 1 and "release not found" in result.stderr.lower():
        return None
    detail = (result.stderr or result.stdout).strip()
    raise WorkflowError(f"gh release view {tag} failed ({result.returncode}). {detail}")


def _missing_assets(
    release: dict[str, object],
    expected: dict[str, tuple[Path, int, str]],
) -> list[str]:
    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list):
        raise WorkflowError("GitHub release asset identity is unavailable.")
    existing: dict[str, dict[str, object]] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict) or not isinstance(raw_asset.get("name"), str):
            raise WorkflowError("GitHub release asset identity is unavailable.")
        name = str(raw_asset["name"])
        if name in existing:
            raise WorkflowError(f"GitHub release has duplicate asset name {name!r}.")
        existing[name] = raw_asset
    unexpected = sorted(set(existing) - set(expected))
    if unexpected:
        raise WorkflowError("GitHub release has unexpected assets: " + ", ".join(unexpected))
    for name, asset in existing.items():
        _path, size, digest = expected[name]
        if asset.get("state") != "uploaded":
            raise WorkflowError(f"GitHub release asset {name!r} is not fully uploaded.")
        if asset.get("size") != size:
            raise WorkflowError(
                f"GitHub release asset {name!r} has divergent bytes (size differs)."
            )
        if asset.get("digest") != digest:
            detail = "digest is unavailable" if not asset.get("digest") else "digest differs"
            raise WorkflowError(f"GitHub release asset {name!r} has divergent bytes ({detail}).")
    return [str(expected[name][0]) for name in expected if name not in existing]


def _verify_metadata(
    release: dict[str, object], tag: str, title: str, body: str, prerelease: bool
) -> None:
    if not isinstance(release.get("isDraft"), bool) or not isinstance(
        release.get("isImmutable"), bool
    ):
        raise WorkflowError("GitHub release mutability state is unavailable.")
    if release.get("isPrerelease") is not prerelease:
        raise WorkflowError(
            "GitHub release prerelease state differs from the expected publication."
        )
    expected = {
        "tagName": tag,
        "name": title,
        "body": body,
    }
    for field, value in expected.items():
        if release.get(field) != value:
            raise WorkflowError(
                f"GitHub release {field} differs from the expected {tag} publication."
            )


def publish(
    tag: str,
    title: str,
    notes: Path,
    target: str,
    assets: Sequence[str],
    *,
    prerelease: bool = False,
) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", target) is None:
        raise WorkflowError(f"Release target must be a full commit SHA, got {target!r}.")
    body = notes.read_text(encoding="utf-8")
    expected = _expected_assets(assets)
    verify_remote_tag(tag, target)
    release = _release(tag)
    if release is None:
        command(
            "gh",
            "release",
            "create",
            tag,
            *assets,
            "--verify-tag",
            "--title",
            title,
            "--notes-file",
            str(notes),
            *(["--prerelease", "--latest=false"] if prerelease else []),
        )
        return
    _verify_metadata(release, tag, title, body, prerelease)
    missing = _missing_assets(release, expected)
    if missing:
        if release.get("isImmutable") is True:
            raise WorkflowError("GitHub release is immutable and is missing expected assets.")
        command("gh", "release", "upload", tag, *missing)
    if release.get("isDraft") is True:
        command("gh", "release", "edit", tag, "--draft=false")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    previous = subparsers.add_parser("previous-tag")
    previous.add_argument("current")
    validation = subparsers.add_parser("validate-tag")
    validation.add_argument("tag")
    validation.add_argument("event_sha", metavar="event-sha", nargs="?")
    metadata = subparsers.add_parser("project-metadata")
    metadata.add_argument("tag")
    metadata.add_argument("project", type=Path)
    publication = subparsers.add_parser("publish")
    publication.add_argument("tag")
    publication.add_argument("title")
    publication.add_argument("notes", type=Path)
    publication.add_argument("target")
    publication.add_argument("assets", nargs="+")
    publication.add_argument("--prerelease", action="store_true")
    args = parser.parse_args()
    if args.operation == "previous-tag":
        tags = command("git", "tag", "--list").stdout.splitlines()
        if selected := previous_tag(args.current, tags):
            print(selected)
        return
    if args.operation == "validate-tag":
        print(validate_tag(args.tag, args.event_sha))
        return
    if args.operation == "project-metadata":
        project_version, prerelease = project_metadata(args.tag, args.project)
        print(f"version={project_version}")
        print(f"prerelease={str(prerelease).lower()}")
        return
    publish(args.tag, args.title, args.notes, args.target, args.assets, prerelease=args.prerelease)


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, UnicodeError, KeyboardInterrupt) as error:
        print(f"Release workflow stopped: {error}", file=sys.stderr)
        sys.exit(1)
