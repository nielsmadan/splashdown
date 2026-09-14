from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tomllib

import pytest

import splashdown as sd
from conftest import _git_init


class _TTYInput(io.StringIO):
    def isatty(self):
        return True


def test_file_name_constants():
    assert sd.RECIPE_NAME == "splashdown.toml"
    assert sd.LOCAL_NAME == "splashdown.local.toml"
    assert sd.ENV_FILE_NAME == "splashdown.env"


def test_cli_prog_name_is_splash():
    assert sd._build_parser().prog == "splash"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("target", "claims"), {"target_cmd": "claims", "target_format": None}),
        (
            ("target", "claims", "--format", "json"),
            {"target_cmd": "claims", "target_format": "json"},
        ),
        (("target", "claim", "pixel"), {"variant": "pixel", "available": None}),
        (
            ("target", "claim", "--available", "android"),
            {"variant": None, "available": "android"},
        ),
        (
            ("target", "claim", "--available", "ios"),
            {"variant": None, "available": "ios"},
        ),
        (
            ("target", "claim", "--available", "any"),
            {"variant": None, "available": "any"},
        ),
        (("target", "claim", "pixel", "--force"), {"variant": "pixel", "force": True}),
        (("target", "release", "pixel"), {"variant": "pixel", "all_owned": False}),
        (("target", "release", "--all"), {"variant": None, "all_owned": True}),
        (
            ("target", "release", "pixel", "--force"),
            {"variant": "pixel", "force": True},
        ),
    ],
)
def test_target_claim_parser_accepts_command_surface(argv, expected):
    parsed = sd._build_parser().parse_args(list(argv))

    assert {name: getattr(parsed, name) for name in expected} == expected


@pytest.mark.parametrize(
    "argv",
    [
        ("target", "claim"),
        ("target", "claim", "pixel", "--available", "android"),
        ("target", "claim", "--available", "android", "--force"),
        ("target", "release"),
        ("target", "release", "pixel", "--all"),
        ("target", "release", "--all", "--force"),
    ],
)
def test_target_claim_dispatch_rejects_invalid_shapes(tmp_path, argv, capsys):
    assert sd.main(["--cwd", str(tmp_path), *argv]) == 2
    assert "requires" in capsys.readouterr().err


def test_target_claim_post_subcommand_format_matches_top_level_format(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        sd.target_commands,
        "cmd_target_claims",
        lambda _registry, fmt: calls.append(fmt) or 0,
        raising=False,
    )

    assert sd.main(["--cwd", str(tmp_path), "--format", "json", "target", "claims"]) == 0
    assert sd.main(["--cwd", str(tmp_path), "target", "claims", "--format", "json"]) == 0

    assert calls == ["json", "json"]


def test_cli_help_shows_tiers(capsys):
    with pytest.raises(SystemExit) as exc:
        sd.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    visible_commands = {
        line.split()[0] for line in out.splitlines() if line.startswith("  ") and line.split()
    }
    normalized = " ".join(out.split())
    assert sd.KNOWN_CMDS - {"hook"} <= visible_commands
    assert "output format for sync, status, init, env/target lists, or target claims" in normalized
    assert "include resolved values for sync, status, or bare env" in normalized
    assert "provision" not in out


def _pending_claim_notice(checkout, *, action="transfer"):
    return sd.ClaimNotice(
        previous_owner=str(checkout.resolve()),
        catalog_identity="recipe:/repo:device:pixel",
        target_label="pixel",
        action=action,
        actor_checkout=str((checkout.parent / "new-owner").resolve()),
        event_at="2026-08-26T10:00:00+00:00",
        expires_at="2099-09-25T10:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("argv", "handler"),
    [
        (("sync",), "_cmd_provision"),
        (("status",), "cmd_status"),
        (("run",), "cmd_run"),
        (("target", "claims"), "_target_dispatch"),
        (("trust",), "cmd_trust"),
        (("untrust",), "cmd_untrust"),
        (("bootstrap",), "cmd_bootstrap"),
    ],
)
def test_claim_notice_next_checkout_command_prints_and_consumes_once(
    tmp_path, monkeypatch, capsys, argv, handler
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    registry = sd.Registry()
    notice = _pending_claim_notice(checkout)
    registry.add_claim_notices([notice])
    monkeypatch.setattr(sd.cli, handler, lambda *_args, **_kwargs: 0)

    assert sd.main(["--cwd", str(checkout), *argv]) == 0
    assert sd.main(["--cwd", str(checkout), *argv]) == 0

    warning = (
        f"warning: physical target pixel was claimed by {notice.actor_checkout} "
        f"at {notice.event_at}; this checkout no longer owns it"
    )
    assert capsys.readouterr().err.count(warning) == 1
    assert registry.consume_claim_notices(str(checkout.resolve())) == ()


def test_claim_notice_is_consumed_before_a_command_that_later_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    registry = sd.Registry()
    notice = _pending_claim_notice(checkout, action="release")
    registry.add_claim_notices([notice])

    def fail_later(*_args, **_kwargs):
        raise ValueError("later failure")

    monkeypatch.setattr(sd.cli, "_cmd_provision", fail_later)

    assert sd.main(["--cwd", str(checkout), "sync"]) == 1

    err = capsys.readouterr().err
    assert "physical target pixel was force-released" in err
    assert "error: later failure" in err
    assert registry.consume_claim_notices(str(checkout.resolve())) == ()


@pytest.mark.parametrize("boundary", ["completion", "help", "version", "argcomplete", "hook"])
def test_claim_notice_early_boundary_does_not_construct_registry_or_consume(
    tmp_path, monkeypatch, boundary
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    registry = sd.Registry()
    notice = _pending_claim_notice(checkout)
    registry.add_claim_notices([notice])
    monkeypatch.setattr(
        sd.cli,
        "Registry",
        lambda: pytest.fail(f"{boundary} constructed a registry"),
    )

    expects_exit = boundary in ("help", "version", "argcomplete")
    if boundary == "completion":
        monkeypatch.setattr(sd.cli, "cmd_completion", lambda _shell: 0)
        argv = ["--cwd", str(checkout), "completion"]
    elif boundary == "help":
        argv = ["--cwd", str(checkout), "--help"]
    elif boundary == "version":
        monkeypatch.setattr("splashdown._version.resolve_version", lambda: "test")
        argv = ["--cwd", str(checkout), "--version"]
    elif boundary == "argcomplete":
        monkeypatch.setattr(
            sd.completion,
            "install",
            lambda _parser: (_ for _ in ()).throw(SystemExit(0)),
        )
        argv = ["--cwd", str(checkout), "status"]
    else:
        monkeypatch.setattr(sd.cli, "cmd_post_checkout_hook", lambda *_args: 0)
        argv = ["--cwd", str(checkout), "hook", "post-checkout", "old", "new", "1"]

    if expects_exit:
        with pytest.raises(SystemExit) as error:
            sd.main(argv)
        assert error.value.code == 0
    else:
        assert sd.main(argv) == 0

    assert registry.consume_claim_notices(str(checkout.resolve())) == (notice,)


def test_claim_notice_bootstrap_reuses_consuming_registry(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    registry = sd.Registry(
        port_file=tmp_path / "registry" / "ports.tsv",
        kv_file=tmp_path / "registry" / "kv.tsv",
        device_file=tmp_path / "registry" / "devices.tsv",
        claim_file=tmp_path / "registry" / "claims.tsv",
        claim_notice_file=tmp_path / "registry" / "claim-notices.tsv",
    )
    registry.add_claim_notices([_pending_claim_notice(checkout)])
    received = []
    monkeypatch.setattr(sd.cli, "Registry", lambda: registry)
    monkeypatch.setattr(
        sd.cli,
        "cmd_bootstrap",
        lambda _cwd, passed_registry=None, *, rerun: received.append(passed_registry) or 0,
    )

    assert sd.main(["--cwd", str(checkout), "bootstrap"]) == 0

    assert received == [registry]


def test_claim_notice_store_error_warns_and_continues_to_handler(tmp_path, monkeypatch, capsys):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    registry = sd.Registry(
        port_file=tmp_path / "registry" / "ports.tsv",
        kv_file=tmp_path / "registry" / "kv.tsv",
        device_file=tmp_path / "registry" / "devices.tsv",
        claim_file=tmp_path / "registry" / "claims.tsv",
        claim_notice_file=tmp_path / "registry" / "claim-notices.tsv",
    )
    notice = _pending_claim_notice(checkout)
    registry.add_claim_notices([notice])
    consume = registry.consume_claim_notices
    handled: list[str] = []
    monkeypatch.setattr(sd.cli, "Registry", lambda: registry)
    monkeypatch.setattr(
        registry,
        "consume_claim_notices",
        lambda _owner: (_ for _ in ()).throw(OSError("notice store unavailable")),
    )
    monkeypatch.setattr(
        sd.cli,
        "_cmd_provision",
        lambda *_args, **_kwargs: handled.append("sync") or 0,
    )

    assert sd.main(["--cwd", str(checkout), "sync"]) == 0

    assert handled == ["sync"]
    err = capsys.readouterr().err
    assert "warning: unable to consume physical target notices: notice store unavailable" in err
    assert "Traceback" not in err
    assert consume(str(checkout.resolve())) == (notice,)


def test_cli_parser_commands_match_known_commands():
    parser = sd._build_parser()
    command_action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )

    assert set(command_action.choices) == sd.KNOWN_CMDS


@pytest.mark.parametrize("command", ["env", "target"])
def test_cli_nested_help_explains_bare_list(command, capsys):
    with pytest.raises(SystemExit) as exc:
        sd.main([command, "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Omit ACTION to list" in out
    assert "[ACTION]" in out.splitlines()[0]


@pytest.mark.parametrize("command", ["sync", "status", "env"])
def test_cli_help_points_to_supported_value_output_flags(command, capsys):
    with pytest.raises(SystemExit) as exc:
        sd.main([command, "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "place before the command" in out
    assert "--format" in out
    assert "--show-values" in out


def test_cli_target_help_points_to_supported_format_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        sd.main(["target", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "place before the command" in out
    assert "--format" in out


@pytest.mark.parametrize("existing_recipe", [False, True])
@pytest.mark.parametrize("option", ["--rescan", "--allow-nested", "--no-sync"])
def test_cli_init_rejects_removed_option(option, existing_recipe, tmp_path, monkeypatch, capsys):
    recipe_path = tmp_path / sd.RECIPE_NAME
    original = '[resources.RUN_ID]\ntype = "uuid"\n'
    if existing_recipe:
        recipe_path.write_text(original)

    def unexpected_init(*_args, **_kwargs):
        pytest.fail("rejected option dispatched init")

    monkeypatch.setattr(sd.cli, "cmd_init", unexpected_init)

    with pytest.raises(SystemExit) as exc:
        sd.main(["--cwd", str(tmp_path), "init", option])

    assert exc.value.code == 2
    assert f"unrecognized arguments: {option}" in capsys.readouterr().err
    if existing_recipe:
        assert recipe_path.read_text() == original
    else:
        assert not recipe_path.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--format", "json", "doctor"],
        ["--show-values", "run"],
        ["--format", "json", "env", "get", "KEY"],
        ["--show-values", "env", "get", "KEY"],
        ["--format", "json", "target", "refresh"],
        ["--show-values", "target"],
        ["--format", "json", "init", "minimal"],
        ["--show-values", "init"],
    ],
)
def test_cli_rejects_output_flags_where_they_are_ignored(argv, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def unexpected_dispatch(*_args, **_kwargs):
        pytest.fail("command dispatched before output-option validation")

    for name in (
        "cmd_doctor",
        "cmd_run",
        "cmd_init",
        "_env_dispatch",
        "_target_dispatch",
    ):
        monkeypatch.setattr(sd.cli, name, unexpected_dispatch)

    with pytest.raises(SystemExit) as exc:
        sd.main(["--cwd", str(tmp_path), *argv])

    assert exc.value.code == 2
    assert argv[0] in capsys.readouterr().err


def test_cli_target_add_help_explains_type_specific_options(capsys):
    with pytest.raises(SystemExit) as exc:
        sd.main(["target", "add", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for phrase in (
        "iOS simulator model",
        "iOS runtime version",
        "Android emulator hardware profile",
        "Android system image",
        "simulator: --model, --ios, --name",
        "emulator: --device, --image, --name",
        "device: --name, --id, --platform",
    ):
        assert phrase in out


def test_cli_target_maintenance_help_describes_instance_lifecycle(capsys):
    with pytest.raises(SystemExit) as exc:
        sd.main(["target", "refresh", "--help"])
    assert exc.value.code == 0
    refresh_help = capsys.readouterr().out
    normalized_refresh_help = " ".join(refresh_help.lower().split())
    for phrase in (
        "all registered checkouts",
        "without confirmation",
        "declared runtime or image",
        "configured as `latest`",
        "undeclared",
        "dead checkouts",
    ):
        assert phrase in normalized_refresh_help

    with pytest.raises(SystemExit) as exc:
        sd.main(["target", "remove", "--help"])
    assert exc.value.code == 0
    remove_help = capsys.readouterr().out
    assert "Local removal destroys the managed simulator/emulator by default" in remove_help
    assert "Global removal edits configuration only until target refresh" in remove_help


def test_cli_target_global_remove_rejects_keep_instance(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    with pytest.raises(SystemExit) as exc:
        sd.main(
            [
                "--cwd",
                str(tmp_path),
                "target",
                "remove",
                "simulator",
                "default",
                "--global",
                "--keep-instance",
            ]
        )

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--global" in err
    assert "--keep-instance" in err


def test_cli_target_physical_device_remove_rejects_keep_instance(capsys):
    with pytest.raises(SystemExit) as exc:
        sd.main(["target", "remove", "device", "phone", "--keep-instance"])

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "device" in err
    assert "--keep-instance" in err


def test_cli_keyboard_interrupt_returns_shell_status(tmp_path, monkeypatch):
    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(sd.cli, "cmd_status", interrupt)

    assert sd.main(["--cwd", str(tmp_path), "status"]) == 130


def test_localconfig_missing_file_is_empty(tmp_path):
    lc = sd.LocalConfig.load(tmp_path / "splashdown.local.toml")
    assert lc.targets == {}


def _device_args(tmp_path, dtype, variant=None):
    import argparse

    return argparse.Namespace(dtype=dtype, variant=variant, cwd=str(tmp_path))


# A recipe declaring all three target types, so type-prefix matching (scoped to
# *declared* types) has every type available to expand against.
_ALL_TYPES_RECIPE = (
    '[targets.simulator.default]\nmodel = "iPhone 17"\n'
    '[targets.emulator.default]\ndevice = "pixel_9"\n'
    "[targets.device.default]\n"
)


@pytest.mark.parametrize(
    ("token", "expected"),
    [("sim", "simulator"), ("em", "emulator"), ("d", "device"), ("simulator", "simulator")],
)
def test_normalize_device_args_expands_type_prefix(tmp_path, token, expected):
    (tmp_path / sd.RECIPE_NAME).write_text(_ALL_TYPES_RECIPE)
    args = _device_args(tmp_path, token)
    sd.cli._normalize_device_args(args)
    assert args.dtype == expected
    assert args.variant is None


def test_normalize_device_args_non_type_demotes_to_variant(tmp_path):
    (tmp_path / sd.RECIPE_NAME).write_text(_ALL_TYPES_RECIPE)
    args = _device_args(tmp_path, "large-screen")
    sd.cli._normalize_device_args(args)
    assert args.dtype is None
    assert args.variant == "large-screen"


def test_normalize_device_args_short_token_not_shadowed_by_undeclared_type(tmp_path):
    # sim-only project: `d` must NOT expand to the undeclared `device` type — it
    # stays in the variant slot so variant-prefix matching can resolve it.
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    args = _device_args(tmp_path, "d")
    sd.cli._normalize_device_args(args)
    assert args.dtype is None
    assert args.variant == "d"


def test_normalize_device_args_prefix_disabled_demotes_type_token(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg" / "splashdown"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text("[settings]\nprefix_match = false\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / sd.RECIPE_NAME).write_text(_ALL_TYPES_RECIPE)
    args = _device_args(tmp_path, "sim")
    sd.cli._normalize_device_args(args)
    assert args.dtype is None
    assert args.variant == "sim"


def test_localconfig_rejects_bad_variant_name(tmp_path):
    p = tmp_path / "splashdown.local.toml"
    p.write_text('[targets.simulator."has spaces"]\nmodel = "iPhone"\n')
    with pytest.raises(ValueError, match="variant name"):
        sd.LocalConfig.load(p)


def test_init_writes_recipe_and_local_skeleton(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    sd.cmd_init(tmp_path)
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[resources." in recipe
    assert "range = [8082, 8200]" in recipe
    assert (tmp_path / "splashdown.local.toml").exists()


def test_init_does_not_clobber_existing_local(tmp_path):
    (tmp_path / "splashdown.local.toml").write_text('[targets.mine]\ntype = "simulator"\n')
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    sd.cmd_init(tmp_path)
    assert "targets.mine" in (tmp_path / "splashdown.local.toml").read_text()


def test_init_refuses_dangling_local_config_symlink(tmp_path):
    outside = tmp_path.parent / "outside-local.toml"
    (tmp_path / sd.LOCAL_NAME).symlink_to(outside)

    with pytest.raises(ValueError, match="not a regular file"):
        sd.cmd_init(tmp_path)

    assert not outside.exists()


@pytest.mark.parametrize("use_cwd", [False, True])
def test_cli_init_creates_nested_project_in_selected_directory(tmp_path, monkeypatch, use_cwd):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)
    parent_recipe = root / sd.RECIPE_NAME
    original = '[resources.PARENT_ID]\ntype = "uuid"\n'
    parent_recipe.write_text(original)
    if use_cwd:
        args = ["--cwd", str(nested), "init"]
    else:
        monkeypatch.chdir(nested)
        args = ["init"]

    rc = sd.main([*args, "--loader", "none"])

    assert rc == 0
    assert sd.Recipe.load(nested / sd.RECIPE_NAME).project["loader"] == "none"
    assert parent_recipe.read_text() == original


@pytest.mark.parametrize("existing_hook", [False, True])
def test_cli_init_nested_project_preserves_root_checkout_hook(tmp_path, capsys, existing_hook):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)
    hook = root / ".git" / "hooks" / "post-checkout"
    original = "#!/bin/sh\necho custom checkout hook\n"
    if existing_hook:
        hook.write_text(original)

    rc = sd.main(["--cwd", str(nested), "init"])

    assert rc == 0
    if existing_hook:
        assert hook.read_text() == original
    else:
        assert not hook.exists()
    err = capsys.readouterr().err
    assert "post-checkout hook not installed for nested project" in err
    assert f"splash --cwd {nested.resolve()} sync" in err


def test_cli_nested_project_trust_leaves_root_checkout_hook_uninstalled(tmp_path, capsys):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)
    hook = root / ".git" / "hooks" / "post-checkout"

    assert sd.main(["--cwd", str(nested), "init", "--loader", "none"]) == 0
    init_err = capsys.readouterr().err
    assert f"splash --cwd {nested.resolve()} sync" in init_err
    assert "activate automatic post-checkout handling" not in init_err

    assert sd.main(["--cwd", str(nested), "trust"]) == 0

    trust_err = capsys.readouterr().err
    assert not hook.exists()
    assert "post-checkout hook not installed for nested project" in trust_err


def test_cli_nested_existing_project_requires_overwrite(tmp_path, capsys):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)
    (nested / "vite.config.ts").write_text("export default {}")
    recipe_path = nested / sd.RECIPE_NAME
    original = '[resources.PARENT_ID]\ntype = "uuid"\n'
    recipe_path.write_text(original)

    assert sd.main(["--cwd", str(nested), "init"]) == 2
    assert "use --overwrite" in capsys.readouterr().err
    assert recipe_path.read_text() == original

    rc = sd.main(["--cwd", str(nested), "init", "--overwrite"])

    assert rc == 0
    assert "[resources.WEB_DEV_PORT]" in recipe_path.read_text()


def test_cli_init_rejects_symlinked_nested_recipe_before_overwrite(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)
    outside = tmp_path / "outside.toml"
    original = '[project]\nloader = "none"\n'
    outside.write_text(original)
    (nested / sd.RECIPE_NAME).symlink_to(outside)
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))

    rc = sd.main(["--cwd", str(nested), "init", "--overwrite"])

    assert rc == 2
    assert "not a regular file" in capsys.readouterr().err
    assert outside.read_text() == original
    assert not (state / "splashdown").exists()


def test_cli_init_overwrite_replaces_recipe_hardlink_without_mutating_target(tmp_path):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)
    outside = tmp_path / "outside.toml"
    original = '[project]\nloader = "none"\n'
    outside.write_text(original)
    os.link(outside, nested / sd.RECIPE_NAME)
    (nested / "vite.config.ts").write_text("export default {}")

    rc = sd.main(["--cwd", str(nested), "init", "--overwrite"])

    assert rc == 0
    assert outside.read_text() == original
    assert "[resources.WEB_DEV_PORT]" in (nested / sd.RECIPE_NAME).read_text()


@pytest.mark.parametrize(
    "preset",
    [
        "minimal",
        "server",
        "electron",
        "rn",
        "react-native",
        "flutter",
        "ios-native",
        "android-native",
        "nextjs",
    ],
)
def test_cli_init_rejects_removed_presets(tmp_path, preset, capsys):
    with pytest.raises(SystemExit) as exc:
        sd.main(["--cwd", str(tmp_path), "init", preset])
    assert exc.value.code == 2
    assert f"unrecognized arguments: {preset}" in capsys.readouterr().err
    assert not (tmp_path / "splashdown.toml").exists()


def test_cli_init_no_arg_runs_scanner(tmp_path, monkeypatch):
    """`splash init` with no positional kicks off the Scanner-driven flow."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "vite.config.ts").write_text("export default {}")
    rc = sd.main(["--cwd", str(tmp_path), "init"])
    assert rc == 0
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[apps." in recipe
    assert 'profile = "vite"' in recipe


def test_cli_init_no_arg_emits_rn_metro_port(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    rc = sd.main(["--cwd", str(tmp_path), "init"])
    assert rc == 0
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert 'profile = "react-native"' in recipe
    assert "[resources.RCT_METRO_PORT]" in recipe
    assert "range = [8082, 8200]" in recipe
    assert 'resources = ["RCT_METRO_PORT"]' in recipe


def test_init_gradle_module_runs_from_workspace_root(tmp_path, monkeypatch):
    (tmp_path / "settings.gradle.kts").write_text('include(":features:demo")\n')
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    module = tmp_path / "features" / "demo"
    module.mkdir(parents=True)
    (module / "build.gradle.kts").write_text('plugins { id("com.android.application") }\n')

    sd.cmd_init(tmp_path)

    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert recipe.project["android"]["module"] == "features:demo"

    metadata = module / "build" / "outputs" / "apk" / "debug"
    metadata.mkdir(parents=True)
    (metadata / "output-metadata.json").write_text(
        '{"variantName":"debug","applicationId":"com.example.demo"}'
    )
    calls = []

    def call(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0

    monkeypatch.setattr(sd.runners.subprocess, "call", call)
    monkeypatch.setattr(
        sd.runners,
        "check_output_finite",
        lambda *_args, **_kwargs: b"com.example.demo/.MainActivity\n",
    )

    assert (
        sd.device_run(
            tmp_path,
            recipe,
            {"kind": "android", "serial": "emulator-5554"},
        )
        == 0
    )
    assert calls[0][0] == ["./gradlew", ":features:demo:installDebug"]
    assert calls[0][1]["cwd"] == tmp_path


def test_init_native_ios_explicit_scheme_skips_discovery(tmp_path, monkeypatch):
    (tmp_path / "Demo.xcodeproj").mkdir()

    def fail(_cwd):
        raise AssertionError("explicit scheme must not run xcodebuild discovery")

    monkeypatch.setattr(sd.commands, "_ios_native_schemes", fail, raising=False)

    sd.cmd_init(tmp_path, ios_scheme="Demo")

    assert sd.Recipe.load(tmp_path / "splashdown.toml").project["ios"]["scheme"] == "Demo"


def test_init_native_ios_single_discovered_scheme_is_written(tmp_path, monkeypatch):
    (tmp_path / "Demo.xcodeproj").mkdir()
    monkeypatch.setattr(
        sd.commands,
        "_ios_native_schemes",
        lambda _cwd: ["Demo"],
        raising=False,
    )

    sd.cmd_init(tmp_path)

    assert sd.Recipe.load(tmp_path / "splashdown.toml").project["ios"]["scheme"] == "Demo"


def test_init_native_ios_multiple_schemes_prompts_for_exact_name(tmp_path, monkeypatch, capsys):
    (tmp_path / "Demo.xcodeproj").mkdir()
    monkeypatch.setattr(
        sd.commands,
        "_ios_native_schemes",
        lambda _cwd: ["Demo", "DemoDev"],
        raising=False,
    )
    monkeypatch.setattr(sys, "stdin", _TTYInput("DemoDev\n"))

    sd.cmd_init(tmp_path)

    assert sd.Recipe.load(tmp_path / "splashdown.toml").project["ios"]["scheme"] == "DemoDev"
    assert "Select native iOS scheme (Demo, DemoDev)" in capsys.readouterr().err


def test_cli_init_native_ios_scheme_option(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "Demo.xcodeproj").mkdir()
    monkeypatch.setattr(
        sd.commands,
        "_ios_native_schemes",
        lambda _cwd: pytest.fail("explicit scheme must bypass discovery"),
        raising=False,
    )

    assert sd.main(["--cwd", str(tmp_path), "init", "--ios-scheme", "Demo"]) == 0
    assert sd.Recipe.load(tmp_path / "splashdown.toml").project["ios"]["scheme"] == "Demo"


def test_init_native_ios_ambiguous_noninteractive_fails_before_writing(tmp_path, monkeypatch):
    (tmp_path / "Demo.xcodeproj").mkdir()
    monkeypatch.setattr(
        sd.commands,
        "_ios_native_schemes",
        lambda _cwd: ["Demo", "DemoDev"],
        raising=False,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO())

    with pytest.raises(sd.DeviceError, match="--ios-scheme NAME"):
        sd.cmd_init(tmp_path)

    assert not (tmp_path / "splashdown.toml").exists()


def test_init_electron_yes_adds_profile_without_replacing_vite(tmp_path, monkeypatch, capsys):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    (tmp_path / "package.json").write_text('{"devDependencies":{"electron":"40"}}')
    monkeypatch.setattr(sys, "stdin", _TTYInput("y\n"))
    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert recipe.apps["main"]["profile"] == "vite"
    assert recipe.resources["ELECTRON_PROFILE_ID"]["template"] == (
        "splashdown-{{ truncate(hash(cwd_abs), 12) }}"
    )
    assert recipe.resources["ELECTRON_PROFILE_ID"]["writer"] == "splashdown-env"
    assert "WEB_DEV_PORT" in recipe.resources
    err = capsys.readouterr().err
    assert "Set up an independent Electron profile for this checkout?" in err
    assert "before requestSingleInstanceLock():" in err
    assert "const profileId = process.env.ELECTRON_PROFILE_ID" in err
    assert "mkdirSync(userData, { recursive: true })" in err


def test_init_electron_no_keeps_renderer_resources(tmp_path, monkeypatch, capsys):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    (tmp_path / "package.json").write_text('{"dependencies":{"electron":"40"}}')
    monkeypatch.setattr(sys, "stdin", _TTYInput("n\n"))
    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert set(recipe.resources) == {"WEB_DEV_PORT"}
    assert "Set up an independent Electron profile for this checkout?" in capsys.readouterr().err


def test_init_electron_noninteractive_defaults_to_shared_user_data(tmp_path, monkeypatch, capsys):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    (tmp_path / "package.json").write_text('{"dependencies":{"electron":"40"}}')
    stdin = io.StringIO("y\n")
    monkeypatch.setattr(sys, "stdin", stdin)
    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert set(recipe.resources) == {"WEB_DEV_PORT"}
    assert stdin.read() == "y\n"
    assert (
        "Set up an independent Electron profile for this checkout?" not in capsys.readouterr().err
    )


def test_init_electron_eof_defaults_to_shared_user_data(tmp_path, monkeypatch):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    (tmp_path / "package.json").write_text('{"dependencies":{"electron":"40"}}')
    monkeypatch.setattr(sys, "stdin", _TTYInput())
    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert set(recipe.resources) == {"WEB_DEV_PORT"}


def test_init_electron_workspace_prompts_once_and_scopes_profile_ids(tmp_path, monkeypatch, capsys):
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - apps/*\n")
    for name in ("desktop.app", "studio-web"):
        app_dir = tmp_path / "apps" / name
        app_dir.mkdir(parents=True)
        (app_dir / "package.json").write_text('{"dependencies":{"electron":"40"}}')
    stdin = _TTYInput("y\nn\n")
    monkeypatch.setattr(sys, "stdin", stdin)
    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert recipe.resources["ELECTRON_PROFILE_ID_DESKTOP_APP"]["template"] == (
        "splashdown-{{ truncate(hash(cwd_abs), 12) }}-desktop-app"
    )
    assert recipe.resources["ELECTRON_PROFILE_ID_STUDIO_WEB"]["template"] == (
        "splashdown-{{ truncate(hash(cwd_abs), 12) }}-studio-web"
    )
    assert stdin.read() == "n\n"
    err = capsys.readouterr().err
    prompt = "Set up independent Electron profiles for these checkouts (desktop.app, studio-web)?"
    assert err.count(prompt) == 1
    assert "process.env.ELECTRON_PROFILE_ID_DESKTOP_APP" in err
    assert "process.env.ELECTRON_PROFILE_ID_STUDIO_WEB" in err


def test_init_electron_profile_flag_works_noninteractively(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "vite.config.ts").write_text("export default {}")
    (tmp_path / "package.json").write_text('{"dependencies":{"electron":"43"}}')
    monkeypatch.setattr(sys, "stdin", io.StringIO())

    assert (
        sd.main(
            [
                "--cwd",
                str(tmp_path),
                "init",
                "--electron-profile",
                "isolated",
            ]
        )
        == 0
    )
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert recipe.apps["main"]["resources"] == ["WEB_DEV_PORT", "ELECTRON_PROFILE_ID"]


def test_init_electron_shared_flag_skips_profile_noninteractively(tmp_path, monkeypatch):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    (tmp_path / "package.json").write_text('{"dependencies":{"electron":"43"}}')
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))

    sd.cmd_init(tmp_path, electron_profile="shared")

    assert set(sd.Recipe.load(tmp_path / "splashdown.toml").resources) == {"WEB_DEV_PORT"}


def test_cli_provision_is_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "co"
    cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type = "port"
range = [18900, 18910]
""")
    code = sd.main(["--cwd", str(cwd)])
    assert code == 0
    assert (cwd / "splashdown.env").exists()


def test_cli_provision_drops_local_skeleton(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "co"
    cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [18900, 18910]
""")
    sd.main(["--cwd", str(cwd)])
    assert (cwd / "splashdown.local.toml").exists()
    assert "targets.simulator" in (cwd / "splashdown.local.toml").read_text()


def test_cli_provision_preserves_existing_local(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "co"
    cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [18920, 18930]
""")
    (cwd / "splashdown.local.toml").write_text('[targets.mine]\ntype = "simulator"\n')
    sd.main(["--cwd", str(cwd)])
    assert "targets.mine" in (cwd / "splashdown.local.toml").read_text()


POST_CHECKOUT_SENTINEL = "splash"


def test_init_appends_gitignore(tmp_path):
    sd.cmd_init(tmp_path)
    gi = (tmp_path / ".gitignore").read_text()
    assert "splashdown.env" in gi
    assert "splashdown.local.toml" in gi


def test_init_gitignore_no_duplicates(tmp_path):
    (tmp_path / ".gitignore").write_text("splashdown.env\n")
    sd.cmd_init(tmp_path)
    gi = (tmp_path / ".gitignore").read_text()
    assert gi.count("splashdown.env") == 1


def test_init_adds_mise_file_directive_new_file(tmp_path):
    sd.cmd_init(tmp_path, loader_override="mise")
    mise = (tmp_path / "mise.toml").read_text()
    assert '_.file = "splashdown.env"' in mise
    assert "[env]" in mise


def test_init_adds_mise_file_directive_existing_env_table(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\nFOO = "bar"\n\n[tools]\nnode = "20"\n')
    sd.cmd_init(tmp_path)
    mise = (tmp_path / "mise.toml").read_text()
    assert '_.file = "splashdown.env"' in mise
    assert 'FOO = "bar"' in mise
    assert 'node = "20"' in mise
    assert mise.count('_.file = "splashdown.env"') == 1


def test_init_mise_directive_idempotent(tmp_path):
    sd.cmd_init(tmp_path, loader_override="mise")
    sd.cmd_init(
        tmp_path,
        options=sd.InitOptions(overwrite=True),
        loader_override="mise",
    )
    mise = (tmp_path / "mise.toml").read_text()
    assert mise.count('_.file = "splashdown.env"') == 1


@pytest.mark.parametrize(
    "existing",
    [
        '[env]\n_.path = "./bin"\n',  # coexisting mise PATH directive
        '[env._]\npath = ["./bin"]\n',  # subtable form
    ],
)
def test_mise_directive_edits_existing_underscore_table_in_place(tmp_path, existing):
    (tmp_path / "mise.toml").write_text(existing)
    sd.cmd_init(
        tmp_path,
        options=sd.InitOptions(overwrite=True),
        loader_override="mise",
    )
    text = (tmp_path / "mise.toml").read_text()
    data = tomllib.loads(text)
    assert data["env"]["_"]["file"] == "splashdown.env"


def test_mise_directive_keeps_an_existing_env_file_alongside_ours(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\n_.file = "other.env"\n')
    sd.cmd_init(
        tmp_path,
        options=sd.InitOptions(overwrite=True),
        loader_override="mise",
    )
    data = tomllib.loads((tmp_path / "mise.toml").read_text())
    assert data["env"]["_"]["file"] == ["other.env", "splashdown.env"]


def _record_approvals(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(sd.loaders, "_run_ok", lambda argv, cwd: calls.append(list(argv)) or True)
    return calls


@pytest.mark.parametrize("loader", ["mise", "direnv"])
def test_cmd_init_never_runs_the_loader_approval_command(loader, tmp_path, monkeypatch):
    # INIT-07: configuration-only init does not invoke `mise trust` or `direnv allow`.
    calls = _record_approvals(monkeypatch)
    sd.cmd_init(tmp_path, loader_override=loader)
    assert calls == []


def test_cmd_init_records_no_trust(tmp_path):
    # INIT-15: init does not implicitly record trust.
    _git_init(tmp_path)
    sd.cmd_init(tmp_path)
    dirs = sd.git_dirs(tmp_path)
    assert not sd.is_trusted(dirs, bootstrap=False)


def test_trust_after_init_authorizes_automatic_handling(tmp_path):
    _git_init(tmp_path)
    sd.cmd_init(tmp_path)

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    assert sd.is_trusted(sd.git_dirs(tmp_path), bootstrap=False)


def test_trust_approves_generated_mise_toml(tmp_path, monkeypatch):
    _git_init(tmp_path)
    sd.cmd_init(tmp_path, loader_override="mise")
    calls = _record_approvals(monkeypatch)

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0
    assert ["mise", "trust", str(tmp_path / "mise.toml")] in calls


@pytest.mark.parametrize(
    ("loader", "detail"),
    [
        ("mise", "mise trusts that path permanently and loads whatever the file holds later"),
        (
            "direnv",
            "direnv loads the file as it stands now and prompts again after any later edit",
        ),
    ],
)
def test_trust_warns_how_the_loader_treats_the_approval(tmp_path, capsys, loader, detail):
    _git_init(tmp_path)
    sd.cmd_init(tmp_path, loader_override=loader)
    capsys.readouterr()

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    err = capsys.readouterr().err
    assert f"trust also approves the {loader} configuration splashdown wired, so {detail}" in err


def test_trust_does_not_approve_pre_existing_mise_toml(tmp_path, monkeypatch):
    # A pre-existing mise.toml may contain user commands, so trust must not auto-trust it.
    _git_init(tmp_path)
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "20"\n')
    sd.cmd_init(tmp_path, loader_override="mise")
    calls = _record_approvals(monkeypatch)

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0
    assert calls == []


def test_trust_approves_mise_config_holding_only_the_splashdown_directive(tmp_path, monkeypatch):
    # Ownership is judged by content: an empty [env] table carries no user commands,
    # and after wiring it is indistinguishable from a file init created itself.
    _git_init(tmp_path)
    (tmp_path / "mise.toml").write_text("[env]\n")
    sd.cmd_init(tmp_path, loader_override="mise")
    calls = _record_approvals(monkeypatch)

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0
    assert ["mise", "trust", str(tmp_path / "mise.toml")] in calls


def test_trust_does_not_approve_mise_config_carrying_other_tables(tmp_path, monkeypatch):
    _git_init(tmp_path)
    (tmp_path / "mise.toml").write_text('[env]\nAPI_URL = "http://localhost"\n')
    sd.cmd_init(tmp_path, loader_override="mise")
    calls = _record_approvals(monkeypatch)

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0
    assert calls == []


def test_trust_approves_generated_envrc(tmp_path, monkeypatch):
    _git_init(tmp_path)
    sd.cmd_init(tmp_path, loader_override="direnv")
    calls = _record_approvals(monkeypatch)

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0
    assert ["direnv", "allow", str(tmp_path)] in calls


def test_trust_does_not_approve_pre_existing_envrc(tmp_path, monkeypatch):
    _git_init(tmp_path)
    (tmp_path / ".envrc").write_text("use nix\n")
    sd.cmd_init(tmp_path, loader_override="direnv")
    calls = _record_approvals(monkeypatch)

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0
    assert calls == []


def test_sync_does_not_approve_mise_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "mise.toml").write_text("[env]\n")
    (tmp_path / "splashdown.toml").write_text(
        '[project]\nloader = "mise"\n\n[resources.PORT]\ntype = "port"\nrange = [18940, 18950]\n'
    )
    calls = _record_approvals(monkeypatch)
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    assert calls == []


def test_sync_does_not_print_loader_approval(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "mise.toml").write_text("[env]\n")
    (tmp_path / "splashdown.toml").write_text(
        '[project]\nloader = "mise"\n\n[resources.PORT]\ntype = "port"\nrange = [18951, 18960]\n'
    )
    _record_approvals(monkeypatch)
    sd.main(["--cwd", str(tmp_path)])
    assert "trusted" not in capsys.readouterr().err


def test_sync_writes_env_without_loader_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "mise.toml").write_text("[env]\n")
    (tmp_path / "splashdown.toml").write_text(
        '[project]\nloader = "mise"\n\n[resources.PORT]\ntype = "port"\nrange = [18961, 18970]\n'
    )
    calls = _record_approvals(monkeypatch)
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    assert (tmp_path / "splashdown.env").exists()
    assert calls == []


def test_trust_installs_native_post_checkout_hook(tmp_path):
    _git_init(tmp_path)
    sd.cmd_init(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-checkout"
    assert not hook.exists()

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    assert os.access(hook, os.X_OK)
    assert POST_CHECKOUT_SENTINEL in hook.read_text()


def test_deinit_in_known_cmds():
    assert "deinit" in sd.KNOWN_CMDS


def test_init_accepts_the_json_output_format(tmp_path, capsys):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    assert sd.main(["--cwd", str(tmp_path), "--format", "json", "init", "--loader=mise"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "init"
    assert payload["loader"]["name"] == "mise"
    assert "mise.toml" in payload["changed"]


def test_init_reports_an_oserror_as_an_error_exit(tmp_path, capsys, monkeypatch):
    (tmp_path / "vite.config.ts").write_text("export default {}")

    def boom(_cwd):
        raise PermissionError(13, "Permission denied", ".gitignore")

    monkeypatch.setattr(sd.commands, "_ensure_gitignore", boom)
    assert sd.main(["--cwd", str(tmp_path), "init", "--loader=mise"]) == 1
    assert "Permission denied" in capsys.readouterr().err


def test_init_json_reports_a_plan_time_failure_in_the_same_envelope(tmp_path, capsys):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    (tmp_path / "mise.toml").write_text("[env]\n_.file = 3\n")
    assert sd.main(["--cwd", str(tmp_path), "--format", "json", "init"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "init"
    assert payload["ok"] is False
    assert payload["loader"]["name"] == "mise"
    assert "cannot wire mise" in payload["error"]
    assert payload["changed"] == []


def test_init_rejects_several_configured_loaders_before_writing(tmp_path, capsys):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    (tmp_path / "mise.toml").write_text("")
    (tmp_path / ".envrc").write_text("")
    assert sd.main(["--cwd", str(tmp_path), "init"]) == 2
    assert "several loaders are configured here (direnv, mise)" in capsys.readouterr().err
    assert not (tmp_path / "splashdown.toml").exists()
