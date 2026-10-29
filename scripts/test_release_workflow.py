import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).with_name("release_workflow.py")
SPEC = importlib.util.spec_from_file_location("release_workflow", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_workflow)


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("GIT_CONFIG")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def result(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.mark.parametrize(
    ("current", "tags", "expected"),
    [
        ("v0.1.0", [], None),
        ("v1.2.3", ["latest", "v1.2", "v1.2.3-rc1", "v01.2.2"], None),
        ("v1.2.3", ["v2.0.0", "v1.2.4", "v1.2.2", "v1.0.9"], "v1.2.2"),
        ("v1.2.0", ["v1.3.0", "v1.2.0", "v1.1.9", "v1.1.10"], "v1.1.10"),
    ],
)
def test_previous_tag_selects_strict_semver_below_current(
    current: str, tags: list[str], expected: str | None
) -> None:
    assert release_workflow.previous_tag(current, tags) == expected


def test_previous_tag_rejects_invalid_current_tag() -> None:
    with pytest.raises(release_workflow.WorkflowError, match="Invalid release tag"):
        release_workflow.previous_tag("v1.2", ["v1.1.0"])


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def release_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Release test")
    git(repository, "config", "user.email", "release@example.invalid")
    (repository / "source").write_text("main\n", encoding="utf-8")
    git(repository, "add", "source")
    git(repository, "commit", "-m", "feat: initial")
    git(repository, "tag", "-a", "v1.2.3", "-m", "Release v1.2.3")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repository, git(repository, "rev-parse", "v1.2.3^{commit}")


def test_validate_tag_requires_annotated_tag_for_event_commit_on_origin_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, event_sha = release_repository(tmp_path)
    monkeypatch.chdir(repository)
    assert release_workflow.validate_tag("v1.2.3", event_sha) == git(
        repository, "rev-parse", "HEAD"
    )


def test_validate_tag_cli_accepts_the_push_event_commit_sha(tmp_path: Path) -> None:
    repository, event_sha = release_repository(tmp_path)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "validate-tag", "v1.2.3", event_sha],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == event_sha


def test_retry_resolves_original_tag_after_main_advances(tmp_path: Path) -> None:
    repository, release_commit = release_repository(tmp_path)
    (repository / "source").write_text("workflow repair\n", encoding="utf-8")
    git(repository, "add", "source")
    git(repository, "commit", "-m", "fix: release workflow")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repository, "branch", "v1.2.3")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "validate-tag", "v1.2.3"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == release_commit


def test_validate_tag_rejects_mismatched_event_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _event_sha = release_repository(tmp_path)
    (repository / "source").write_text("later\n", encoding="utf-8")
    git(repository, "add", "source")
    git(repository, "commit", "-m", "fix: later")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.chdir(repository)
    with pytest.raises(release_workflow.WorkflowError, match="does not identify"):
        release_workflow.validate_tag("v1.2.3", git(repository, "rev-parse", "HEAD"))


@pytest.mark.parametrize("check_event", [True, False])
def test_validate_tag_rejects_lightweight_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_event: bool
) -> None:
    repository, _event_sha = release_repository(tmp_path)
    git(repository, "tag", "v1.2.4")
    monkeypatch.chdir(repository)
    with pytest.raises(release_workflow.WorkflowError, match="annotated tag"):
        release_workflow.validate_tag(
            "v1.2.4", git(repository, "rev-parse", "v1.2.4") if check_event else None
        )


@pytest.mark.parametrize("check_event", [True, False])
def test_validate_tag_rejects_commit_outside_origin_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_event: bool
) -> None:
    repository, _event_sha = release_repository(tmp_path)
    git(repository, "checkout", "--orphan", "unmerged")
    (repository / "source").write_text("unmerged\n", encoding="utf-8")
    git(repository, "add", "source")
    git(repository, "commit", "-m", "feat: unmerged")
    git(repository, "tag", "-a", "v2.0.0", "-m", "Release v2.0.0")
    monkeypatch.chdir(repository)
    with pytest.raises(release_workflow.WorkflowError, match="not contained"):
        release_workflow.validate_tag(
            "v2.0.0", git(repository, "rev-parse", "v2.0.0") if check_event else None
        )


@pytest.mark.parametrize(
    ("classifiers", "prerelease"),
    [
        (["Development Status :: 2 - Pre-Alpha"], True),
        (["Development Status :: 3 - Alpha"], True),
        (["Development Status :: 4 - Beta"], True),
        (["Development Status :: 5 - Production/Stable"], False),
        ([], False),
    ],
)
def test_project_metadata_cli_reports_version_and_release_status(
    tmp_path: Path, classifiers: list[str], prerelease: bool
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        f'[project]\nversion = "0.9.0"\nclassifiers = {json.dumps(classifiers)}\n',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "project-metadata", "v0.9.0", str(project)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"version=0.9.0\nprerelease={str(prerelease).lower()}\n"


@pytest.mark.parametrize(
    ("tag", "error"),
    [("v1.2.3", "does not match project version"), ("v0.9", "Invalid release tag")],
)
def test_project_metadata_rejects_invalid_or_mismatched_version(
    tmp_path: Path, tag: str, error: str
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nversion = "0.9.0"\n', encoding="utf-8")
    with pytest.raises(release_workflow.WorkflowError, match=error):
        release_workflow.project_metadata(tag, project)


def publication_files(tmp_path: Path) -> tuple[Path, list[str], str]:
    notes = tmp_path / "notes.md"
    notes.write_text("Release notes\n", encoding="utf-8")
    wheel = tmp_path / "splashdown.whl"
    sdist = tmp_path / "splashdown.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    return notes, [str(wheel), str(sdist)], "a" * 40


def test_remote_tag_keeps_original_commit_after_main_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, release_commit = release_repository(tmp_path)
    git(repository, "remote", "add", "origin", str(repository))
    (repository / "source").write_text("workflow repair\n", encoding="utf-8")
    git(repository, "add", "source")
    git(repository, "commit", "-m", "fix: release workflow")
    monkeypatch.chdir(repository)
    release_workflow.verify_remote_tag("v1.2.3", release_commit)


@pytest.mark.parametrize("state", ["moved", "lightweight", "missing"])
def test_publication_rejects_changed_remote_tag_before_accessing_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    repository, release_commit = release_repository(tmp_path)
    git(repository, "remote", "add", "origin", str(repository))
    git(repository, "tag", "-d", "v1.2.3")
    if state == "moved":
        (repository / "source").write_text("other release\n", encoding="utf-8")
        git(repository, "add", "source")
        git(repository, "commit", "-m", "feat: other release")
        git(repository, "tag", "-a", "v1.2.3", "-m", "Moved tag")
    elif state == "lightweight":
        git(repository, "tag", "v1.2.3")
    notes, assets, _target = publication_files(tmp_path)
    monkeypatch.chdir(repository)
    error = "ls-remote.*failed" if state == "missing" else "must be annotated and identify"
    with (
        patch.object(release_workflow, "_release") as release_lookup,
        pytest.raises(release_workflow.WorkflowError, match=error),
    ):
        release_workflow.publish("v1.2.3", "splashdown 1.2.3", notes, release_commit, assets)
    release_lookup.assert_not_called()


def remote_tag(target: str) -> subprocess.CompletedProcess[str]:
    return result(stdout=f"{'b' * 40}\trefs/tags/v1.2.3\n{target}\trefs/tags/v1.2.3^{{}}\n")


def asset(path: str) -> dict[str, object]:
    data = Path(path).read_bytes()
    return {
        "name": Path(path).name,
        "size": len(data),
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "state": "uploaded",
    }


def metadata(
    notes: Path,
    assets: list[str],
    target: str,
    *,
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, object]:
    return {
        "tagName": "v1.2.3",
        "name": "splashdown 1.2.3",
        "body": notes.read_text(encoding="utf-8"),
        "targetCommitish": target,
        "isDraft": draft,
        "isPrerelease": prerelease,
        "isImmutable": False,
        "assets": [asset(path) for path in assets],
    }


@pytest.mark.parametrize("prerelease", [False, True])
def test_first_publication_verifies_existing_tag_and_creates_release(
    tmp_path: Path, prerelease: bool
) -> None:
    notes, assets, target = publication_files(tmp_path)
    responses = [remote_tag(target), result(1, stderr="release not found\n"), result()]
    with (
        patch.object(release_workflow.subprocess, "run", side_effect=responses) as runner,
        patch.object(
            sys,
            "argv",
            [
                str(SCRIPT),
                "publish",
                "v1.2.3",
                "splashdown 1.2.3",
                str(notes),
                target,
                *assets,
                *(["--prerelease"] if prerelease else []),
            ],
        ),
    ):
        release_workflow.main()
    assert runner.call_args_list[0].args == (
        (
            "git",
            "ls-remote",
            "--exit-code",
            "--tags",
            "origin",
            "refs/tags/v1.2.3",
            "refs/tags/v1.2.3^{}",
        ),
    )
    assert runner.call_args_list[2].args == (
        (
            "gh",
            "release",
            "create",
            "v1.2.3",
            *assets,
            "--verify-tag",
            "--title",
            "splashdown 1.2.3",
            "--notes-file",
            str(notes),
            *(["--prerelease", "--latest=false"] if prerelease else []),
        ),
    )


@pytest.mark.parametrize("prerelease", [False, True])
@pytest.mark.parametrize("target_commitish", ["main", "a" * 40])
def test_retry_accepts_identical_release_without_mutation(
    tmp_path: Path, prerelease: bool, target_commitish: str
) -> None:
    notes, assets, target = publication_files(tmp_path)
    responses = [
        remote_tag(target),
        result(stdout=json.dumps(metadata(notes, assets, target_commitish, prerelease=prerelease))),
    ]
    with patch.object(release_workflow.subprocess, "run", side_effect=responses) as runner:
        release_workflow.publish(
            "v1.2.3", "splashdown 1.2.3", notes, target, assets, prerelease=prerelease
        )
    assert runner.call_count == 2
    assert "isPrerelease" in runner.call_args.args[0][-1].split(",")


def test_partial_publication_uploads_only_missing_asset_without_clobber(tmp_path: Path) -> None:
    notes, assets, target = publication_files(tmp_path)
    responses = [
        remote_tag(target),
        result(stdout=json.dumps(metadata(notes, assets[:1], target))),
        result(),
    ]
    with patch.object(release_workflow.subprocess, "run", side_effect=responses) as runner:
        release_workflow.publish("v1.2.3", "splashdown 1.2.3", notes, target, assets)
    assert runner.call_args_list[2].args == (("gh", "release", "upload", "v1.2.3", assets[1]),)


@pytest.mark.parametrize("prerelease", [False, True])
def test_matching_draft_is_published_after_missing_assets_upload(
    tmp_path: Path, prerelease: bool
) -> None:
    notes, assets, target = publication_files(tmp_path)
    responses = [
        remote_tag(target),
        result(
            stdout=json.dumps(
                metadata(notes, assets[:1], target, draft=True, prerelease=prerelease)
            )
        ),
        result(),
        result(),
    ]
    with patch.object(release_workflow.subprocess, "run", side_effect=responses) as runner:
        release_workflow.publish(
            "v1.2.3", "splashdown 1.2.3", notes, target, assets, prerelease=prerelease
        )
    assert runner.call_args_list[3].args == (("gh", "release", "edit", "v1.2.3", "--draft=false"),)


@pytest.mark.parametrize("field", ["name", "body", "tagName"])
def test_retry_rejects_divergent_release_metadata(tmp_path: Path, field: str) -> None:
    notes, assets, target = publication_files(tmp_path)
    release = metadata(notes, assets, target)
    release[field] = "different"
    with (
        patch.object(
            release_workflow.subprocess,
            "run",
            side_effect=[remote_tag(target), result(stdout=json.dumps(release))],
        ) as runner,
        pytest.raises(release_workflow.WorkflowError, match=field),
    ):
        release_workflow.publish("v1.2.3", "splashdown 1.2.3", notes, target, assets)
    assert runner.call_count == 2


@pytest.mark.parametrize(
    ("prerelease", "value"),
    [(False, True), (True, False), (False, None), (False, "false"), (False, 0), (True, 1)],
)
@pytest.mark.parametrize("draft", [False, True])
def test_retry_rejects_mismatched_or_unavailable_prerelease_state(
    tmp_path: Path, draft: bool, prerelease: bool, value: object
) -> None:
    notes, assets, target = publication_files(tmp_path)
    release = metadata(notes, assets, target, draft=draft)
    if value is None:
        del release["isPrerelease"]
    else:
        release["isPrerelease"] = value
    with (
        patch.object(
            release_workflow.subprocess,
            "run",
            side_effect=[remote_tag(target), result(stdout=json.dumps(release))],
        ) as runner,
        pytest.raises(release_workflow.WorkflowError, match="prerelease state differs"),
    ):
        release_workflow.publish(
            "v1.2.3", "splashdown 1.2.3", notes, target, assets, prerelease=prerelease
        )
    assert runner.call_count == 2


@pytest.mark.parametrize("value", [None, "sha256:" + "0" * 64])
def test_retry_rejects_unverifiable_or_divergent_asset_digest(
    tmp_path: Path, value: str | None
) -> None:
    notes, assets, target = publication_files(tmp_path)
    release = metadata(notes, assets, target)
    release_assets = release["assets"]
    assert isinstance(release_assets, list)
    first_asset = release_assets[0]
    assert isinstance(first_asset, dict)
    first_asset["digest"] = value
    with (
        patch.object(
            release_workflow.subprocess,
            "run",
            side_effect=[remote_tag(target), result(stdout=json.dumps(release))],
        ),
        pytest.raises(release_workflow.WorkflowError, match="divergent bytes"),
    ):
        release_workflow.publish("v1.2.3", "splashdown 1.2.3", notes, target, assets)


def test_release_lookup_error_does_not_attempt_creation(tmp_path: Path) -> None:
    notes, assets, target = publication_files(tmp_path)
    with (
        patch.object(
            release_workflow.subprocess,
            "run",
            side_effect=[remote_tag(target), result(1, stderr="HTTP 500: server error\n")],
        ) as runner,
        pytest.raises(release_workflow.WorkflowError, match="HTTP 500"),
    ):
        release_workflow.publish("v1.2.3", "splashdown 1.2.3", notes, target, assets)
    assert runner.call_count == 2


def test_workflow_coverage_step_runs_from_a_fresh_directory(tmp_path: Path) -> None:
    workflow = SCRIPT.parent.parent / ".github/workflows/release.yml"
    match = re.search(
        r"      - name: Test with branch coverage\n        run: \|\n((?:          .*\n)+)",
        workflow.read_text(encoding="utf-8"),
    )
    assert match is not None
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests/test_temporary_directory.py").write_text(
        "def test_temporary_directory(tmp_path):\n"
        "    result = tmp_path / 'result'\n"
        "    result.write_text('passed')\n"
        "    assert result.read_text() == 'passed'\n",
        encoding="utf-8",
    )
    commands = tmp_path / "bin"
    commands.mkdir()
    uv = commands / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert sys.argv[1:3] == ['run', 'pytest']\n"
        "os.execv(sys.executable, [sys.executable, '-m', *sys.argv[2:]])\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    environment = dict(os.environ, PATH=f"{commands}{os.pathsep}{os.defpath}")
    environment.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", textwrap.dedent(match[1])],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout
    results = list((tmp_path / ".cache/release/pytest").glob("*/result"))
    assert results
    assert all(path.read_text(encoding="utf-8") == "passed" for path in results)


def test_all_workflow_actions_use_full_commit_pins() -> None:
    workflows = SCRIPT.parent.parent / ".github/workflows"
    for path in workflows.glob("*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.search(r"\buses:\s+[^@\s]+@([^\s]+)", line)
            if match is not None:
                assert re.fullmatch(r"[0-9a-f]{40}", match[1]), f"{path}: {line}"
