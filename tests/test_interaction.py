import sys

import pytest

from splashdown.errors import ApplicationError
from splashdown.interaction import require_confirmation


@pytest.mark.parametrize("fmt,terminal", [("json", True), ("text", False)])
def test_unavailable_confirmation_never_reads(fmt, terminal, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: terminal)
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("must not read"))
    with pytest.raises(ApplicationError) as caught:
        require_confirmation("Delete?", yes=False, fmt=fmt, next_step="Use --yes")
    assert caught.value.code == "confirmation_required"
    assert caught.value.next_steps == ("Use --yes",)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("answer", ["", "n", "no", "anything", EOFError])
def test_declined_confirmation(answer, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def read():
        if answer is EOFError:
            raise EOFError
        return answer

    monkeypatch.setattr("builtins.input", read)
    with pytest.raises(ApplicationError) as caught:
        require_confirmation("Delete?", yes=False, next_step="Use --yes")
    assert (caught.value.code, caught.value.exit_code) == ("confirmation_declined", 1)
    assert capsys.readouterr().err == "Delete? [y/N] "


@pytest.mark.parametrize("answer", ["y", " YES "])
def test_affirmative_confirmation(answer, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: answer)
    require_confirmation("Delete?", yes=False, next_step="Use --yes")


def test_yes_does_not_read_even_in_json(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("must not read"))
    require_confirmation("Delete?", yes=True, fmt="json", next_step="Use --yes")


def test_interruption_propagates(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def read():
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", read)
    with pytest.raises(KeyboardInterrupt):
        require_confirmation("Delete?", yes=False, next_step="Use --yes")


@pytest.mark.parametrize("command", ["destroy", "prune"])
@pytest.mark.parametrize(
    "response,terminal,code",
    [(EOFError, True, 1), (KeyboardInterrupt, True, 130), ("n", True, 1), ("yes", False, 1)],
)
def test_destructive_cli_refusal_preserves_devices(
    tmp_path, monkeypatch, command, response, terminal, code
):
    import splashdown as sd

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 15"\n')
    registry = sd.Registry()
    registry.set_device(str(tmp_path.resolve()), "simulator", "default", "owned", "Phone", "18.5")
    destroyed = []
    monkeypatch.setattr(sd.target_commands, "device_destroy_row", destroyed.append)
    monkeypatch.setattr(sd.target_commands, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.target_commands, "ios_shutdown", destroyed.append)
    monkeypatch.setattr(
        sd.target_commands, "_discover_foreign_ios", lambda managed: [("foreign", "Phone", "18.5")]
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: terminal)

    def read():
        if not terminal:
            pytest.fail("must not read a pipe")
        if isinstance(response, type):
            raise response
        return response

    monkeypatch.setattr("builtins.input", read)
    arguments = ["destroy", "simulator"] if command == "destroy" else ["target", "prune", "ios"]
    assert sd.main(["--cwd", str(tmp_path), *arguments]) == code
    assert destroyed == []
    assert (
        registry.get_device(str(tmp_path.resolve()), "simulator", "default").identifier == "owned"
    )
