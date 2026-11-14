from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from splashdown import device_discovery as discovery
from splashdown import doctor, runtime_checks
from splashdown.device_types import PhysicalClaim, SimulatorRecord
from splashdown.inventory import AppInventory
from splashdown.recipe import Recipe
from splashdown.registry import RegistryReadError, RegistrySnapshot
from splashdown.results import Diagnostic
from splashdown.runtime_checks import ToolFact, ToolObservations, app_tooling


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(doctor, "_app_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "_hook_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "compose_wiring_checks", lambda *_: [])
    monkeypatch.setattr(
        ToolObservations, "executable", lambda *_, **__: ToolFact(True, "controlled executable")
    )
    monkeypatch.setattr(ToolObservations, "ios", lambda _: ToolFact(False, "Xcode missing"))
    monkeypatch.setattr(
        ToolObservations, "android", lambda _: ToolFact(True, "controlled Android SDK")
    )
    monkeypatch.setattr(ToolObservations, "jdk", lambda *_: ToolFact(True, "controlled JDK"))
    return tmp_path


def write_recipe(root, profiles=("flutter",), extra=""):
    (root / "splashdown.toml").write_text(
        extra
        + "\n"
        + "\n".join(
            f'[apps.app{i}]\npath="."\nprofile="{profile}"\nresources=[]\n'
            for i, profile in enumerate(profiles)
        )
    )


def outcomes(result, check_id):
    return [row for row in result.data["outcomes"] if row["check_id"] == check_id]


def test_optional_routes_are_per_app(project):
    write_recipe(project, ("flutter", "ios-native"))
    result = doctor.run_doctor(project)
    assert [(row["app"], row["state"]) for row in outcomes(result, "tools-ios")] == [
        ("app0", "warning"),
        ("app1", "problem"),
    ]
    assert result.exit_code == 1


def test_alternative_and_common_tools(project, monkeypatch):
    write_recipe(project, ("react-native",))
    result = doctor.run_doctor(project)
    assert result.exit_code == 0
    assert outcomes(result, "tools-ios")[0]["state"] == "warning"
    monkeypatch.setattr(
        ToolObservations, "executable", lambda *_, **__: ToolFact(False, "node missing")
    )
    result = doctor.run_doctor(project)
    assert outcomes(result, "tools-common")[0]["state"] == "problem"
    assert result.exit_code == 1


def test_all_routes_known_missing_are_problems(project, monkeypatch):
    write_recipe(project)
    monkeypatch.setattr(ToolObservations, "android", lambda _: ToolFact(False, "SDK missing"))
    result = doctor.run_doctor(project)
    assert [
        outcomes(result, f"tools-{platform}")[0]["state"] for platform in ("ios", "android")
    ] == ["problem", "problem"]
    assert result.exit_code == 1


def test_failed_route_is_incomplete_even_with_working_alternative(project, monkeypatch):
    write_recipe(project, ("flutter", "flutter"))
    calls = []

    def failing():
        calls.append(1)
        raise TimeoutError("selected Xcode timed out")

    monkeypatch.setattr(ToolObservations, "ios", lambda self: self.once("ios", failing))
    result = doctor.run_doctor(project)
    assert [row["state"] for row in outcomes(result, "tools-ios")] == ["incomplete", "incomplete"]
    assert len(calls) == 1
    assert result.exit_code == 1


def test_tool_check_construction_is_independent(project, monkeypatch):
    write_recipe(project, ("flutter", "flutter"))
    original = doctor.tooling_checks

    def construct(app, recipe, observations):
        if app.name == "app0":
            raise ValueError("construction failed")
        return original(app, recipe, observations)

    monkeypatch.setattr(doctor, "tooling_checks", construct)
    result = doctor.run_doctor(project)
    assert outcomes(result, "tool-check-construction")[0]["state"] == "incomplete"
    assert outcomes(result, "tools-android")[0]["app"] == "app1"
    assert outcomes(result, "loader")[0]["state"] == "skipped"


def test_custom_commands_have_explicit_unsupported_coverage(project, monkeypatch):
    write_recipe(project, extra='[project]\nrun="touch MUST-NOT-EXIST"')
    monkeypatch.setattr(
        ToolObservations, "executable", lambda *_: pytest.fail("custom dependencies inferred")
    )
    result = doctor.run_doctor(project, fix=True)
    assert outcomes(result, "tools-ios")[0]["state"] == "skipped"
    assert outcomes(result, "tools-android")[0]["state"] == "skipped"
    assert result.exit_code == 0
    assert not (project / "MUST-NOT-EXIST").exists()


def test_partial_custom_route_does_not_claim_available_toolchain(project):
    write_recipe(project, extra='[project.run]\nandroid="custom-build"')
    result = doctor.run_doctor(project)
    assert outcomes(result, "tools-ios")[0]["state"] == "warning"
    assert outcomes(result, "tools-android")[0]["state"] == "skipped"
    assert "unsupported" in outcomes(result, "tools-android")[0]["message"]


def test_pods_can_reuse_app_route_availability(project):
    write_recipe(project)
    recipe = Recipe.load(project / "splashdown.toml")
    app = AppInventory("app0", project, "flutter")
    assert app_tooling(app, recipe, ToolObservations()).ios_optional is True
    app.profile = "ios-native"
    assert app_tooling(app, recipe, ToolObservations()).ios_optional is False


@pytest.mark.parametrize(
    "present,mode,available", [(False, 0, False), (True, 0o644, False), (True, 0o755, True)]
)
def test_executable_checks_never_execute_checkout_tools(
    tmp_path, monkeypatch, present, mode, available
):
    path = tmp_path / "tool"
    if present:
        path.write_text("raise SystemExit('do not execute')")
        path.chmod(mode)
    monkeypatch.setattr(
        runtime_checks.subprocess, "run", lambda *_a, **_k: pytest.fail("executed tool")
    )
    fact = ToolObservations().executable("tool", path=path)
    assert fact.available is available


def test_xcode_failed_probe_is_bounded_and_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_checks.sys, "platform", "darwin")
    monkeypatch.delenv("DEVELOPER_DIR", raising=False)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, 3)

    monkeypatch.setattr(runtime_checks.subprocess, "run", run)
    observations = ToolObservations()
    assert observations.ios().available is None
    assert observations.ios().available is None
    assert len(calls) == 1
    assert calls[0][0] == ["/usr/bin/xcode-select", "--print-path"]
    assert calls[0][1]["timeout"] == 3
    assert calls[0][1]["cwd"] == "/"


def test_ios_unsupported_host_does_not_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_checks.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime_checks.subprocess, "run", lambda *_a, **_k: pytest.fail("SDK probe")
    )
    fact = ToolObservations().ios()
    assert fact.available is False
    assert "unsupported" in fact.detail


def test_native_gradle_wrapper_selection_uses_app_scope(project, monkeypatch):
    app_root = project / "nested"
    app_root.mkdir()
    (app_root / "gradlew").write_text("never execute")
    recipe = Recipe.parse("", project / "splashdown.toml")
    seen = []

    def executable(self, name, *, path=None, cwd=None):
        seen.append((name, path))
        return ToolFact(True, "controlled")

    monkeypatch.setattr(ToolObservations, "executable", executable)
    result = app_tooling(
        AppInventory("native", app_root, "android-native"), recipe, ToolObservations()
    )
    assert result.routes[0].available is True
    assert ("./gradlew", app_root / "gradlew") in seen


@contextmanager
def adb_server(response):
    requests = []
    errors = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)

        def serve():
            try:
                with listener.accept()[0] as connection:
                    connection.settimeout(3)
                    request = bytearray()
                    while len(request) < 18:
                        request.extend(connection.recv(18 - len(request)))
                    requests.append(bytes(request))
                    connection.sendall(response)
            except OSError as error:
                errors.append(error)

        thread = Thread(target=serve)
        thread.start()
        try:
            yield {"ADB_SERVER_SOCKET": f"tcp:127.0.0.1:{listener.getsockname()[1]}"}, requests
        finally:
            thread.join(4)
            assert not thread.is_alive()
        assert errors == []


def test_direct_adb_protocol_preserves_unverified_identity_and_connection_type():
    payload = b"PHONE\tdevice usb:1-1 model:Pixel transport_id:1\n10.0.0.2:3456\toffline model:Pixel transport_id:2\n"
    with adb_server(b"OKAY" + f"{len(payload):04x}".encode() + payload) as (env, requests):
        result = discovery.observe_existing_adb(env)
    assert requests == [b"000ehost:devices-l"]
    assert [
        (row.hardware_id, row.transport_id, row.state, row.connection_type, row.selectable)
        for row in result.devices
    ] == [
        (None, "PHONE", "device", "usb", False),
        (None, "10.0.0.2:3456", "offline", "network", False),
    ]


@pytest.mark.parametrize(
    "response",
    [
        b"WHAT",
        b"OKAYxxxx",
        b"OKAY0005abc",
        b"FAIL0006denied",
        b"OKAY0003\xff\xff\xff",
        b"OKAY0003bad",
        b"OKAY0000trailing",
    ],
)
def test_adb_protocol_errors_remain_failures(response):
    with adb_server(response) as (env, _), pytest.raises((ValueError, UnicodeError)):
        discovery.observe_existing_adb(env)


def test_adb_refused_server_is_idle(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def settimeout(self, _):
            pass

        def connect(self, _):
            raise ConnectionRefusedError("controlled refusal")

    monkeypatch.setattr(discovery.socket, "socket", lambda *_: Connection())
    assert discovery.observe_existing_adb({}).idle is True


@pytest.mark.parametrize(
    "env,endpoint",
    [
        ({}, ("127.0.0.1", 5037)),
        (
            {"ANDROID_ADB_SERVER_ADDRESS": "127.0.0.2", "ANDROID_ADB_SERVER_PORT": "5038"},
            ("127.0.0.2", 5038),
        ),
        ({"ADB_SERVER_SOCKET": "tcp:[::1]:5039", "ANDROID_ADB_SERVER_PORT": "5000"}, ("::1", 5039)),
    ],
)
def test_adb_endpoint_precedence(env, endpoint):
    assert discovery._adb_endpoint(env)[1] == endpoint


@pytest.mark.parametrize(
    "value",
    ["", "tcp:0", "tcp:65536", "tcp:host:5037", "localreserved:test", "tcp:127.0.0.1:not-port"],
)
def test_adb_invalid_config_never_falls_back(monkeypatch, value):
    monkeypatch.setattr(
        discovery.socket, "socket", lambda *_: pytest.fail("default endpoint queried")
    )
    with pytest.raises(ValueError):
        discovery.observe_existing_adb({"ADB_SERVER_SOCKET": value})


def test_adb_receive_uses_total_deadline(monkeypatch):
    ticks = iter([0.1, 0.9, 1.1])
    monkeypatch.setattr(discovery.time, "monotonic", lambda: next(ticks))

    class Connection:
        def settimeout(self, timeout):
            assert 0 < timeout <= 1

        def recv(self, size):
            return b"x"

    with pytest.raises(TimeoutError, match="deadline"):
        discovery._adb_receive(Connection(), 4, 1)


def test_device_scope_and_partial_snapshot_are_retained(project, monkeypatch):
    write_recipe(
        project,
        (),
        '[targets.device.phone]\nid="LOCAL"\nplatform="ios"\n[targets.simulator.default]\nmodel="iPhone 16"\nios="18.0"',
    )
    state = RegistrySnapshot(
        devices=(SimulatorRecord(str(project), "default", "UDID", "iPhone 16", "18.0", "now"),),
        claims=(
            PhysicalClaim("global:test", "ios", "OWN", "mine", str(project), "now"),
            PhysicalClaim("global:other", "android", "OTHER", "other", "/other-checkout", "now"),
        ),
    )

    def partial():
        raise RegistryReadError(state, (Diagnostic("registry_read_error", "bad row"),))

    monkeypatch.setattr(doctor, "read_registry_snapshot", partial)
    monkeypatch.setattr(
        doctor, "observe_existing_adb", lambda *_: pytest.fail("unrelated Android device queried")
    )
    result = doctor.run_doctor(project)
    assert outcomes(result, "device-registry-0")[0]["state"] == "incomplete"
    assert "UDID" in outcomes(result, "device:simulator.default")[0]["message"]
    assert "OWN" in outcomes(result, "device:claim:mine")[0]["message"]
    assert "paired idle" in outcomes(result, "device:device.phone")[0]["message"]
    assert {
        row["check_id"] for row in result.data["outcomes"] if row["check_id"].startswith("device:")
    } == {"device:simulator.default", "device:device.phone", "device:claim:mine"}


@pytest.mark.parametrize("failure", [False, True])
def test_android_success_and_failure_cached_for_multiple_targets(project, monkeypatch, failure):
    write_recipe(
        project,
        (),
        '[targets.device.one]\nid="ONE"\nplatform="android"\n[targets.device.two]\nid="TWO"\nplatform="android"',
    )
    calls = []

    def observe(_):
        calls.append(1)
        if failure:
            raise TimeoutError("controlled timeout")
        return discovery.ExistingADB(idle=True)

    monkeypatch.setattr(doctor, "observe_existing_adb", observe)
    result = doctor.run_doctor(project, fix=True)
    assert len(calls) == 1
    assert [outcomes(result, f"device:device.{name}")[0]["state"] for name in ("one", "two")] == [
        "incomplete" if failure else "warning"
    ] * 2
    assert result.exit_code == int(failure)
    assert not (project / "state").exists()


@pytest.mark.parametrize("state", ["unauthorized", "offline", "device"])
def test_usb_transport_state_is_not_claimed_as_saved_hardware_state(project, monkeypatch, state):
    write_recipe(project, (), '[targets.device.phone]\nid="PHONE"\nplatform="android"')
    devices = discovery._existing_adb_devices(f"PHONE\t{state} usb:1-2\n".encode())
    monkeypatch.setattr(doctor, "observe_existing_adb", lambda _: discovery.ExistingADB(devices))
    result = doctor.run_doctor(project)
    row = outcomes(result, "device:device.phone")[0]
    assert row["state"] == "warning"
    assert f"reports {state}" in row["message"]
    assert "hardware identity is unverified" in row["message"]
    assert result.exit_code == 0


def test_wireless_transport_never_matches_hardware(project, monkeypatch):
    write_recipe(project, (), '[targets.device.phone]\nid="PHONE"\nplatform="android"')
    devices = discovery._existing_adb_devices(b"10.0.0.2:5555\tdevice model:Pixel\n")
    monkeypatch.setattr(doctor, "observe_existing_adb", lambda _: discovery.ExistingADB(devices))
    row = outcomes(doctor.run_doctor(project), "device:device.phone")[0]
    assert row["state"] == "skipped"
    assert "cannot verify hardware identities" in row["message"]


def test_global_phones_do_not_expand_scope(project, monkeypatch):
    write_recipe(project, ())
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "splashdown"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        '[targets.device.unrelated]\nid="UNRELATED"\nplatform="android"'
    )
    monkeypatch.setattr(
        doctor, "observe_existing_adb", lambda *_: pytest.fail("global target queried")
    )
    result = doctor.run_doctor(project)
    assert result.exit_code == 0
    assert [row for row in result.data["outcomes"] if row["check_id"].startswith("device:")] == []


@pytest.mark.parametrize("fix", [False, True])
def test_real_cli_device_observation_never_invokes_sdk_clients(tmp_path, tmp_path_factory, fix):
    write_recipe(
        tmp_path,
        (),
        '[targets.device.android]\nid="PHONE"\nplatform="android"\n[targets.device.ios]\nid="IOS"\nplatform="ios"\n[targets.emulator.default]\ndevice="pixel"\nimage="system-images;android-35;google_apis;arm64-v8a"',
    )
    bin_dir = tmp_path_factory.mktemp("trusted-tools")
    sentinel = tmp_path / "SDK-INVOKED"
    git = bin_dir / "git"
    git.write_text(
        "#!/bin/sh\necho 'fatal: not a git repository (or any of the parent directories): .git' >&2\nexit 128\n"
    )
    git.chmod(0o755)
    for tool in ("adb", "xcrun", "xcodebuild", "flutter", "gradle", "pod", "npx"):
        path = bin_dir / tool
        path.write_text(f'#!/bin/sh\ntouch "{sentinel}"\nexit 77\n')
        path.chmod(0o755)
    with adb_server(b"OKAY0000") as (env, requests):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "splashdown",
                "--cwd",
                str(tmp_path),
                "doctor",
                "--format",
                "json",
                *(["--fix"] if fix else []),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env={
                **os.environ,
                **env,
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "XDG_STATE_HOME": str(tmp_path / "state"),
            },
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
    assert requests == [b"000ehost:devices-l"]
    payload = json.loads(result.stdout)
    assert payload["data"]["summary"]["warning"] == 1
    assert not sentinel.exists()
    assert not (tmp_path / "state").exists()


def test_configured_xcode_is_inspected_without_executing_it(tmp_path, monkeypatch):
    developer = tmp_path / "Xcode.app/Contents/Developer"
    sdk = developer / "Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk"
    sdk.mkdir(parents=True)
    binary = developer / "usr/bin/xcodebuild"
    binary.parent.mkdir(parents=True)
    binary.write_text("DO NOT EXECUTE")
    binary.chmod(0o755)
    xcrun = tmp_path / "xcrun"
    xcrun.write_text("DO NOT EXECUTE")
    xcrun.chmod(0o755)
    monkeypatch.setenv("DEVELOPER_DIR", str(tmp_path / "Xcode.app"))
    monkeypatch.setattr(runtime_checks.sys, "platform", "darwin")
    monkeypatch.setattr(runtime_checks.shutil, "which", lambda *_, **__: str(xcrun))
    monkeypatch.setattr(
        runtime_checks.subprocess, "run", lambda *_a, **_k: pytest.fail("SDK execution")
    )
    observations = ToolObservations()
    assert observations.ios().available is True
    binary.unlink()
    assert observations.ios().available is True
    assert ToolObservations().ios().available is False


def test_android_sdk_and_jdk_files_are_inspected_without_execution(tmp_path, monkeypatch):
    for name in ("platforms/android-35", "build-tools/35.0.0", "platform-tools", "jdk/bin"):
        (tmp_path / name).mkdir(parents=True)
    for name in ("platform-tools/adb", "jdk/bin/java"):
        executable = tmp_path / name
        executable.write_text("DO NOT EXECUTE")
        executable.chmod(0o755)
    monkeypatch.setenv("JAVA_HOME", str(tmp_path / "jdk"))
    monkeypatch.setattr(runtime_checks, "_android_home", lambda: tmp_path)
    monkeypatch.setattr(
        runtime_checks, "_android_bin", lambda _: str(tmp_path / "platform-tools/adb")
    )
    monkeypatch.setattr(
        runtime_checks.subprocess, "run", lambda *_a, **_k: pytest.fail("SDK execution")
    )
    recipe = Recipe.parse("", tmp_path / "splashdown.toml")
    app = AppInventory("app", tmp_path, "react-native")
    monkeypatch.setattr(ToolObservations, "ios", lambda _: ToolFact(False, "unsupported fixture"))
    assert app_tooling(app, recipe, ToolObservations()).routes[1].available is True
    (tmp_path / "jdk/bin/java").unlink()
    assert app_tooling(app, recipe, ToolObservations()).routes[1].available is False


def test_macos_java_shim_does_not_establish_a_jdk(monkeypatch):
    monkeypatch.setattr(runtime_checks.sys, "platform", "darwin")
    monkeypatch.setattr(runtime_checks.shutil, "which", lambda *_, **__: "/usr/bin/java")
    fact = ToolObservations().executable("java")
    assert fact.available is None
    assert "configure JAVA_HOME" in fact.detail


def test_executable_inspection_errors_cached(tmp_path, monkeypatch):
    calls = []

    def denied(*_, **__):
        calls.append(1)
        raise PermissionError("controlled inaccessible PATH")

    monkeypatch.setattr(runtime_checks.shutil, "which", denied)
    observations = ToolObservations()
    assert observations.executable("flutter").available is None
    assert observations.executable("flutter").available is None
    assert calls == [1]


def test_local_targets_added_and_local_errors_preserve_recipe_scope(project, monkeypatch):
    write_recipe(project, (), '[targets.device.recipe]\nid="RECIPE"\nplatform="ios"')
    local = project / "splashdown.local.toml"
    local.write_text('[targets.device.local]\nid="LOCAL"\nplatform="ios"')
    result = doctor.run_doctor(project)
    assert outcomes(result, "device:device.local")[0]["state"] == "skipped"
    local.write_text("[invalid")
    result = doctor.run_doctor(project)
    assert outcomes(result, "device-configuration")[0]["state"] == "incomplete"
    assert outcomes(result, "device:device.recipe")[0]["state"] == "skipped"


def test_unsupported_adb_endpoint_is_explicit_skip(project, monkeypatch):
    write_recipe(project, (), '[targets.device.phone]\nid="PHONE"\nplatform="android"')
    monkeypatch.setenv("ADB_SERVER_SOCKET", "localreserved:adb")
    monkeypatch.setattr(
        discovery.socket, "socket", lambda *_: pytest.fail("fallback endpoint queried")
    )
    row = outcomes(doctor.run_doctor(project), "device:device.phone")[0]
    assert row["state"] == "skipped"
    assert "unsupported ADB_SERVER_SOCKET" in row["message"]


def test_device_construction_error_keeps_independent_loader_results(project, monkeypatch):
    write_recipe(project, ())

    def fail():
        raise ValueError("snapshot construction")

    monkeypatch.setattr(doctor, "read_registry_snapshot", fail)
    result = doctor.run_doctor(project)
    assert outcomes(result, "device-check-construction")[0]["state"] == "incomplete"
    assert outcomes(result, "loader")[0]["state"] == "skipped"


@pytest.mark.parametrize(
    "error",
    [PermissionError("permission denied"), TimeoutError("timeout"), ValueError("invalid response")],
)
def test_adb_observation_failures_are_not_idle(project, monkeypatch, error):
    write_recipe(project, (), '[targets.device.phone]\nid="PHONE"\nplatform="android"')

    def fail(_):
        raise error

    monkeypatch.setattr(doctor, "observe_existing_adb", fail)
    row = outcomes(doctor.run_doctor(project), "device:device.phone")[0]
    assert row["state"] == "incomplete"
    assert str(error) in row["message"]


def test_runtime_observations_stay_outside_configuration_repair_lock(project, monkeypatch):
    from splashdown.wiring import WiringCheck

    write_recipe(project, extra='[targets.device.phone]\nid="PHONE"\nplatform="android"')
    locked = []
    observations = []

    @contextmanager
    def lock(_):
        locked.append(True)
        try:
            yield
        finally:
            locked.pop()

    def tool(*_, **__):
        assert not locked
        observations.append("tool")
        return ToolFact(True, "controlled")

    def devices(_):
        assert not locked
        observations.append("devices")
        return discovery.ExistingADB(idle=True)

    check = WiringCheck(
        "repair",
        "test config repair",
        lambda _: True,
        lambda path: ("ok", "fixed") if (path / "config").exists() else ("problem", "missing"),
        lambda path: (path / "config").write_text("fixed"),
        None,
        files=lambda path: (path / "config",),
    )
    monkeypatch.setattr(doctor, "_app_checks", lambda *_: [check])
    monkeypatch.setattr(doctor, "checkout_operation_lock", lock)
    monkeypatch.setattr(ToolObservations, "executable", tool)
    monkeypatch.setattr(doctor, "observe_existing_adb", devices)
    result = doctor.run_doctor(project, fix=True)
    assert result.exit_code == 0
    assert set(observations) == {"tool", "devices"}
    assert outcomes(result, "repair")[0]["repair"] == "repaired"


def test_relative_path_tools_resolve_per_app(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "bin")
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "bin").mkdir(parents=True)
    second.mkdir()
    tool = first / "bin/flutter"
    tool.write_text("NEVER EXECUTE")
    tool.chmod(0o755)
    observations = ToolObservations()
    assert observations.executable("flutter", cwd=first).available is True
    assert observations.executable("flutter", cwd=second).available is False


@pytest.mark.parametrize("jdk_source", ["configured-jdk", "AndroidStudio/jbr"])
def test_flutter_external_jdk_selection_is_incomplete_without_shell_java(
    tmp_path, monkeypatch, jdk_source
):
    app_path = tmp_path / "app"
    app_path.mkdir()
    external = tmp_path / jdk_source / "bin/java"
    external.parent.mkdir(parents=True)
    external.write_text("DO NOT EXECUTE")
    external.chmod(0o755)
    if jdk_source == "configured-jdk":
        (tmp_path / ".flutter_settings").write_text(
            json.dumps({"jdk-dir": str(external.parent.parent)})
        )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setenv("PATH", str(app_path))
    sdk = tmp_path / "sdk"
    for directory in ("platforms/android-35", "build-tools/35.0.0", "platform-tools"):
        (sdk / directory).mkdir(parents=True)
    adb = sdk / "platform-tools/adb"
    adb.write_text("DO NOT EXECUTE")
    adb.chmod(0o755)
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    flutter = app_path / "flutter"
    flutter.write_text("DO NOT EXECUTE")
    flutter.chmod(0o755)
    monkeypatch.setattr(runtime_checks.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime_checks.subprocess, "run", lambda *_a, **_k: pytest.fail("Flutter/SDK execution")
    )
    recipe = Recipe.parse("", app_path / "splashdown.toml")
    app = AppInventory("app", app_path, "flutter")
    observations = ToolObservations()
    facts = app_tooling(app, recipe, observations)
    assert facts.routes[1].available is None
    assert facts.ios_optional is False
    checks = {
        check.id: check.detect(app_path)
        for check in runtime_checks.tooling_checks(app, recipe, observations)
    }
    assert checks["tools-common"][0] == "ok"
    assert checks["tools-ios"][0] == "warning"
    assert checks["tools-android"][0] == "incomplete"
    assert (
        "configured jdk-dir and Android Studio bundled JDK lookup are unsupported"
        in checks["tools-android"][1]
    )


def test_flutter_shell_java_does_not_establish_selected_jdk(tmp_path, monkeypatch):
    java = tmp_path / "bin/java"
    java.parent.mkdir()
    java.write_text("DO NOT EXECUTE")
    java.chmod(0o755)
    monkeypatch.setenv("JAVA_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(java.parent))
    fact = ToolObservations().jdk("flutter", tmp_path)
    assert fact.available is None
    assert "JAVA_HOME/PATH alone cannot establish the JDK Flutter selects" in fact.detail


@pytest.mark.parametrize(
    "selected_present,path_present", [(True, False), (False, True), (True, True)]
)
def test_native_ios_checks_selected_and_path_xcodebuild(
    tmp_path, monkeypatch, selected_present, path_present
):
    developer = tmp_path / "Xcode.app/Contents/Developer"
    (developer / "Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk").mkdir(parents=True)
    app_path = tmp_path / "app"
    (app_path / "bin").mkdir(parents=True)
    paths = [app_path / "bin/xcrun"]
    if selected_present:
        paths.append(developer / "usr/bin/xcodebuild")
    if path_present:
        paths.append(app_path / "bin/xcodebuild")
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DO NOT EXECUTE")
        path.chmod(0o755)
    monkeypatch.setenv("DEVELOPER_DIR", str(developer))
    monkeypatch.setenv("PATH", "bin")
    monkeypatch.setattr(runtime_checks.sys, "platform", "darwin")
    monkeypatch.setattr(
        runtime_checks.subprocess, "run", lambda *_a, **_k: pytest.fail("SDK execution")
    )
    recipe = Recipe.parse("", tmp_path / "splashdown.toml")
    result = app_tooling(AppInventory("app", app_path, "ios-native"), recipe, ToolObservations())
    assert result.routes[0].available is (selected_present and path_present)
    if selected_present and not path_present:
        assert "xcodebuild missing from PATH" in result.routes[0].detail


@pytest.mark.parametrize(
    "variable", ["JAVA_HOME", "DEVELOPER_DIR", "ANDROID_HOME", "ANDROID_SDK_ROOT"]
)
def test_relative_sdk_environment_paths_are_explicitly_incomplete(tmp_path, monkeypatch, variable):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "relative").mkdir()
    monkeypatch.setenv(variable, "relative")
    monkeypatch.setattr(runtime_checks.sys, "platform", "darwin")
    monkeypatch.setattr(
        runtime_checks.subprocess, "run", lambda *_a, **_k: pytest.fail("SDK execution")
    )
    observations = ToolObservations()
    if variable == "JAVA_HOME":
        fact = observations.jdk("react-native", tmp_path / "other-app")
    elif variable == "DEVELOPER_DIR":
        fact = observations.ios()
    else:
        monkeypatch.delenv(
            "ANDROID_HOME" if variable == "ANDROID_SDK_ROOT" else "ANDROID_SDK_ROOT", raising=False
        )
        monkeypatch.setattr(
            runtime_checks, "_android_home", lambda: pytest.fail("relative SDK resolution")
        )
        fact = observations.android()
    assert fact.available is None
    assert "Relative" in fact.detail
    assert "absolute" in fact.detail


def test_claim_supplies_omitted_device_platform_without_android_observation(project, monkeypatch):
    write_recipe(project, (), '[targets.device.phone]\nid="IOS-HARDWARE"')
    snapshot = RegistrySnapshot(
        claims=(PhysicalClaim("local:test", "ios", "IOS-HARDWARE", "phone", str(project), "now"),)
    )
    monkeypatch.setattr(doctor, "read_registry_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        doctor, "observe_existing_adb", lambda *_: pytest.fail("unrelated Android observation")
    )
    result = doctor.run_doctor(project)
    row = outcomes(result, "device:device.phone")[0]
    assert row["state"] == "skipped"
    assert "Saved iOS hardware ID IOS-HARDWARE" in row["message"]
    assert result.exit_code == 0
    assert [
        row["check_id"] for row in result.data["outcomes"] if row["check_id"].startswith("device:")
    ] == ["device:device.phone"]


def test_conflicting_claim_platforms_are_incomplete_without_device_observation(
    project, monkeypatch
):
    write_recipe(project, (), '[targets.device.phone]\nid="HARDWARE"')
    snapshot = RegistrySnapshot(
        claims=tuple(
            PhysicalClaim(f"local:{platform}", platform, "HARDWARE", "phone", str(project), "now")
            for platform in ("ios", "android")
        )
    )
    monkeypatch.setattr(doctor, "read_registry_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        doctor, "observe_existing_adb", lambda *_: pytest.fail("ambiguous platform queried")
    )
    result = doctor.run_doctor(project)
    row = outcomes(result, "device:device.phone")[0]
    assert row["state"] == "incomplete"
    assert "HARDWARE" in row["message"]
    assert "claims conflict on platform (android, ios)" in row["message"]
    assert result.exit_code == 1


@pytest.mark.parametrize("shape", ["symlink", "dangling", "fifo", "directory", "invalid-utf8"])
def test_unsafe_local_destinations_preserve_independent_observations(project, monkeypatch, shape):
    write_recipe(project, (), '[targets.device.phone]\nid="IOS"\nplatform="ios"')
    local = project / "splashdown.local.toml"
    if shape in {"symlink", "dangling"}:
        destination = project / "destination"
        if shape == "symlink":
            destination.write_text("")
        local.symlink_to(destination)
    elif shape == "fifo":
        os.mkfifo(local)
    elif shape == "directory":
        local.mkdir()
    else:
        local.write_bytes(b"\xff")
    result = doctor.run_doctor(project)
    assert outcomes(result, "device-configuration")[0]["state"] == "incomplete"
    assert outcomes(result, "device:device.phone")[0]["state"] == "skipped"
    assert outcomes(result, "loader")[0]["state"] == "skipped"
    assert result.exit_code == 1
