from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from splashdown import cli, completion, project_selection
from splashdown.constants import RECIPE_NAME
from splashdown.errors import ApplicationError
from splashdown.project_selection import ProjectSelection, resolve_start_directory, select_project


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_nearest_recipe_and_exact_init(tmp_path):
    _git(tmp_path, "init", "-q")
    nested = tmp_path / "apps" / "nested"
    nested.mkdir(parents=True)
    (tmp_path / RECIPE_NAME).write_text("")
    assert select_project(nested).directory == tmp_path
    assert select_project(nested, exact=True) == ProjectSelection(nested, nested, None, None)
    (nested.parent / RECIPE_NAME).write_text("malformed = [")
    selected = select_project(nested)
    assert selected == ProjectSelection(
        nested, nested.parent, tmp_path, nested.parent / RECIPE_NAME
    )


@pytest.mark.parametrize("kind", ["directory", "dangling", "malformed"])
def test_invalid_nearest_entry_wins(tmp_path, kind):
    _git(tmp_path, "init", "-q")
    (tmp_path / RECIPE_NAME).write_text("")
    child = tmp_path / "child"
    child.mkdir()
    recipe = child / RECIPE_NAME
    if kind == "directory":
        recipe.mkdir()
    elif kind == "dangling":
        recipe.symlink_to("absent")
    else:
        recipe.write_text("not valid [[[")
    assert select_project(child).recipe_path == recipe
    assert select_project(child).directory == child


def test_outside_git_is_exact_and_missing_recipe_keeps_start(tmp_path):
    (tmp_path / RECIPE_NAME).write_text("")
    child = tmp_path / "child"
    child.mkdir()
    assert select_project(child) == ProjectSelection(child, child, None, None)
    _git(child, "init", "-q")
    nested = child / "nested"
    nested.mkdir()
    assert select_project(nested) == ProjectSelection(nested, nested, child, None)


def test_linked_worktree_bounds_discovery(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(
        main,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-qm",
        "initial",
    )
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "--detach", str(linked))
    (tmp_path / RECIPE_NAME).write_text("")
    child = linked / "child"
    child.mkdir()
    assert select_project(child) == ProjectSelection(child, child, linked, None)
    (linked / RECIPE_NAME).write_text("broken = [")
    assert select_project(child).directory == linked


@pytest.mark.parametrize("kind", ["missing", "file", "dangling"])
def test_invalid_directory_is_operational_failure(tmp_path, kind):
    path = tmp_path / kind
    if kind == "file":
        path.write_text("")
    elif kind == "dangling":
        path.symlink_to("absent")
    with pytest.raises(ApplicationError) as caught:
        resolve_start_directory(path)
    assert (caught.value.code, caught.value.exit_code) == ("invalid_directory", 1)


def test_start_canonicalizes_symlinks(tmp_path):
    alias = tmp_path / "alias"
    real = tmp_path / "real"
    real.mkdir()
    alias.symlink_to(real)
    assert resolve_start_directory(alias / ".." / "alias") == real


def test_unreadable_directory_is_not_absence(tmp_path, monkeypatch):
    def denied(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(project_selection.os, "scandir", denied)
    with pytest.raises(ApplicationError) as caught:
        resolve_start_directory(tmp_path)
    assert caught.value.code == "invalid_directory"


def test_unreadable_recipe_lookup_does_not_fall_back(tmp_path, monkeypatch):
    def denied(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(project_selection, "_worktree_root", lambda _start: tmp_path)
    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(PermissionError):
        select_project(tmp_path)


@pytest.mark.parametrize(
    "failure", [FileNotFoundError("git missing"), subprocess.TimeoutExpired("git", 5)]
)
def test_git_execution_failure_is_not_nonworktree(tmp_path, monkeypatch, failure):
    def failed(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(project_selection.subprocess, "run", failed)
    with pytest.raises(ApplicationError) as caught:
        select_project(tmp_path)
    assert caught.value.code == "io_error"


def test_broken_git_is_not_nonworktree(tmp_path, monkeypatch):
    monkeypatch.setattr(
        project_selection.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 128, "", "fatal: bad config"),
    )
    with pytest.raises(ApplicationError, match="bad config"):
        select_project(tmp_path)


def test_all_cwd_occurrences_validated_before_effects(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Registry", lambda: pytest.fail("registry initialized"))
    assert (
        cli.main(
            ["--format", "json", "--cwd", str(tmp_path / "missing"), "env", "--cwd", str(tmp_path)]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "invalid_directory"
    assert result["exit_code"] == 1


@pytest.mark.parametrize(
    "command", [["--help"], ["--version"], ["help", "init"], ["completion", "bash"]]
)
def test_information_does_not_select_directory(command, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "resolve_start_directory", lambda *_a: pytest.fail("resolved directory")
    )
    monkeypatch.setattr(cli, "Registry", lambda: pytest.fail("registry initialized"))
    try:
        result = cli.main(["--cwd", "/unavailable", *command])
    except SystemExit as error:
        result = error.code
    assert result == 0
    assert capsys.readouterr().out


def test_registry_failure_before_legacy_handler_uses_envelope(tmp_path, monkeypatch, capsys):
    def denied():
        raise PermissionError("state denied")

    monkeypatch.setattr(cli, "Registry", denied)
    assert cli.main(["--cwd", str(tmp_path), "sync", "--format", "json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == {"code": "io_error", "message": "state denied"}


def test_cli_normalization_and_completion_use_selected_project(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    nested = tmp_path / "src"
    nested.mkdir()
    (tmp_path / RECIPE_NAME).write_text('[targets.simulator.phone]\nmodel = "iPhone"\n')
    (nested / "splashdown.local.toml").write_text("invalid = [")
    args = Namespace(cwd=str(nested), dtype="sim", variant=None)
    args.selection = select_project(resolve_start_directory(args.cwd))
    cli._normalize_device_args(args)
    assert args.dtype == "simulator"
    monkeypatch.setattr(cli, "Registry", lambda: pytest.fail("registry initialized"))
    assert completion.variant_completer("", Namespace(cwd=str(nested), dtype="sim")) == ["phone"]
    assert completion.device_arg_completer("", Namespace(cwd=str(nested))) == ["phone", "simulator"]


def test_completion_selection_failure_is_quiet(tmp_path, monkeypatch, capsys):
    def failure(*_args, **_kwargs):
        raise ApplicationError("broken git")

    monkeypatch.setattr(completion, "select_project", failure)
    assert completion.variant_completer("", Namespace(cwd=str(tmp_path))) == []
    assert completion.device_arg_completer("", Namespace(cwd=str(tmp_path))) == []
    assert capsys.readouterr() == ("", "")


def test_cli_init_keeps_exact_directory(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / RECIPE_NAME).write_text("")
    nested = tmp_path / "nested"
    nested.mkdir()
    selected = []
    monkeypatch.setattr(cli, "cmd_init", lambda cwd, **_kwargs: selected.append(cwd))
    assert cli.main(["--cwd", str(nested), "init"]) == 0
    assert selected == [nested]


def test_empty_env_report_is_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert cli.main(["--cwd", str(tmp_path), "env"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"(empty) {tmp_path}\n"
    assert captured.err == ""


def test_missing_process_cwd_has_structured_error(monkeypatch, capsys):
    def missing():
        raise FileNotFoundError("working directory removed")

    monkeypatch.setattr(Path, "cwd", missing)
    assert cli.main(["--format", "json", "env"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_directory"


@pytest.mark.parametrize(
    "argv",
    [
        ["gc"],
        ["status", "all"],
        ["target", "prune"],
        ["target", "refresh"],
        ["hook", "post-checkout", "old", "new", "1"],
    ],
)
def test_exact_security_and_machinewide_paths_skip_recipe_lookup(tmp_path, monkeypatch, argv):
    monkeypatch.setattr(cli, "select_project", lambda *_a, **_k: pytest.fail("project lookup"))
    state = cli.ParseState()
    args = cli._build_parser(state).parse_args(["--cwd", str(tmp_path), *argv])
    assert cli._select_command_directory(args, state) == tmp_path
    assert args.selection == ProjectSelection(tmp_path, tmp_path, None, None)


def test_dispatch_uses_same_selected_directory_for_normalization_and_handler(
    tmp_path, monkeypatch, registry
):
    _git(tmp_path, "init", "-q")
    (tmp_path / RECIPE_NAME).write_text('[targets.simulator.phone]\nmodel = "iPhone"\n')
    nested = tmp_path / "nested"
    nested.mkdir()
    monkeypatch.setattr(cli, "Registry", lambda: registry)
    calls = []

    def run(cwd, reg, dtype, variant):
        calls.append((cwd, reg, dtype, variant))
        return 0

    monkeypatch.setattr(cli, "cmd_run", run)
    assert cli.main(["--cwd", str(nested), "run", "sim", "phone"]) == 0
    assert calls == [(tmp_path, registry, "simulator", "phone")]


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_stdout_writer_data_preserved_without_routine_values(tmp_path, monkeypatch, capsys, fmt):
    (tmp_path / RECIPE_NAME).write_text(
        '[resources.SECRET]\ntype = "set"\ndefault = "hidden-value"\n'
        '[resources.DATA]\ntype = "set"\ndefault = "raw-value"\nwriter = "stdout"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert cli.main(["--cwd", str(tmp_path), "sync", "--format", fmt]) == 0
    captured = capsys.readouterr()
    if fmt == "json":
        assert json.loads(captured.out) == {
            "writers": ["splashdown.env: 1 vars", "stdout: 1 vars"],
            "stdout": {"DATA": "raw-value"},
            "setup": [],
            "changed": True,
            "changed_keys": ["DATA", "SECRET"],
            "resolved_keys": ["DATA", "SECRET"],
        }
    else:
        assert captured.out == "DATA=raw-value\n"
        assert "SECRET (changed)" in captured.err
    assert "hidden-value" not in captured.out + captured.err


def test_selection_interrupt_uses_recognized_json(monkeypatch, capsys):
    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "resolve_start_directory", interrupt)
    assert cli.main(["--format", "json", "env"]) == 130
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == {"code": "interrupted", "message": "Operation interrupted."}
    assert payload["exit_code"] == 130


def test_env_nearest_registered_identity_and_closer_recipe(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / RECIPE_NAME).write_text("")
    nested = tmp_path / "nested"
    child = nested / "child"
    child.mkdir(parents=True)
    known = [str(nested)]
    assert select_project(child, known_checkouts=known).directory == nested
    (child / RECIPE_NAME).write_text("broken = [")
    assert select_project(child, known_checkouts=known).directory == child


def test_env_deleted_explicit_identity_skips_git_and_ancestors(tmp_path, monkeypatch, capsys):
    from splashdown.constants import state_directory

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    state_directory().mkdir(parents=True)
    missing = tmp_path / "deleted"
    (state_directory() / "kv.tsv").write_text(f"{missing}\tKEY\tstored\n")
    monkeypatch.setattr(
        project_selection, "_worktree_root", lambda *_a: pytest.fail("Git discovery")
    )
    assert cli.main(["env", "get", "KEY", "--cwd", str(missing), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == {
        "checkout": str(missing),
        "key": "KEY",
        "value": "stored",
    }
    assert cli.main(["--cwd", str(missing), "env", "release"]) == 1
    assert "Invalid starting directory" in capsys.readouterr().err


def test_env_validates_overridden_cwd_and_does_not_admit_known_file(tmp_path, monkeypatch, capsys):
    from splashdown.constants import state_directory

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    state_directory().mkdir(parents=True)
    file = tmp_path / "file"
    file.write_text("")
    (state_directory() / "kv.tsv").write_text(f"{file}\tKEY\tstored\n")
    for bad in (file, tmp_path / "unknown"):
        assert cli.main(["--format", "json", "--cwd", str(bad), "env", "--cwd", str(tmp_path)]) == 1
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_directory"
