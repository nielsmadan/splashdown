import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("release_workflow.py")
SPEC = importlib.util.spec_from_file_location("release_workflow", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release_workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_workflow)


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


def test_workflow_coverage_step_keeps_temporary_directories_outside_git(tmp_path: Path) -> None:
    workflow = SCRIPT.parent.parent / ".github/workflows/release.yml"
    match = re.search(
        r"      - name: Test with branch coverage\n        run: \|\n((?:          .*\n)+)",
        workflow.read_text(encoding="utf-8"),
    )
    assert match is not None
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    runner_temp = tmp_path / "runner temp"
    runner_temp.mkdir()
    (checkout / "tests").mkdir()
    (checkout / "scripts").mkdir()
    (checkout / "tests/test_temporary_directory.py").write_text(
        "import subprocess\n"
        "def test_temporary_directory(tmp_path):\n"
        "    git = subprocess.run(['git', 'rev-parse', '--show-toplevel'],\n"
        "                         cwd=tmp_path, capture_output=True, text=True)\n"
        "    assert git.returncode == 128\n"
        "    assert 'not a git repository' in git.stderr\n"
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
    environment = dict(
        os.environ,
        PATH=f"{commands}{os.pathsep}{os.defpath}",
        RUNNER_TEMP=str(runner_temp),
    )
    environment.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", textwrap.dedent(match[1])],
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout
    results = list((runner_temp / "splashdown-pytest").glob("*/result"))
    assert results
    assert all(path.read_text(encoding="utf-8") == "passed" for path in results)
