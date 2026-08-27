from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

import splashdown as sd


def test_example_recipe_is_valid():
    sd.Recipe.load(Path(__file__).parent.parent / "examples" / "splashdown.toml")


def test_template_basic_vars(tmp_path):
    cwd = tmp_path / "myrepo.feat"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "feat", {})
    assert sd.render_template("{{ cwd }}", scope) == "myrepo.feat"
    assert sd.render_template("port-{{ basename(cwd_abs) }}", scope) == "port-myrepo.feat"
    assert sd.render_template("{{ slug(cwd) }}", scope) == "myrepo-feat"


def test_template_cross_resource(tmp_path):
    cwd = tmp_path / "x"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "", {"PORT": "8081"})
    assert sd.render_template("http://localhost:{{ PORT }}", scope) == "http://localhost:8081"


def test_template_refs():
    refs = sd.template_refs("http://x:{{ PORT }}/{{ basename(cwd) }}")
    assert "PORT" in refs
    assert "basename" in refs


def test_template_refs_ignores_string_literals():
    # An identifier that only appears inside a string literal is not a real
    # dependency and must not fabricate an edge (which could trip cycle detection).
    refs = sd.template_refs('{{ "PORT" + slug(cwd) }}')
    assert "PORT" not in refs
    assert {"slug", "cwd"} <= refs


def test_template_error_on_bad_expr(tmp_path):
    cwd = tmp_path / "x"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "", {})
    with pytest.raises(sd.TemplateError):
        sd.render_template("{{ no_such_var }}", scope)


@pytest.mark.parametrize(
    "payload",
    [
        "{{ ().__class__ }}",
        "{{ cwd.__class__ }}",
        "{{ [].__class__.__base__.__subclasses__() }}",
        '{{ __import__("os") }}',
        "{{ ().__class__.__bases__[0].__subclasses__() }}",
        "{{ lambda: 1 }}",
        "{{ [x for x in (1, 2)] }}",
    ],
)
def test_template_rejects_sandbox_escapes(tmp_path, payload):
    # The restricted evaluator must reject attribute access, dunders, lambdas,
    # and comprehensions — the building blocks of every eval-sandbox escape.
    cwd = tmp_path / "x"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "main", {})
    with pytest.raises(sd.TemplateError):
        sd.render_template(payload, scope)


@pytest.mark.parametrize(
    "payload",
    [
        "{{ 'x' * 999999999 }}",
        "{{ 999999999 * 'x' }}",
        "{{ ('ab' * 100000) * 100000 }}",
    ],
)
def test_template_rejects_huge_sequence_repetition(tmp_path, payload):
    # render_template runs automatically from the post-checkout hook on an untrusted
    # clone; an unbounded `"x" * N` would OOM/hang the machine on checkout.
    cwd = tmp_path / "x"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "main", {})
    with pytest.raises(sd.TemplateError):
        sd.render_template(payload, scope)


def test_template_allows_small_repetition(tmp_path):
    cwd = tmp_path / "x"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "main", {})
    assert sd.render_template("{{ '-' * 3 }}", scope) == "---"


def test_template_allows_slicing_and_nested_calls(tmp_path):
    cwd = tmp_path / "foo" / "bar"
    cwd.mkdir(parents=True)
    scope = sd._make_scope(cwd, "main", {})
    assert len(sd.render_template("{{ uuid()[:8] }}", scope)) == 8
    assert sd.render_template("{{ basename(dirname(cwd_abs)) }}", scope) == "foo"


@pytest.mark.parametrize("bad", ["has\ttab", "has\nnewline", "has\rcarriage"])
def test_registry_rejects_control_chars_in_value(registry, bad):
    with pytest.raises(ValueError, match="tab or newline"):
        registry.set_kv("/checkout/a", "KEY", bad)


def test_registry_rejects_control_chars_in_checkout_path(registry):
    with pytest.raises(ValueError, match="tab or newline"):
        registry.set_kv("/checkout\twith-tab", "KEY", "value")


def test_registry_rejects_control_chars_in_device_field(registry):
    with pytest.raises(ValueError, match="tab or newline"):
        registry.set_device("/co", "simulator", "default", "udid\ninjected", "iPhone 17", "18.5")


def test_ios_boot_raises_deviceerror_on_boot_failure(monkeypatch):
    monkeypatch.setattr(sd.capabilities.sys, "platform", "darwin")

    def fake_run(cmd, *a, **k):
        if "boot" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boot failed: no space")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sd.devices.subprocess, "run", fake_run)
    with pytest.raises(sd.DeviceError, match="simctl boot failed"):
        sd.devices.ios_boot("UDID-X", "Shutdown")


def test_ios_boot_tolerates_already_booted_race(monkeypatch):
    monkeypatch.setattr(sd.capabilities.sys, "platform", "darwin")

    def fake_run(cmd, *a, **k):
        if "boot" in cmd:
            return subprocess.CompletedProcess(
                cmd, 149, stdout="", stderr="Unable to boot device in current state: Booted"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sd.devices.subprocess, "run", fake_run)
    sd.devices.ios_boot("UDID-X", "Shutdown")


def test_recipe_loads(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[resources.A]
type = "uuid"
[resources.B]
type = "template"
template = "x-{{ A }}"
""")
    r = sd.Recipe.load(p)
    assert "A" in r.resources and "B" in r.resources


def test_recipe_accepts_complete_schema(tmp_path):
    recipe = sd.Recipe.parse(
        """
[project]
workspace = "single"
loader = "none"
framework = "flutter"

[project.run]
ios = "flutter run -d {device_id}"
android = "flutter run -d {device_id}"

[project.ios]
scheme = "Demo"
mode = "Debug"
configuration = "Debug"
workspace = "Demo.xcworkspace"
project = "Demo.xcodeproj"

[project.android]
mode = "debug"
module = "app"
variant = "debug"
application_id = "com.example.demo"
launch_activity = ".MainActivity"

[apps.main]
path = "."
profile = "flutter"
resources = ["PORT", "ID", "URL", "ROOT", "SLUG", "TOKEN"]

[resources.PORT]
type = "port"
range = [1, 65535]
writer = "splashdown-env"

[resources.ID]
type = "uuid"
writer = "none"

[resources.URL]
type = "template"
template = "http://localhost:{{ PORT }}"
writer = "stdout"

[resources.ROOT]
type = "cwd"
writer = "envrc"

[resources.SLUG]
type = "cwd-slug"
writer = "envfile=apps/demo/.env"

[resources.TOKEN]
type = "set"
default = ""

[setup.dev]
run = ["echo one", "echo two"]

[targets.simulator.default]
model = "iPhone 17"
ios = "latest"
name = "Demo"

[targets.emulator.default]
device = "pixel_9"
image = "system-images;android-36;google_apis;arm64-v8a"
name = "Demo"

[targets.device.iphone]
id = "ABC"
name = "Phone"
platform = "ios"
""",
        tmp_path / "splashdown.toml",
    )
    assert set(recipe.resources) == {"PORT", "ID", "URL", "ROOT", "SLUG", "TOKEN"}
    assert recipe.apps["main"]["profile"] == "flutter"


def test_recipe_accepts_auto_framework(tmp_path):
    recipe = sd.Recipe.parse('[project]\nframework = "auto"\n', tmp_path / "splashdown.toml")
    assert recipe.project["framework"] == "auto"


@pytest.mark.parametrize("policy", ["ios", "android", "any"])
def test_recipe_accepts_worktree_claim_device_policy(tmp_path, policy):
    recipe = sd.Recipe.parse(
        f'[project.worktree]\nclaim_device = "{policy}"\n',
        tmp_path / "splashdown.toml",
    )

    assert recipe.project["worktree"] == {"claim_device": policy}


@pytest.mark.parametrize(
    ("text", "path", "expected"),
    [
        (
            '[project]\nworktree = "android"\n',
            "project.worktree",
            'a table containing exactly `claim_device = "ios" | "android" | "any"`',
        ),
        (
            "[project.worktree]\n",
            "project.worktree",
            'exactly `claim_device = "ios" | "android" | "any"`',
        ),
        (
            '[project.worktree]\nclaim_device = "ios"\nextra = true\n',
            "project.worktree",
            'exactly `claim_device = "ios" | "android" | "any"`',
        ),
        (
            '[project.worktree]\nclaim_device = "windows"\n',
            "project.worktree.claim_device",
            "one of android, any, ios",
        ),
    ],
)
def test_recipe_rejects_invalid_worktree_claim_device_policy(tmp_path, text, path, expected):
    with pytest.raises(
        ValueError,
        match=rf"splashdown\.toml: \[{re.escape(path)}\].*{re.escape(expected)}",
    ):
        sd.Recipe.parse(text, tmp_path / "splashdown.toml")


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("[unknown]\nvalue = 1\n", "document"),
        ('project = "single"\n', "project"),
        ('[project]\nunknown = "x"\n', "project"),
        ('[project]\nworkspace = "bun"\n', "project.workspace"),
        ('[project]\nloader = "dotenv"\n', "project.loader"),
        ('[project]\nframework = "cobol-on-cogs"\n', "project.framework"),
        ('[project]\nrun = ""\n', "project.run"),
        ('[project.run]\nwindows = "x"\n', "project.run"),
        ("[project.run]\n", "project.run"),
        ('[project.ios]\nscheme = ""\n', "project.ios.scheme"),
        ('[project.android]\nunknown = "x"\n', "project.android"),
    ],
)
def test_recipe_rejects_invalid_project_schema(tmp_path, text, path):
    with pytest.raises(ValueError, match=rf"splashdown\.toml: \[{re.escape(path)}\]"):
        sd.Recipe.parse(text, tmp_path / "splashdown.toml")


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ('[apps.main]\npath = "."\nprofile = "unknown"\n', "apps.main"),
        (
            '[apps.main]\npath = "."\nprofile = "bogus"\nresources = []\n',
            "apps.main.profile",
        ),
        (
            '[apps.main]\npath = "."\nprofile = "unknown"\nresources = ["MISSING"]\n',
            "apps.main.resources",
        ),
        (
            '[apps.main]\npath = "."\nprofile = "unknown"\nresources = ["A", "A"]\n'
            '\n[resources.A]\ntype = "uuid"\n',
            "apps.main.resources",
        ),
        (
            '[apps.main]\npath = "."\nprofile = "unknown"\nresources = []\nextra = true\n',
            "apps.main",
        ),
    ],
)
def test_recipe_rejects_invalid_app_schema(tmp_path, text, path):
    with pytest.raises(ValueError, match=rf"\[{re.escape(path)}\]"):
        sd.Recipe.parse(text, tmp_path / "splashdown.toml")


@pytest.mark.parametrize(
    ("body", "path"),
    [
        ('type = "port"\n', "resources.VALUE"),
        ('type = "port"\nrange = [0, 2]\n', "resources.VALUE.range"),
        ('type = "port"\nrange = [3, 2]\n', "resources.VALUE.range"),
        ('type = "port"\nrange = [1, 65536]\n', "resources.VALUE.range"),
        ('type = "port"\nrange = [true, 2]\n', "resources.VALUE.range"),
        ('type = "template"\n', "resources.VALUE"),
        ('type = "template"\ntemplate = 1\n', "resources.VALUE.template"),
        ('type = "set"\ndefault = 1\n', "resources.VALUE.default"),
        ('type = "uuid"\ndefault = "x"\n', "resources.VALUE"),
        ('type = "unknown"\n', "resources.VALUE.type"),
        ('writer = "none"\n', "resources.VALUE"),
    ],
)
def test_recipe_rejects_invalid_resource_schema(tmp_path, body, path):
    text = f"[resources.VALUE]\n{body}"
    with pytest.raises(ValueError, match=rf"\[{re.escape(path)}\]"):
        sd.Recipe.parse(text, tmp_path / "splashdown.toml")


@pytest.mark.parametrize(
    "writer",
    [
        "splashdown-env",
        "envrc",
        "stdout",
        "none",
        "envfile=.env",
        "envfile=apps/web/.env.local",
    ],
)
def test_recipe_accepts_valid_writers(tmp_path, writer):
    text = f'[resources.VALUE]\ntype = "uuid"\nwriter = "{writer}"\n'
    assert (
        sd.Recipe.parse(text, tmp_path / "splashdown.toml").resources["VALUE"]["writer"] == writer
    )


@pytest.mark.parametrize(
    "writer",
    [
        "",
        "envfile",
        "envfile=",
        "envfile=/tmp/out",
        "envfile=../out",
        "envfile=apps/../../out",
        "envfilefoo=.env",
        "stdout=foo",
    ],
)
def test_recipe_rejects_invalid_writers(tmp_path, writer):
    text = f'[resources.VALUE]\ntype = "uuid"\nwriter = "{writer}"\n'
    with pytest.raises(ValueError, match=r"\[resources\.VALUE\.writer\]"):
        sd.Recipe.parse(text, tmp_path / "splashdown.toml")


def test_recipe_rejects_envfile_path_through_escaping_symlink(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    text = '[resources.VALUE]\ntype = "uuid"\nwriter = "envfile=linked/.env"\n'
    with pytest.raises(ValueError, match="stays inside the checkout"):
        sd.Recipe.parse(text, tmp_path / "splashdown.toml")


@pytest.mark.parametrize(
    "template",
    [
        "{{ MISSING }}",
        "{{ cwd.__class__ }}",
        "{{ lambda: 1 }}",
        "{{ *cwd }}",
        "{{ cwd",
    ],
)
def test_recipe_preflights_template_expressions(tmp_path, template):
    text = f'[resources.VALUE]\ntype = "template"\ntemplate = "{template}"\n'
    with pytest.raises(ValueError, match=r"\[resources\.VALUE\.template\]"):
        sd.Recipe.parse(text, tmp_path / "splashdown.toml")


@pytest.mark.parametrize(
    "text",
    [
        "[setup.dev]\n",
        '[setup.dev]\nrun = ""\n',
        "[setup.dev]\nrun = []\n",
        '[setup.dev]\nrun = ["ok", ""]\n',
        '[setup.dev]\nrun = "ok"\nunknown = true\n',
    ],
)
def test_recipe_rejects_invalid_setup_schema(tmp_path, text):
    with pytest.raises(ValueError, match=r"\[setup\.dev"):
        sd.Recipe.parse(text, tmp_path / "splashdown.toml")


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        ('"echo ready"', ("echo ready",)),
        ('["echo one", "echo two"]', ("echo one", "echo two")),
    ],
)
def test_recipe_accepts_bootstrap_schema(tmp_path, run, expected):
    recipe = sd.Recipe.parse(
        f"[bootstrap]\nrun = {run}\n",
        tmp_path / "splashdown.toml",
    )
    assert recipe.bootstrap is not None
    assert recipe.bootstrap.commands == expected


@pytest.mark.parametrize(
    "text",
    [
        "[bootstrap]\n",
        '[bootstrap]\nrun = ""\n',
        "[bootstrap]\nrun = []\n",
        '[bootstrap]\nrun = ["ok", ""]\n',
        '[bootstrap]\nrun = "ok"\nunknown = true\n',
    ],
)
def test_recipe_rejects_invalid_bootstrap_schema(tmp_path, text):
    with pytest.raises(ValueError, match=r"\[bootstrap"):
        sd.Recipe.parse(text, tmp_path / "splashdown.toml")


@pytest.mark.parametrize(
    ("dtype", "fields"),
    [
        ("simulator", {"model": "iPhone", "ios": "latest", "name": "Demo"}),
        ("emulator", {"device": "pixel_9", "image": "image", "name": "Demo"}),
        ("device", {"id": "ABC", "name": "Phone", "platform": "android"}),
    ],
)
def test_target_schema_accepts_every_type(tmp_path, dtype, fields):
    recipe = sd.Recipe(
        {"targets": {dtype: {"default": fields}}},
        tmp_path / "splashdown.toml",
    )
    local = sd.LocalConfig(
        {"targets": {dtype: {"local": fields}}},
        tmp_path / "splashdown.local.toml",
    )
    global_config = sd.GlobalConfig(
        {"targets": {dtype: {"global": fields}}},
        tmp_path / "config.toml",
    )
    assert recipe.targets[dtype]["default"] == fields
    assert local.targets[dtype]["local"] == fields
    assert global_config.targets[dtype]["global"] == fields


@pytest.mark.parametrize(
    ("dtype", "fields"),
    [
        ("simulator", {"device": "pixel"}),
        ("emulator", {"ios": "latest"}),
        ("device", {"model": "iPhone"}),
        ("device", {"platform": "windows"}),
        ("simulator", {"name": ""}),
    ],
)
def test_target_schema_rejects_incompatible_or_invalid_fields(tmp_path, dtype, fields):
    with pytest.raises(ValueError, match=rf"\[targets\.{dtype}\.default"):
        sd.Recipe(
            {"targets": {dtype: {"default": fields}}},
            tmp_path / "splashdown.toml",
        )


@pytest.mark.parametrize("config_type", [sd.LocalConfig, sd.GlobalConfig])
def test_auxiliary_configs_reject_unknown_top_level_sections(tmp_path, config_type):
    with pytest.raises(ValueError, match=r"\[document\] unknown field `project`"):
        config_type({"project": {}}, tmp_path / "config.toml")


def test_schema_values_track_loader_and_profile_registries(tmp_path):
    for loader in sd.LOADERS:
        sd.Recipe({"project": {"loader": loader}}, tmp_path / "splashdown.toml")
    for profile in sd.PROFILES:
        sd.Recipe(
            {"apps": {"main": {"path": ".", "profile": profile, "resources": []}}},
            tmp_path / "splashdown.toml",
        )


def test_all_builtin_scaffolds_validate(tmp_path):
    for name, scaffold in sd.SCAFFOLDS.items():
        recipe = sd.Recipe.parse(
            scaffold.replace("__SPLASH_LOADER__", "none"),
            tmp_path / f"{name}.toml",
        )
        assert isinstance(recipe.resources, dict)


def test_builtin_scaffolds_are_intent_only():
    assert set(sd.SCAFFOLDS) == {"minimal", "server", "electron"}


def test_template_name_schema_tracks_the_render_scope(tmp_path):
    """_TEMPLATE_NAMES is the load-time whitelist; _make_scope is what render
    actually binds. Drift either way is silent: a helper added to the scope is
    unusable, and a name dropped from the scope passes validation then fails at
    render."""
    assert set(sd.recipe._make_scope(tmp_path, "main", {})) == sd.recipe._TEMPLATE_NAMES


def test_topo_sort_orders_refs(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[resources.B]
type = "template"
template = "x-{{ A }}"

[resources.A]
type = "uuid"
""")
    r = sd.Recipe.load(p)
    order = sd.topo_sort(r)
    assert order.index("A") < order.index("B")


def test_topo_sort_detects_cycle(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[resources.A]
type = "template"
template = "{{ B }}"
[resources.B]
type = "template"
template = "{{ A }}"
""")
    with pytest.raises(ValueError, match=r"\[resources\.A\.template\].*cycle"):
        sd.Recipe.load(p)


def test_recipe_rejects_bad_name(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[resources."BAD-NAME"]
type = "uuid"
""")
    with pytest.raises(ValueError):
        sd.Recipe.load(p)


def test_recipe_parses_project(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text('[project]\nframework = "flutter"\n')
    r = sd.Recipe.load(p)
    assert r.project["framework"] == "flutter"


def test_resolve_device_name_template(tmp_path):
    cwd = tmp_path / "feat-y"
    cwd.mkdir()
    spec = {"name": "{{ basename(parent) }}-{{ cwd }}"}
    out = sd._resolve_device_name(spec, cwd, "default")
    assert out == f"{tmp_path.name}-feat-y"


def test_resolve_device_name_default_uses_variant_suffix(tmp_path):
    cwd = tmp_path / "feat-z"
    cwd.mkdir()
    out = sd._resolve_device_name({}, cwd, "small-screen")
    assert out == sd._default_sim_name(cwd, "small-screen")


def test_resolve_device_name_sanitized_for_android(tmp_path):
    """avdmanager rejects '/' in names; the default path-derived name has two slashes."""
    cwd = tmp_path / "feat-z"
    cwd.mkdir()
    out = sd._resolve_device_name({}, cwd, "default", dtype="emulator")
    assert "/" not in out
    assert out == sd.devices._sanitize_avd_name(sd._default_sim_name(cwd, "default"))


def test_resolve_device_name_ios_keeps_slashes(tmp_path):
    """iOS sims accept '/' so we preserve the human-readable separators."""
    cwd = tmp_path / "feat-z"
    cwd.mkdir()
    out = sd._resolve_device_name({}, cwd, "default", dtype="simulator")
    assert out == sd._default_sim_name(cwd, "default")


def test_default_sim_name_includes_variant(tmp_path):
    cwd = tmp_path / "myapp.feat-x"
    cwd.mkdir()
    digest = hashlib.sha256(str(cwd.resolve()).encode()).hexdigest()[:8]
    assert sd._default_sim_name(cwd, "default") == (
        f"{tmp_path.name}/myapp.feat-x/default-{digest}"
    )
    assert sd._default_sim_name(cwd, "small-screen") == (
        f"{tmp_path.name}/myapp.feat-x/small-screen-{digest}"
    )


def test_resolve_variant_explicit_wins():
    catalog = {"default": {"model": "A"}, "small-screen": {"model": "B"}}
    name, spec = sd.resolve_variant(catalog, "small-screen")
    assert name == "small-screen"
    assert spec["model"] == "B"


def test_resolve_variant_picks_default_when_unspecified():
    catalog = {"default": {"model": "A"}, "small-screen": {"model": "B"}}
    name, _ = sd.resolve_variant(catalog, None)
    assert name == "default"


def test_resolve_variant_picks_single_when_no_default():
    catalog = {"lonely": {"model": "X"}}
    name, _ = sd.resolve_variant(catalog, None)
    assert name == "lonely"


def test_resolve_variant_errors_when_multiple_no_default():
    with pytest.raises(sd.DeviceError, match="default"):
        sd.resolve_variant({"a": {}, "b": {}}, None)


def test_resolve_variant_errors_when_unknown_variant():
    with pytest.raises(sd.DeviceError, match="no variant `ghost`"):
        sd.resolve_variant({"default": {}}, "ghost")


def test_resolve_variant_errors_when_empty_catalog():
    with pytest.raises(sd.DeviceError, match="no variants"):
        sd.resolve_variant({}, None)


def test_resolve_variant_prefix_unique_hit():
    catalog = {"large-screen": {"model": "B"}, "default": {"model": "A"}}
    name, spec = sd.resolve_variant(catalog, "lar", prefix_match=True)
    assert name == "large-screen"
    assert spec["model"] == "B"


def test_resolve_variant_prefix_exact_still_wins():
    catalog = {"de": {"model": "X"}, "default": {"model": "Y"}}
    name, _ = sd.resolve_variant(catalog, "de", prefix_match=True)
    assert name == "de"


def test_resolve_variant_prefix_ambiguous_errors():
    catalog = {"small-a": {}, "small-b": {}}
    with pytest.raises(sd.DeviceError, match="ambiguous variant `small`"):
        sd.resolve_variant(catalog, "small", prefix_match=True)


def test_resolve_variant_prefix_no_match_falls_through():
    with pytest.raises(sd.DeviceError, match="no variant `ghost`"):
        sd.resolve_variant({"default": {}}, "ghost", prefix_match=True)


def test_resolve_variant_prefix_disabled_requires_exact():
    with pytest.raises(sd.DeviceError, match="no variant `lar`"):
        sd.resolve_variant({"large-screen": {}}, "lar", prefix_match=False)


def test_resolve_variant_empty_prefix_is_not_ambiguous():
    # "" prefixes every variant; the guard must not let it raise "ambiguous".
    with pytest.raises(sd.DeviceError, match="no variant"):
        sd.resolve_variant({"default": {}, "large-screen": {}}, "", prefix_match=True)


def test_parse_settings_rejects_non_table():
    with pytest.raises(ValueError, match=r"x\.toml: \[settings\].*expected a table"):
        sd.recipe._parse_settings({"settings": "nope"}, source="x.toml")


def test_parse_settings_rejects_unknown_key():
    with pytest.raises(ValueError, match=r"\[settings\] unknown field `prefix_mtach`"):
        sd.recipe._parse_settings({"settings": {"prefix_mtach": False}}, source="x.toml")


def test_parse_settings_rejects_wrong_type():
    with pytest.raises(ValueError, match=r"\[settings\.prefix_match\].*expected bool"):
        sd.recipe._parse_settings({"settings": {"prefix_match": 1}}, source="x.toml")


def test_load_settings_defaults_on(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert sd.load_settings(tmp_path).prefix_match is True


def test_load_settings_global_override(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg" / "splashdown"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text("[settings]\nprefix_match = false\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    co = tmp_path / "co"
    co.mkdir()
    assert sd.load_settings(co).prefix_match is False


def test_load_settings_local_overrides_global(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg" / "splashdown"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text("[settings]\nprefix_match = false\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    co = tmp_path / "co"
    co.mkdir()
    (co / sd.LOCAL_NAME).write_text("[settings]\nprefix_match = true\n")
    assert sd.load_settings(co).prefix_match is True


def test_merged_devices_unions_recipe_and_local(tmp_path):
    r = sd.Recipe(
        {"targets": {"simulator": {"default": {"model": "iPhone 17"}}}},
        tmp_path / "splashdown.toml",
    )
    lc = sd.LocalConfig(
        {"targets": {"simulator": {"repro-bug": {"model": "iPhone 16"}}}},
        tmp_path / "splashdown.local.toml",
    )
    merged = sd.merged_targets(r, lc)
    assert set(merged["simulator"]) == {"default", "repro-bug"}


def test_merged_devices_collision_errors(tmp_path):
    r = sd.Recipe(
        {"targets": {"simulator": {"default": {"model": "A"}}}},
        tmp_path / "splashdown.toml",
    )
    lc = sd.LocalConfig(
        {"targets": {"simulator": {"default": {"model": "B"}}}},
        tmp_path / "splashdown.local.toml",
    )
    with pytest.raises(ValueError, match="already exists in recipe"):
        sd.merged_targets(r, lc)


def test_global_config_absent_is_empty(tmp_path):
    assert sd.GlobalConfig.load(tmp_path / "config.toml").targets == {}


def test_global_config_malformed_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[targets.bogus.x]\n")
    with pytest.raises(ValueError, match="unknown target type"):
        sd.GlobalConfig.load(p)


def test_merged_targets_global_device_available_everywhere(tmp_path):
    r = sd.Recipe({}, tmp_path / "splashdown.toml")
    lc = sd.LocalConfig({}, tmp_path / "splashdown.local.toml")
    gc = sd.GlobalConfig(
        {"targets": {"device": {"my-iphone": {"platform": "ios"}}}},
        tmp_path / "config.toml",
    )
    assert sd.merged_targets(r, lc, gc)["device"]["my-iphone"] == {"platform": "ios"}


def test_merged_targets_global_sim_gated_by_declared_type(tmp_path):
    r = sd.Recipe({}, tmp_path / "splashdown.toml")
    lc = sd.LocalConfig({}, tmp_path / "splashdown.local.toml")
    gc = sd.GlobalConfig(
        {"targets": {"simulator": {"g": {"model": "iPhone 17"}}}},
        tmp_path / "config.toml",
    )
    assert "simulator" not in sd.merged_targets(r, lc, gc)


def test_merged_targets_global_sim_appears_when_type_declared(tmp_path):
    r = sd.Recipe(
        {"targets": {"simulator": {"default": {"model": "iPhone 17"}}}},
        tmp_path / "splashdown.toml",
    )
    lc = sd.LocalConfig({}, tmp_path / "splashdown.local.toml")
    gc = sd.GlobalConfig(
        {"targets": {"simulator": {"g": {"model": "iPhone 12"}}}},
        tmp_path / "config.toml",
    )
    assert set(sd.merged_targets(r, lc, gc)["simulator"]) == {"default", "g"}


def test_merged_targets_project_silently_shadows_global(tmp_path):
    r = sd.Recipe(
        {"targets": {"device": {"main": {"platform": "ios", "name": "recipe-phone"}}}},
        tmp_path / "splashdown.toml",
    )
    lc = sd.LocalConfig({}, tmp_path / "splashdown.local.toml")
    gc = sd.GlobalConfig(
        {"targets": {"device": {"main": {"platform": "android"}}}},
        tmp_path / "config.toml",
    )
    merged = sd.merged_targets(r, lc, gc)  # no error, unlike recipe-vs-local
    assert merged["device"]["main"]["name"] == "recipe-phone"


def test_merged_targets_local_silently_shadows_global(tmp_path):
    r = sd.Recipe({}, tmp_path / "splashdown.toml")
    lc = sd.LocalConfig(
        {"targets": {"device": {"main": {"platform": "ios", "name": "local-phone"}}}},
        tmp_path / "splashdown.local.toml",
    )
    gc = sd.GlobalConfig(
        {"targets": {"device": {"main": {"platform": "android"}}}},
        tmp_path / "config.toml",
    )
    assert sd.merged_targets(r, lc, gc)["device"]["main"]["name"] == "local-phone"


def test_merged_targets_global_emulator_gated_by_declared_type(tmp_path):
    lc = sd.LocalConfig({}, tmp_path / "splashdown.local.toml")
    gc = sd.GlobalConfig(
        {"targets": {"emulator": {"g": {"device": "pixel_9"}}}},
        tmp_path / "config.toml",
    )
    assert "emulator" not in sd.merged_targets(sd.Recipe({}, tmp_path / "splashdown.toml"), lc, gc)
    r = sd.Recipe({"targets": {"emulator": {"default": {}}}}, tmp_path / "splashdown.toml")
    assert set(sd.merged_targets(r, lc, gc)["emulator"]) == {"default", "g"}


def test_recipe_accepts_nested_device_variants(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[targets.simulator.default]
model = "iPhone 17"

[targets.simulator.lowest-supported]
model = "iPhone 12"
""")
    r = sd.Recipe.load(p)
    assert set(r.targets["simulator"]) == {"default", "lowest-supported"}
    assert r.targets["simulator"]["default"]["model"] == "iPhone 17"


@pytest.mark.parametrize(
    ("config_type", "filename"),
    [
        (sd.Recipe, "splashdown.toml"),
        (sd.LocalConfig, "splashdown.local.toml"),
        (sd.GlobalConfig, "config.toml"),
    ],
)
def test_configs_reject_legacy_devices_table(tmp_path, config_type, filename):
    p = tmp_path / filename
    p.write_text('[devices.simulator.default]\nmodel = "iPhone 17"\n')
    with pytest.raises(
        ValueError,
        match=rf"{re.escape(filename)}: \[devices\].*renamed to `\[targets",
    ):
        config_type.load(p)


def test_cli_surfaces_recipe_errors_cleanly(tmp_path, monkeypatch, capsys):
    # Recipe validation (ValueError) should print `error: …` and exit 1, not
    # dump a traceback — notably the [devices.*]→[targets.*] migration error.
    (tmp_path / "splashdown.toml").write_text('[devices.simulator.default]\nmodel = "X"\n')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = sd.main(["--cwd", str(tmp_path), "target"])
    assert rc == 1
    assert "renamed to `[targets" in capsys.readouterr().err


def test_recipe_rejects_unknown_device_type(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text('[targets.cardboard-vr.default]\nmodel = "Pixel"\n')
    with pytest.raises(ValueError, match="unknown target type"):
        sd.Recipe.load(p)


def test_localconfig_accepts_nested_device_variants(tmp_path):
    p = tmp_path / "splashdown.local.toml"
    p.write_text("""
[targets.simulator.repro-bug]
model = "iPhone 16"
ios   = "17.5"
""")
    lc = sd.LocalConfig.load(p)
    assert lc.targets["simulator"]["repro-bug"]["ios"] == "17.5"


def test_template_scope_helpers(tmp_path):
    scope = sd._make_scope(tmp_path, "main", {})
    assert sd.render_template("{{ lower('HELLO') }}", scope) == "hello"
    assert sd.render_template("{{ upper('hello') }}", scope) == "HELLO"
    assert sd.render_template("{{ truncate('hello', 3) }}", scope) == "hel"
    h = sd.render_template("{{ hash('a', 'b') }}", scope)
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    p = sd.render_template("{{ port_hash('x') }}", scope)
    assert 8000 <= int(p) <= 9000
    assert sd.render_template("{{ port_hash('x') }}", scope) == p
