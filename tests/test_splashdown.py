"""Tests for splashdown.

Run with: python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import splashdown as sd  # noqa: E402


@pytest.fixture
def registry(tmp_path: Path) -> sd.Registry:
    return sd.Registry(port_file=tmp_path / "ports.tsv", kv_file=tmp_path / "kv.tsv")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    d = tmp_path / "co"
    d.mkdir()
    return d


# ---------- registry ----------

def test_port_allocate_persists(registry, checkout):
    p1 = registry.allocate_port(str(checkout), "METRO", 18081, 18100)
    p2 = registry.allocate_port(str(checkout), "METRO", 18081, 18100)
    assert p1 == p2


def test_two_checkouts_get_different_ports(registry, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    pa = registry.allocate_port(str(a), "METRO", 18081, 18100)
    pb = registry.allocate_port(str(b), "METRO", 18081, 18100)
    assert pa != pb


def test_gc_frees_dead_checkout(registry, tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    pa = registry.allocate_port(str(a), "X", 18101, 18110)
    a.rmdir()  # simulate worktree removal
    pb = registry.allocate_port(str(b), "X", 18101, 18110)
    # b should be allowed to take a's port now
    assert pb == pa


def test_kv_set_get(registry, checkout):
    registry.set_kv(str(checkout), "K", "v1")
    assert registry.get_kv(str(checkout), "K") == "v1"
    registry.set_kv(str(checkout), "K", "v2")
    assert registry.get_kv(str(checkout), "K") == "v2"


def test_unpin_clears_entries(registry, checkout):
    registry.allocate_port(str(checkout), "P", 18200, 18210)
    registry.set_kv(str(checkout), "K", "v")
    n = registry.unpin(str(checkout))
    assert n == 2
    assert registry.all_for(str(checkout)) == {}


def test_all_for_returns_combined(registry, checkout):
    registry.allocate_port(str(checkout), "PORT", 18300, 18310)
    registry.set_kv(str(checkout), "ID", "abc")
    assert set(registry.all_for(str(checkout))) == {"PORT", "ID"}


# ---------- templates ----------

def test_template_basic_vars(tmp_path):
    cwd = tmp_path / "myrepo.feat"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "feat", {})
    assert sd.render_template("{{ cwd }}", scope) == "myrepo.feat"
    assert sd.render_template("port-{{ basename(cwd_abs) }}", scope) == "port-myrepo.feat"
    assert sd.render_template("{{ slug(cwd) }}", scope) == "myrepo-feat"


def test_template_cross_resource(tmp_path):
    cwd = tmp_path / "x"; cwd.mkdir()
    scope = sd._make_scope(cwd, "", {"PORT": "8081"})
    assert sd.render_template("http://localhost:{{ PORT }}", scope) == "http://localhost:8081"


def test_template_refs():
    refs = sd.template_refs("http://x:{{ PORT }}/{{ basename(cwd) }}")
    assert "PORT" in refs
    assert "basename" in refs


def test_template_error_on_bad_expr(tmp_path):
    cwd = tmp_path / "x"; cwd.mkdir()
    scope = sd._make_scope(cwd, "", {})
    with pytest.raises(sd.TemplateError):
        sd.render_template("{{ no_such_var }}", scope)


# ---------- recipe / topo ----------

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


# ---------- end-to-end provision ----------

def _write_recipe(cwd: Path, body: str) -> None:
    (cwd / "splashdown.toml").write_text(body)


def test_provision_writes_mise_local(registry, checkout):
    _write_recipe(checkout, """
[resources.PORT]
type  = "port"
range = [18400, 18410]

[resources.RUN_ID]
type = "uuid"

[resources.URL]
type     = "template"
template = "http://localhost:{{ PORT }}"
""")
    resolved = sd.provision(checkout, registry=registry)
    assert 18400 <= int(resolved["PORT"]) <= 18410
    assert resolved["URL"] == f"http://localhost:{resolved['PORT']}"
    assert len(resolved["RUN_ID"]) == 36  # uuid string length

    recipe = sd.Recipe.load(checkout / "splashdown.toml")
    sd.write_outputs(checkout, recipe, resolved)
    text = (checkout / "mise.local.toml").read_text()
    assert "[env]" in text
    assert f'PORT = "{resolved["PORT"]}"' in text
    assert "URL = " in text


def test_provision_idempotent(registry, checkout):
    _write_recipe(checkout, """
[resources.RUN_ID]
type = "uuid"
[resources.PORT]
type  = "port"
range = [18500, 18510]
""")
    r1 = sd.provision(checkout, registry=registry)
    r2 = sd.provision(checkout, registry=registry)
    assert r1 == r2


def test_provision_reprovision_regenerates_uuid(registry, checkout):
    _write_recipe(checkout, """
[resources.RUN_ID]
type = "uuid"
""")
    r1 = sd.provision(checkout, registry=registry)
    r2 = sd.provision(checkout, registry=registry, reprovision=True)
    assert r1["RUN_ID"] != r2["RUN_ID"]


def test_mise_local_preserves_user_keys(registry, checkout):
    (checkout / "mise.local.toml").write_text("""\
[env]
USER_KEY = "keep-me"
PORT = "stale"

[tools]
node = "20"
""")
    _write_recipe(checkout, """
[resources.PORT]
type  = "port"
range = [18600, 18610]
""")
    resolved = sd.provision(checkout, registry=registry)
    recipe = sd.Recipe.load(checkout / "splashdown.toml")
    sd.write_outputs(checkout, recipe, resolved)
    text = (checkout / "mise.local.toml").read_text()
    assert 'USER_KEY = "keep-me"' in text
    assert '[tools]' in text
    assert 'node = "20"' in text
    # PORT line should be updated, not duplicated
    assert text.count("PORT =") == 1
    assert f'PORT = "{resolved["PORT"]}"' in text


def test_envfile_writer(registry, checkout):
    _write_recipe(checkout, """
[resources.MY_VAR]
type     = "template"
template = "hello"
writer   = "envfile=.env.local"
""")
    resolved = sd.provision(checkout, registry=registry)
    recipe = sd.Recipe.load(checkout / "splashdown.toml")
    sd.write_outputs(checkout, recipe, resolved)
    text = (checkout / ".env.local").read_text()
    assert "MY_VAR=hello" in text


def test_cwd_resource_type(registry, tmp_path):
    cwd = tmp_path / "mybranch"; cwd.mkdir()
    _write_recipe(cwd, """
[resources.NAME]
type = "cwd"
""")
    resolved = sd.provision(cwd, registry=registry)
    assert resolved["NAME"] == "mybranch"


def test_set_type_uses_default(registry, checkout):
    _write_recipe(checkout, """
[resources.MODE]
type    = "set"
default = "dev"
""")
    resolved = sd.provision(checkout, registry=registry)
    assert resolved["MODE"] == "dev"


def test_set_type_persists_user_value(registry, checkout):
    _write_recipe(checkout, """
[resources.MODE]
type = "set"
""")
    registry.set_kv(str(checkout.resolve()), "MODE", "prod")
    resolved = sd.provision(checkout, registry=registry)
    assert resolved["MODE"] == "prod"


def test_toml_quoting_escapes_specials():
    assert sd._toml_quote("a\\b") == r'"a\\b"'
    assert sd._toml_quote('he said "hi"') == r'"he said \"hi\""'
    assert sd._toml_quote("line\nbreak") == r'"line\nbreak"'


# ---------- writers helper ----------

def test_find_table_locates_env(tmp_path):
    lines = ["[tools]", "node = \"20\"", "", "[env]", "X = \"1\"", "Y = \"2\"", "", "[other]", "k = 1"]
    s, e = sd._find_table(lines, "env")
    assert s == 3
    assert e == 7


def test_find_table_missing(tmp_path):
    lines = ["[tools]", "node = \"20\""]
    s, e = sd._find_table(lines, "env")
    assert s is None


# ---------- recipe: devices + project ----------

def test_recipe_parses_devices_and_project(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[devices.iphone]
type  = "ios-sim"
model = "iPhone 16 Pro"

[devices.android]
type = "android-emulator"

[project]
framework = "react-native"
""")
    r = sd.Recipe.load(p)
    assert set(r.devices) == {"iphone", "android"}
    assert r.devices["iphone"]["type"] == "ios-sim"
    assert r.project["framework"] == "react-native"


def test_recipe_rejects_bad_device_name(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[devices."has spaces"]
type = "ios-sim"
""")
    with pytest.raises(ValueError):
        sd.Recipe.load(p)


def test_default_sim_name(tmp_path):
    cwd = tmp_path / "myrepo.feat-x"; cwd.mkdir()
    assert sd._default_sim_name(cwd) == f"{tmp_path.name}/myrepo.feat-x"


def test_resolve_device_name_template(tmp_path):
    cwd = tmp_path / "feat-y"; cwd.mkdir()
    spec = {"name": "{{ basename(parent) }}-{{ cwd }}"}
    out = sd._resolve_device_name(spec, cwd)
    assert out == f"{tmp_path.name}-feat-y"


def test_resolve_device_name_default(tmp_path):
    cwd = tmp_path / "feat-z"; cwd.mkdir()
    out = sd._resolve_device_name({"type": "ios-sim"}, cwd)
    assert out == f"{tmp_path.name}/feat-z"


# ---------- framework detection ----------

def test_detect_framework_flutter(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: x\n")
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "flutter"


def test_detect_framework_rn(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react-native": "0.74"}}')
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "react-native"


def test_detect_framework_expo(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"expo": "50"}}')
    (tmp_path / "app.json").write_text("{}")
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "expo"


def test_detect_framework_override_wins(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: x\n")
    r = sd.Recipe({"project": {"framework": "react-native"}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "react-native"


def test_detect_framework_errors_when_unknown(tmp_path):
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    with pytest.raises(sd.DeviceError):
        sd.detect_framework(tmp_path, r)


# ---------- pick_device ----------

def test_pick_device_single(tmp_path):
    lc = sd.LocalConfig({"devices": {"only": {"type": "ios-sim"}}}, tmp_path / "splashdown.local.toml")
    name, spec = sd.pick_device(lc, None)
    assert name == "only"


def test_pick_device_multiple_requires_name(tmp_path):
    lc = sd.LocalConfig(
        {"devices": {"a": {"type": "ios-sim"}, "b": {"type": "android-emulator"}}},
        tmp_path / "splashdown.local.toml",
    )
    with pytest.raises(sd.DeviceError):
        sd.pick_device(lc, None)
    name, _ = sd.pick_device(lc, "b")
    assert name == "b"


def test_pick_device_unknown_name(tmp_path):
    lc = sd.LocalConfig({"devices": {"a": {"type": "ios-sim"}}}, tmp_path / "splashdown.local.toml")
    with pytest.raises(sd.DeviceError):
        sd.pick_device(lc, "nope")


def test_pick_device_none_declared(tmp_path):
    lc = sd.LocalConfig({}, tmp_path / "splashdown.local.toml")
    with pytest.raises(sd.DeviceError):
        sd.pick_device(lc, None)


# ---------- CLI ----------

def test_file_name_constants():
    assert sd.RECIPE_NAME == "splashdown.toml"
    assert sd.LOCAL_NAME == "splashdown.local.toml"
    assert sd.ENV_FILE_NAME == "splashdown.env"


def test_cli_prog_name_is_splash():
    assert sd._build_parser().prog == "splash"


def test_cli_help_shows_subcommands(capsys):
    with pytest.raises(SystemExit):
        sd.main(["--help"])
    out = capsys.readouterr().out
    assert "provision" in out
    assert "device" in out
    assert "init" in out


def test_localconfig_parses_devices(tmp_path):
    p = tmp_path / "splashdown.local.toml"
    p.write_text("""
[devices.iphone]
type  = "ios-sim"
model = "iPhone 16 Pro"

[devices.android]
type = "android-emulator"
""")
    lc = sd.LocalConfig.load(p)
    assert set(lc.devices) == {"iphone", "android"}
    assert lc.devices["iphone"]["model"] == "iPhone 16 Pro"


def test_localconfig_missing_file_is_empty(tmp_path):
    lc = sd.LocalConfig.load(tmp_path / "splashdown.local.toml")
    assert lc.devices == {}


def test_localconfig_rejects_bad_device_name(tmp_path):
    p = tmp_path / "splashdown.local.toml"
    p.write_text('[devices."has spaces"]\ntype = "ios-sim"\n')
    with pytest.raises(ValueError):
        sd.LocalConfig.load(p)


def test_cli_provision_is_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "co"; cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type = "port"
range = [18900, 18910]
""")
    code = sd.main(["--cwd", str(cwd)])
    assert code == 0
    assert (cwd / "mise.local.toml").exists()
