"""Tests for splashdown completion behavior."""

from __future__ import annotations

import os
import sys

import pytest

import splashdown as sd
from conftest import (
    ROOT,
    _write_recipe,
)


def test_argcomplete_marker_present_in_cli():
    cli_src = (ROOT / "src" / "splashdown" / "cli.py").read_text()
    # Marker must be within the first 1 KB so argcomplete's wrapper-follow finds it.
    assert "# PYTHON_ARGCOMPLETE_OK" in cli_src[:1024]


def test_argcomplete_importable():
    import argcomplete  # noqa: F401


from argparse import Namespace

from splashdown.completion import device_arg_completer, variant_completer


def test_variant_completer_lists_variants_for_typed_dtype(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.simulator.small-screen]\nmodel = "B"\n',
    )
    args = Namespace(cwd=str(checkout), dtype="simulator")
    assert variant_completer("", args) == ["default", "small-screen"]


def test_variant_completer_prefix_filters(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.simulator.small-screen]\nmodel = "B"\n',
    )
    args = Namespace(cwd=str(checkout), dtype="simulator")
    assert variant_completer("sm", args) == ["small-screen"]


def test_variant_completer_infers_single_type_when_dtype_none(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.simulator.tablet]\nmodel = "B"\n',
    )
    args = Namespace(cwd=str(checkout), dtype=None)
    assert variant_completer("", args) == ["default", "tablet"]


def test_variant_completer_dedupes_across_types(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.emulator.default]\nimage = "X"\n',
    )
    args = Namespace(cwd=str(checkout), dtype=None)
    # `default` declared under both types must appear once.
    assert variant_completer("", args) == ["default"]


def test_device_arg_completer_offers_variants_for_single_type(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.simulator.tablet]\nmodel = "B"\n',
    )
    args = Namespace(cwd=str(checkout), dtype=None)
    # type name + variant names, sorted, deduped.
    assert device_arg_completer("", args) == ["default", "simulator", "tablet"]


def test_device_arg_completer_offers_only_type_names_for_multi_type(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.emulator.default]\nimage = "X"\n',
    )
    args = Namespace(cwd=str(checkout), dtype=None)
    # Two declared types: offer only type names, no variants.
    assert device_arg_completer("", args) == ["emulator", "simulator"]


def test_completer_fail_silent_on_malformed_toml(checkout):
    _write_recipe(checkout, "this is not = valid toml [[[")
    args = Namespace(cwd=str(checkout), dtype="simulator")
    assert variant_completer("", args) == []
    assert device_arg_completer("", args) == []


from splashdown.cli import _normalize_device_args
from splashdown.devices import DeviceError


def test_normalize_leaves_explicit_type_and_variant():
    args = Namespace(dtype="simulator", variant="small-screen")
    _normalize_device_args(args)
    assert (args.dtype, args.variant) == ("simulator", "small-screen")


def test_normalize_leaves_bare_type():
    args = Namespace(dtype="simulator", variant=None)
    _normalize_device_args(args)
    assert (args.dtype, args.variant) == ("simulator", None)


def test_normalize_reinterprets_lone_variant():
    args = Namespace(dtype="small-screen", variant=None)
    _normalize_device_args(args)
    assert (args.dtype, args.variant) == (None, "small-screen")


def test_normalize_leaves_nothing():
    args = Namespace(dtype=None, variant=None)
    _normalize_device_args(args)
    assert (args.dtype, args.variant) == (None, None)


def test_normalize_rejects_nontype_with_variant():
    args = Namespace(dtype="foo", variant="bar")
    with pytest.raises(DeviceError):
        _normalize_device_args(args)


def test_normalize_type_name_wins_as_type():
    args = Namespace(dtype="simulator", variant=None)
    _normalize_device_args(args)
    assert (args.dtype, args.variant) == ("simulator", None)


def test_run_accepts_lone_variant(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text(
        '[targets.simulator.small-screen]\nmodel = "iPhone SE"\n'
    )
    captured = {}

    def fake_cmd_run(cwd, registry, dtype, variant):
        captured["dtype"] = dtype
        captured["variant"] = variant
        return 0

    # main() resolves `cmd_run` in the cli module's namespace (it does
    # `from .commands import cmd_run`), so patch it there, not on the package.
    monkeypatch.setattr("splashdown.cli.cmd_run", fake_cmd_run)
    rc = sd.main(["--cwd", str(tmp_path), "run", "small-screen"])
    assert rc == 0
    assert captured == {"dtype": None, "variant": "small-screen"}


def test_run_rejects_nontype_with_variant_via_main(tmp_path):
    (tmp_path / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "A"\n')
    # `foo` is not a device type and a variant is already given -> DeviceError,
    # which main()'s try/except turns into exit code 1 (not an uncaught crash).
    rc = sd.main(["--cwd", str(tmp_path), "run", "foo", "bar"])
    assert rc == 1


import io


def _argcomplete_completions(parser, comp_line, cwd):
    """Drive argcomplete in-process via its env protocol and return the list of
    completion strings it would emit for `comp_line`."""
    import contextlib

    import argcomplete

    env = {
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\013",
        "_ARGCOMPLETE_SUPPRESS_SPACE": "1",
        "COMP_LINE": comp_line,
        "COMP_POINT": str(len(comp_line)),
    }
    out = io.StringIO()
    saved = dict(os.environ)
    saved_cwd = os.getcwd()
    os.environ.update(env)
    os.chdir(cwd)
    # argcomplete's completion protocol unconditionally reopens its debug stream
    # via `os.fdopen(9, "w")` (finders.py) and never closes it. Under pytest, fd 9
    # is the faulthandler's dup of stderr, so the dangling wrapper collides with
    # pytest's own close of fd 9 at teardown ("Bad file descriptor"). Divert just
    # that fd-9 open to an in-memory stream so argcomplete never touches the real
    # fd. (fd 8, the completion output, is already handled via `output_stream`.)
    real_fdopen = os.fdopen

    def _fdopen(fd, *args, **kwargs):
        if fd == 9:
            return io.StringIO()
        return real_fdopen(fd, *args, **kwargs)

    os.fdopen = _fdopen
    try:
        with contextlib.suppress(SystemExit):
            argcomplete.autocomplete(parser, exit_method=sys.exit, output_stream=out)
    finally:
        os.fdopen = real_fdopen
        os.environ.clear()
        os.environ.update(saved)
        os.chdir(saved_cwd)
    return out.getvalue().split("\013")


def test_comp_line_offers_variants_for_run_single_type(tmp_path):
    (tmp_path / "splashdown.toml").write_text(
        '[targets.simulator.default]\nmodel = "A"\n[targets.simulator.small-screen]\nmodel = "B"\n'
    )
    parser = sd._build_parser()
    out = _argcomplete_completions(parser, "splash run ", tmp_path)
    assert "small-screen" in out
    assert "default" in out


def test_install_is_noop_without_argcomplete_env(monkeypatch):
    from splashdown.completion import install

    # No _ARGCOMPLETE in env -> returns without importing/inspecting.
    monkeypatch.delenv("_ARGCOMPLETE", raising=False)
    assert install(sd._build_parser()) is None


# --- `splash completion` subcommand (shell registration shellcode) ---


def test_completion_zsh_outputs_native_zsh(capsys):
    rc = sd.cmd_completion("zsh")
    out = capsys.readouterr().out
    assert rc == 0
    assert "#compdef splash" in out  # native zsh, no bashcompinit needed


def test_completion_bash_outputs_shellcode(capsys):
    rc = sd.cmd_completion("bash")
    out = capsys.readouterr().out
    assert rc == 0
    assert "splash" in out
    assert "complete" in out


def test_completion_autodetects_shell_from_env(monkeypatch, capsys):
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    rc = sd.cmd_completion(None)
    assert rc == 0
    assert "#compdef splash" in capsys.readouterr().out


def test_completion_rejects_unsupported_shell(capsys):
    rc = sd.cmd_completion("notashell")
    assert rc != 0
    assert "notashell" in capsys.readouterr().err


def test_completion_via_main_registers_subcommand(capsys):
    rc = sd.main(["completion", "zsh"])
    assert rc == 0
    assert "#compdef splash" in capsys.readouterr().out


def test_completion_autodetect_falls_back_to_bash_when_shell_unset(monkeypatch, capsys):
    monkeypatch.delenv("SHELL", raising=False)
    rc = sd.cmd_completion(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "complete" in out  # bash shellcode, not zsh


def test_completion_autodetect_empty_shell_falls_back_to_bash(monkeypatch, capsys):
    monkeypatch.setenv("SHELL", "")
    rc = sd.cmd_completion(None)
    assert rc == 0
    assert "complete" in capsys.readouterr().out


def test_completion_via_main_autodetects_from_env(monkeypatch, capsys):
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    rc = sd.main(["completion"])
    assert rc == 0
    assert "#compdef splash" in capsys.readouterr().out


def test_device_arg_completer_includes_global_device(checkout):
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[targets.device.my-iphone]\nplatform = "ios"\n')
    args = Namespace(cwd=str(checkout), dtype=None)
    assert "my-iphone" in device_arg_completer("", args)


def test_completer_silent_on_malformed_global(checkout):
    (checkout / sd.RECIPE_NAME).write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[targets.bogus.x]\n")
    args = Namespace(cwd=str(checkout), dtype="simulator")
    assert variant_completer("", args) == []


def test_device_arg_completer_offers_project_variants_despite_global_device(checkout):
    (checkout / sd.RECIPE_NAME).write_text(
        '[targets.simulator.default]\nmodel = "iPhone 17"\n'
        '[targets.simulator.tablet]\nmodel = "iPad"\n'
    )
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[targets.device.my-iphone]\nplatform = "ios"\n')
    args = Namespace(cwd=str(checkout), dtype=None)
    out = device_arg_completer("", args)
    # project has exactly one type (simulator) -> its variants are still offered,
    # and the always-available global device type name is offered too
    assert "default" in out
    assert "tablet" in out
    assert "device" in out
