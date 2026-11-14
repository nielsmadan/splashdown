from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import splashdown as sd
from splashdown import doctor, profiles_server, profiles_web
from splashdown.wiring import WiringCheck

pytestmark = pytest.mark.usefixtures("controlled_doctor_toolchains")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def recipe(root, apps=("one",), profile="vite"):
    (root / sd.RECIPE_NAME).write_text(
        "\n".join(
            f'[apps.{name}]\npath = "."\nprofile = "{profile}"\nresources = []\n' for name in apps
        )
    )


def check(name="sample", **changes):
    values = {
        "id": name,
        "description": name,
        "applies": lambda _: True,
        "detect": lambda _: ("problem", "broken consumer"),
        "autofix": lambda path: (path / "consumer").write_text("fixed"),
        "manual_instructions": lambda _: "Repair the consumer.",
        "files": lambda path: (path / "consumer",),
    }
    values.update(changes)
    return WiringCheck(**values)


def install_checks(monkeypatch, checks):
    monkeypatch.setattr(doctor, "tooling_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "pods_check", lambda *_: None)
    monkeypatch.setattr(doctor, "_app_checks", lambda app, parsed: checks)
    monkeypatch.setattr(doctor, "compose_wiring_checks", lambda _: [])


def rows(result):
    return result.data["outcomes"]


def sample(result, name="sample"):
    return next(row for row in rows(result) if row["check_id"] == name)


def test_configured_inventory_keeps_resource_writer_context(tmp_path):
    parsed = sd.Recipe.parse(
        """
[project]
env_file = "custom.env"
[resources.WEB]
type = "port"
range = [4100, 4200]
[resources.SHELL]
type = "uuid"
writer = "envrc"
[resources.HIDDEN]
type = "uuid"
writer = "none"
[apps.web]
path = "apps/web"
profile = "vite"
resources = ["WEB", "SHELL", "HIDDEN"]
""",
        tmp_path / sd.RECIPE_NAME,
    )
    app = doctor.configured_app_inventory(parsed)[0]
    assert app.name == "web"
    assert app.path == tmp_path / "apps/web"
    assert [(r.name, r.kind, r.writer, r.destination) for r in app.managed_resources] == [
        ("WEB", "port", "envfile=custom.env", "custom.env"),
        ("SHELL", "uuid", "envrc", ".envrc.local"),
        ("HIDDEN", "uuid", "none", None),
    ]


def test_all_repeated_apps_and_shared_checks_once(tmp_path, monkeypatch):
    recipe(tmp_path, ("one", "two"), "react-native")
    install_checks(monkeypatch, [check(autofix=None)])
    monkeypatch.setattr(doctor, "_hook_checks", lambda _: [check("hook", autofix=None)])
    monkeypatch.setattr(doctor, "WATCHMAN_CHECK", check("watch", autofix=None))
    result = doctor.run_doctor(tmp_path)
    assert [(r["app"], r["check_id"]) for r in rows(result)] == [
        (None, "configuration"),
        ("one", "sample"),
        ("two", "sample"),
        (None, "loader"),
        (None, "hook"),
        (None, "watch"),
    ]


def test_missing_and_invalid_configuration_stop_before_checks(tmp_path, monkeypatch):
    install_checks(monkeypatch, [check()])
    for content in (None, "[invalid"):
        if content is not None:
            (tmp_path / sd.RECIPE_NAME).write_text(content)
        result = doctor.run_doctor(tmp_path, fix=True)
        assert result.exit_code == 1
        assert [r["check_id"] for r in rows(result)] == ["configuration"]
        assert rows(result)[0]["path"] == str(tmp_path / sd.RECIPE_NAME)
        assert not (tmp_path / "state").exists()
        assert not (tmp_path / "consumer").exists()


def test_no_applicable_checks_reports_coverage(tmp_path, monkeypatch, capsys):
    recipe(tmp_path)
    install_checks(monkeypatch, [check(applies=lambda _: False)])
    assert doctor.cmd_doctor(tmp_path) == 0
    captured = capsys.readouterr()
    assert "No applicable checks" in captured.out
    assert "skipped: Not applicable" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("phase", ["applicability", "inspection", "guidance", "files"])
def test_phase_failure_retains_independent_results_and_never_repairs(tmp_path, monkeypatch, phase):
    recipe(tmp_path)
    calls = []

    def broken(_):
        raise OSError(f"{phase} failed")

    fields = {
        "applicability": "applies",
        "inspection": "detect",
        "guidance": "manual_instructions",
        "files": "files",
    }
    bad = check(**{fields[phase]: broken}, autofix=lambda _: calls.append("bad"))
    install_checks(
        monkeypatch,
        [bad, check("independent", detect=lambda _: ("warning", "advisory"), autofix=None)],
    )
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result)["state"] == "incomplete"
    assert phase in sample(result)["message"].lower()
    assert sample(result, "independent")["state"] == "warning"
    assert calls == []
    assert not (tmp_path / "state").exists()


def test_app_and_shared_construction_failures_are_isolated(tmp_path, monkeypatch):
    recipe(tmp_path, ("one", "two"), "react-native")

    def construction(app, parsed):
        if app.name == "one":
            raise ValueError("app construction")
        return [check(autofix=None)]

    def compose(_):
        raise ValueError("compose construction")

    monkeypatch.setattr(doctor, "_app_checks", construction)
    monkeypatch.setattr(doctor, "compose_wiring_checks", compose)
    monkeypatch.setattr(doctor, "_hook_checks", lambda _: [check("hook", autofix=None)])
    result = doctor.run_doctor(tmp_path)
    assert result.exit_code == 1
    assert sample(result)["app"] == "two"
    assert sample(result, "hook")["state"] == "problem"
    assert sum(r["state"] == "incomplete" for r in rows(result)) == 2


@pytest.mark.parametrize("failure", ["repair", "recheck", "interrupt", "accounting"])
def test_partial_changes_survive_failures_and_cancellation(tmp_path, monkeypatch, failure):
    recipe(tmp_path)

    def repair(path):
        (path / "consumer").write_text("fixed")
        if failure == "repair":
            raise OSError("partial repair")
        if failure == "interrupt":
            raise KeyboardInterrupt

    def detect(path):
        if (path / "consumer").exists():
            if failure == "recheck":
                raise OSError("recheck unavailable")
            return "ok", "verified"
        return "problem", "needs repair"

    def files(path):
        if failure == "accounting" and (path / "consumer").exists():
            raise OSError("accounting unavailable")
        return (path / "consumer",)

    first = check(
        "earlier",
        detect=lambda path: (
            ("ok", "verified") if (path / "earlier").exists() else ("problem", "broken")
        ),
        autofix=lambda path: (path / "earlier").write_text("fixed"),
        files=lambda path: (path / "earlier",),
    )
    install_checks(
        monkeypatch,
        [first, check(detect=detect, autofix=repair, files=files), check("later", autofix=None)],
    )
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == (130 if failure == "interrupt" else 1)
    assert sample(result, "earlier")["repair"] == "repaired"
    assert sample(result)["changed_files"] == [str(tmp_path / "consumer")]
    assert sample(result)["repair"] == ("failed" if failure == "repair" else "unverified")
    assert result.data["summary"]["changed_files"] == [
        str(tmp_path / "consumer"),
        str(tmp_path / "earlier"),
    ]
    assert ("later" in [r["check_id"] for r in rows(result)]) is (failure != "interrupt")


@pytest.mark.parametrize("phase", ["enumeration", "read"])
def test_interrupted_change_accounting_retains_known_changes(tmp_path, monkeypatch, phase):
    recipe(tmp_path)
    candidates = (tmp_path / "consumer-a", tmp_path / "consumer-b")
    read_bytes = doctor.read_optional_editable_bytes

    def read(path, *, root):
        if phase == "read" and path == candidates[1] and path.exists():
            raise KeyboardInterrupt
        return read_bytes(path, root=root)

    def files(_):
        if phase == "enumeration" and candidates[0].exists():
            raise KeyboardInterrupt
        return candidates

    def repair(_):
        for path in candidates:
            path.write_text("fixed")

    first = check(
        "earlier",
        detect=lambda path: (
            ("ok", "verified") if (path / "earlier").exists() else ("problem", "broken")
        ),
        autofix=lambda path: (path / "earlier").write_text("fixed"),
        files=lambda path: (path / "earlier",),
    )
    install_checks(
        monkeypatch,
        [
            first,
            check(
                detect=lambda _: (
                    ("ok", "verified") if candidates[0].exists() else ("problem", "broken")
                ),
                autofix=repair,
                files=files,
            ),
            check("later", autofix=None),
        ],
    )
    monkeypatch.setattr(doctor, "read_optional_editable_bytes", read)

    result = doctor.run_doctor(tmp_path, fix=True)

    assert result.exit_code == 130
    assert [row["check_id"] for row in rows(result)] == ["configuration", "earlier", "sample"]
    assert sample(result, "earlier")["repair"] == "repaired"
    outcome = sample(result)
    assert outcome["state"] == "incomplete"
    assert outcome["repair"] == "unverified"
    assert "change accounting incomplete" in outcome["message"]
    assert "interrupted" in outcome["message"]
    known_changes = candidates if phase == "enumeration" else candidates[:1]
    assert outcome["changed_files"] == [str(path) for path in known_changes]
    assert result.data["summary"]["changed_files"] == [
        str(path) for path in (*known_changes, tmp_path / "earlier")
    ]
    assert result.data["summary"]["incomplete"] == 1
    assert result.data["summary"]["interrupted"] is True
    assert [path.read_text() for path in candidates] == ["fixed", "fixed"]


def test_repair_reloads_context_inside_lock_and_skips_resolved_work(tmp_path, monkeypatch):
    recipe(tmp_path)
    seen = []
    repair_calls = []

    def detect(path):
        seen.append((path / "consumer").exists())
        return ("ok", "already repaired") if seen[-1] else ("problem", "broken")

    install_checks(monkeypatch, [check(detect=detect, autofix=lambda _: repair_calls.append(True))])

    @contextmanager
    def lock(_):
        (tmp_path / "consumer").write_text("fixed concurrently")
        yield

    monkeypatch.setattr(doctor, "checkout_operation_lock", lock)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert seen == [False, True]
    assert repair_calls == []


def test_repair_rebuilds_changed_recipe_resource_context(tmp_path, monkeypatch):
    recipe(tmp_path)
    contexts = []

    def construction(app, parsed):
        contexts.append(parsed.env_file)
        return [
            check(
                detect=lambda path: (
                    ("ok", "verified")
                    if (path / parsed.env_file).exists()
                    else ("problem", "broken")
                ),
                autofix=lambda path: (path / parsed.env_file).write_text("fixed"),
                files=lambda path: (path / parsed.env_file,),
            )
        ]

    monkeypatch.setattr(doctor, "_app_checks", construction)
    monkeypatch.setattr(doctor, "compose_wiring_checks", lambda _: [])

    @contextmanager
    def lock(_):
        p = tmp_path / sd.RECIPE_NAME
        p.write_text(p.read_text() + '\n[project]\nenv_file = "fresh.env"\n')
        yield

    monkeypatch.setattr(doctor, "checkout_operation_lock", lock)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert contexts == ["splashdown.env", "fresh.env"]
    assert sample(result)["changed_files"] == [str(tmp_path / "fresh.env")]


@pytest.mark.parametrize("names", [["RENAMED"], []])
def test_pending_framework_checks_refresh_after_waiting_repair(tmp_path, monkeypatch, names):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    def configure(names):
        configured(tmp_path, "vite", names, app_path="first")
        path = tmp_path / sd.RECIPE_NAME
        path.write_text(
            path.read_text()
            + f'\n[apps.second]\npath="second"\nprofile="vite"\nresources={json.dumps(names)}\n'
        )

    configure(["CUSTOM"])
    vite_config(first, "Number(env.CUSTOM ?? 4321)", None)
    vite_config(second, "Number(env.CUSTOM ?? 4321)", None)

    @contextmanager
    def lock(_):
        configure(names)
        vite_config(first, "Number(env.RENAMED ?? 4321)")
        vite_config(second, "Number(env.RENAMED ?? 4321)")
        yield

    monkeypatch.setattr(doctor, "checkout_operation_lock", lock)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    pending = next(
        row
        for row in rows(result)
        if row["app"] == "second" and row["check_id"] == "vite-port-wired"
    )
    assert pending["state"] == ("ok" if names else "skipped")
    assert result.data["summary"]["changed_files"] == []
    assert "CUSTOM" not in json.dumps(result.data)
    assert "env.RENAMED" in (second / "vite.config.ts").read_text()
    assert sample(result, "configuration")["state"] == "ok"


def test_activation_callbacks_never_run_or_create_locks(tmp_path, monkeypatch):
    recipe(tmp_path)
    calls = []
    install_checks(
        monkeypatch, [check("hook", activation=True, autofix=lambda _: calls.append(True))]
    )
    assert doctor.run_doctor(tmp_path, fix=True).exit_code == 1
    assert calls == []
    assert not (tmp_path / "state").exists()


def test_resource_free_app_does_not_repair_scaffold_port(tmp_path):
    recipe(tmp_path, profile="react-native")
    path = tmp_path / "metro.config.js"
    path.write_text("module.exports = { server: { port: 8081 } };\n")
    before = path.read_bytes()
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert sample(result, "managed-port-scope")["state"] == "skipped"
    assert path.read_bytes() == before
    assert not (tmp_path / "state").exists()


def test_fix_has_only_lock_state_footprint_and_preserves_existing_metadata(tmp_path, monkeypatch):
    recipe(tmp_path)
    state = tmp_path / "state" / "splashdown"
    state.mkdir(parents=True)
    state.chmod(0o751)
    for name in ("ports.tsv", "kv.tsv", "devices.tsv", "claims.tsv", "claim-notices.tsv"):
        (state / name).write_text(f"existing {name}\n")
        (state / name).chmod(0o640)
    before = {
        path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
        for path in state.iterdir()
    }
    install_checks(
        monkeypatch,
        [
            check(
                detect=lambda path: (
                    ("ok", "verified") if (path / "consumer").exists() else ("problem", "broken")
                )
            )
        ],
    )
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result)["repair"] == "repaired"
    assert len([row for row in rows(result) if row["check_id"].startswith("device-registry-")]) == 4
    assert {
        path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) for path in before
    } == before
    assert len(list(state.glob("checkout-operation-*.lock"))) == 1
    assert state.stat().st_mode & 0o777 == 0o751


def cli(root, *args):
    return subprocess.run(
        [sys.executable, "-m", "splashdown", "--cwd", str(root), *args],
        text=True,
        capture_output=True,
        timeout=15,
        env={
            **os.environ,
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"),
        },
    )


@pytest.mark.parametrize("fix", [False, True])
def test_real_cli_json_and_text_have_no_registry_or_notice_effects(tmp_path, fix):
    recipe(tmp_path, profile="nextjs")
    args = ["doctor", *(["--fix"] if fix else [])]
    response = cli(tmp_path, "--format", "json", *args)
    assert response.returncode == 0, response.stderr
    payload = json.loads(response.stdout)
    assert payload["command"] == "doctor"
    assert payload["data"]["outcomes"][0]["check_id"] == "configuration"
    assert response.stderr == ""
    assert not (tmp_path / "state").exists()
    state = tmp_path / "state" / "splashdown"
    state.mkdir(parents=True)
    notice = state / "claim-notices.tsv"
    notice.write_text("opaque notice bytes\n")
    before = (notice.read_bytes(), notice.stat().st_mode, notice.stat().st_mtime_ns)
    response = cli(tmp_path, *args)
    assert response.returncode == 0
    assert "No applicable checks" in response.stdout
    assert response.stderr == ""
    assert (notice.read_bytes(), notice.stat().st_mode, notice.stat().st_mtime_ns) == before
    assert list(state.iterdir()) == [notice]


@pytest.mark.parametrize("flag", ["--framework", "--check"])
def test_removed_flags_rejected_before_effects(tmp_path, flag):
    response = cli(tmp_path, "--format", "json", "doctor", flag, "vite")
    assert response.returncode == 2
    assert json.loads(response.stdout)["exit_code"] == 2
    assert not (tmp_path / "state").exists()


def test_real_cli_invalid_nearest_recipe_does_not_fall_back(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    recipe(tmp_path, profile="nextjs")
    child = tmp_path / "child"
    child.mkdir()
    (child / sd.RECIPE_NAME).write_text("[broken")
    nested = child / "nested"
    nested.mkdir()
    response = cli(nested, "--format", "json", "doctor", "--fix")
    assert response.returncode == 1
    outcome = json.loads(response.stdout)["data"]["outcomes"][0]
    assert outcome["path"] == str(child / sd.RECIPE_NAME)
    assert "Cannot load" in outcome["message"]
    assert not (nested / "state").exists()


@pytest.mark.parametrize("content", ["", '[project]\nframework = "auto"\n'])
def test_unconfigured_app_scope_never_scans_filesystem(tmp_path, content):
    (tmp_path / sd.RECIPE_NAME).write_text(content)
    (tmp_path / "vite.config.ts").write_text("export default {};\n")
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert [r["check_id"] for r in rows(result)] == [
        "configuration",
        "app-coverage",
        "loader",
        "hook",
    ]
    assert rows(result)[1]["state"] == "skipped"
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("phase", ["applies", "detect", "manual_instructions", "construction"])
def test_interrupt_during_observation_retains_previous_results(tmp_path, monkeypatch, phase):
    recipe(tmp_path)

    def interrupt(*_):
        raise KeyboardInterrupt

    install_checks(
        monkeypatch,
        [
            check("earlier", detect=lambda _: ("ok", "verified")),
            check(**({phase: interrupt} if phase != "construction" else {})),
        ],
    )
    if phase == "construction":
        monkeypatch.setattr(doctor, "_app_checks", interrupt)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 130
    assert rows(result)[0]["check_id"] == "configuration"
    assert result.data["summary"]["interrupted"] is True
    if phase != "construction":
        assert sample(result, "earlier")["state"] == "ok"
        assert sample(result)["state"] == "incomplete"
    assert not (tmp_path / "state").exists()


def test_post_repair_new_candidates_make_change_accounting_incomplete(tmp_path, monkeypatch):
    recipe(tmp_path)

    def files(path):
        return (
            (path / "consumer", path / "new")
            if (path / "consumer").exists()
            else (path / "consumer",)
        )

    install_checks(
        monkeypatch,
        [
            check(
                files=files,
                detect=lambda path: (
                    ("ok", "verified") if (path / "consumer").exists() else ("problem", "broken")
                ),
            )
        ],
    )
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result)["state"] == "incomplete"
    assert sample(result)["repair"] == "unverified"
    assert sample(result)["changed_files"] == [str(tmp_path / "consumer")]
    assert "no before snapshot" in sample(result)["message"]


def test_safe_snapshot_refusal_does_not_repair_or_create_lock(tmp_path, monkeypatch):
    recipe(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("keep")
    (tmp_path / "consumer").symlink_to(outside)
    install_checks(monkeypatch, [check()])
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert "symlink" in sample(result)["message"]
    assert outside.read_text() == "keep"
    assert not (tmp_path / "state").exists()


def test_relative_run_directory_uses_canonical_lock_identity(tmp_path, monkeypatch):
    recipe(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    install_checks(monkeypatch, [check()])
    keys = []

    @contextmanager
    def lock(key):
        keys.append(key)
        yield

    monkeypatch.setattr(doctor, "checkout_operation_lock", lock)
    doctor.run_doctor(type(tmp_path)(tmp_path.name), fix=True)
    assert keys == [str(tmp_path.resolve())]


def test_real_cli_missing_recipe_reports_stdout_and_operational_stderr(tmp_path):
    response = cli(tmp_path, "doctor", "--fix")
    assert response.returncode == 1
    assert str(tmp_path / sd.RECIPE_NAME) in response.stdout
    assert "splash init" in response.stdout
    assert "error: Doctor" in response.stderr
    assert not (tmp_path / "state").exists()


def configured(root, profile, names, *, writer=None, app_path="."):
    resources = "".join(
        f'[resources.{name}]\ntype="port"\nrange=[12000,12100]\n'
        + (f'writer="{writer}"\n' if writer else "")
        for name in names
    )
    (root / sd.RECIPE_NAME).write_text(
        resources
        + f'[apps.app]\npath="{app_path}"\nprofile="{profile}"\nresources={json.dumps(names)}\n'
    )


def vite_config(root, expression, prefix='""', *, directory="import.meta.dirname", extra=""):
    args = f"mode, {directory}" + (f", {prefix}" if prefix is not None else "")
    text = (
        'import { defineConfig, loadEnv } from "vite";\n'
        "export default defineConfig(({ mode }) => {\n"
        f"  const env = loadEnv({args});\n{extra}"
        f"  return {{ server: {{ port: {expression} }} }};\n}});\n"
    )
    (root / "vite.config.ts").write_text(text)
    return text


@pytest.mark.parametrize(
    ("name", "prefix", "state"),
    [
        ("CUSTOM", '""', "ok"),
        ("CUSTOM", '"CUS"', "ok"),
        ("CUSTOM", '["VITE_", "CUS"]', "ok"),
        ("CUSTOM", '[""]', "ok"),
        ("VITE_PORT", None, "ok"),
        ("CUSTOM", None, "problem"),
        ("CUSTOM", "[]", "problem"),
        ("CUSTOM", "prefixes", "incomplete"),
        ("CUSTOM", "[prefix]", "incomplete"),
    ],
)
def test_configured_vite_prefix_contract(tmp_path, name, prefix, state):
    configured(tmp_path, "vite", [name])
    text = vite_config(tmp_path, f"Number(env.{name}) || 5173", prefix)
    result = doctor.run_doctor(tmp_path)
    assert sample(result, "vite-port-wired")["state"] == state
    if state != "problem":
        doctor.run_doctor(tmp_path, fix=True)
        assert (tmp_path / "vite.config.ts").read_text() == text


@pytest.mark.parametrize(
    "writer", ["envfile=apps/web/.env", "envfile=config/custom", "envrc", "none", "stdout"]
)
def test_vite_inherited_loadenv_needs_no_dotenv_destination(tmp_path, writer):
    configured(tmp_path, "vite", ["CUSTOM"], writer=writer)
    before = vite_config(tmp_path, "Number(env.CUSTOM ?? 5173)", directory='"./independent-env"')
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert sample(result, "vite-port-wired")["state"] == "ok"
    assert (tmp_path / "vite.config.ts").read_text() == before
    assert result.data["summary"]["changed_files"] == []


def test_vite_fix_changes_only_managed_consumer_bytes(tmp_path):
    configured(tmp_path, "vite", ["CUSTOM"])
    text = vite_config(
        tmp_path,
        "Number(env.CUSTOM ?? 4321)",
        None,
        extra='  const API_URL = env.API_URL || "http://localhost:9080";\n  const unused = env.CUSTOM;\n',
    )
    path = tmp_path / "vite.config.ts"
    path.write_bytes(text.replace("\n", "\r\n").encode())
    before = path.read_bytes()
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert path.read_bytes() == before.replace(
        b"Number(env.CUSTOM ?? 4321)", b"Number((process.env.CUSTOM ?? env.CUSTOM) ?? 4321)"
    )
    assert sample(result, "vite-port-wired")["changed_files"] == [str(path)]


@pytest.mark.parametrize(
    "expression,extra",
    [
        ("5173", "  const unused = process.env.CUSTOM;\n"),
        ("otherPort", "  const unused = process.env.CUSTOM;\n"),
        ("Number(env.CUSTOM)", '  env.CUSTOM = "5173";\n'),
        ("port", "  const port = Number(env.CUSTOM);\n  port = 5173;\n"),
    ],
)
def test_vite_unrelated_or_dynamic_flow_is_not_healthy(tmp_path, expression, extra):
    configured(tmp_path, "vite", ["CUSTOM"])
    text = vite_config(tmp_path, expression, extra=extra)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "vite-port-wired")["state"] in {"problem", "incomplete"}
    assert result.exit_code == 1
    assert (tmp_path / "vite.config.ts").read_text() == text


@pytest.mark.parametrize(
    "body",
    [
        "export default { ...shared, server: {port: Number(process.env.CUSTOM)} };",
        "export default { server: {port: Number(process.env.CUSTOM), ...shared} };",
        "export default { vite: {server: {port: Number(process.env.CUSTOM)}} };",
        "const unused = {server: {port: Number(process.env.CUSTOM)}}; export default buildConfig();",
    ],
)
def test_vite_unrecognized_consumer_is_preserved(tmp_path, body):
    configured(tmp_path, "vite", ["CUSTOM"])
    (tmp_path / "vite.config.ts").write_text(body)
    assert doctor.run_doctor(tmp_path, fix=True).exit_code == 1
    assert (tmp_path / "vite.config.ts").read_text() == body


@pytest.mark.parametrize(
    "names,expression,state",
    [
        ([], "5173", "skipped"),
        (["ONE", "TWO"], "5173", "incomplete"),
        (["ONE", "TWO"], "Number(process.env.TWO) || 5173", "ok"),
        (["ONE", "TWO"], "Number(process.env.ONE || process.env.TWO)", "incomplete"),
    ],
)
def test_vite_port_scope_is_not_guessed(tmp_path, names, expression, state):
    configured(tmp_path, "vite", names)
    vite_config(tmp_path, expression)
    result = doctor.run_doctor(tmp_path, fix=True)
    check_id = "vite-port-wired" if names else "managed-port-scope"
    assert sample(result, check_id)["state"] == state


def test_vite_envprefix_does_not_change_loadenv_filter(tmp_path):
    configured(tmp_path, "vite", ["CUSTOM"])
    text = vite_config(tmp_path, "Number(env.CUSTOM)", None)
    (tmp_path / "vite.config.ts").write_text(
        text.replace("return { server", 'return { envPrefix: "CUS", server')
    )
    assert sample(doctor.run_doctor(tmp_path), "vite-port-wired")["state"] == "problem"


@pytest.mark.parametrize("writer", ["envrc", "none", "stdout"])
def test_rn_non_dotenv_writer_never_fabricates_xcode_source(tmp_path, writer):
    configured(tmp_path, "react-native", ["RCT_METRO_PORT"], writer=writer)
    (tmp_path / "ios").mkdir()
    path = tmp_path / "ios" / ".xcode.env"
    path.write_text("export NODE_BINARY=node\n")
    result = doctor.run_doctor(tmp_path, fix=True)
    row = sample(result, "rn-xcode-env")
    assert row["state"] == "incomplete"
    assert path.read_text() == "export NODE_BINARY=node\n"
    assert "no dotenv source established" in row["message"]


def test_rn_custom_metro_resource_preserves_numeric_fallback(tmp_path):
    configured(tmp_path, "react-native", ["MOBILE_PORT"])
    path = tmp_path / "metro.config.js"
    original = "module.exports = { server: { port: 8097 }, other: 'API_URL' };\n"
    path.write_text(original)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "rn-metro-config")["repair"] == "repaired"
    assert path.read_text() == original.replace(
        "port: 8097", "port: Number(process.env.MOBILE_PORT) || 8097"
    )


@pytest.mark.parametrize(
    "profile,filename,original,expected",
    [
        (
            "angular",
            "package.json",
            '{"scripts":{"start":"ng serve --port 4209"},"other": [ 1,2 ]}\n',
            '{"scripts":{"start":"ng serve --port ${CUSTOM:-4209}"},"other": [ 1,2 ]}\n',
        ),
        (
            "deno",
            "deno.json",
            '{"tasks":{"dev":"deno serve --port 8009 main.ts"},"other": [ 1,2 ]}\n',
            '{"tasks":{"dev":"deno serve --port ${CUSTOM:-8009} main.ts"},"other": [ 1,2 ]}\n',
        ),
        (
            "astro",
            "astro.config.mjs",
            'export default { title: "API_URL" };\n',
            'export default {\n  server: { port: Number(process.env.CUSTOM) || 4321 }, title: "API_URL" };\n',
        ),
    ],
)
def test_configured_framework_repairs_use_actual_name_and_preserve_bytes(
    tmp_path, profile, filename, original, expected
):
    configured(tmp_path, profile, ["CUSTOM"])
    if profile == "angular":
        (tmp_path / "angular.json").write_text("{}")
    (tmp_path / filename).write_text(original)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert (tmp_path / filename).read_text() == expected


@pytest.mark.parametrize(
    "profile,filename,body",
    [
        ("angular", "package.json", '{"scripts":{"start":"ng serve --port ${CUSTOM:-4209}"}}'),
        ("deno", "deno.json", '{"tasks":{"dev":"deno serve --port ${CUSTOM:-8009} main.ts"}}'),
        (
            "astro",
            "astro.config.mjs",
            "export default {server:{port:Number(process.env.CUSTOM || 4327)}}",
        ),
        (
            "react-native",
            "metro.config.js",
            "module.exports = {server:{port:Number(process.env.CUSTOM || 8097)}}",
        ),
    ],
)
def test_configured_framework_preserves_handwritten_fallbacks(tmp_path, profile, filename, body):
    configured(tmp_path, profile, ["CUSTOM"])
    if profile == "angular":
        (tmp_path / "angular.json").write_text("{}")
    (tmp_path / filename).write_text(body)
    result = doctor.run_doctor(tmp_path, fix=True)
    app_rows = [row for row in rows(result) if row["scope"] == "app" and row["state"] != "skipped"]
    assert all(row["state"] == "ok" for row in app_rows)
    assert (tmp_path / filename).read_text() == body


def test_springboot_checks_actual_declared_resource(tmp_path):
    configured(tmp_path, "springboot", ["CUSTOM"])
    path = tmp_path / "src/main/resources"
    path.mkdir(parents=True)
    (path / "application.properties").write_text("server.port=${CUSTOM:8080}\n")
    assert doctor.run_doctor(tmp_path).exit_code == 0


@pytest.mark.parametrize(
    "expression",
    [
        "Number(process.env.CUSTOM) | 1",
        "(Number(process.env.CUSTOM), 5173)",
        "5173 || Number(process.env.CUSTOM)",
        "parseInt(process.env.CUSTOM, 16)",
        "Number(process.env.OTHER || 5173 || process.env.CUSTOM)",
        "Number(process.env.OTHER || 5173) || Number(process.env.CUSTOM)",
    ],
)
@pytest.mark.parametrize(
    "profile,filename,prefix",
    [
        ("vite", "vite.config.ts", "export default"),
        ("astro", "astro.config.mjs", "export default"),
        ("react-native", "metro.config.js", "module.exports ="),
    ],
)
def test_port_transforms_and_discarded_reads_are_manual(
    tmp_path, expression, profile, filename, prefix
):
    configured(tmp_path, profile, ["CUSTOM"])
    path = tmp_path / filename
    before = f"{prefix} {{server:{{port:{expression}}}}};"
    path.write_text(before)
    result = doctor.run_doctor(tmp_path, fix=True)
    row = next(
        row
        for row in rows(result)
        if row["check_id"] in {"vite-port-wired", "astro-config-port", "rn-metro-config"}
    )
    assert row["state"] == "incomplete"
    assert path.read_text() == before


@pytest.mark.parametrize(
    "expression", ["parseInt(process.env.CUSTOM, 10)", "Number(process.env.CUSTOM ?? '5173')"]
)
def test_transparent_port_conversion_is_recognized(tmp_path, expression):
    configured(tmp_path, "vite", ["CUSTOM"])
    vite_config(tmp_path, expression)
    assert sample(doctor.run_doctor(tmp_path), "vite-port-wired")["state"] == "ok"


def test_literal_alias_cannot_precede_managed_fallback(tmp_path):
    configured(tmp_path, "vite", ["CUSTOM"])
    before = vite_config(
        tmp_path, "fixed || Number(process.env.CUSTOM)", extra="  const fixed = 5173;\n"
    )
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "vite-port-wired")["state"] == "incomplete"
    assert (tmp_path / "vite.config.ts").read_text() == before


@pytest.mark.parametrize(
    "binding,expression,state",
    [
        ("", "process.env.OTHER || process.env.CUSTOM", "incomplete"),
        ("", "5173 || process.env.CUSTOM", "incomplete"),
        ("", "process.env.OTHER || 5173 || process.env.CUSTOM", "incomplete"),
        (
            "const fallback = Number(process.env.OTHER) || 5173;",
            "fallback || Number(process.env.CUSTOM)",
            "incomplete",
        ),
        (
            "const fallback = Number(process.env.OTHER) || 5173; const alias = fallback;",
            "alias || Number(process.env.CUSTOM)",
            "incomplete",
        ),
        ("", "Number(process.env.CUSTOM || 5173) || 5174", "ok"),
        ("", "Number(process.env.CUSTOM ?? 5173) || 5174", "ok"),
        ("", "parseInt((process.env.CUSTOM ?? '5173'), 10) || 5174", "ok"),
        ("", "process.env.CUSTOM || process.env.OTHER", "ok"),
        (
            "const fallback = Number(process.env.CUSTOM) || 5173;",
            "fallback || 5174",
            "ok",
        ),
        (
            "const fallback = Number(process.env.CUSTOM) || 5173; const alias = fallback;",
            "Number(alias) || 5174",
            "ok",
        ),
        (
            "const fallback = Number(process.env.OTHER) || 5173;",
            "Number(process.env.CUSTOM) || fallback",
            "ok",
        ),
        ("const {CUSTOM} = process.env;", "Number(CUSTOM ?? 5173) || 5174", "ok"),
        ("", "process.env.CUSTOM || 5173 ?? 5174", "incomplete"),
        ("", "process.env.CUSTOM ?? 5173 || 5174", "incomplete"),
        ("", "(process.env.CUSTOM || 5173) ?? 5174", "ok"),
    ],
)
@pytest.mark.parametrize(
    "framework",
    [
        ("vite", "vite.config.ts", "export default", "vite-port-wired"),
        ("astro", "astro.config.mjs", "export default", "astro-config-port"),
        ("react-native", "metro.config.js", "module.exports =", "rn-metro-config"),
    ],
)
def test_port_fallback_preserves_leading_read_through_aliases(
    tmp_path, binding, expression, state, framework
):
    profile, filename, export, check_id = framework
    configured(tmp_path, profile, ["CUSTOM"])
    before = f"{binding}\n{export} {{server: {{port: {expression}}}}};\n".encode()
    path = tmp_path / filename
    path.write_bytes(before)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, check_id)["state"] == state
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "expression,binding,state",
    [
        ("env.CUSTOM || process.env.CUSTOM", "", "ok"),
        ("Number(env.CUSTOM ?? process.env.CUSTOM) || 5173", "", "ok"),
        ("Number(env.CUSTOM) ?? process.env.CUSTOM", "", "problem"),
        ("env.CUSTOM || 5173 || process.env.CUSTOM", "", "problem"),
        ("Number(env.CUSTOM ?? 5173) || Number(process.env.CUSTOM)", "", "problem"),
        ("Number(env.CUSTOM ?? '5173') || Number(process.env.CUSTOM)", "", "problem"),
        ("env.CUSTOM || '0' || process.env.CUSTOM", "", "problem"),
        ("Number(env.CUSTOM || '0') || Number(process.env.CUSTOM)", "", "ok"),
        (
            "fallback || Number(process.env.CUSTOM)",
            "  const fallback = Number(env.CUSTOM) || 5173;\n",
            "problem",
        ),
        (
            "fallback || 5174",
            "  const fallback = Number(env.CUSTOM ?? 5173);\n",
            "problem",
        ),
        ("env.CUSTOM || process.env.OTHER || process.env.CUSTOM", "", "incomplete"),
    ],
)
def test_vite_prefix_checks_only_reachable_fallback_sources(tmp_path, expression, binding, state):
    configured(tmp_path, "vite", ["CUSTOM"])
    before = vite_config(tmp_path, expression, None, extra=binding).encode()
    path = tmp_path / "vite.config.ts"
    assert sample(doctor.run_doctor(tmp_path), "vite-port-wired")["state"] == state
    result = doctor.run_doctor(tmp_path, fix=True)
    if state == "problem":
        assert sample(result, "vite-port-wired")["repair"] == "repaired"
        assert sample(result, "vite-port-wired")["state"] == "ok"
        assert path.read_bytes() == before.replace(
            b"env.CUSTOM", b"(process.env.CUSTOM ?? env.CUSTOM)", 1
        )
    else:
        assert sample(result, "vite-port-wired")["state"] == state
        assert path.read_bytes() == before


@pytest.mark.parametrize(
    "flags", ['--configuration="label --port $CUSTOM x" --port 4200', "--port $CUSTOM --port 9999"]
)
def test_angular_quoted_or_duplicate_flags_do_not_establish_consumption(tmp_path, flags):
    before = "ng serve " + flags
    assert profiles_web._angular_script_wired(before, "CUSTOM") is False
    updated = profiles_web._angular_wire_serve_script(before, "CUSTOM")
    if flags.startswith("--configuration"):
        assert updated == before.replace("--port 4200", "--port ${CUSTOM:-4200}")
        assert profiles_web._angular_script_wired(updated, "CUSTOM") is True
    else:
        assert updated == before


@pytest.mark.parametrize(
    "profile,filename,member,command,expected",
    [
        ("angular", "package.json", "scripts", "ng serve", "ng serve --port $CUSTOM"),
        ("deno", "deno.json", "tasks", "deno serve main.ts", "deno serve --port $CUSTOM main.ts"),
    ],
)
def test_framework_json_repair_targets_root_member(
    tmp_path, profile, filename, member, command, expected
):
    configured(tmp_path, profile, ["CUSTOM"])
    if profile == "angular":
        (tmp_path / "angular.json").write_text("{}")
    obj = json.dumps({member: {"start": command}}, separators=(",", ":"))
    before = '{"example":' + obj + "," + obj[1:]
    path = tmp_path / filename
    path.write_text(before)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert path.read_text() == before[: len('{"example":') + len(obj)] + before[
        len('{"example":') + len(obj) :
    ].replace(command, expected)


def test_dotnet_repair_targets_profile_beneath_root_profiles(tmp_path):
    path = tmp_path / "Properties/launchSettings.json"
    path.parent.mkdir()
    spec = '{"commandName":"Project","applicationUrl":"http://localhost:5000"}'
    before = '{"example":{"app":' + spec + '},"profiles":{"app":' + spec + "}}"
    path.write_bytes(b"\xef\xbb\xbf" + before.encode() + b"\r\n")
    profiles_server._aspnet_launch_settings_autofix(tmp_path)
    expected = before.replace(
        '"profiles":{"app":' + spec, '"profiles":{"app":{"commandName":"Project"}'
    )
    assert path.read_bytes() == b"\xef\xbb\xbf" + expected.encode() + b"\r\n"


@pytest.mark.parametrize("value", ["9${CUSTOM}", "${CUSTOM}0", "${CUSTOM:8080}0"])
def test_spring_placeholder_must_be_the_complete_value(tmp_path, value):
    path = tmp_path / "src/main/resources/application.properties"
    path.parent.mkdir(parents=True)
    path.write_text("server.port=" + value)
    assert profiles_server._springboot_app_props_detect(tmp_path, "CUSTOM")[0] == "problem"


@pytest.mark.parametrize(
    "value", ["${CUSTOM}", "${CUSTOM:8080}", '"${CUSTOM:8080}"', "'${CUSTOM}'"]
)
def test_spring_supported_complete_placeholder(tmp_path, value):
    path = tmp_path / "src/main/resources/application.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("server.port: " + value)
    assert profiles_server._springboot_app_props_detect(tmp_path, "CUSTOM")[0] == "ok"


@pytest.mark.parametrize("destination", ["config/mobile.values", "config/mobile values"])
def test_xcode_generated_reader_accepts_destination_without_env_suffix(tmp_path, destination):
    path = tmp_path / "ios/.xcode.env"
    path.parent.mkdir()
    path.write_text("export NODE_BINARY=node\n")
    sd._rn_xcode_autofix(tmp_path, destination)
    assert sd._rn_xcode_detect(tmp_path, destination)[0] == "ok"
    before = path.read_bytes()
    sd._rn_xcode_autofix(tmp_path, destination)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "flags,state",
    [
        ("", "ok"),
        ("--port $RCT_METRO_PORT", "ok"),
        ("--port $OTHER", "problem"),
        ("--port $RCT_METRO_PORT --port 9999", "problem"),
        ('--configuration="label --port $RCT_METRO_PORT x" --port $OTHER', "problem"),
    ],
)
def test_canonical_rn_port_override_consumes_managed_value(tmp_path, flags, state):
    before = json.dumps({"scripts": {"start": "react-native start " + flags}})
    path = tmp_path / "package.json"
    path.write_text(before)
    assert sd._rn_pkg_detect(tmp_path)[0] == state
    if state == "problem":
        sd._rn_pkg_autofix(tmp_path)
        assert path.read_text() == before


def test_deno_unrelated_env_read_is_incomplete(tmp_path):
    configured(tmp_path, "deno", ["CUSTOM"])
    (tmp_path / "deno.json").write_text("{}")
    (tmp_path / "main.ts").write_text(
        'console.log(Deno.env.get("CUSTOM"));\nDeno.serve(handler);\n'
    )
    assert sample(doctor.run_doctor(tmp_path), "deno-port-wired")["state"] == "incomplete"


@pytest.mark.parametrize(
    "expression",
    [
        'Number(Deno.env.get("CUSTOM")) || 8000',
        'Number(Deno.env.get("CUSTOM") ?? 8000)',
        'parseInt(Deno.env.get("CUSTOM") ?? "8000", 10)',
        'Number(Deno.env.get("CUSTOM") ?? Deno.env.get("CUSTOM") ?? 8000)',
    ],
)
def test_deno_server_port_read_is_recognized(tmp_path, expression):
    configured(tmp_path, "deno", ["CUSTOM"])
    (tmp_path / "deno.json").write_text("{}")
    (tmp_path / "main.ts").write_text(f"Deno.serve({{port: {expression}}}, handler);\n")
    assert sample(doctor.run_doctor(tmp_path), "deno-port-wired")["state"] == "ok"


@pytest.mark.parametrize(
    "extra,expression",
    [
        ("  function loadEnv() { return {}; }\n", "Number(env.CUSTOM)"),
        ("  env.CUSTOM = 1234;\n", "Number(env.CUSTOM)"),
        ("  env = {};\n", "Number(env.CUSTOM)"),
        ("", "Number(env.CUSTOM) + 1"),
        ("", "-Number(env.CUSTOM)"),
    ],
)
def test_vite_dynamic_bindings_and_transforms_require_manual_check(tmp_path, extra, expression):
    configured(tmp_path, "vite", ["CUSTOM"])
    before = vite_config(tmp_path, expression, extra=extra)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "vite-port-wired")["state"] == "incomplete"
    assert (tmp_path / "vite.config.ts").read_text() == before


def test_vite_nested_helper_return_is_not_the_exported_config(tmp_path):
    configured(tmp_path, "vite", ["CUSTOM"])
    text = """export default defineConfig(() => {
  function unused() { return {server: {port: Number(process.env.CUSTOM)}}; }
  return otherConfig;
});"""
    (tmp_path / "vite.config.ts").write_text(text)
    assert sample(doctor.run_doctor(tmp_path, fix=True), "vite-port-wired")["state"] == "incomplete"
    assert (tmp_path / "vite.config.ts").read_text() == text


def test_metro_arithmetic_and_string_mentions_are_incomplete(tmp_path):
    configured(tmp_path, "react-native", ["CUSTOM"])
    path = tmp_path / "metro.config.js"
    for expression in ("Number(process.env.CUSTOM) + 1", '"process.env.CUSTOM"'):
        before = f"module.exports = {{server: {{port: {expression}}}}};"
        path.write_text(before)
        row = sample(doctor.run_doctor(tmp_path, fix=True), "rn-metro-config")
        assert row["state"] == "incomplete"
        assert path.read_text() == before


def test_rn_package_ignores_echo_and_preserves_chained_sidecar(tmp_path):
    from splashdown.wiring import _rn_pkg_autofix

    path = tmp_path / "package.json"
    text = '{"scripts":{"echo":"echo react-native start --port 4567","start":"react-native start --port 8090 && psql --port 5432"},"other": [1, 2]}'
    path.write_text(text)
    _rn_pkg_autofix(tmp_path)
    assert path.read_text() == text.replace(
        "react-native start --port 8090", "react-native start --port ${RCT_METRO_PORT:-8090}"
    )


def test_rn_xcode_handwritten_fallback_is_byte_identical(tmp_path):
    configured(tmp_path, "react-native", ["RCT_METRO_PORT"])
    (tmp_path / "ios").mkdir()
    path = tmp_path / "ios/.xcode.env"
    before = 'export API_URL="$(cat ../.env | grep API_URL)"\nexport RCT_METRO_PORT="${RCT_METRO_PORT:-$(custom_port)}"\n\n'
    path.write_text(before)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "rn-xcode-env")["state"] in {"problem", "incomplete"}
    assert path.read_text() == before


@pytest.mark.parametrize("destination", ["config/mobile.env", "config/mobile values"])
def test_rn_xcode_uses_resource_override_relative_to_app(tmp_path, destination):
    app = tmp_path / "apps/mobile"
    (app / "ios").mkdir(parents=True)
    configured(
        tmp_path,
        "react-native",
        ["RCT_METRO_PORT"],
        writer="envfile=" + destination,
        app_path="apps/mobile",
    )
    path = app / "ios/.xcode.env"
    path.write_text("export NODE_BINARY=node\nexport RCT_METRO_PORT=8099\n")
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "rn-xcode-env")["repair"] == "repaired"
    text = path.read_text()
    assert "${SRCROOT}/../../../" + destination in text
    assert "${RCT_METRO_PORT:-8099}" in text


@pytest.mark.parametrize(
    "profile,filename",
    [
        ("vite", "vite.config.ts"),
        ("react-native", "metro.config.js"),
        ("angular", "package.json"),
        ("deno", "deno.json"),
        ("astro", "astro.config.mjs"),
        ("springboot", "application.properties"),
        ("aspnetcore", "Properties/launchSettings.json"),
    ],
)
def test_configured_portless_profiles_never_impose_scaffold_keys(tmp_path, profile, filename):
    configured(tmp_path, profile, [])
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("handwritten")
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "managed-port-scope")["state"] == "skipped"
    assert path.read_text() == "handwritten"


def test_vite_repair_refuses_symlink_without_touching_target(tmp_path):
    configured(tmp_path, "vite", ["CUSTOM"])
    path = tmp_path / "original"
    text = vite_config(tmp_path, "Number(env.CUSTOM)", None)
    (tmp_path / "vite.config.ts").rename(path)
    (tmp_path / "vite.config.ts").symlink_to(path)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "vite-port-wired")["state"] == "incomplete"
    assert path.read_text() == text


@pytest.mark.parametrize(
    "profile,filename,field,command",
    [
        ("angular", "package.json", "scripts", "ng serve"),
        ("deno", "deno.json", "tasks", "deno serve"),
    ],
)
def test_shell_echo_is_preserved_and_never_counts_as_port_consumer(
    tmp_path, profile, filename, field, command
):
    configured(tmp_path, profile, ["CUSTOM"])
    if profile == "angular":
        (tmp_path / "angular.json").write_text("{}")
    text = json.dumps({field: {"dev": f'echo "{command} --port 1234"'}})
    (tmp_path / filename).write_text(text)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert (tmp_path / filename).read_text() == text


def loader_recipe(root, name, destination="config/generated.env"):
    (root / sd.RECIPE_NAME).write_text(
        f'[project]\nloader="{name}"\nenv_file={json.dumps(destination)}\n'
    )


def present_loader(monkeypatch, name):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: f"/controlled/{name}")
    monkeypatch.setattr(doctor, "direnv_approval", lambda *_: ("ok", "Observed allowed."))


@pytest.mark.parametrize(
    "name,filename,content",
    [
        ("mise", "mise.toml", '[env]\n_.file = "config/generated.env"\n'),
        ("direnv", ".envrc", 'dotenv_if_exists "config/generated.env"\n'),
        (
            "devbox",
            "devbox.json",
            '{"shell":{"init_hook":"set -a; source config/generated.env; set +a"}}\n',
        ),
    ],
)
def test_loader_reuses_valid_user_directives(tmp_path, monkeypatch, name, filename, content):
    loader_recipe(tmp_path, name)
    path = tmp_path / filename
    path.write_text(content)
    present_loader(monkeypatch, name)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert sample(result, "loader")["state"] == "ok"
    assert "config/generated.env" in sample(result, "loader")["message"]
    assert path.read_text() == content
    assert result.data["summary"]["changed_files"] == []
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "name,filename", [("mise", "mise.toml"), ("direnv", ".envrc"), ("devbox", "devbox.json")]
)
def test_loader_fix_only_writes_selected_configuration(tmp_path, monkeypatch, name, filename):
    loader_recipe(tmp_path, name)
    present_loader(monkeypatch, name)
    monkeypatch.setattr(
        sd.loaders.Loader, "approve", lambda *_a, **_k: pytest.fail("approval called")
    )
    monkeypatch.setattr(
        sd.loaders.MiseLoader, "approve", lambda *_a, **_k: pytest.fail("trust called")
    )
    monkeypatch.setattr(
        sd.loaders.DirenvLoader, "approve", lambda *_a, **_k: pytest.fail("allow called")
    )
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert sample(result, "loader")["repair"] == "repaired"
    assert sample(result, "loader")["changed_files"] == [str(tmp_path / filename)]
    assert "config/generated.env" in (tmp_path / filename).read_text()
    assert not (tmp_path / "config" / "generated.env").exists()


@pytest.mark.parametrize("name", ["mise", "direnv", "devbox"])
def test_selected_loader_missing_tool_is_required_problem(tmp_path, monkeypatch, name):
    loader_recipe(tmp_path, name)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "loader")["repair"] == "repaired"
    assert sample(result, "loader-tool")["state"] == "problem"
    assert sample(result, "loader-approval")["state"] == "skipped"


@pytest.mark.parametrize(
    "name,filename,content",
    [
        ("mise", "mise.toml", "[bad"),
        ("mise", "mise.toml", '[env]\n_="conflict"\n'),
        ("devbox", "devbox.json", "[bad"),
        ("devbox", "devbox.json", '{"shell": false}'),
    ],
)
def test_loader_malformed_or_conflicting_config_has_no_repair(
    tmp_path, monkeypatch, name, filename, content
):
    loader_recipe(tmp_path, name)
    present_loader(monkeypatch, name)
    (tmp_path / filename).write_text(content)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "loader")["state"] == "problem"
    assert sample(result, "loader")["repair"] == "none"
    assert (tmp_path / filename).read_text() == content
    assert not (tmp_path / "state").exists()


def test_none_loader_ignores_unselected_malformed_configs(tmp_path, monkeypatch):
    loader_recipe(tmp_path, "none")
    (tmp_path / "mise.toml").write_text("[bad")
    monkeypatch.setattr(doctor.shutil, "which", lambda _: pytest.fail("unselected tool probe"))
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert sample(result, "loader")["state"] == "skipped"


def test_loader_repair_refreshes_recipe_inside_lock_and_probes_after_release(tmp_path, monkeypatch):
    loader_recipe(tmp_path, "direnv", "old.env")
    locked = False

    @contextmanager
    def lock(_):
        nonlocal locked
        loader_recipe(tmp_path, "direnv", "new.env")
        locked = True
        yield
        locked = False

    def probe(path, executable):
        assert not locked
        assert "new.env" in (path / ".envrc").read_text()
        return "problem", "Approval invalidated by configuration edit."

    present_loader(monkeypatch, "direnv")
    monkeypatch.setattr(doctor, "direnv_approval", probe)
    monkeypatch.setattr(doctor, "checkout_operation_lock", lock)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "loader")["repair"] == "repaired"
    assert sample(result, "loader-approval")["state"] == "problem"
    assert "new.env" in sample(result, "loader")["message"]


@pytest.mark.parametrize(
    "allowed,state",
    [(0, "ok"), (1, "problem"), (2, "problem"), (True, "incomplete"), (3, "incomplete")],
)
def test_direnv_approval_uses_exact_current_found_rc(tmp_path, monkeypatch, allowed, state):
    loader_recipe(tmp_path, "direnv")
    (tmp_path / ".envrc").write_text("dotenv config/generated.env\n")
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/controlled/direnv")
    original = sd.loaders.subprocess.run

    def run(argv, **kwargs):
        if argv[0] != "/controlled/direnv":
            return original(argv, **kwargs)
        assert argv == ["/controlled/direnv", "status", "--json"]
        assert kwargs["timeout"] == 5
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "state": {
                        "foundRC": {"path": str(tmp_path / ".envrc"), "allowed": allowed},
                        "loadedRC": {"path": "/unrelated/.envrc", "allowed": 0},
                    }
                }
            ),
            "",
        )

    monkeypatch.setattr(sd.loaders.subprocess, "run", run)
    result = doctor.run_doctor(tmp_path)
    assert sample(result, "loader-approval")["state"] == state
    assert result.exit_code == (0 if state == "ok" else 1)


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "json", "path", "shape"])
def test_direnv_failed_applicable_probe_is_incomplete(tmp_path, monkeypatch, failure):
    loader_recipe(tmp_path, "direnv")
    (tmp_path / ".envrc").write_text("dotenv config/generated.env\n")
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/controlled/direnv")
    original = sd.loaders.subprocess.run

    def run(argv, **kwargs):
        if argv[0] != "/controlled/direnv":
            return original(argv, **kwargs)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 5)
        output = {
            "json": "{",
            "path": '{"state":{"foundRC":{"path":"/elsewhere/.envrc","allowed":0}}}',
            "shape": "{}",
        }.get(failure, "{}")
        return subprocess.CompletedProcess(argv, 7 if failure == "nonzero" else 0, output, "")

    monkeypatch.setattr(sd.loaders.subprocess, "run", run)
    result = doctor.run_doctor(tmp_path)
    assert result.exit_code == 1
    assert sample(result, "loader-approval")["state"] == "incomplete"


def init_git(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=10)


@pytest.mark.parametrize("profile", ["nextjs", "flutter", "react-native"])
def test_hook_config_fix_applies_once_independent_of_framework(tmp_path, monkeypatch, profile):
    init_git(tmp_path)
    recipe(tmp_path, ("one", "two"), profile)
    (tmp_path / "lefthook.yml").write_text(
        "pre-commit:\n  commands:\n    test:\n      run: echo keep\n"
    )
    monkeypatch.setattr(sd.hooks, "_ensure_post_checkout_hook", lambda _: pytest.fail("activation"))
    monkeypatch.setattr(sd.hooks, "_run_lefthook_install", lambda _: pytest.fail("install"))
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert len([row for row in rows(result) if row["check_id"] == "hook"]) == 1
    assert sample(result, "hook")["repair"] == "repaired"
    assert sample(result, "hook-activation")["state"] == "problem"
    assert "echo keep" in (tmp_path / "lefthook.yml").read_text()
    assert sd.hooks._LEFTHOOK_RUN in (tmp_path / "lefthook.yml").read_text()
    assert not (tmp_path / ".git" / "hooks" / "post-checkout").exists()


def test_native_hook_is_manual_activation_and_never_locks(tmp_path):
    init_git(tmp_path)
    recipe(tmp_path, profile="nextjs")
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "hook")["state"] == "skipped"
    assert sample(result, "hook-activation")["state"] == "problem"
    assert not (tmp_path / ".git" / "hooks" / "post-checkout").exists()
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "content", ["post-checkout: [unterminated\n", "post-checkout:\n  commands: [unterminated\n"]
)
def test_malformed_hook_is_incomplete_and_not_repaired(tmp_path, content):
    init_git(tmp_path)
    recipe(tmp_path)
    (tmp_path / "lefthook.yml").write_text(content)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "hook")["state"] == "incomplete"
    assert result.exit_code == 1
    assert (tmp_path / "lefthook.yml").read_text() == content
    assert not (tmp_path / "state").exists()


def test_conflicting_hook_managers_are_manual_only(tmp_path):
    init_git(tmp_path)
    recipe(tmp_path)
    (tmp_path / "lefthook.yml").write_text("{}\n")
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "hook")["state"] == "problem"
    assert "several hook managers" in sample(result, "hook")["message"]
    assert sample(result, "hook")["repair"] == "none"
    assert not (tmp_path / "state").exists()


def test_hook_fix_uses_bounded_discovery_and_first_config(tmp_path, monkeypatch):
    init_git(tmp_path)
    recipe(tmp_path, profile="nextjs")
    (tmp_path / "lefthook.yml").write_text("pre-commit:\n  commands: {}\n")
    (tmp_path / "lefthook.yaml").write_text("[bad ignored")
    original = sd.hooks.subprocess.run

    def run(argv, **kwargs):
        assert argv[0] == "git"
        assert kwargs["timeout"] == 5
        assert os.path.isabs(kwargs["executable"])
        return original(argv, **kwargs)

    monkeypatch.setattr(sd.hooks.subprocess, "run", run)
    monkeypatch.setattr(
        sd.hooks.subprocess, "check_output", lambda *_a, **_k: pytest.fail("unbounded fallback")
    )
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "hook")["repair"] == "repaired"
    assert (tmp_path / "lefthook.yaml").read_text() == "[bad ignored"


@pytest.mark.parametrize("fix", [False, True])
def test_real_cli_loader_hook_state_footprint(tmp_path, fix):
    init_git(tmp_path)
    loader_recipe(tmp_path, "mise")
    (tmp_path / "lefthook.yml").write_text("pre-commit:\n  commands: {}\n")
    state = tmp_path / "state" / "splashdown"
    state.mkdir(parents=True)
    files = [state / "kv.tsv", state / "ports.tsv", state / "claim-notices.json"]
    for path in files:
        path.write_bytes(b"preserve bytes\n")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    response = cli(tmp_path, "doctor", "--format", "json", *(["--fix"] if fix else []))
    assert response.returncode == 1
    payload = json.loads(response.stdout)
    outcomes = payload["data"]["outcomes"]
    assert (
        next(row for row in outcomes if row["check_id"] == "hook-activation")["state"] == "problem"
    )
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files} == before
    assert not (tmp_path / ".git" / "hooks" / "post-checkout").exists()
    expected = {path.name for path in files}
    actual = {path.name for path in state.iterdir()}
    assert actual == expected if not fix else len(actual - expected) == 1
    if fix:
        assert all(name.startswith("checkout-operation-") for name in actual - expected)
        assert next(row for row in outcomes if row["check_id"] == "loader")["repair"] == "repaired"
        assert next(row for row in outcomes if row["check_id"] == "hook")["repair"] == "repaired"


@pytest.mark.parametrize(
    "content",
    [
        'dotenv_if_exists "unterminated\n',
        "# >>> splashdown-managed dotenv >>>\ndotenv_if_exists old.env\n",
    ],
)
def test_direnv_malformed_directive_or_markers_are_not_repaired(tmp_path, monkeypatch, content):
    loader_recipe(tmp_path, "direnv")
    present_loader(monkeypatch, "direnv")
    (tmp_path / ".envrc").write_text(content)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "loader")["state"] == "problem"
    assert sample(result, "loader")["repair"] == "none"
    assert (tmp_path / ".envrc").read_text() == content
    assert not (tmp_path / "state").exists()


def test_loader_observation_refreshes_changed_selection_after_repair(tmp_path, monkeypatch):
    loader_recipe(tmp_path, "mise")
    observed = []

    @contextmanager
    def lock(_):
        loader_recipe(tmp_path, "direnv")
        yield

    def which(name):
        observed.append(name)
        return f"/controlled/{name}"

    monkeypatch.setattr(doctor, "checkout_operation_lock", lock)
    monkeypatch.setattr(doctor.shutil, "which", which)
    monkeypatch.setattr(
        doctor, "direnv_approval", lambda *_: ("problem", "New .envrc not allowed.")
    )
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert observed == ["direnv", "direnv"]
    assert "New .envrc" in sample(result, "loader-approval")["message"]
    assert "config/generated.env" in (tmp_path / ".envrc").read_text()


def test_direnv_checkout_executable_is_never_run(tmp_path, monkeypatch):
    loader_recipe(tmp_path, "direnv")
    (tmp_path / ".envrc").write_text("dotenv config/generated.env\n")
    monkeypatch.setattr(doctor.shutil, "which", lambda _: str(tmp_path / "bin" / "direnv"))
    monkeypatch.setattr(
        doctor, "direnv_approval", lambda *_: pytest.fail("project command executed")
    )
    result = doctor.run_doctor(tmp_path)
    assert result.exit_code == 1
    assert sample(result, "loader-approval")["state"] == "incomplete"


@pytest.mark.parametrize("fix", [False, True])
@pytest.mark.parametrize("git_entry", ["directory", "file"])
@pytest.mark.parametrize("location", ["outer", "outer-link", "external-link", "relative"])
def test_nested_recipe_refuses_checkout_direnv_before_authorization_probe(
    tmp_path, monkeypatch, fix, git_entry, location
):
    checkout = tmp_path / "checkout"
    project = checkout / "apps/mobile"
    project.mkdir(parents=True)
    marker = checkout / ".git"
    if git_entry == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: /unused/worktrees/mobile\n")
    loader_recipe(project, "direnv")
    (project / ".envrc").write_text("dotenv config/generated.env\n")
    tools = checkout / "tools"
    tools.mkdir()
    executable = tools / "direnv"
    if location in {"outer-link", "relative"}:
        executable.symlink_to("/usr/bin/true")
    else:
        executable.write_text("#!/bin/sh\ntouch daemon-started\n")
        executable.chmod(0o755)
    if location == "external-link":
        external = tmp_path / "direnv"
        external.symlink_to(executable)
        executable = external
    candidate = "../../tools/direnv" if location == "relative" else str(executable)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: candidate)
    monkeypatch.setattr(doctor, "_hook_checks", lambda _: [])
    monkeypatch.setattr(
        subprocess, "run", lambda *_args, **_kwargs: pytest.fail("executed project code")
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = doctor.run_doctor(project, fix=fix)
    outcome = sample(result, "loader-approval")
    assert result.exit_code == 1
    assert outcome["state"] == "incomplete"
    assert "checkout-controlled direnv" in outcome["message"]
    assert outcome["changed_files"] == []
    assert outcome["repair"] == "none"
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("stage", ["root", "common", "config"])
def test_failed_git_probe_never_becomes_non_git_skip(tmp_path, monkeypatch, stage):
    recipe(tmp_path)

    def run(argv, **kwargs):
        if "--show-toplevel" in argv:
            return subprocess.CompletedProcess(
                argv, 9 if stage == "root" else 0, str(tmp_path), "permission denied"
            )
        if "--git-common-dir" in argv:
            return subprocess.CompletedProcess(
                argv, 9 if stage == "common" else 0, str(tmp_path / ".git"), "failure"
            )
        return subprocess.CompletedProcess(argv, 9, "", "failure")

    monkeypatch.setattr(sd.hooks.subprocess, "run", run)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "hook")["state"] == "incomplete"
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("old", [sd.hooks._LEFTHOOK_LEGACY_RUN, sd.hooks._LEFTHOOK_GUARDED_RUN])
def test_doctor_migrates_exact_legacy_lefthook_command(tmp_path, old):
    init_git(tmp_path)
    recipe(tmp_path)
    (tmp_path / "lefthook.yml").write_text(
        f"post-checkout:\n  commands:\n    splashdown:\n      run: {old}\n"
    )
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "hook")["repair"] == "repaired"
    assert (
        (tmp_path / "lefthook.yml").read_text()
        == f"post-checkout:\n  commands:\n    splashdown:\n      run: {sd.hooks._LEFTHOOK_RUN}\n"
    )


def test_doctor_does_not_migrate_custom_lefthook_command(tmp_path, monkeypatch):
    init_git(tmp_path)
    recipe(tmp_path)
    content = "post-checkout:\n  commands:\n    splashdown:\n      run: echo custom\n"
    (tmp_path / "lefthook.yml").write_text(content)
    original = sd.hooks.subprocess.run

    def run(argv, **kwargs):
        if argv[0] == "git":
            assert kwargs.get("timeout") == 5
        return original(argv, **kwargs)

    monkeypatch.setattr(sd.hooks.subprocess, "run", run)
    monkeypatch.setattr(
        sd.hooks.subprocess, "check_output", lambda *_a, **_k: pytest.fail("unbounded fallback")
    )
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "hook")["repair"] == "failed"
    assert (tmp_path / "lefthook.yml").read_text() == content


@pytest.mark.parametrize(
    "content",
    [
        'export FOO="unterminated\n',
        "cat <<EOF\ndotenv_if_exists config/generated.env\n",
        'export FOO="first\ndotenv_if_exists config/generated.env\nlast"\n',
        "if true; then\ndotenv_if_exists config/generated.env\nfi\n",
        "load_env() {\ndotenv_if_exists config/generated.env\n}\n",
        "export FOO=$(\ndotenv_if_exists config/generated.env\n)\n",
    ],
)
def test_doctor_preserves_unrecognized_direnv_context(tmp_path, monkeypatch, content):
    loader_recipe(tmp_path, "direnv")
    present_loader(monkeypatch, "direnv")
    (tmp_path / ".envrc").write_text(content)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "loader")["state"] == "problem"
    assert sample(result, "loader")["repair"] == "none"
    assert (tmp_path / ".envrc").read_text() == content
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("name", ["direnv", "devbox"])
@pytest.mark.parametrize(
    "destination", ["my config/generated.env", "my config/a'b;$(echo nope)\"\\file.env"]
)
def test_doctor_shell_loader_quotes_exact_destination(tmp_path, monkeypatch, name, destination):
    loader_recipe(tmp_path, name, destination)
    present_loader(monkeypatch, name)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 0
    assert sample(result, "loader")["repair"] == "repaired"
    if name == "direnv":
        command = (tmp_path / ".envrc").read_text()
        words = shlex.split(command, comments=True)
        assert words == ["dotenv_if_exists", destination]
    else:
        command = json.loads((tmp_path / "devbox.json").read_text())["shell"]["init_hook"][0]
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";")
        lexer.whitespace_split = True
        assert list(lexer) == ["set", "-a", ";", "source", destination, ";", "set", "+a"]
    assert sample(doctor.run_doctor(tmp_path), "loader")["state"] == "ok"


@pytest.mark.parametrize(
    "content",
    [
        "{}\n",
        "post-checkout: {}\n",
        "post-checkout:\n  commands: {}\n",
        "post-checkout:\n  commands:\n    splashdown: {}\n",
        "post-checkout: {}\npost-checkout:\n  commands: {}\n",
    ],
)
def test_doctor_preserves_lefthook_flow_edit_regions(tmp_path, content):
    init_git(tmp_path)
    recipe(tmp_path)
    (tmp_path / "lefthook.yml").write_text(content)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "hook")["state"] == "problem"
    assert sample(result, "hook")["repair"] == "none"
    assert (tmp_path / "lefthook.yml").read_text() == content
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("key", ["post-checkout", "commands", "splashdown"])
@pytest.mark.parametrize(
    "spelling",
    [
        "{key}: # preserve",
        "{key} :",
        "{key} : # preserve",
        "{key}\t:",
        "{key}\t: # preserve",
        '"{key}":',
        "'{key}':",
        "{key}: &keep",
        "{key}: !!map",
        "- {key}:",
        "? {key}",
    ],
)
def test_doctor_preserves_lefthook_unlocatable_edit_keys(tmp_path, key, spelling):
    content = (
        "post-checkout:\n  commands:\n    splashdown:\n"
        f"      run: {sd.hooks._LEFTHOOK_LEGACY_RUN}\n"
    )
    content = content.replace(f"{key}:", spelling.format(key=key))
    assert_lefthook_repair_refused(tmp_path, content)


@pytest.mark.parametrize(
    "content",
    [
        "post-checkout:\n    commands:\n      check:\n        run: echo keep\n",
        "post-checkout:\n  commands:\n      check:\n        run: echo keep\n",
        "post-checkout:\n  commands:\n      splashdown:\n        run: echo keep\n",
        "post-checkout:\n    parallel: true\n",
        "post-checkout:\n  options:\n    commands:\n      check:\n        run: echo keep\n",
    ],
)
def test_doctor_preserves_lefthook_unsupported_edit_indentation(tmp_path, content):
    assert_lefthook_repair_refused(tmp_path, content)


def assert_lefthook_repair_refused(tmp_path, content):
    init_git(tmp_path)
    recipe(tmp_path)
    path = tmp_path / "lefthook.yml"
    original = content.encode()
    path.write_bytes(original)

    result = doctor.run_doctor(tmp_path, fix=True)

    assert result.exit_code == 1
    assert sample(result, "hook")["state"] == "problem"
    assert sample(result, "hook")["repair"] == "none"
    assert path.read_bytes() == original
    assert not (tmp_path / "state").exists()
    assert sd.hooks._wire_post_checkout_lefthook(tmp_path) is False
    assert path.read_bytes() == original


def test_loader_repair_does_not_run_unrelated_git_probes_under_lock(tmp_path, monkeypatch):
    init_git(tmp_path)
    loader_recipe(tmp_path, "mise")
    present_loader(monkeypatch, "mise")
    locked = False

    @contextmanager
    def lock(_):
        nonlocal locked
        locked = True
        yield
        locked = False

    original = sd.hooks.subprocess.run

    def run(argv, **kwargs):
        if argv[0] == "git":
            assert not locked
        return original(argv, **kwargs)

    monkeypatch.setattr(doctor, "checkout_operation_lock", lock)
    monkeypatch.setattr(sd.hooks.subprocess, "run", run)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert sample(result, "loader")["repair"] == "repaired"
    assert sample(result, "hook-activation")["state"] == "problem"


@pytest.mark.parametrize("manager", ["lefthook", "husky"])
@pytest.mark.parametrize("mode,state", [(0o644, "problem"), (0o755, "ok")])
def test_doctor_manager_activation_requires_executable_entry(tmp_path, manager, mode, state):
    init_git(tmp_path)
    loader_recipe(tmp_path, "none")
    if manager == "lefthook":
        (tmp_path / "lefthook.yml").write_text(
            f"post-checkout:\n  commands:\n    splashdown:\n      run: {sd.hooks._LEFTHOOK_RUN}\n"
        )
        hook = tmp_path / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\n# call_lefthook\n")
    else:
        husky = tmp_path / ".husky"
        husky.mkdir()
        owned = husky / "post-checkout"
        owned.write_text(sd.hooks.POST_CHECKOUT_HOOK)
        owned.chmod(0o755)
        (husky / "_").mkdir()
        hook = husky / "_" / "post-checkout"
        hook.write_text('#!/bin/sh\n. "$(dirname "$0")/h"\n')
        subprocess.run(["git", "config", "core.hooksPath", ".husky/_"], cwd=tmp_path, check=True)
    hook.chmod(mode)
    result = doctor.run_doctor(tmp_path)
    assert result.exit_code == (0 if state == "ok" else 1)
    assert sample(result, "hook")["state"] == "ok"
    assert sample(result, "hook-activation")["state"] == state


def test_doctor_dangling_preferred_mise_config_prevents_lower_repair(tmp_path, monkeypatch):
    loader_recipe(tmp_path, "mise")
    present_loader(monkeypatch, "mise")
    (tmp_path / "mise.toml").symlink_to("absent.toml")
    lower = tmp_path / ".mise.toml"
    content = '[tools]\npython="3.13"\n'
    lower.write_text(content)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "loader")["repair"] == "none"
    assert sample(result, "loader")["state"] == "problem"
    assert lower.read_text() == content
    assert (tmp_path / "mise.toml").is_symlink()
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("fix", [False, True])
@pytest.mark.parametrize("output_format", ["text", "json"])
@pytest.mark.parametrize("unknown", [False, True])
def test_compose_report_only_severity_and_no_side_effects(
    tmp_path, monkeypatch, capsys, fix, output_format, unknown
):
    (tmp_path / sd.RECIPE_NAME).write_text('[project]\nloader="none"\n')
    (tmp_path / "compose.yaml").write_text(
        'services:\n  data:\n    ports: ["4300:80"]\n'
        "    container_name: shared-data\n"
        '    volumes: ["/srv/shared:/data"]\n' + ("  other: *unknown\n" if unknown else "")
    )
    state_home = tmp_path / "state"
    state_home.mkdir()
    (state_home / "stored-values-sentinel").write_bytes(b"unchanged allocations and notices\n")
    monkeypatch.setattr(doctor, "_hook_checks", lambda _: [])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "Compose must not query Docker/Colima or execute project commands"
        ),
    )
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert doctor.cmd_doctor(tmp_path, fix=fix, output_format=output_format) == (
        1 if unknown else 0
    )
    output = capsys.readouterr()
    state = "incomplete" if unknown else "warning"
    if output_format == "json":
        result = json.loads(output.out)
        row = next(
            row
            for row in result["data"]["outcomes"]
            if row["check_id"] == "compose-hardcoded-ports"
        )
        assert row["state"] == state
        assert row["changed_files"] == []
        assert row["repair"] == "none"
        message = row["message"]
        assert "COMPOSE_PROJECT_NAME is not declared" in " ".join(row["next_steps"])
    else:
        assert f"compose-hardcoded-ports: {state}:" in output.out
        assert "COMPOSE_PROJECT_NAME is not declared" in output.out
        message = output.out
    assert "services.data.ports[0]" in message
    assert "services.data.container_name" in message
    assert "services.data.volumes[0].source = '/srv/shared'" in message
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_compose_advisory_does_not_downgrade_a_proven_managed_consumer_problem(
    tmp_path, monkeypatch
):
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[resources.MANAGED]\ntype="port"\nrange=[4000,4100]\n'
        '[apps.web]\npath="."\nprofile="vite"\nresources=["MANAGED"]\n'
    )
    vite_config(tmp_path, "Number(env.MANAGED)", prefix=None)
    (tmp_path / "compose.yaml").write_text('services: {db: {ports: ["5432:5432"]}}\n')
    monkeypatch.setattr(doctor, "_hook_checks", lambda _: [])
    result = doctor.run_doctor(tmp_path)
    assert result.exit_code == 1
    assert sample(result, "compose-hardcoded-ports")["state"] == "warning"
    assert sample(result, "vite-port-wired")["state"] == "problem"


@pytest.mark.parametrize("fix", [False, True])
@pytest.mark.parametrize(
    "body, cause",
    [
        ('ports: []\n      - "8080:80"', "continuation after ports"),
        ('image: busybox\n      ports: ["8080:80"]', "continuation after image"),
        (r'volumes: ["\u002Fsrv/shared:/data"]', "unsupported quoted scalar"),
        (
            r'volumes: [{type: bind, source: "\u002Fsrv/shared", target: /data}]',
            "unsupported quoted scalar",
        ),
        ('container_name: "prefix" "suffix"', "unsupported quoted scalar"),
        ("container_name: 'prefix' 'suffix'", "unsupported quoted scalar"),
        (r'container_name: "prefix\"suffix"', "unsupported quoted scalar"),
    ],
)
def test_compose_unsupported_continuations_and_quoted_scalars_are_incomplete(
    tmp_path, monkeypatch, fix, body, cause
):
    (tmp_path / sd.RECIPE_NAME).write_text('[project]\nloader="none"\n')
    content = f'services:\n  db:\n    {body}\n  api: {{ports: ["4300:80"]}}\n'
    path = tmp_path / "compose.yaml"
    path.write_text(content)
    monkeypatch.setattr(doctor, "_hook_checks", lambda _: [])
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("executed a tool"))
    result = doctor.run_doctor(tmp_path, fix=fix)
    outcome = sample(result, "compose-hardcoded-ports")
    assert result.exit_code == 1
    assert outcome["state"] == "incomplete"
    assert "compose.yaml: services.db" in outcome["message"]
    assert cause in outcome["message"]
    assert "services.api.ports[0]: fixed host port 4300" in outcome["message"]
    assert outcome["repair"] == "none"
    assert outcome["changed_files"] == []
    assert path.read_text() == content


@pytest.mark.parametrize("fix", [False, True])
@pytest.mark.parametrize(
    "mount",
    [
        '"/srv/shared:/data"',
        "'/srv/shared:/data'",
        '{type: bind, source: "/srv/shared", target: /data}',
        "{type: bind, source: '/srv/shared', target: /data}",
    ],
)
def test_compose_understood_quoted_absolute_source_remains_advisory(
    tmp_path, monkeypatch, fix, mount
):
    (tmp_path / sd.RECIPE_NAME).write_text('[project]\nloader="none"\n')
    content = f"services: {{db: {{volumes: [{mount}]}}}}\n"
    path = tmp_path / "compose.yaml"
    path.write_text(content)
    monkeypatch.setattr(doctor, "_hook_checks", lambda _: [])
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("executed a tool"))
    result = doctor.run_doctor(tmp_path, fix=fix)
    outcome = sample(result, "compose-hardcoded-ports")
    assert result.exit_code == 0
    assert outcome["state"] == "warning"
    assert "compose.yaml: services.db.volumes[0].source = '/srv/shared'" in outcome["message"]
    assert outcome["repair"] == "none"
    assert outcome["changed_files"] == []
    assert path.read_text() == content


@pytest.mark.parametrize("name", ["RCT_METRO_PORT", "CUSTOM"])
@pytest.mark.parametrize(
    "script",
    [
        "react-native start --port 8090 && echo $(date)",
        "react-native start --port 8090 && echo `date`",
        "react-native start --port 8090\necho done",
        'react-native start --port 8090 && echo "unterminated',
    ],
)
@pytest.mark.parametrize("fix", [False, True])
def test_doctor_unsupported_rn_script_remains_incomplete(tmp_path, name, script, fix):
    configured(tmp_path, "react-native", [name])
    path = tmp_path / "package.json"
    before = json.dumps({"scripts": {"start": script}})
    path.write_text(before)
    result = doctor.run_doctor(tmp_path, fix=fix)
    outcome = sample(result, "rn-pkg-port")
    assert result.exit_code == 1
    assert outcome["state"] == "incomplete"
    assert outcome["next_steps"]
    assert outcome["repair"] == "none"
    assert path.read_text() == before


@pytest.mark.parametrize(
    "source",
    [
        'Deno.serve({port: 8000 || Number(Deno.env.get("CUSTOM"))}, handler);',
        'Deno.serve({port: 8000 ?? Number(Deno.env.get("CUSTOM"))}, handler);',
        'const example = `Deno.serve({port: Number(Deno.env.get("CUSTOM"))}, handler)`;\nDeno.serve(handler);',
        "const example = 'Deno.serve({port: Number(Deno.env.get(\"CUSTOM\"))}, handler)';\nDeno.serve(handler);",
        "Deno.serve({port: Number(\"Deno.env.get('CUSTOM')\")}, handler);",
    ],
)
def test_doctor_deno_requires_effective_unquoted_port_read(tmp_path, source):
    configured(tmp_path, "deno", ["CUSTOM"])
    config = tmp_path / "deno.json"
    config.write_text("{}")
    path = tmp_path / "main.ts"
    path.write_text(source)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "deno-port-wired")["state"] == "incomplete"
    assert path.read_text() == source
    assert config.read_text() == "{}"


@pytest.mark.parametrize("leading", ["settings.CUSTOM", 'settings["CUSTOM"]', "process.env.CUSTOM"])
@pytest.mark.parametrize("fix", [False, True])
def test_doctor_deno_unrelated_leading_property_is_incomplete(tmp_path, leading, fix):
    configured(tmp_path, "deno", ["CUSTOM"])
    config = tmp_path / "deno.json"
    config.write_text("{}")
    path = tmp_path / "main.ts"
    source = (
        "const settings = { CUSTOM: 8000 };\n"
        f'Deno.serve({{port: Number({leading} ?? Deno.env.get("CUSTOM"))}}, handler);\n'
    )
    path.write_text(source)
    result = doctor.run_doctor(tmp_path, fix=fix)
    assert result.exit_code == 1
    assert sample(result, "deno-port-wired")["state"] == "incomplete"
    assert path.read_text() == source
    assert config.read_text() == "{}"


@pytest.mark.parametrize(
    "mutation",
    [
        "config.server.port = 8081;",
        "config.server = {port: 8081};",
        "config['server']['port'] = 8081;",
        "Object.assign(config.server, {port: 8081});",
        "const server = config.server; server.port = 8081;",
        "module.exports.server.port = 8081;",
    ],
)
@pytest.mark.parametrize("before_export", [False, True])
def test_doctor_metro_later_mutations_are_manual(tmp_path, mutation, before_export):
    configured(tmp_path, "react-native", ["CUSTOM"])
    export = "module.exports = config;"
    tail = mutation + export if before_export else export + mutation
    text = "const config = {server: {port: Number(process.env.CUSTOM)}};" + tail
    path = tmp_path / "metro.config.js"
    path.write_text(text)
    result = doctor.run_doctor(tmp_path, fix=True)
    assert result.exit_code == 1
    assert sample(result, "rn-metro-config")["state"] == "incomplete"
    assert sample(result, "rn-metro-config")["repair"] == "none"
    assert path.read_text() == text


@pytest.mark.parametrize(
    "destination",
    [
        "config/$(touch sentinel).env",
        'config/quote".env',
        "config/quote'.env",
        "config/back\\slash.env",
        "config/new\nline.env",
        "config/`date`.env",
    ],
)
@pytest.mark.parametrize("fix", [False, True])
def test_doctor_refuses_shell_special_xcode_destination(tmp_path, destination, fix):
    configured(tmp_path, "react-native", ["RCT_METRO_PORT"])
    config = tmp_path / sd.RECIPE_NAME
    config.write_text(
        config.read_text().replace(
            'type="port"', 'type="port"\nwriter=' + json.dumps("envfile=" + destination)
        )
    )
    path = tmp_path / "ios/.xcode.env"
    path.parent.mkdir()
    original = "export NODE_BINARY=node\nexport RCT_METRO_PORT=8081\n"
    path.write_text(original)
    result = doctor.run_doctor(tmp_path, fix=fix)
    outcome = sample(result, "rn-xcode-env")
    assert result.exit_code == 1
    assert outcome["state"] == "incomplete"
    assert outcome["repair"] == "none"
    assert outcome["next_steps"] == [
        "Choose an envfile destination containing only letters, digits, spaces, dots, slashes, underscores or hyphens, or wire RCT_METRO_PORT manually with literal shell quoting."
    ]
    assert path.read_text() == original
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("marker", ["directory", "worktree"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("outside", [False, True])
def test_doctor_cli_refuses_checkout_git(tmp_path, monkeypatch, marker, nested, outside):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git_marker = checkout / ".git"
    if marker == "directory":
        git_marker.mkdir()
    else:
        git_marker.write_text("gitdir: /unused/worktrees/mobile\n")
    project = checkout / "nested" if nested else checkout
    project.mkdir(exist_ok=True)
    recipe(project, profile="nextjs")
    tools = checkout / "bin"
    tools.mkdir()
    fake = tools / "git"
    fake.write_text("#!/bin/sh\nexit 99\n")
    fake.chmod(0o755)
    from shutil import which

    external_git = which("git")
    assert external_git is not None
    monkeypatch.chdir(tmp_path if outside else project)
    relative = "../bin" if nested else "bin"
    monkeypatch.setenv("PATH", relative + os.pathsep + str(Path(external_git).parent))
    from contextlib import redirect_stdout
    from io import StringIO

    from splashdown.cli import main

    def run(argv, **kwargs):
        assert outside
        assert argv == ["git", "-C", str(project), "rev-parse", "--show-toplevel"]
        assert kwargs["executable"] == str(Path(external_git).resolve())
        return subprocess.CompletedProcess(argv, 0, str(checkout), "")

    monkeypatch.setattr(subprocess, "run", run)
    output = StringIO()
    with redirect_stdout(output):
        code = main(["--cwd", str(project), "--format", "json", "doctor"])
    result = json.loads(output.getvalue())
    assert code == 1
    assert "checkout-controlled Git" in output.getvalue()
    if outside:
        outcome = next(row for row in result["data"]["outcomes"] if row["check_id"] == "hook")
        assert outcome["state"] == "incomplete"
    else:
        assert result["error"]["code"] == "io_error"


def test_vite_reports_one_consumer_and_repairs_that_outcome(tmp_path):
    configured(tmp_path, "vite", ["CUSTOM"])
    vite_config(tmp_path, "Number(env.CUSTOM ?? 4321)", None)
    result = doctor.run_doctor(tmp_path)
    consumers = [row for row in rows(result) if row["check_id"].startswith("vite-")]
    assert [(row["check_id"], row["state"]) for row in consumers] == [
        ("vite-port-wired", "problem")
    ]
    assert result.data["summary"]["problem"] == 1
    fixed = doctor.run_doctor(tmp_path, fix=True)
    assert fixed.exit_code == 0
    assert sample(fixed, "vite-port-wired")["repair"] == "repaired"
    assert sample(fixed, "vite-port-wired")["changed_files"] == [str(tmp_path / "vite.config.ts")]
