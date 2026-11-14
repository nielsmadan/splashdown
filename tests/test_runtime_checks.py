from __future__ import annotations

import json
import plistlib
import subprocess

import pytest

import splashdown as sd
from splashdown import runtime_checks as checks

pytestmark = pytest.mark.usefixtures("controlled_doctor_toolchains")


@pytest.fixture
def existing_watchman(monkeypatch):
    import shutil

    original = shutil.which
    monkeypatch.setattr(checks, "watchman_available", lambda *_: True)
    monkeypatch.setattr(
        checks.shutil,
        "which",
        lambda name, **kwargs: "/usr/bin/true" if name == "watchman" else original(name, **kwargs),
    )


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
        (
            "Resource API_URL contains loopback addresses. On a physical device these refer to "
            "the device itself. Use the development machine's reachable LAN address for host "
            "services, or verify that you configured port forwarding."
        )
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
        (
            "Expo uses dynamic app config; verify ios.infoPlist.NSLocalNetworkUsageDescription "
            "in the resolved config and generated iOS app."
        )
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
    tmp_path, monkeypatch, root_kind, existing_watchman
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

    assert status == ("warning" if root_kind == "ancestor" else "ok")
    if root_kind == "ancestor":
        assert str(tmp_path) in detail
        assert "Intentional sharing is valid" in detail
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["watchman", "--no-spawn", "--no-local", "watch-list"]
    assert kwargs["timeout"] == 3


@pytest.mark.parametrize("checkout_first", [True, False])
def test_watchman_ancestor_is_advisory_even_with_checkout_watch(
    tmp_path, monkeypatch, checkout_first, existing_watchman
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

    assert status == "warning"
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
        '{"error": "", "roots": []}',
        '{"roots": ["/bad\\u0000root"]}',
    ],
)
def test_watchman_unrecognized_response_is_incomplete(
    tmp_path, monkeypatch, output, existing_watchman
):
    monkeypatch.setattr(
        checks.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, output, ""),
    )

    status, detail = checks.watchman_watch_root(tmp_path)

    assert status == "incomplete"
    assert "unverified" in detail


def test_watchman_failed_command_is_incomplete_even_with_valid_stdout(
    tmp_path, monkeypatch, existing_watchman
):
    monkeypatch.setattr(
        checks.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 1, '{"roots": []}', "unavailable"
        ),
    )

    assert checks.watchman_watch_root(tmp_path)[0] == "incomplete"


@pytest.mark.parametrize(
    "error", [FileNotFoundError("watchman"), subprocess.TimeoutExpired("watchman", 3)]
)
def test_watchman_unavailable_query_is_reported(tmp_path, monkeypatch, error, existing_watchman):
    def query(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(checks.subprocess, "run", query)

    assert checks.watchman_watch_root(tmp_path)[0] == "incomplete"


def test_doctor_watchman_check_uses_checkout_root_in_monorepo(
    tmp_path, monkeypatch, capsys, existing_watchman
):
    app = tmp_path / "apps" / "mobile"
    app.mkdir(parents=True)
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[apps.mobile]\npath = "apps/mobile"\nprofile = "expo"\nresources = []\n'
    )
    monkeypatch.setattr(checks, "watchman_available", lambda *_: True)
    roots = []
    original = checks.subprocess.run

    def query(argv, **kwargs):
        if argv[0] != "watchman":
            return original(argv, **kwargs)
        roots.append(kwargs["cwd"])
        return subprocess.CompletedProcess(argv, 0, json.dumps({"roots": [str(tmp_path)]}), "")

    monkeypatch.setattr(checks.subprocess, "run", query)

    assert sd.cmd_doctor(tmp_path, fix=True) == 0

    assert roots == [tmp_path]
    assert "watchman-root: ok:" in capsys.readouterr().out


def pods_fixture(root, profile="react-native", name="mobile", path=".", extra=""):
    from splashdown.inventory import AppInventory
    from splashdown.recipe import Recipe

    app_path = root / path
    app_path.mkdir(parents=True, exist_ok=True)
    recipe = Recipe.parse(
        f'[apps.{name}]\npath = "{path}"\nprofile = "{profile}"\nresources = []\n' + extra,
        root / sd.RECIPE_NAME,
    )
    app = AppInventory(name, app_path, profile, root)
    check = checks.pods_check(app, recipe, checks.ToolObservations())
    return app_path, check


def write_pods(native, lock=b"LOCK\n", manifest=b"LOCK\n"):
    native.mkdir(parents=True, exist_ok=True)
    (native / "Podfile").write_text("platform :ios, '15.0'\n")
    if lock is not None:
        (native / "Podfile.lock").write_bytes(lock)
    if manifest is not None:
        (native / "Pods").mkdir(exist_ok=True)
        (native / "Pods" / "Manifest.lock").write_bytes(manifest)


def test_pods_equal_bytes_only_establish_manifest_consistency_after_podfile_change(tmp_path):
    app, check = pods_fixture(tmp_path)
    write_pods(app / "ios", lock=b"\xff\n", manifest=b"\xff\n")
    (app / "ios" / "Podfile").write_text("pod 'NewDependency'\n")

    state, message = check.detect(app)

    assert state == "ok"
    assert "equal bytes establish manifest consistency only" in message
    assert "Podfile/dependency changes, freshness and build readiness are unverified" in message
    assert str(app / "ios" / "Podfile.lock") in message
    assert str(app / "ios" / "Pods" / "Manifest.lock") in message
    assert "mobile:" in message


@pytest.mark.parametrize(
    ("lock", "manifest", "expected"),
    [
        (b"A", b"B", "differ"),
        (None, b"A", "Podfile.lock"),
        (b"A", None, "Manifest.lock"),
        (None, None, "missing"),
    ],
)
def test_pods_mismatched_and_missing_inputs_are_distinct_current_snapshots(
    tmp_path, lock, manifest, expected
):
    app, check = pods_fixture(tmp_path, profile="ios-native")
    write_pods(app, lock, manifest)

    state, message = check.detect(app)

    assert state == "problem"
    assert expected in message
    assert "current installation snapshot" in message
    assert "ordinary project preparation may repair it" in message


@pytest.mark.parametrize("profile", ["expo", "react-native", "flutter", "ios-native"])
def test_pods_no_established_integration_skips_instead_of_inventing_installation(tmp_path, profile):
    app, check = pods_fixture(tmp_path, profile=profile)
    assert check.detect(app)[0] == "skipped"


def test_pods_unrelated_profiles_have_no_check(tmp_path):
    _, check = pods_fixture(tmp_path, profile="vite")
    assert check is None


@pytest.mark.parametrize("form", ["module.exports =", "export default"])
def test_pods_honors_whole_static_rn_source_dir_per_app(tmp_path, form):
    app, check = pods_fixture(tmp_path, path="apps/one", name="one")
    (app / "react-native.config.js").write_text(
        f"{form} {{project: {{ios: {{sourceDir: 'native/apple'}}}}}};\n"
    )
    write_pods(app / "native" / "apple")
    other, other_check = pods_fixture(tmp_path, path="apps/two", name="two")
    write_pods(other / "ios", lock=b"A", manifest=b"B")

    assert check.detect(app)[0] == "ok"
    assert str(app / "native" / "apple" / "Podfile.lock") in check.detect(app)[1]
    assert other_check.detect(other)[0] == "warning"


@pytest.mark.parametrize("field", ["workspace", "project"])
def test_pods_honors_native_platform_project_parent(tmp_path, field):
    app, check = pods_fixture(
        tmp_path, profile="ios-native", extra=f'[project.ios]\n{field} = "native/App.xcworkspace"\n'
    )
    write_pods(app / "native")
    assert check.detect(app)[0] == "ok"
    assert str(app / "native" / "Pods" / "Manifest.lock") in check.detect(app)[1]


@pytest.mark.parametrize(
    "content",
    [
        "const config = makeConfig(); module.exports = config;",
        "module.exports = {project: {ios: {sourceDir: getPath()}}};",
        "module.exports = {} || {project: {ios: {sourceDir: 'actual'}}};",
        "module.exports = {project: {} || {ios: {sourceDir: 'actual'}}};",
        "module.exports = {project: {ios: {sourceDir: 'elsewhere'}}}; module.exports.project.ios.sourceDir = 'actual';",
        "// sourceDir: 'fake'\nmodule.exports = loadConfig();",
        "module.exports = {unrelated: {sourceDir: 'fake'}, project: makeProject()};",
        "module.exports = {platforms: {ios: require('custom-platform')}};",
        "module.exports = {...config, project: {ios: {sourceDir: 'native'}}};",
        "module.exports = {project: {ios: {sourceDir: '../outside'}}};",
    ],
)
def test_pods_dynamic_or_unproven_layout_is_incomplete(tmp_path, content):
    app, check = pods_fixture(tmp_path)
    (app / "react-native.config.js").write_text(content)
    write_pods(app / "ios")
    assert check.detect(app)[0] == "incomplete"


def test_doctor_pods_unknown_layout_retains_manual_guidance_and_later_outcome(
    tmp_path, monkeypatch
):
    from splashdown import doctor

    (tmp_path / sd.RECIPE_NAME).write_text(
        '[apps.first]\npath="first"\nprofile="react-native"\nresources=[]\n'
        '[apps.second]\npath="second"\nprofile="react-native"\nresources=[]\n'
    )
    first = tmp_path / "first"
    first.mkdir()
    (first / "react-native.config.js").write_text("module.exports = loadConfig();")
    write_pods(tmp_path / "second" / "ios")
    monkeypatch.setattr(doctor, "_app_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "_project_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "tooling_checks", lambda *_: [])
    monkeypatch.setattr(checks.subprocess, "run", lambda *_a, **_k: pytest.fail("execution"))
    locations = checks._pods_locations
    inspected = []

    def inspect_layout(app, recipe):
        inspected.append(app.name)
        return locations(app, recipe)

    monkeypatch.setattr(checks, "_pods_locations", inspect_layout)

    result = doctor.run_doctor(tmp_path)

    outcomes = [row for row in result.data["outcomes"] if row["check_id"] == "pods-manifest"]
    assert result.exit_code == 1
    assert inspected == ["first", "second"]
    assert [(row["app"], row["state"]) for row in outcomes] == [
        ("first", "incomplete"),
        ("second", "ok"),
    ]
    assert outcomes[0]["message"] == (
        f"first: CocoaPods layout inspection incomplete: {first / 'react-native.config.js'}: "
        "dynamic or unsupported React Native configuration"
    )
    assert outcomes[0]["next_steps"] == [
        (
            "Verify the app's native source, Podfile, Podfile.lock and installed Pods/Manifest.lock "
            "paths manually without executing them through doctor."
        )
    ]


def test_pods_static_disabled_ios_skips(tmp_path):
    app, check = pods_fixture(tmp_path)
    (app / "react-native.config.cjs").write_text("module.exports = {project: {ios: null}};")
    assert check.detect(app)[0] == "skipped"


def test_pods_unproven_xcconfig_path_settings_are_coverage_limit(tmp_path):
    app, check = pods_fixture(tmp_path)
    write_pods(app / "ios")
    (app / "ios" / "custom.xcconfig").write_text("PODS_ROOT = ${SRCROOT}/custom-pods\n")
    state, message = check.detect(app)
    assert state == "incomplete"
    assert "standalone xcconfig does not establish active paths" in message


@pytest.mark.parametrize("kind", ["fifo", "symlink", "dangling", "directory", "parent-symlink"])
def test_pods_nonregular_inputs_are_unreadable_not_missing(tmp_path, kind):
    import os

    app, check = pods_fixture(tmp_path)
    write_pods(app / "ios")
    manifest = app / "ios" / "Pods" / "Manifest.lock"
    manifest.unlink()
    if kind == "fifo":
        os.mkfifo(manifest)
    elif kind == "directory":
        manifest.mkdir()
    elif kind == "parent-symlink":
        manifest.parent.rmdir()
        other = app / "other"
        other.mkdir()
        (other / "Manifest.lock").write_bytes(b"LOCK\n")
        manifest.parent.symlink_to(other, target_is_directory=True)
    else:
        manifest.symlink_to(app / ("missing" if kind == "dangling" else "ios/Podfile.lock"))
    state, message = check.detect(app)
    assert state == "incomplete"
    assert "cannot inspect inputs" in message
    assert "symlink or not a" in message


def test_pods_read_failure_preserves_actual_cause(tmp_path, monkeypatch):
    app, check = pods_fixture(tmp_path)
    write_pods(app / "ios")
    read = checks._pods_bytes

    def denied(path, root):
        if path.name == "Podfile.lock":
            raise PermissionError("specific read denial")
        return read(path, root)

    monkeypatch.setattr(checks, "_pods_bytes", denied)
    state, message = check.detect(app)
    assert state == "incomplete"
    assert "specific read denial" in message


@pytest.mark.parametrize("android", [True, False, None])
def test_pods_optional_ios_uses_only_verified_same_app_android_route(
    tmp_path, monkeypatch, android
):
    from splashdown.runtime_checks import ToolFact, ToolObservations

    monkeypatch.setattr(ToolObservations, "android", lambda _: ToolFact(android, "Android"))
    monkeypatch.setattr(ToolObservations, "jdk", lambda *_: ToolFact(True, "JDK"))
    app, check = pods_fixture(tmp_path)
    write_pods(app / "ios", manifest=None)
    assert check.detect(app)[0] == ("warning" if android else "problem")


@pytest.mark.parametrize("bundled", [True, False])
def test_pods_install_guidance_uses_bundler_evidence_and_podfile_directory(
    tmp_path, monkeypatch, bundled
):
    monkeypatch.delenv("BUNDLE_GEMFILE", raising=False)
    app, check = pods_fixture(tmp_path, path="apps/mobile")
    write_pods(app / "ios", manifest=None)
    (tmp_path / "Gemfile").write_text("gem 'cocoapods', '~> 1.16'\n" if bundled else "gem 'rake'\n")
    guidance = check.manual_instructions(app)
    assert f"cd {app / 'ios'} && {'bundle exec ' if bundled else ''}pod install" in guidance
    assert "project's normal iOS dependency preparation" in guidance


@pytest.mark.parametrize("relative", ["apps/mobile/ios", "apps/mobile", "apps"])
def test_pods_bundler_guidance_uses_nearest_gemfile(tmp_path, monkeypatch, relative):
    monkeypatch.delenv("BUNDLE_GEMFILE", raising=False)
    app, check = pods_fixture(tmp_path, path="apps/mobile")
    write_pods(app / "ios", manifest=None)
    (tmp_path / "Gemfile").write_text("gem 'cocoapods'\n")
    (tmp_path / relative / "Gemfile").write_text("gem 'rake'\n")

    guidance = check.manual_instructions(app)

    assert f"`cd {app / 'ios'} && pod install`" in guidance


def test_pods_bundler_override_requires_manual_preparation(tmp_path, monkeypatch):
    monkeypatch.setenv("BUNDLE_GEMFILE", "custom/Gemfile")
    app, check = pods_fixture(tmp_path)
    write_pods(app / "ios", manifest=None)
    (tmp_path / "Gemfile").write_text("gem 'cocoapods'\n")

    guidance = check.manual_instructions(app)

    assert guidance == (
        "Run the project's normal iOS dependency preparation. Verify the Gemfile selected "
        "by BUNDLE_GEMFILE and its CocoaPods setup manually before running pod install."
    )


@pytest.mark.parametrize(
    ("relative", "read_count"),
    [
        ("react-native.config.js", 0),
        ("ios/Podfile", 0),
        ("ios/Podfile.lock", 2),
        ("ios/Pods/Manifest.lock", 2),
        ("ios/custom.xcconfig", 1),
    ],
)
def test_pods_oversized_sparse_inputs_report_incomplete_without_reading(
    tmp_path, monkeypatch, relative, read_count
):
    app, check = pods_fixture(tmp_path)
    write_pods(app / "ios")
    path = app / relative
    with path.open("wb") as stream:
        stream.truncate(1024**3)
    original = checks.os.fdopen
    reads = []

    class GuardedStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            assert 0 < size <= checks._PODS_READ_LIMIT + 1
            reads.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(checks.os, "fdopen", lambda *args: GuardedStream(original(*args)))

    state, message = check.detect(app)

    assert state == "incomplete"
    assert str(path) in message
    assert "exceeds 1048576-byte inspection limit" in message
    assert reads == [checks._PODS_READ_LIMIT + 1] * read_count


def test_pods_oversized_gemfile_reports_incomplete_in_doctor(tmp_path, monkeypatch):
    from splashdown import doctor

    monkeypatch.delenv("BUNDLE_GEMFILE", raising=False)
    app, check = pods_fixture(tmp_path)
    write_pods(app / "ios", manifest=None)
    path = tmp_path / "Gemfile"
    with path.open("wb") as stream:
        stream.truncate(1024**3)

    outcome = doctor._inspect(doctor._CheckTarget("app", "mobile", app, check))

    assert outcome.state == "incomplete"
    assert f"`{path}`: input exceeds 1048576-byte inspection limit" in outcome.message


def test_pods_reader_bounds_input_that_grows_after_metadata_check(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(checks, "_PODS_READ_LIMIT", 4)
    path = tmp_path / "Podfile.lock"
    path.write_bytes(b"12345")
    original = checks.os.fstat

    def smaller_metadata(fd):
        actual = original(fd)
        return SimpleNamespace(
            st_size=0, st_mode=actual.st_mode, st_ino=actual.st_ino, st_dev=actual.st_dev
        )

    monkeypatch.setattr(checks.os, "fstat", smaller_metadata)

    with pytest.raises(ValueError, match="exceeds 4-byte inspection limit"):
        checks._pods_bytes(path, tmp_path)


@pytest.mark.parametrize("fix", [False, True])
def test_doctor_pods_read_only_and_not_inside_repair_lock(tmp_path, monkeypatch, fix):
    from splashdown import doctor

    (tmp_path / sd.RECIPE_NAME).write_text(
        '[apps.mobile]\npath="."\nprofile="expo"\nresources=[]\n'
    )
    write_pods(tmp_path / "ios", manifest=None)
    monkeypatch.setattr(doctor, "_app_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "_project_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "tooling_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "checkout_operation_lock", lambda *_: pytest.fail("repair lock"))
    monkeypatch.setattr(
        checks.subprocess, "run", lambda *_a, **_k: pytest.fail("project execution")
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = doctor.run_doctor(tmp_path, fix=fix)

    outcome = next(row for row in result.data["outcomes"] if row["check_id"] == "pods-manifest")
    assert outcome["app"] == "mobile"
    assert outcome["state"] == "warning"
    assert outcome["repair"] == "none"
    assert outcome["changed_files"] == []
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_pods_unreadable_bundler_guidance_is_incomplete_in_doctor(tmp_path, monkeypatch):
    from splashdown import doctor

    (tmp_path / sd.RECIPE_NAME).write_text(
        '[apps.mobile]\npath="."\nprofile="expo"\nresources=[]\n'
    )
    write_pods(tmp_path / "ios", manifest=None)
    (tmp_path / "Gemfile").symlink_to(tmp_path / "missing")
    monkeypatch.setattr(doctor, "_app_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "_project_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "tooling_checks", lambda *_: [])
    result = doctor.run_doctor(tmp_path)
    outcome = next(row for row in result.data["outcomes"] if row["check_id"] == "pods-manifest")
    assert result.exit_code == 1
    assert outcome["state"] == "incomplete"
    assert "Guidance failed" in outcome["message"]
    assert "Gemfile" in outcome["message"]


def test_doctor_pods_continues_after_independent_tool_check_construction_failure(
    tmp_path, monkeypatch
):
    from splashdown import doctor

    (tmp_path / sd.RECIPE_NAME).write_text(
        '[apps.mobile]\npath="."\nprofile="expo"\nresources=[]\n'
    )
    write_pods(tmp_path / "ios")
    monkeypatch.setattr(doctor, "_app_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "_project_checks", lambda *_: [])

    def failing_tools(*_):
        raise ValueError("tool observation construction failed")

    monkeypatch.setattr(doctor, "tooling_checks", failing_tools)
    result = doctor.run_doctor(tmp_path)
    outcomes = {row["check_id"]: row for row in result.data["outcomes"]}
    assert outcomes["tool-check-construction"]["state"] == "incomplete"
    assert outcomes["pods-manifest"]["state"] == "ok"


def test_pods_reader_rejects_fifo_substituted_after_metadata_inspection(tmp_path, monkeypatch):
    import os

    path = tmp_path / "Podfile.lock"
    path.write_text("LOCK")
    original = checks.os.open

    def substitute(candidate, flags):
        assert flags & os.O_NONBLOCK
        path.unlink()
        os.mkfifo(path)
        return original(candidate, flags)

    monkeypatch.setattr(checks.os, "open", substitute)
    with pytest.raises(ValueError, match="not a regular file"):
        checks._pods_bytes(path, tmp_path)


def test_pods_ignores_source_dir_literal_outside_project_ios_slot(tmp_path):
    app, check = pods_fixture(tmp_path)
    (app / "react-native.config.js").write_text(
        "module.exports = {unrelated: {sourceDir: 'fake'}};"
    )
    write_pods(app / "ios")
    assert check.detect(app)[0] == "ok"
    assert str(app / "ios" / "Podfile.lock") in check.detect(app)[1]


@pytest.mark.parametrize("fix", [False, True])
@pytest.mark.parametrize("state", ["warning", "incomplete", "ok", "skipped"])
def test_doctor_watchman_preserves_project_state_and_existing_daemon(
    tmp_path, monkeypatch, capsys, existing_watchman, fix, state
):
    from splashdown import doctor

    (tmp_path / sd.RECIPE_NAME).write_text(
        '[apps.one]\npath="."\nprofile="expo"\nresources=[]\n'
        '[apps.two]\npath="."\nprofile="react-native"\nresources=[]\n'
    )
    state_home = tmp_path / "state"
    state_home.mkdir()
    (state_home / "daemon-sentinel").write_text("existing service and watches")
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setattr(doctor, "_hook_checks", lambda _: [])
    monkeypatch.setattr(checks, "watchman_available", lambda *_: state != "skipped")
    calls = []

    def query(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            1 if state == "incomplete" else 0,
            json.dumps({"roots": [str(tmp_path.parent)] if state == "warning" else []}),
            "permission denied" if state == "incomplete" else "",
        )

    monkeypatch.setattr(checks.subprocess, "run", query)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert doctor.cmd_doctor(tmp_path, fix=fix, output_format="json") == (
        1 if state == "incomplete" else 0
    )
    payload = json.loads(capsys.readouterr().out)
    rows = [row for row in payload["data"]["outcomes"] if row["check_id"] == "watchman-root"]
    assert len(rows) == 1
    assert rows[0]["state"] == state
    assert rows[0]["path"] == str(tmp_path)
    assert rows[0]["changed_files"] == []
    assert rows[0]["repair"] == "none"
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert len(calls) == (0 if state == "skipped" else 1)
    if calls:
        assert calls[0][0] == ["watchman", "--no-spawn", "--no-local", "watch-list"]
        assert calls[0][1]["cwd"] == tmp_path
        assert calls[0][1]["timeout"] == 3


@pytest.mark.parametrize("symlink", [False, True])
def test_watchman_refuses_checkout_controlled_executable(tmp_path, monkeypatch, symlink):
    executable = tmp_path / "watchman"
    if symlink:
        executable.symlink_to("/usr/bin/true")
    else:
        executable.write_text("#!/bin/sh\ntouch daemon-started\n")
        executable.chmod(0o755)
    monkeypatch.setattr(checks, "watchman_available", lambda *_: True)
    monkeypatch.setattr(checks.shutil, "which", lambda *_args, **_kwargs: str(executable))
    monkeypatch.setattr(
        checks.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("executed project code")
    )
    state, detail = checks.watchman_watch_root(tmp_path)
    assert state == "incomplete"
    assert "checkout-controlled" in detail
    assert list(tmp_path.iterdir()) == [executable]


@pytest.mark.parametrize(
    "diagnostic", ["permission denied", "connection refused", "daemon missing", "protocol error"]
)
def test_watchman_nonzero_does_not_guess_idle(tmp_path, monkeypatch, existing_watchman, diagnostic):
    monkeypatch.setattr(
        checks.subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 1, "", diagnostic),
    )
    state, detail = checks.watchman_watch_root(tmp_path)
    assert state == "incomplete"
    assert diagnostic in detail
    assert "daemon state and watch roots are unverified" in detail


@pytest.mark.parametrize("relative", ["", ".tools"])
def test_watchman_relative_path_is_resolved_at_checkout(tmp_path, monkeypatch, relative):
    directory = tmp_path / relative
    directory.mkdir(exist_ok=True)
    executable = directory / "watchman"
    executable.write_text("#!/bin/sh\ntouch daemon-started\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", relative)
    monkeypatch.setattr(checks, "watchman_available", lambda *_: True)
    monkeypatch.setattr(
        checks.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("executed project code")
    )
    state, detail = checks.watchman_watch_root(tmp_path)
    assert state == "incomplete"
    assert "checkout-controlled" in detail


@pytest.mark.parametrize("fix", [False, True])
@pytest.mark.parametrize("git_entry", ["directory", "file"])
@pytest.mark.parametrize("location", ["outer", "outer-link", "external-link"])
def test_doctor_nested_recipe_refuses_watchman_anywhere_in_enclosing_checkout(
    tmp_path, monkeypatch, fix, git_entry, location
):
    from splashdown import doctor

    checkout = tmp_path / "checkout"
    project = checkout / "apps/mobile"
    project.mkdir(parents=True)
    marker = checkout / ".git"
    if git_entry == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: /unused/worktrees/mobile\n")
    (project / sd.RECIPE_NAME).write_text(
        '[project]\nloader="none"\n[apps.mobile]\npath="."\nprofile="expo"\nresources=[]\n'
    )
    tools = checkout / "tools"
    tools.mkdir()
    executable = tools / "watchman"
    if location == "outer-link":
        executable.symlink_to("/usr/bin/true")
    else:
        executable.write_text("#!/bin/sh\ntouch daemon-started\n")
        executable.chmod(0o755)
    if location == "external-link":
        external = tmp_path / "external"
        external.mkdir()
        (external / "watchman").symlink_to(executable)
        tools = external
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(checks, "watchman_available", lambda *_: True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(doctor, "_hook_checks", lambda _: [])
    monkeypatch.setattr(doctor, "_app_checks", lambda *_: [])
    monkeypatch.setattr(doctor, "pods_check", lambda *_: None)
    monkeypatch.setattr(
        subprocess, "run", lambda *_args, **_kwargs: pytest.fail("executed project code")
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = doctor.run_doctor(project, fix=fix)
    outcome = next(row for row in result.data["outcomes"] if row["check_id"] == "watchman-root")
    assert result.exit_code == 1
    assert outcome["state"] == "incomplete"
    assert outcome["path"] == str(project)
    assert "checkout-controlled Watchman" in outcome["message"]
    assert outcome["repair"] == "none"
    assert outcome["changed_files"] == []
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
