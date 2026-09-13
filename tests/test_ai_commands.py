import json
import subprocess
import sys

import flyrail as fr
import pytest

import splashdown as sd


@pytest.fixture
def ai_project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("# Rules\n")
    (project / "vite.config.ts").write_text("export default {}\n")
    sd.cmd_init(project, loader_override="none")
    return project


def test_status_is_source_free_read_only_and_reports_recorded_content(ai_project, capsys):
    (ai_project / sd.RECIPE_NAME).write_text("invalid TOML ===")
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in ai_project.rglob("*")
        if path.is_file()
    }
    capsys.readouterr()
    assert sd.main(["--cwd", str(ai_project), "--format", "json", "ai", "status"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["files"][0]["content_current"] is True
    assert report["files"][0]["status"] == "recorded-current"
    assert report["files"][0]["desired_current"] is None
    assert report["activation"] == "Agent sessions may need to reload instruction files."
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in ai_project.rglob("*")
        if path.is_file()
    } == before


def test_update_conflict_replace_uninstall_and_json(ai_project, capsys):
    path = ai_project / "AGENTS.md"
    path.write_text(path.read_text().replace("Never hardcode", "User edit"))
    capsys.readouterr()
    arguments = ["--cwd", str(ai_project), "--format", "json", "ai"]
    assert sd.main([*arguments, "update"]) == 1
    assert json.loads(capsys.readouterr().out)["files"][0]["status"] == "failed"
    assert sd.main([*arguments, "update", "--replace"]) == 0
    assert json.loads(capsys.readouterr().out)["files"][0]["content_current"] is True
    (ai_project / sd.RECIPE_NAME).unlink()
    assert sd.main([*arguments, "uninstall"]) == 0
    assert json.loads(capsys.readouterr().out)["files"][0]["status"] == "applied"
    assert path.read_text() == "# Rules\n"


def test_update_requires_recipe_and_uninstall_preserves_edited_block(ai_project, capsys):
    path = ai_project / "AGENTS.md"
    original = path.read_text().replace("Never hardcode", "User edit")
    path.write_text(original)
    (ai_project / sd.RECIPE_NAME).unlink()
    assert sd.main(["--cwd", str(ai_project), "ai", "update"]) == 1
    assert "failed" in capsys.readouterr().out
    assert sd.main(["--cwd", str(ai_project), "ai", "uninstall"]) == 1
    assert "failed" in capsys.readouterr().out
    assert path.read_text() == original


@pytest.mark.parametrize(
    "args,words",
    [
        (["ai"], ["status", "update", "uninstall"]),
        (["ai", "update"], ["--replace", "first baseline"]),
        (["ai", "status"], ["usage:"]),
        (["ai", "uninstall"], ["usage:"]),
    ],
)
def test_ai_help(args, words, capsys):
    with pytest.raises(SystemExit) as error:
        sd.main([*args, "--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert all(word in output for word in words)


def test_absent_status_does_not_create_registry_or_metadata(tmp_path, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("status must not create a registry")

    monkeypatch.setattr(sd.cli, "Registry", unexpected)
    assert sd.main(["--cwd", str(tmp_path), "ai", "status"]) == 0
    assert capsys.readouterr().out.startswith("AGENTS.md: absent\nCLAUDE.md: absent\n")
    assert list(tmp_path.iterdir()) == []


def test_ai_completion_uses_the_real_parser(monkeypatch):
    import argcomplete

    finder = argcomplete.CompletionFinder()
    finder(sd._build_parser(), always_complete_options=False)
    assert set(finder._get_completions(["splash", "ai"], "", "", None)) == {
        "status",
        "update",
        "uninstall",
    }
    assert finder._get_completions(["splash", "ai", "update"], "--r", "", None) == ["--replace "]


def test_status_reports_recipe_drift_without_writing(ai_project, capsys):
    path = ai_project / "AGENTS.md"
    original = path.read_bytes()
    recipe = ai_project / sd.RECIPE_NAME
    recipe.write_text(recipe.read_text().replace('profile = "vite"', 'profile = "angular"'))
    assert sd.main(["--cwd", str(ai_project), "--format", "json", "ai", "status"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["files"][0]["status"] == "outdated"
    assert report["files"][0]["content_current"] is True
    assert report["files"][0]["desired_current"] is False
    assert path.read_bytes() == original
    assert sd.main(["--cwd", str(ai_project), "ai", "update"]) == 0
    capsys.readouterr()
    assert sd.main(["--cwd", str(ai_project), "--format", "json", "ai", "status"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["files"][0]["status"] == "current"
    assert report["files"][0]["desired_current"] is True


def _cli(project, action):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "splashdown",
            "--cwd",
            str(project),
            "--format",
            "json",
            "ai",
            action,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _interrupt_update(project, name="AGENTS.md", boundary="prepared"):
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import sys
from pathlib import Path
import flyrail._lifecycle as lifecycle
import flyrail._resource_transaction as transaction
import splashdown as sd

root, name, boundary = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
original_prepare, original_move = lifecycle.prepare, transaction.move

def prepare(plan):
    journal = original_prepare(plan)
    if boundary == "prepared" and plan.resource.destination == root / name:
        os._exit(77)
    return journal

def move(source, destination, revision):
    original_move(source, destination, revision)
    if boundary == "backup" and source == root / name and destination.parent.name == "backup":
        os._exit(77)

lifecycle.prepare, transaction.move = prepare, move
raise SystemExit(sd.main(["--cwd", str(root), "ai", "update"]))
""",
            str(project),
            name,
            boundary,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 77, process


def _files(project):
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in project.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("operation", ["update", "uninstall", "source-free-uninstall"])
def test_interrupted_update_retries_through_real_cli(ai_project, operation):
    recipe = ai_project / sd.RECIPE_NAME
    recipe.write_text(recipe.read_text().replace('profile = "vite"', 'profile = "angular"'))
    _interrupt_update(ai_project)
    target = fr.InstallationTarget(ai_project / ".splashdown-ai" / "agents")
    observation = fr.inspect_installation("splashdown", target)
    assert observation.pending and observation.resources[0].recovery_paths
    before = _files(ai_project)
    status = _cli(ai_project, "status")
    assert status.returncode == 1
    assert json.loads(status.stdout)["files"][0]["status"] == "incomplete"
    assert _files(ai_project) == before
    if operation == "source-free-uninstall":
        recipe.unlink()
    retried = _cli(ai_project, "update" if operation == "update" else "uninstall")
    assert retried.returncode == 0, retried
    assert not fr.inspect_installation("splashdown", target).pending
    path = ai_project / "AGENTS.md"
    if operation == "update":
        assert json.loads(retried.stdout)["files"][0]["content_current"] is True
        assert "Framework: `angular`" in path.read_text()
    else:
        assert path.read_bytes() == b"# Rules\n"


def test_claude_import_selection_follows_recovery_of_missing_file(ai_project):
    claude = ai_project / "CLAUDE.md"
    claude.write_bytes(b"# Claude rules\n")
    assert _cli(ai_project, "update").returncode == 0
    claude.write_bytes(b"@AGENTS.md\n" + claude.read_bytes())
    _interrupt_update(ai_project, "CLAUDE.md", "backup")
    assert not claude.exists()
    target = fr.InstallationTarget(ai_project / ".splashdown-ai" / "claude")
    assert fr.inspect_installation("splashdown", target).pending
    retried = _cli(ai_project, "update")
    assert retried.returncode == 0, retried
    assert claude.read_bytes() == b"@AGENTS.md\n# Claude rules\n"
    observation = fr.inspect_installation("splashdown", target)
    assert observation.version is None and not observation.pending


def test_unknown_interruption_keeps_evidence_and_fails_cli_until_reconciled(ai_project):
    recipe = ai_project / sd.RECIPE_NAME
    recipe.write_text(recipe.read_text().replace('profile = "vite"', 'profile = "angular"'))
    _interrupt_update(ai_project)
    path = ai_project / "AGENTS.md"
    original = path.read_bytes()
    path.write_bytes(original + b"Foreign edit during interruption.\n")
    before = _files(ai_project)
    for action in ("update", "uninstall"):
        result = _cli(ai_project, action)
        assert result.returncode == 1, result
        assert json.loads(result.stdout)["files"][0]["status"] == "incomplete"
        assert _files(ai_project) == before
    path.write_bytes(original)
    assert _cli(ai_project, "uninstall").returncode == 0
    assert path.read_bytes() == b"# Rules\n"
