from __future__ import annotations

import io
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
    with pytest.raises(SystemExit):
        sd.main(["--help"])
    out = capsys.readouterr().out
    for token in ("run", "sync", "status", "init", "target", "env"):
        assert token in out
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
        sd.cmd_init(tmp_path, preset="minimal")

    assert not outside.exists()


def test_cli_init_named_preset_is_positional(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = sd.main(["--cwd", str(tmp_path), "init", "server"])
    assert rc == 0
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[resources.PORT]" in recipe
    assert "[resources.DATABASE_URL]" in recipe


def test_cli_init_rejects_nested_git_directory_before_writes(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))

    rc = sd.main(["--cwd", str(nested), "init", "minimal", "--no-sync"])

    assert rc == 2
    err = capsys.readouterr().err
    assert str(root.resolve()) in err
    assert "--allow-nested" in err
    assert not (nested / sd.RECIPE_NAME).exists()
    assert not (nested / sd.LOCAL_NAME).exists()
    assert not (nested / ".gitignore").exists()
    assert not (state / "splashdown").exists()


def test_cli_init_allows_explicit_nested_project(tmp_path):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)

    rc = sd.main(["--cwd", str(nested), "init", "minimal", "--allow-nested", "--no-sync"])

    assert rc == 0
    assert "[resources.RUN_ID]" in (nested / sd.RECIPE_NAME).read_text()


def test_cli_init_nested_project_skips_root_checkout_hook(tmp_path, capsys):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)

    rc = sd.main(["--cwd", str(nested), "init", "minimal", "--allow-nested", "--no-sync"])

    assert rc == 0
    assert not (root / ".git" / "hooks" / "post-checkout").exists()
    err = capsys.readouterr().err
    assert "post-checkout hook not installed for nested project" in err
    assert f"splash --cwd {nested.resolve()} sync" in err


def test_cli_nested_existing_project_rescans_without_override(tmp_path):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)
    sd.cmd_init(nested, preset="minimal", options=sd.InitOptions(allow_nested=True))

    rc = sd.main(["--cwd", str(nested), "init", "--rescan"])

    assert rc == 0
    assert "[apps.main]" in (nested / sd.RECIPE_NAME).read_text()


def test_cli_nested_existing_project_overwrites_without_nested_override(tmp_path):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)
    sd.cmd_init(nested, preset="minimal", options=sd.InitOptions(allow_nested=True))

    rc = sd.main(["--cwd", str(nested), "init", "server", "--overwrite", "--no-sync"])

    assert rc == 0
    assert "[resources.PORT]" in (nested / sd.RECIPE_NAME).read_text()


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

    rc = sd.main(["--cwd", str(nested), "init", "server", "--overwrite", "--no-sync"])

    assert rc == 2
    assert "not a regular file" in capsys.readouterr().err
    assert outside.read_text() == original
    assert not (state / "splashdown").exists()


def test_cli_init_rescan_rejects_symlinked_recipe(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)
    outside = tmp_path / "outside.toml"
    original = '[project]\nworkspace = "single"\nloader = "none"\n'
    outside.write_text(original)
    (nested / sd.RECIPE_NAME).symlink_to(outside)
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))

    rc = sd.main(["--cwd", str(nested), "init", "--rescan"])

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

    rc = sd.main(["--cwd", str(nested), "init", "server", "--overwrite", "--no-sync"])

    assert rc == 0
    assert outside.read_text() == original
    assert "[resources.PORT]" in (nested / sd.RECIPE_NAME).read_text()


def test_cli_init_rescan_replaces_recipe_hardlink_without_mutating_target(tmp_path):
    root = tmp_path / "repo"
    nested = root / "apps" / "web"
    nested.mkdir(parents=True)
    _git_init(root)
    outside = tmp_path / "outside.toml"
    original = '[project]\nworkspace = "single"\nloader = "none"\n'
    outside.write_text(original)
    os.link(outside, nested / sd.RECIPE_NAME)

    rc = sd.main(["--cwd", str(nested), "init", "--rescan"])

    assert rc == 0
    assert outside.read_text() == original
    assert "[apps.main]" in (nested / sd.RECIPE_NAME).read_text()


@pytest.mark.parametrize(
    "preset", ["rn", "react-native", "flutter", "ios-native", "android-native", "nextjs"]
)
def test_cli_init_rejects_removed_presets(tmp_path, preset):
    with pytest.raises(SystemExit) as exc:
        sd.main(["--cwd", str(tmp_path), "init", preset])
    assert exc.value.code == 2
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


def test_cli_init_rescan_updates_inventory(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text('[project]\nworkspace = "single"\nloader = "mise"\n')
    (tmp_path / "pubspec.yaml").write_text("name: demo\n")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    called = {}

    def _fake(cwd):
        called["cwd"] = cwd
        return 0

    monkeypatch.setattr(sd.cli, "cmd_refresh_inventory", _fake)
    rc = sd.main(["--cwd", str(tmp_path), "init", "--rescan"])
    assert rc == 0
    assert called["cwd"] == tmp_path


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

    assert sd.main(["--cwd", str(tmp_path), "init", "--ios-scheme", "Demo", "--no-sync"]) == 0
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


def test_init_server_preset_writes_generic_scaffold(tmp_path):
    sd.cmd_init(tmp_path, preset="server")
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[resources.PORT]" in recipe
    assert "[resources.DATABASE_URL]" in recipe
    assert "range = [3001, 3100]" in recipe
    assert (
        'template = "postgres://localhost:5432/myapp_{{ slug(cwd) }}_'
        '{{ truncate(hash(cwd_abs), 8) }}"' in recipe
    )
    assert "Next.js preset" not in recipe


def test_server_database_name_distinguishes_matching_path_tails(tmp_path):
    sd.cmd_init(tmp_path, preset="server")
    template = sd.Recipe.load(tmp_path / sd.RECIPE_NAME).resources["DATABASE_URL"]["template"]
    one = sd.render_template(
        template, sd._make_scope(tmp_path / "one" / "work" / "checkout", None, {})
    )
    two = sd.render_template(
        template, sd._make_scope(tmp_path / "two" / "work" / "checkout", None, {})
    )

    assert one.startswith("postgres://localhost:5432/myapp_checkout_")
    assert two.startswith("postgres://localhost:5432/myapp_checkout_")
    assert one != two


def test_init_electron_preset_includes_profile_id(tmp_path, capsys):
    sd.cmd_init(tmp_path, preset="electron")
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[resources.PORT]" in recipe
    assert "[resources.ELECTRON_PROFILE_ID]" in recipe
    assert "range = [3001, 3100]" in recipe
    assert 'template = "splashdown-{{ truncate(hash(cwd_abs), 12) }}"' in recipe
    err = capsys.readouterr().err
    assert "const profileId = process.env.ELECTRON_PROFILE_ID" in err
    assert "mkdirSync(userData, { recursive: true })" in err
    assert "before requestSingleInstanceLock()" in err


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
    assert "const profileId = process.env.ELECTRON_PROFILE_ID" in err


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
                "--no-sync",
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
    sd.cmd_init(tmp_path, preset="minimal")
    gi = (tmp_path / ".gitignore").read_text()
    assert "splashdown.env" in gi
    assert "splashdown.local.toml" in gi


def test_init_gitignore_no_duplicates(tmp_path):
    (tmp_path / ".gitignore").write_text("splashdown.env\n")
    sd.cmd_init(tmp_path, preset="minimal")
    gi = (tmp_path / ".gitignore").read_text()
    assert gi.count("splashdown.env") == 1


def test_init_adds_mise_file_directive_new_file(tmp_path):
    sd.cmd_init(tmp_path, preset="minimal", loader_override="mise")
    mise = (tmp_path / "mise.toml").read_text()
    assert '_.file = "splashdown.env"' in mise
    assert "[env]" in mise


def test_init_adds_mise_file_directive_existing_env_table(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\nFOO = "bar"\n\n[tools]\nnode = "20"\n')
    sd.cmd_init(tmp_path, preset="minimal")
    mise = (tmp_path / "mise.toml").read_text()
    assert '_.file = "splashdown.env"' in mise
    assert 'FOO = "bar"' in mise
    assert 'node = "20"' in mise
    assert mise.count('_.file = "splashdown.env"') == 1


def test_init_mise_directive_idempotent(tmp_path):
    sd.cmd_init(tmp_path, preset="minimal", loader_override="mise")
    sd.cmd_init(
        tmp_path,
        preset="minimal",
        options=sd.InitOptions(overwrite=True),
        loader_override="mise",
    )
    mise = (tmp_path / "mise.toml").read_text()
    assert mise.count('_.file = "splashdown.env"') == 1


@pytest.mark.parametrize(
    "existing",
    [
        '[env]\n_.path = "./bin"\n',  # coexisting mise PATH directive
        '[env]\n_.file = "other.env"\n',  # different file value (dotted form)
        '[env._]\npath = ["./bin"]\n',  # subtable form
    ],
)
def test_mise_directive_edits_existing_underscore_table_in_place(tmp_path, existing):
    (tmp_path / "mise.toml").write_text(existing)
    sd.cmd_init(
        tmp_path,
        preset="minimal",
        options=sd.InitOptions(overwrite=True),
        loader_override="mise",
    )
    text = (tmp_path / "mise.toml").read_text()
    data = tomllib.loads(text)
    assert data["env"]["_"]["file"] == "splashdown.env"


def _record_approvals(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(sd.loaders, "_run_ok", lambda argv, cwd: calls.append(list(argv)) or True)
    return calls


def test_cmd_init_auto_approves_freshly_created_mise_toml(tmp_path, monkeypatch):
    calls = _record_approvals(monkeypatch)
    sd.cmd_init(tmp_path, preset="minimal", loader_override="mise")
    assert ["mise", "trust", str(tmp_path / "mise.toml")] in calls


def test_cmd_init_does_not_auto_approve_pre_existing_mise_toml(tmp_path, monkeypatch):
    # A pre-existing mise.toml may contain user commands, so init must not auto-trust it.
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "20"\n')
    calls = _record_approvals(monkeypatch)
    sd.cmd_init(tmp_path, preset="minimal", loader_override="mise")
    assert calls == []


def test_full_init_does_not_trust_pre_existing_mise(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "mise.toml").write_text("[env]\n")
    calls = _record_approvals(monkeypatch)
    assert sd.main(["--cwd", str(tmp_path), "init", "--loader", "mise"]) == 0
    assert calls == []


def test_init_no_sync_does_not_trust_pre_existing_mise(tmp_path, monkeypatch):
    (tmp_path / "mise.toml").write_text("[env]\n")
    calls = _record_approvals(monkeypatch)
    assert sd.main(["--cwd", str(tmp_path), "init", "--loader", "mise", "--no-sync"]) == 0
    assert calls == []


def test_cmd_init_auto_approves_freshly_created_envrc(tmp_path, monkeypatch):
    calls = _record_approvals(monkeypatch)
    sd.cmd_init(tmp_path, preset="minimal", loader_override="direnv")
    assert ["direnv", "allow", str(tmp_path)] in calls


def test_cmd_init_does_not_auto_approve_pre_existing_envrc(tmp_path, monkeypatch):
    (tmp_path / ".envrc").write_text("use nix\n")
    calls = _record_approvals(monkeypatch)
    sd.cmd_init(tmp_path, preset="minimal", loader_override="direnv")
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


def test_init_writes_post_checkout_hook(tmp_path):
    _git_init(tmp_path)
    sd.cmd_init(tmp_path, preset="minimal")
    hook = tmp_path / ".git" / "hooks" / "post-checkout"
    assert hook.exists()
    assert os.access(hook, os.X_OK)
    assert POST_CHECKOUT_SENTINEL in hook.read_text()


def test_deinit_in_known_cmds():
    assert "deinit" in sd.KNOWN_CMDS
