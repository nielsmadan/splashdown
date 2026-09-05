from __future__ import annotations

import json
import plistlib
import subprocess

import pytest

import splashdown as sd
from splashdown import runtime_checks as checks


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:3000",
        "LOCALHOST",
        "http://127.0.0.1:3000",
        "127.1.2.3",
        "http://[::1]:8082",
    ],
)
def test_loopback_warning_identifies_resource_without_disclosing_its_value(value):
    assert checks.loopback_warnings({"API_URL": value}) == [
        "Resource API_URL contains loopback addresses. On a physical device these refer to "
        "the device itself. Use the development machine's reachable LAN address for host "
        "services, or verify that you configured port forwarding."
    ]


@pytest.mark.parametrize(
    "value",
    ["http://localhost.example.com:3000", "http://mylocalhost:3000", "192.168.1.3", "[2001::1]"],
)
def test_loopback_check_accepts_non_loopback_hosts(value):
    assert checks.loopback_warnings({"API_URL": value}) == []


def test_local_network_check_reads_binary_plist(tmp_path):
    path = tmp_path / "ios" / "App" / "Info.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(
        plistlib.dumps(
            {"NSLocalNetworkUsageDescription": "Connect to Metro"}, fmt=plistlib.FMT_BINARY
        )
    )

    assert checks.local_network_warnings(tmp_path, "react-native") == []


def test_local_network_check_ignores_extension_plist_when_app_is_valid(tmp_path):
    for name, data in (
        ("App", {"NSLocalNetworkUsageDescription": "Connect to Metro"}),
        (
            "ShareExtension",
            {"NSExtension": {"NSExtensionPointIdentifier": "com.apple.share-services"}},
        ),
    ):
        path = tmp_path / "ios" / name / "Info.plist"
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps(data))

    assert checks.local_network_warnings(tmp_path, "react-native") == []


@pytest.mark.parametrize("bundle_type", ["BNDL", "FMWK"])
def test_local_network_check_ignores_non_application_bundles(tmp_path, bundle_type):
    for name, data in (
        (
            "App",
            {"CFBundlePackageType": "APPL", "NSLocalNetworkUsageDescription": "Connect to Metro"},
        ),
        ("AppTests", {"CFBundlePackageType": bundle_type}),
    ):
        path = tmp_path / "ios" / name / "Info.plist"
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps(data))

    assert checks.local_network_warnings(tmp_path, "react-native") == []


@pytest.mark.parametrize("description", [None, "", "  ", 7, "$(NETWORK_REASON)"])
def test_local_network_check_reports_missing_or_unresolved_descriptions(tmp_path, description):
    path = tmp_path / "ios" / "App" / "Info.plist"
    path.parent.mkdir(parents=True)
    data = {} if description is None else {"NSLocalNetworkUsageDescription": description}
    path.write_bytes(plistlib.dumps(data))

    warnings = checks.local_network_warnings(tmp_path, "react-native")

    assert len(warnings) == 1
    assert warnings[0].startswith("ios/App/Info.plist:")


@pytest.mark.parametrize("content", [b"broken", b"<?xml version='1.0'?><plist><dict>"])
def test_local_network_check_reports_unparseable_plist(tmp_path, content):
    path = tmp_path / "ios" / "App" / "Info.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    warnings = checks.local_network_warnings(tmp_path, "react-native")

    assert len(warnings) == 1
    assert "could not read plist" in warnings[0]


def test_expo_local_network_check_reads_static_config_before_prebuild(tmp_path):
    (tmp_path / "app.json").write_text(
        json.dumps(
            {"expo": {"ios": {"infoPlist": {"NSLocalNetworkUsageDescription": "Connect to Metro"}}}}
        )
    )

    assert checks.local_network_warnings(tmp_path, "expo") == []


def test_expo_local_network_check_reports_dynamic_config_as_unverified(tmp_path):
    (tmp_path / "app.config.ts").write_text("export default {};\n")
    (tmp_path / "app.json").write_text('{"expo": {}}')

    assert checks.local_network_warnings(tmp_path, "expo") == [
        "Expo uses dynamic app config; verify ios.infoPlist.NSLocalNetworkUsageDescription "
        "in the resolved config and generated iOS app."
    ]


def test_device_preflight_reports_all_network_findings_before_launch(
    tmp_path, registry, monkeypatch, capsys
):
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[project]\nframework = "react-native"\n'
        '[apps.mobile]\npath = "apps/mobile"\nprofile = "react-native"\nresources = ["API_URL"]\n'
        '[targets.device.iphone]\nplatform = "ios"\nid = "PHONE"\n'
        '[resources.API_URL]\ntype = "set"\ndefault = "http://secret@localhost:3000"\n'
    )
    app = tmp_path / "apps" / "mobile"
    path = app / "ios" / "App" / "Info.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({}))
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: ({"id": "PHONE", "name": "iPhone", "platform": "ios"},),
    )
    diagnostics = []

    def launch(_cwd, _recipe, _destination, env):
        diagnostics.append(capsys.readouterr().err)
        assert env["API_URL"] == "http://secret@localhost:3000"
        return 0

    monkeypatch.setattr(sd.target_commands, "device_run", launch)

    assert sd.cmd_run(tmp_path, registry, "device", "iphone") == 0

    assert len(diagnostics) == 1
    assert "Resource API_URL contains loopback addresses" in diagnostics[0]
    assert "ios/App/Info.plist: add a nonempty NSLocalNetworkUsageDescription" in diagnostics[0]
    assert "secret" not in diagnostics[0]


def test_simulator_preflight_has_no_device_warnings(tmp_path, capsys):
    sd.launching.device_run_preflight(
        tmp_path,
        sd.Recipe({"project": {"framework": "react-native"}}, tmp_path / sd.RECIPE_NAME),
        sd.IOSDestination("Simulator", "SIM", owned=True),
        {"API_URL": "http://localhost:3000"},
    )

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("root_kind", ["checkout", "sibling", "none", "ancestor"])
def test_watchman_root_check_reads_existing_watches_without_mutating(
    tmp_path, monkeypatch, root_kind
):
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    root = {"checkout": cwd, "sibling": tmp_path / "checkout2", "ancestor": tmp_path}.get(root_kind)
    calls = []

    def query(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"roots": [str(root)] if root else []}), ""
        )

    monkeypatch.setattr(checks.subprocess, "run", query)

    status, detail = checks.watchman_watch_root(cwd)

    assert status == ("problem" if root_kind == "ancestor" else "ok")
    if root_kind == "ancestor":
        assert str(tmp_path) in detail
        assert "watchman watch-del PATH" in detail
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["watchman", "--no-spawn", "--no-local", "watch-list"]
    assert kwargs["timeout"] == 3


@pytest.mark.parametrize("checkout_first", [True, False])
def test_watchman_ancestor_is_a_problem_even_with_checkout_watch(
    tmp_path, monkeypatch, checkout_first
):
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    roots = [str(cwd), str(tmp_path)] if checkout_first else [str(tmp_path), str(cwd)]
    monkeypatch.setattr(
        checks.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps({"roots": roots}), ""
        ),
    )

    status, detail = checks.watchman_watch_root(cwd)

    assert status == "problem"
    assert f"Watchman watches an ancestor of this checkout: {tmp_path}." in detail


@pytest.mark.parametrize(
    "output",
    [
        "bad json",
        "[]",
        "{}",
        '{"roots": [1]}',
        '{"roots": ["relative"]}',
        '{"error": "poisoned", "roots": []}',
    ],
)
def test_watchman_unrecognized_response_is_a_problem(tmp_path, monkeypatch, output):
    monkeypatch.setattr(
        checks.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, output, ""),
    )

    status, detail = checks.watchman_watch_root(tmp_path)

    assert status == "problem"
    assert "unverified" in detail


def test_watchman_failed_command_is_a_problem_even_with_valid_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        checks.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 1, '{"roots": []}', "unavailable"
        ),
    )

    assert checks.watchman_watch_root(tmp_path)[0] == "problem"


@pytest.mark.parametrize(
    "error", [FileNotFoundError("watchman"), subprocess.TimeoutExpired("watchman", 3)]
)
def test_watchman_unavailable_query_is_reported(tmp_path, monkeypatch, error):
    def query(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(checks.subprocess, "run", query)

    assert checks.watchman_watch_root(tmp_path)[0] == "problem"


def test_doctor_watchman_check_uses_checkout_root_in_monorepo(tmp_path, monkeypatch, capsys):
    app = tmp_path / "apps" / "mobile"
    app.mkdir(parents=True)
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[apps.mobile]\npath = "apps/mobile"\nprofile = "expo"\nresources = []\n'
    )
    monkeypatch.setattr(checks, "watchman_available", lambda: True)
    roots = []

    def query(argv, **kwargs):
        roots.append(kwargs["cwd"])
        return subprocess.CompletedProcess(argv, 0, json.dumps({"roots": [str(tmp_path)]}), "")

    monkeypatch.setattr(checks.subprocess, "run", query)

    assert sd.cmd_doctor(tmp_path, fix=True) == 0

    assert roots == [tmp_path]
    assert "✓  watchman-root" in capsys.readouterr().err
