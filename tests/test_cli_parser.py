import json

import pytest

from splashdown import cli


@pytest.mark.parametrize("path", [[], ["ai"], ["ai", "status"]])
def test_globals_at_every_depth(path):
    state = cli.ParseState()
    rest = ["ai", "status"][len(path) :]
    args = cli._build_parser(state).parse_args(
        [*path, "--cwd", "chosen", "--format", "json", *rest]
    )
    assert (args.cwd, args.format) == ("chosen", "json")
    assert (state.command, state.formats, state.cwd_values) == ("ai status", ["json"], ["chosen"])


@pytest.mark.parametrize("first,last", [("json", "text"), ("text", "json")])
def test_last_global_wins_and_records_all_occurrences(first, last):
    state = cli.ParseState()
    args = cli._build_parser(state).parse_args(
        [
            "--cwd",
            "first",
            "--format",
            first,
            "ai",
            "--cwd",
            "second",
            "--format",
            last,
            "status",
            "--cwd",
            "missing-is-selection-policy",
        ]
    )
    assert (args.cwd, args.format) == ("missing-is-selection-policy", last)
    assert state.formats == [first, last]
    assert state.cwd_values == ["first", "second", "missing-is-selection-policy"]


@pytest.mark.parametrize(
    "argv,command",
    [
        (["--format", "json", "no-command"], None),
        (["ai", "--format", "json"], "ai"),
        (["ai", "--format", "json", "no-action"], "ai"),
        (["ai", "update", "--format", "json", "--replac"], "ai update"),
        (["--unknown", "--format", "json", "ai", "status"], "ai status"),
        (["--format", "json", "ai", "--format", "invalid", "status", "--format", "text"], "ai"),
    ],
)
def test_usage_errors_use_recognized_json_and_command(argv, command, capsys):
    assert cli.main(argv) == 2
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["command"] == command
    assert payload["error"]["code"] == "invalid_arguments"
    assert payload["status"] == "error"
    assert output.err.startswith(f"usage: splash{(' ' + command) if command else ''} ")


@pytest.mark.parametrize(
    "argv",
    [
        ["--format", "invalid", "ai", "status", "--format", "json"],
        ["no-command", "--format", "json"],
        ["--format", "json", "ai", "--format", "text", "status", "--bad"],
        ["--for", "json", "ai", "status"],
    ],
)
def test_unrecognized_or_superseded_json_uses_native_error(argv, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["--format", "json", "ai", "update", "-h"], "usage: splash ai update"),
        (["--format", "json", "--version"], "splashdown "),
    ],
)
def test_native_information_ignores_json(argv, expected, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code == 0
    assert capsys.readouterr().out.startswith(expected)


def test_context_help_uses_exact_parser_tree_without_selection(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_resolve_cwd", lambda _: pytest.fail("selected directory for help"))
    assert cli.main(["--format", "json", "--cwd", "missing", "help", "target", "add"]) == 0
    assert capsys.readouterr().out.startswith("usage: splash target add")
    assert cli.main(["--format", "json", "help", "target", "ad"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_arguments"


def test_completion_native_and_unsupported_json_before_effects(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "cmd_completion", lambda shell: calls.append(shell) or 0)
    assert cli.main(["completion", "bash", "--format", "json"]) == 2
    assert calls == []
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "unsupported_format"
    assert cli.main(["completion", "bash", "--format", "text"]) == 0
    assert calls == ["bash"]


def test_default_sync_selected_after_parse_preserves_flags(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "Registry", object)
    monkeypatch.setattr(cli, "_consume_claim_notices", lambda *_: None)
    monkeypatch.setattr(cli, "_cmd_provision", lambda args, *_: calls.append(args) or 0)
    assert cli.main(["--cwd", str(tmp_path), "--force", "--setup=dev"]) == 0
    assert (calls[0].cmd, calls[0].force, calls[0].setup) == ("sync", True, "dev")


def test_end_of_options_and_option_looking_values_are_not_rewritten():
    args = cli._build_parser().parse_args(["env", "get", "--", "--format"])
    assert args.key == "--format"
    args = cli._build_parser().parse_args(["--cwd=--help", "ai", "status"])
    assert args.cwd == "--help"


@pytest.mark.parametrize("option", [["--force"], ["--setup", "dev"]])
def test_bare_sync_flags_reject_other_commands(option, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main([*option, "doctor"])
    assert error.value.code == 2
    assert "require sync" in capsys.readouterr().err
