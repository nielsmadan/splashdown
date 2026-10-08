import json
from pathlib import Path

import pytest

from splashdown.cli_output import emit_result, render_ai_result
from splashdown.errors import ApplicationError, UsageError
from splashdown.results import CommandResult, Diagnostic, result_payload


def test_complete_envelope_and_ordered_deduplication(capsys):
    warning = Diagnostic("notice", "Notice")
    result = CommandResult(
        "ai status",
        "success",
        0,
        {"files": [], "activation": "Reload"},
        warnings=(warning, warning),
        next_steps=("Next", "Next"),
    )
    assert emit_result(result, "json", render_text=render_ai_result) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "command": "ai status",
        "status": "success",
        "exit_code": 0,
        "data": {"files": [], "activation": "Reload"},
        "error": None,
        "warnings": [{"code": "notice", "message": "Notice"}],
        "next_steps": ["Next"],
    }
    assert captured.err == ""
    assert emit_result(result, "text", render_text=render_ai_result) == 0
    captured = capsys.readouterr()
    assert captured.out == "Reload\n"
    assert captured.err == "warning: Notice\nNext\n"


@pytest.mark.parametrize(
    "result",
    [
        CommandResult(None, "success", 1),
        CommandResult(None, "success", 0, error=Diagnostic("bad", "Bad")),
        CommandResult(None, "error", 0, error=Diagnostic("bad", "Bad")),
        CommandResult(None, "error", 1),
        CommandResult(None, "partial", 1, error=Diagnostic("bad", "Bad")),
        CommandResult(None, "success", 0, {"path": Path(".")}),
        CommandResult(None, "success", 0, {"number": float("nan")}),
        CommandResult(None, "success", 0, {"values": (1, 2)}),
    ],
)
def test_invalid_results_are_rejected(result):
    with pytest.raises(ValueError):
        result_payload(result)


def test_deeply_nested_result_data_is_valid():
    nested = "leaf"
    for _ in range(1500):
        nested = [nested]
    data = {"nested": nested}
    assert result_payload(CommandResult(None, "success", 0, data))["data"] is data


def test_cyclic_result_data_is_rejected():
    nested = []
    nested.append(nested)
    with pytest.raises(ValueError, match="JSON-native"):
        result_payload(CommandResult(None, "success", 0, {"nested": nested}))
    mapping = {}
    mapping["self"] = mapping
    with pytest.raises(ValueError, match="JSON-native"):
        result_payload(CommandResult(None, "success", 0, mapping))


def test_shared_acyclic_result_data_is_valid():
    shared = [1]
    data = {"first": shared, "second": shared}
    assert result_payload(CommandResult(None, "success", 0, data))["data"] is data


def test_exception_contract():
    error = UsageError("Bad argument")
    assert (error.code, error.exit_code, error.is_error) == ("invalid_arguments", 2, False)
    with pytest.raises(ValueError, match="partial requires"):
        ApplicationError("Bad", partial=True)
