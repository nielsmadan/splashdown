"""Tests for splashdown recipe behavior."""

from __future__ import annotations

import subprocess

import pytest

import splashdown as sd


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
    def fake_run(cmd, *a, **k):
        if "boot" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boot failed: no space")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sd.devices.subprocess, "run", fake_run)
    with pytest.raises(sd.DeviceError, match="simctl boot failed"):
        sd.devices.ios_boot("UDID-X", "Shutdown")


def test_ios_boot_tolerates_already_booted_race(monkeypatch):
    def fake_run(cmd, *a, **k):
        if "boot" in cmd:
            return subprocess.CompletedProcess(
                cmd, 149, stdout="", stderr="Unable to boot device in current state: Booted"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sd.devices.subprocess, "run", fake_run)
    sd.devices.ios_boot("UDID-X", "Shutdown")  # benign race must not raise


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
    r = sd.Recipe.load(p)
    with pytest.raises(ValueError):
        sd.topo_sort(r)


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
    assert out == f"{tmp_path.name}/feat-z/small-screen"


def test_resolve_device_name_sanitized_for_android(tmp_path):
    """avdmanager rejects '/' in names; the default path-derived name has two slashes."""
    cwd = tmp_path / "feat-z"
    cwd.mkdir()
    out = sd._resolve_device_name({}, cwd, "default", dtype="emulator")
    assert "/" not in out
    assert out == f"{tmp_path.name}_feat-z_default"


def test_resolve_device_name_ios_keeps_slashes(tmp_path):
    """iOS sims accept '/' so we preserve the human-readable separators."""
    cwd = tmp_path / "feat-z"
    cwd.mkdir()
    out = sd._resolve_device_name({}, cwd, "default", dtype="simulator")
    assert out == f"{tmp_path.name}/feat-z/default"


def test_default_sim_name_includes_variant(tmp_path):
    cwd = tmp_path / "myapp.feat-x"
    cwd.mkdir()
    assert sd._default_sim_name(cwd, "default") == f"{tmp_path.name}/myapp.feat-x/default"
    assert sd._default_sim_name(cwd, "small-screen") == f"{tmp_path.name}/myapp.feat-x/small-screen"


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
    # An exact key match short-circuits before prefix expansion.
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
    with pytest.raises(ValueError, match="must be a table"):
        sd.recipe._parse_settings({"settings": "nope"}, source="x.toml")


def test_parse_settings_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown setting `prefix_mtach`"):
        sd.recipe._parse_settings({"settings": {"prefix_mtach": False}}, source="x.toml")


def test_parse_settings_rejects_wrong_type():
    with pytest.raises(ValueError, match="must be bool"):
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


def test_recipe_rejects_legacy_devices_table(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text('[devices.simulator.default]\nmodel = "iPhone 17"\n')
    with pytest.raises(ValueError, match=r"renamed to `\[targets"):
        sd.Recipe.load(p)


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
    assert sd.render_template("{{ port_hash('x') }}", scope) == p  # deterministic
