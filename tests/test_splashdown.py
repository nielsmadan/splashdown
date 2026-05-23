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


def test_provision_writes_splashdown_env(registry, checkout):
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
    text = (checkout / "splashdown.env").read_text()
    assert f'PORT={resolved["PORT"]}' in text
    assert "URL=" in text


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


def test_splashdown_env_writer_basic(tmp_path):
    target = tmp_path / "splashdown.env"
    sd.write_splashdown_env(target, {"PORT": "8082", "RUN_ID": "abc-def"})
    text = target.read_text()
    assert "PORT=8082" in text
    assert "RUN_ID=abc-def" in text


def test_splashdown_env_writer_quotes_specials(tmp_path):
    target = tmp_path / "splashdown.env"
    sd.write_splashdown_env(target, {"URL": "http://localhost:8082", "MSG": "has spaces"})
    text = target.read_text()
    assert 'MSG="has spaces"' in text
    # A plain URL has no spaces; ':' and '/' are allowed unquoted.
    assert "URL=http://localhost:8082" in text


def test_splashdown_env_writer_overwrites_wholesale(tmp_path):
    target = tmp_path / "splashdown.env"
    target.write_text("STALE=1\nOLD=2\n")
    sd.write_splashdown_env(target, {"PORT": "8082"})
    text = target.read_text()
    assert "STALE" not in text
    assert text.strip() == "PORT=8082"


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

def test_recipe_rejects_devices_section(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[resources.PORT]
type  = "port"
range = [3000, 3100]

[devices.iphone]
type = "ios-sim"
""")
    with pytest.raises(ValueError, match="devices"):
        sd.Recipe.load(p)


def test_recipe_parses_project(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text('[project]\nframework = "flutter"\n')
    r = sd.Recipe.load(p)
    assert r.project["framework"] == "flutter"


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


def test_init_writes_recipe_and_local_skeleton(tmp_path):
    sd.cmd_init(tmp_path, preset="rn")
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[resources." in recipe
    assert "[devices." not in recipe          # devices never in the committed recipe
    assert (tmp_path / "splashdown.local.toml").exists()
    skeleton = (tmp_path / "splashdown.local.toml").read_text()
    assert "devices.iphone" in skeleton       # commented example present


def test_init_does_not_clobber_existing_local(tmp_path):
    (tmp_path / "splashdown.local.toml").write_text("[devices.mine]\ntype = \"ios-sim\"\n")
    sd.cmd_init(tmp_path, preset="rn")
    assert "devices.mine" in (tmp_path / "splashdown.local.toml").read_text()


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
    assert (cwd / "splashdown.env").exists()


def test_cli_provision_drops_local_skeleton(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "co"; cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [18900, 18910]
""")
    sd.main(["--cwd", str(cwd)])
    assert (cwd / "splashdown.local.toml").exists()
    assert "devices.iphone" in (cwd / "splashdown.local.toml").read_text()


def test_cli_provision_preserves_existing_local(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "co"; cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [18920, 18930]
""")
    (cwd / "splashdown.local.toml").write_text('[devices.mine]\ntype = "ios-sim"\n')
    sd.main(["--cwd", str(cwd)])
    assert "devices.mine" in (cwd / "splashdown.local.toml").read_text()


def test_device_add_appends_block(tmp_path):
    sd.device_add(tmp_path, "iphone", "ios-sim", {"model": "iPhone 16 Pro"})
    lc = sd.LocalConfig.load(tmp_path / "splashdown.local.toml")
    assert lc.devices["iphone"]["type"] == "ios-sim"
    assert lc.devices["iphone"]["model"] == "iPhone 16 Pro"


def test_device_add_rejects_duplicate(tmp_path):
    sd.device_add(tmp_path, "iphone", "ios-sim", {})
    with pytest.raises(sd.DeviceError, match="already"):
        sd.device_add(tmp_path, "iphone", "ios-sim", {})


def test_device_add_rejects_bad_type(tmp_path):
    with pytest.raises(sd.DeviceError, match="type"):
        sd.device_add(tmp_path, "iphone", "not-a-type", {})


def test_device_add_rejects_bad_name(tmp_path):
    with pytest.raises(sd.DeviceError):
        sd.device_add(tmp_path, "has spaces", "ios-sim", {})


def test_device_remove_deletes_block(tmp_path):
    sd.device_add(tmp_path, "iphone", "ios-sim", {})
    sd.device_add(tmp_path, "android", "android-emulator", {})
    sd.device_remove(tmp_path, "iphone")
    lc = sd.LocalConfig.load(tmp_path / "splashdown.local.toml")
    assert set(lc.devices) == {"android"}


def test_device_remove_unknown_errors(tmp_path):
    with pytest.raises(sd.DeviceError, match="no device"):
        sd.device_remove(tmp_path, "ghost")


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
    sd.cmd_init(tmp_path, preset="minimal")
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
    sd.cmd_init(tmp_path, preset="minimal")
    sd.cmd_init(tmp_path, preset="minimal", force=True)
    mise = (tmp_path / "mise.toml").read_text()
    assert mise.count('_.file = "splashdown.env"') == 1


def test_init_writes_post_checkout_hook(tmp_path):
    sd.cmd_init(tmp_path, preset="minimal")
    hook = tmp_path / ".githooks" / "post-checkout"
    assert hook.exists()
    assert os.access(hook, os.X_OK)
    assert POST_CHECKOUT_SENTINEL in hook.read_text()


# ---------- hook-manager detection and wiring ----------

def test_detect_hook_manager_clean(tmp_path):
    assert sd._detect_hook_manager(tmp_path) == "none"


def test_detect_hook_manager_lefthook_yml(tmp_path):
    (tmp_path / "lefthook.yml").write_text("pre-commit:\n  commands: {}\n")
    assert sd._detect_hook_manager(tmp_path) == "lefthook"


def test_detect_hook_manager_lefthook_yaml(tmp_path):
    (tmp_path / "lefthook.yaml").write_text("")
    assert sd._detect_hook_manager(tmp_path) == "lefthook"


def test_detect_hook_manager_lefthook_dotted(tmp_path):
    (tmp_path / ".lefthook.yml").write_text("")
    assert sd._detect_hook_manager(tmp_path) == "lefthook"


def test_detect_hook_manager_lefthook_via_pkg(tmp_path):
    (tmp_path / "package.json").write_text('{"devDependencies": {"lefthook": "^1.0"}}')
    assert sd._detect_hook_manager(tmp_path) == "lefthook"


def test_detect_hook_manager_husky(tmp_path):
    (tmp_path / ".husky").mkdir()
    assert sd._detect_hook_manager(tmp_path) == "husky"


def test_wire_lefthook_appends_block_when_absent(tmp_path):
    (tmp_path / "lefthook.yml").write_text("pre-commit:\n  commands:\n    lint:\n      run: echo lint\n")
    sd._wire_post_checkout_lefthook(tmp_path)
    text = (tmp_path / "lefthook.yml").read_text()
    assert "pre-commit:" in text
    assert "post-checkout:" in text
    assert "splashdown:" in text
    assert "run: splash" in text


def test_wire_lefthook_inserts_under_existing_post_checkout(tmp_path):
    (tmp_path / "lefthook.yml").write_text(
        "post-checkout:\n  commands:\n    notify:\n      run: echo hi\n"
    )
    sd._wire_post_checkout_lefthook(tmp_path)
    text = (tmp_path / "lefthook.yml").read_text()
    assert "notify:" in text  # existing command preserved
    assert "splashdown:" in text  # ours added
    assert text.count("post-checkout:") == 1  # not duplicated


def test_wire_lefthook_idempotent(tmp_path):
    (tmp_path / "lefthook.yml").write_text("")
    sd._wire_post_checkout_lefthook(tmp_path)
    once = (tmp_path / "lefthook.yml").read_text()
    sd._wire_post_checkout_lefthook(tmp_path)
    twice = (tmp_path / "lefthook.yml").read_text()
    assert once == twice
    assert twice.count("splashdown:") == 1


def test_wire_lefthook_creates_config_if_only_pkg_dep(tmp_path):
    # Detected via package.json but no lefthook.yml yet.
    (tmp_path / "package.json").write_text('{"devDependencies": {"lefthook": "^1.0"}}')
    sd._wire_post_checkout_lefthook(tmp_path)
    assert (tmp_path / "lefthook.yml").exists()
    assert "splashdown:" in (tmp_path / "lefthook.yml").read_text()


def test_wire_husky_creates_executable_hook(tmp_path):
    sd._wire_post_checkout_husky(tmp_path)
    hook = tmp_path / ".husky" / "post-checkout"
    assert hook.exists()
    assert os.access(hook, os.X_OK)
    assert "splash" in hook.read_text()


def test_ensure_hook_chooses_lefthook(tmp_path):
    (tmp_path / "lefthook.yml").write_text("")
    sd._ensure_post_checkout_hook(tmp_path)
    # lefthook wiring happened; no .githooks dir created.
    assert "splashdown:" in (tmp_path / "lefthook.yml").read_text()
    assert not (tmp_path / ".githooks").exists()


def test_ensure_hook_chooses_husky(tmp_path):
    (tmp_path / ".husky").mkdir()
    sd._ensure_post_checkout_hook(tmp_path)
    assert (tmp_path / ".husky" / "post-checkout").exists()
    assert not (tmp_path / ".githooks").exists()


def test_ensure_hook_clean_falls_back_to_corehookspath(tmp_path):
    sd._ensure_post_checkout_hook(tmp_path)
    hook = tmp_path / ".githooks" / "post-checkout"
    assert hook.exists()
    assert os.access(hook, os.X_OK)


# ---------- doctor (no framework wiring entries yet) ----------

def test_doctor_help_in_cli(capsys):
    with pytest.raises(SystemExit):
        sd.main(["doctor", "--help"])
    out = capsys.readouterr().out
    assert "--fix" in out
    assert "--framework" in out


def test_doctor_no_framework_returns_1(tmp_path, capsys):
    # No recipe, no package.json, no pubspec — detect_framework fails.
    rc = sd.cmd_doctor(tmp_path)
    assert rc == 1
    err = capsys.readouterr().err
    assert "framework" in err.lower()


def test_doctor_unknown_framework_no_checks_returns_0(tmp_path, capsys):
    # Override to a framework that has no WIRING entries.
    rc = sd.cmd_doctor(tmp_path, framework_override="nonesuch")
    assert rc == 0
    err = capsys.readouterr().err
    assert "no wiring checks" in err.lower()


def test_doctor_detects_framework_from_recipe(tmp_path):
    (tmp_path / "splashdown.toml").write_text('[project]\nframework = "nonesuch"\n')
    # nonesuch has no WIRING entries → returns 0 without erroring.
    assert sd.cmd_doctor(tmp_path) == 0


def test_doctor_uses_filesystem_when_no_recipe(tmp_path):
    # package.json with react-native → detect_framework returns "react-native".
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    # WIRING["react-native"] now exists (rn-hook). A clean RN dir is missing the
    # hook → doctor reports a problem.
    assert sd.cmd_doctor(tmp_path) == 1


# ---------- rn-hook check ----------

import subprocess as _subprocess


def _git_init(path):
    _subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_rn_hook_clean_detect_problem(tmp_path):
    status, _ = sd._rn_hook_detect(tmp_path)
    assert status == "problem"


def test_rn_hook_clean_autofix_then_ok(tmp_path):
    _git_init(tmp_path)
    sd._ensure_post_checkout_hook(tmp_path)
    status, _ = sd._rn_hook_detect(tmp_path)
    assert status == "ok"


def test_rn_hook_lefthook_detect_problem(tmp_path):
    (tmp_path / "lefthook.yml").write_text("pre-commit:\n  commands: {}\n")
    status, detail = sd._rn_hook_detect(tmp_path)
    assert status == "problem"
    assert "lefthook" in detail


def test_rn_hook_lefthook_autofix_then_ok(tmp_path):
    (tmp_path / "lefthook.yml").write_text("pre-commit:\n  commands:\n    lint:\n      run: echo lint\n")
    sd._ensure_post_checkout_hook(tmp_path)
    status, _ = sd._rn_hook_detect(tmp_path)
    assert status == "ok"
    text = (tmp_path / "lefthook.yml").read_text()
    assert "lint" in text  # existing preserved
    assert "splashdown:" in text  # ours added


def test_rn_hook_husky_detect_problem(tmp_path):
    (tmp_path / ".husky").mkdir()
    status, _ = sd._rn_hook_detect(tmp_path)
    assert status == "problem"


def test_rn_hook_husky_autofix_then_ok(tmp_path):
    (tmp_path / ".husky").mkdir()
    sd._ensure_post_checkout_hook(tmp_path)
    status, _ = sd._rn_hook_detect(tmp_path)
    assert status == "ok"


def test_doctor_fix_wires_hook_in_clean_rn_dir(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    assert sd.cmd_doctor(tmp_path) == 1  # not wired
    assert sd.cmd_doctor(tmp_path, fix=True) == 0  # now wired
    assert sd.cmd_doctor(tmp_path) == 0  # idempotent re-check
    assert (tmp_path / ".githooks" / "post-checkout").exists()
