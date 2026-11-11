from __future__ import annotations

import json
import subprocess
import threading

import pytest

from splashdown import device_android as android
from splashdown import device_discovery as discovery
from splashdown import device_ios as ios
from splashdown.device_types import (
    AndroidDestination,
    IOSDestination,
    PhysicalObservation,
    as_launch_destination,
)
from splashdown.errors import CapabilityError, DeviceError


def adb_inventory(monkeypatch, rows, identities):
    calls = []
    monkeypatch.setattr(android, "_android_bin", lambda name: "/fake/adb")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1:] == ["devices", "-l"]:
            return ("List of devices attached\n" + "\n".join(rows) + "\n").encode()
        value = identities[(argv[1], argv[2], argv[-1])]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(android, "check_output_finite", run)
    return calls


def iphone(udid="PHONE", *, pairing="paired", tunnel="disconnected", name="iPhone"):
    return {
        "hardwareProperties": {"platform": "iOS", "udid": udid},
        "deviceProperties": {"name": name},
        "connectionProperties": {"pairingState": pairing, "tunnelState": tunnel},
    }


def test_android_uses_hardware_identity_and_current_wifi_endpoint(monkeypatch):
    calls = adb_inventory(
        monkeypatch,
        ["192.0.2.4:4567 device model:Pixel_9 transport_id:12"],
        {("-t", "12", "ro.serialno"): b"PHONE-A\n"},
    )
    observed = android._android_physical_observations()
    assert observed == [
        PhysicalObservation(
            "android",
            "Pixel_9",
            "PHONE-A",
            "192.0.2.4:4567",
            "ready",
            "",
            True,
            adb_transport_id="12",
            connection_type="network",
        )
    ]
    assert calls[1][0] == ["/fake/adb", "-t", "12", "shell", "getprop", "ro.serialno"]
    assert 0 < calls[1][1]["timeout"] <= 2
    assert android._android_physical_devices() == [
        {
            "id": "PHONE-A",
            "name": "Pixel_9",
            "platform": "android",
            "transport_id": "192.0.2.4:4567",
            "adb_transport_id": "12",
            "connection_type": "network",
        }
    ]


def test_android_usb_is_metadata_based_and_preferred(monkeypatch):
    adb_inventory(
        monkeypatch,
        [
            "PHONE-A device usb:1-1 model:Pixel_9 transport_id:1",
            "wifi-endpoint device model:Pixel_9 transport_id:2",
        ],
        {("-t", "1", "ro.serialno"): b"PHONE-A\n", ("-t", "2", "ro.serialno"): b"PHONE-A\n"},
    )
    observed = android._android_physical_observations()
    assert len(observed) == 1
    assert observed[0].transport_id == "PHONE-A"
    assert observed[0].selectable
    assert observed[0].connection_type == "usb"


@pytest.mark.parametrize(
    "endpoint,metadata,expected",
    [
        ("PHONE-A", "usb:1-1", "usb"),
        ("192.0.2.4:4567", "usb:1-1", "usb"),
        ("192.0.2.4:4567", "", "network"),
        ("[2001:db8::1]:4567", "", "network"),
        ("adb-PHONE-A (2)._adb-tls-connect._tcp.", "", "network"),
        ("PHONE-A", "", None),
    ],
)
def test_android_discovery_reports_only_known_connection_types(
    monkeypatch, endpoint, metadata, expected
):
    adb_inventory(
        monkeypatch,
        [f"{endpoint} device {metadata} model:Pixel transport_id:1"],
        {("-t", "1", "ro.serialno"): b"PHONE-A"},
    )
    observed = android._android_physical_observations()[0]
    assert observed.selectable
    assert observed.connection_type == expected
    assert android._android_physical_devices()[0].get("connection_type") == expected


@pytest.mark.parametrize("transport", [True, False])
def test_android_mdns_name_with_spaces_keeps_its_connection_state(monkeypatch, transport):
    endpoint = "adb-PHONE-A-random (2)._adb-tls-connect._tcp"
    selector = ("-t", "12") if transport else ("-s", endpoint)
    calls = adb_inventory(
        monkeypatch,
        [f"{endpoint} device model:Pixel_9" + (" transport_id:12" if transport else "")],
        {(*selector, "ro.serialno"): b"PHONE-A\n"},
    )
    observed = android._android_physical_observations()
    assert [(item.hardware_id, item.transport_id, item.state) for item in observed] == [
        ("PHONE-A", endpoint, "ready")
    ]
    assert observed[0].selectable
    assert calls[1][0] == ["/fake/adb", *selector, "shell", "getprop", "ro.serialno"]


@pytest.mark.parametrize("reverse", [False, True])
def test_android_verified_wifi_aliases_select_one_stable_connection(monkeypatch, reverse):
    rows = [
        "adb-PHONE-A-random (2)._adb-tls-connect._tcp device model:Galaxy transport_id:3",
        "adb-PHONE-A-random._adb-tls-connect._tcp device model:Galaxy transport_id:2",
        "192.0.2.4:4567 device model:Galaxy transport_id:1",
    ]
    adb_inventory(
        monkeypatch,
        list(reversed(rows)) if reverse else rows,
        {("-t", str(token), "ro.serialno"): b"PHONE-A\n" for token in (1, 2, 3)},
    )
    observed = android._android_physical_observations()
    assert len(observed) == 1
    assert observed[0].hardware_id == "PHONE-A"
    assert observed[0].transport_id == "192.0.2.4:4567"
    assert observed[0].adb_transport_id == "1"
    assert observed[0].selectable


def test_android_wifi_aliases_with_conflicting_models_remain_blocked(monkeypatch):
    adb_inventory(
        monkeypatch,
        [
            "192.0.2.4:4567 device model:Pixel transport_id:1",
            "adb-PHONE-A-random._adb-tls-connect._tcp device model:Galaxy transport_id:2",
        ],
        {("-t", str(token), "ro.serialno"): b"PHONE-A\n" for token in (1, 2)},
    )
    observed = android._android_physical_observations()
    assert len(observed) == 1
    assert observed[0].state == "ambiguous"
    assert observed[0].selectable is False


@pytest.mark.parametrize(
    "label,expected",
    [
        (b"Xiaomi\n\0Redmi A5\n\0Redmi A5\n\0", "Xiaomi Redmi A5"),
        (b"samsung\n\0Niels's A15\n\0\0", "Samsung Niels's A15"),
        (b"HONOR\n\0null\n\0\0HONOR Test Phone\n", "HONOR Test Phone"),
        (b"Google\n\0\0\0", "Google Pixel 9"),
        (b"unparseable", "Pixel_9"),
        (DeviceError("name query timed out"), "Pixel_9"),
    ],
)
def test_onboarding_android_names_are_optional_and_preserve_hardware_identity(
    monkeypatch, label, expected
):
    calls = adb_inventory(
        monkeypatch,
        ["192.0.2.4:4567 device model:Pixel_9 transport_id:12"],
        {
            ("-t", "12", "ro.serialno"): b"PHONE-A\n",
            ("-t", "12", android._PHONE_LABEL_COMMAND): label,
        },
    )
    observed = android._android_physical_observations(include_names=True)
    assert [
        (item.hardware_id, item.name, item.transport_id, item.selectable) for item in observed
    ] == [("PHONE-A", expected, "192.0.2.4:4567", True)]
    assert len(calls) == 3
    assert 0 < calls[-1][1]["timeout"] <= 2


def test_android_does_not_prefer_unusable_usb_over_wifi(monkeypatch):
    calls = adb_inventory(
        monkeypatch,
        [
            "PHONE-A unauthorized usb:1-1 model:Pixel_9 transport_id:1",
            "wifi device model:Pixel_9 transport_id:2",
        ],
        {("-t", "2", "ro.serialno"): b"PHONE-A\n"},
    )
    observed = android._android_physical_observations()
    assert [(item.transport_id, item.state, item.selectable) for item in observed] == [
        ("PHONE-A", "unauthorized", False),
        ("wifi", "ready", True),
    ]
    assert len(calls) == 2


@pytest.mark.parametrize("usb", ["", "usb:1-1"])
def test_android_unresolved_duplicate_connections_are_ambiguous(monkeypatch, usb):
    adb_inventory(
        monkeypatch,
        [
            f"first device {usb} model:Pixel transport_id:1",
            f"second device {usb} model:Pixel transport_id:2",
        ],
        {("-t", "1", "ro.serialno"): b"PHONE-A", ("-t", "2", "ro.serialno"): b"PHONE-A"},
    )
    observed = android._android_physical_observations()
    assert len(observed) == 1
    assert observed[0].state == "ambiguous"
    assert observed[0].selectable is False
    assert "first, second" in observed[0].detail
    records = android._android_physical_devices()
    assert records[0]["id"] == "PHONE-A"
    assert "ambiguous" in records[0]["error"]


def test_android_conflicting_models_do_not_get_usb_preference(monkeypatch):
    adb_inventory(
        monkeypatch,
        [
            "first device usb:1-1 model:Pixel transport_id:1",
            "second device model:Galaxy transport_id:2",
        ],
        {("-t", "1", "ro.serialno"): b"PHONE-A", ("-t", "2", "ro.serialno"): b"PHONE-A"},
    )
    assert android._android_physical_observations()[0].state == "ambiguous"


def test_android_ready_device_survives_an_unrelated_ambiguous_identity(monkeypatch):
    adb_inventory(
        monkeypatch,
        [
            "first device model:Pixel transport_id:1",
            "second device model:Pixel transport_id:2",
            "PHONE-B device usb:1-2 model:Other transport_id:3",
        ],
        {
            ("-t", "1", "ro.serialno"): b"PHONE-A",
            ("-t", "2", "ro.serialno"): b"PHONE-A",
            ("-t", "3", "ro.serialno"): b"PHONE-B",
        },
    )
    records = android._android_physical_devices()
    assert records[0]["id"] == "PHONE-A"
    assert "ambiguous" in records[0]["error"]
    assert records[1] == {
        "id": "PHONE-B",
        "name": "Other",
        "platform": "android",
        "adb_transport_id": "3",
        "connection_type": "usb",
    }


def test_android_blocked_states_are_retained_and_emulators_excluded(monkeypatch):
    calls = adb_inventory(
        monkeypatch,
        [
            "PHONE-A unauthorized",
            "PHONE-B offline",
            "PHONE-C no permissions (check udev rules)",
            "emulator-5554 device",
            "PHONE-D recovery",
        ],
        {},
    )
    observed = android._android_physical_observations()
    assert [(item.transport_id, item.state) for item in observed] == [
        ("PHONE-A", "unauthorized"),
        ("PHONE-B", "offline"),
        ("PHONE-C", "no_permissions"),
        ("PHONE-D", "recovery"),
    ]
    assert all(not item.selectable and item.hardware_id is None for item in observed)
    assert "authorization prompt" in observed[0].detail
    assert "offline" in observed[1].detail
    assert len(calls) == 1


@pytest.mark.parametrize(
    "detail",
    [
        "no permissions; see [http://developer.android.com/tools/device.html]",
        "no permissions (user is not in the plugdev group); see [http://developer.android.com/tools/device.html]",
    ],
)
def test_android_permission_errors_offer_usb_permissions_guidance(monkeypatch, detail):
    calls = adb_inventory(monkeypatch, [f"PHONE-A {detail}"], {})
    observed = android._android_physical_observations()
    assert [(item.transport_id, item.state, item.selectable) for item in observed] == [
        ("PHONE-A", "no_permissions", False)
    ]
    assert observed[0].detail == (
        "The host cannot access this device. Check Android USB permissions, then reconnect and rescan."
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "invalid",
    [b"", b"unknown\n", b"null", b"000000", b"192.0.2.1:1234", b"one\ntwo", b"0123456789ABCDEF"],
)
def test_android_invalid_serials_never_fall_back_to_endpoint(monkeypatch, invalid):
    adb_inventory(
        monkeypatch,
        ["PHONE-A device"],
        {("-s", "PHONE-A", "ro.serialno"): invalid, ("-s", "PHONE-A", "ro.boot.serialno"): invalid},
    )
    observed = android._android_physical_observations()[0]
    assert observed.state == "identity_unavailable"
    assert observed.hardware_id is None
    assert observed.selectable is False
    assert "hardware serial" in observed.detail
    assert "USB" in observed.detail


def test_android_falls_back_to_boot_serial_property(monkeypatch):
    calls = adb_inventory(
        monkeypatch,
        ["USB-ENDPOINT device usb:1-1"],
        {
            ("-s", "USB-ENDPOINT", "ro.serialno"): b"unknown",
            ("-s", "USB-ENDPOINT", "ro.boot.serialno"): b"PHONE-A\n",
        },
    )
    observed = android._android_physical_observations()[0]
    assert observed.hardware_id == "PHONE-A"
    assert observed.selectable
    assert [call[0][-1] for call in calls[1:]] == ["ro.serialno", "ro.boot.serialno"]


def test_android_probe_failure_keeps_other_devices(monkeypatch):
    adb_inventory(
        monkeypatch,
        ["A device", "B device"],
        {
            ("-s", "A", "ro.serialno"): DeviceError("identity query timed out"),
            ("-s", "B", "ro.serialno"): b"PHONE-B",
        },
    )
    observed = android._android_physical_observations()
    assert [(item.state, item.hardware_id) for item in observed] == [
        ("identity_unavailable", None),
        ("ready", "PHONE-B"),
    ]
    assert "timed out" in observed[0].detail


def test_android_identity_probes_share_discovery_budget(monkeypatch):
    calls = []
    now = [10.0]
    monkeypatch.setattr(android, "_android_bin", lambda name: "/fake/adb")
    monkeypatch.setattr(android.time, "monotonic", lambda: now[0])

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1:] == ["devices", "-l"]:
            now[0] = 10.5
            return b"List of devices attached\nA device\nB device\n"
        return f"PHONE-{argv[2]}".encode()

    monkeypatch.setattr(android, "check_output_finite", run)
    observed = android._android_physical_observations(timeout=1)
    assert [(item.state, item.hardware_id) for item in observed] == [
        ("ready", "PHONE-A"),
        ("ready", "PHONE-B"),
    ]
    assert calls[0][1]["timeout"] == 1
    assert [kwargs["timeout"] for _, kwargs in calls[1:]] == [0.5, 0.5]


def test_android_expired_discovery_budget_keeps_unresolved_rows(monkeypatch):
    calls = []
    now = [10.0]
    monkeypatch.setattr(android, "_android_bin", lambda name: "/fake/adb")
    monkeypatch.setattr(android.time, "monotonic", lambda: now[0])

    def run(argv, **kwargs):
        calls.append(argv)
        now[0] = 11.1
        return b"List of devices attached\nA device\nB device\n"

    monkeypatch.setattr(android, "check_output_finite", run)
    observed = android._android_physical_observations(timeout=1)
    assert [(item.transport_id, item.state) for item in observed] == [
        ("A", "identity_unavailable"),
        ("B", "identity_unavailable"),
    ]
    assert all("timed out" in item.detail for item in observed)
    assert calls == [["/fake/adb", "devices", "-l"]]


def test_android_responsive_phone_is_probed_while_slow_peers_are_pending(monkeypatch):
    responsive_probed = threading.Event()
    monkeypatch.setattr(android, "_android_bin", lambda name: "/fake/adb")

    def run(argv, **kwargs):
        if argv[1:] == ["devices", "-l"]:
            return b"List of devices attached\nA device\nB device\nC device\nD device usb:1-1\n"
        if argv[2] == "D":
            responsive_probed.set()
            return b"PHONE-D"
        assert responsive_probed.wait(2), "responsive phone waited behind slow identity queries"
        raise DeviceError("identity query timed out")

    monkeypatch.setattr(android, "check_output_finite", run)
    observed = android._android_physical_observations(timeout=5)
    assert [(item.transport_id, item.state, item.hardware_id) for item in observed] == [
        ("A", "identity_unavailable", None),
        ("B", "identity_unavailable", None),
        ("C", "identity_unavailable", None),
        ("D", "ready", "PHONE-D"),
    ]


def test_android_identity_queries_limit_parallel_tool_processes(monkeypatch):
    batch = threading.Barrier(4, timeout=2)
    lock = threading.Lock()
    active = 0
    peak = 0
    monkeypatch.setattr(android, "_android_bin", lambda name: "/fake/adb")

    def run(argv, **kwargs):
        nonlocal active, peak
        if argv[1:] == ["devices", "-l"]:
            rows = "\n".join(f"PHONE-{index} device" for index in range(12))
            return f"List of devices attached\n{rows}\n".encode()
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            batch.wait()
            return argv[2].encode()
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(android, "check_output_finite", run)
    observed = android._android_physical_observations()
    assert [item.hardware_id for item in observed] == [f"PHONE-{index}" for index in range(12)]
    assert peak == 4


@pytest.mark.parametrize("output", [b"", b"daemon failed\n", b"List of devices attached\nPHONE\n"])
def test_android_malformed_inventory_is_an_inspection_error(monkeypatch, output):
    monkeypatch.setattr(android, "_android_bin", lambda name: "/fake/adb")
    monkeypatch.setattr(android, "check_output_finite", lambda *a, **kw: output)
    with pytest.raises(DeviceError, match="could not parse adb"):
        android._android_physical_observations()


def test_android_listing_failure_is_not_an_empty_inventory(monkeypatch):
    monkeypatch.setattr(android, "_android_bin", lambda name: "/fake/adb")

    def run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(android, "check_output_finite", run)
    with pytest.raises(DeviceError, match="adb devices failed"):
        android._android_physical_observations()


def test_ios_preserves_pairing_states_and_idle_tunnels(monkeypatch):
    devices = [
        iphone("READY"),
        iphone("UNPAIRED", pairing="unpaired"),
        iphone("GONE", tunnel="unavailable"),
        iphone("PENDING", tunnel="new-state"),
    ]
    monkeypatch.setattr(ios, "_devicectl_json", lambda *a, **kw: {"result": {"devices": devices}})
    observed = ios._ios_physical_observations()
    assert [(item.hardware_id, item.state, item.selectable) for item in observed] == [
        ("READY", "ready", True),
        ("UNPAIRED", "unpaired", False),
        ("GONE", "unavailable", False),
        ("PENDING", "connection_unknown", False),
    ]
    assert ios._ios_physical_devices() == [{"id": "READY", "name": "iPhone", "platform": "ios"}]


@pytest.mark.parametrize("current_schema", [False, True])
@pytest.mark.parametrize("tunnel", ["connected", "disconnected"])
@pytest.mark.parametrize(
    "transport,expected",
    [
        ("wired", "usb"),
        ("localNetwork", "network"),
        ("future", None),
        ("", None),
        (None, None),
        (False, None),
        (42, None),
    ],
)
def test_ios_connection_type_survives_discovery(
    monkeypatch, current_schema, tunnel, transport, expected
):
    device = iphone(tunnel=tunnel)
    device["connectionProperties"]["transportType"] = transport
    if current_schema:
        device = {
            "properties": {
                "hardware": device["hardwareProperties"],
                "state": device["deviceProperties"],
                "connection": {
                    "pairingState": "paired",
                    "state": tunnel,
                    "transportType": transport,
                },
            }
        }
    monkeypatch.setattr(ios, "_devicectl_json", lambda *a, **kw: {"result": {"devices": [device]}})

    observed = ios._ios_physical_observations()[0]
    assert observed.selectable
    assert observed.connection_type == expected
    assert ios._ios_physical_devices()[0].get("connection_type") == expected


@pytest.mark.parametrize("current_schema", [False, True])
def test_ios_physical_discovery_excludes_simulators(monkeypatch, current_schema):
    devices = [iphone("REAL", name="Test iPhone"), iphone("SIM", name="iPhone simulator")]
    for device, reality in zip(devices, ["physical", "simulated"], strict=True):
        device["hardwareProperties"]["reality"] = reality
    if current_schema:
        devices = [
            {
                "properties": {
                    "hardware": device["hardwareProperties"],
                    "state": device["deviceProperties"],
                    "connection": {"pairingState": "paired", "state": "disconnected"},
                }
            }
            for device in devices
        ]
    monkeypatch.setattr(ios, "_devicectl_json", lambda *a, **kw: {"result": {"devices": devices}})
    observed = ios._ios_physical_observations()
    assert [(item.hardware_id, item.name, item.selectable) for item in observed] == [
        ("REAL", "Test iPhone", True)
    ]
    assert ios._ios_physical_devices() == [{"id": "REAL", "name": "Test iPhone", "platform": "ios"}]


def test_ios_unknown_device_kind_is_not_selectable(monkeypatch):
    device = iphone()
    device["hardwareProperties"]["reality"] = "new-kind"
    monkeypatch.setattr(ios, "_devicectl_json", lambda *a, **kw: {"result": {"devices": [device]}})
    observed = ios._ios_physical_observations()[0]
    assert observed.state == "device_kind_unknown"
    assert "new-kind" in observed.detail
    assert observed.selectable is False


def test_ios_deduplicates_udid_and_uses_available_record(monkeypatch):
    devices = [
        iphone(tunnel="unavailable"),
        iphone(name="Ready iPhone"),
        iphone(tunnel="connected"),
    ]
    monkeypatch.setattr(ios, "_devicectl_json", lambda *a, **kw: {"result": {"devices": devices}})
    observed = ios._ios_physical_observations()
    assert len(observed) == 1
    assert observed[0].hardware_id == "PHONE"
    assert observed[0].name == "Ready iPhone"
    assert observed[0].selectable


def test_ios_does_not_save_coredevice_identifier_as_hardware_id(monkeypatch):
    device = {**iphone(udid=""), "identifier": "CORE-DEVICE-ID"}
    monkeypatch.setattr(ios, "_devicectl_json", lambda *a, **kw: {"result": {"devices": [device]}})
    observed = ios._ios_physical_observations()[0]
    assert observed.hardware_id is None
    assert observed.state == "identity_unavailable"
    assert observed.selectable is False


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"result": []},
        {"result": {}},
        {"result": {"devices": {}}},
        {"result": {"devices": [None]}},
    ],
)
def test_ios_malformed_inventory_is_an_inspection_error(monkeypatch, payload):
    monkeypatch.setattr(ios, "_devicectl_json", lambda *a, **kw: payload)
    with pytest.raises(DeviceError, match="could not parse devicectl"):
        ios._ios_physical_observations()


def test_discovery_reports_missing_sdk_and_keeps_other_platform(monkeypatch):
    phone = PhysicalObservation("ios", "iPhone", "UDID", "UDID", "ready", "", True)
    monkeypatch.setattr(discovery.sys, "platform", "darwin")
    monkeypatch.setattr(discovery, "_ios_physical_observations", lambda **kw: [phone])

    def unavailable(**kwargs):
        raise CapabilityError("android", "Android SDK not found; set ANDROID_HOME")

    monkeypatch.setattr(discovery, "_android_physical_observations", unavailable)
    snapshot = discovery.discover_for_onboarding()
    assert snapshot.devices == (phone,)
    assert snapshot.issues == ("android: Android SDK not found; set ANDROID_HOME",)


def test_discovery_on_linux_only_probes_android_by_default(monkeypatch):
    monkeypatch.setattr(discovery.sys, "platform", "linux")
    calls = []

    def android_probe(**kwargs):
        calls.append("android")
        return []

    def ios_probe(**kwargs):
        calls.append("ios")
        raise CapabilityError("ios", "iOS physical-device support requires macOS and Xcode")

    monkeypatch.setattr(discovery, "_android_physical_observations", android_probe)
    monkeypatch.setattr(discovery, "_ios_physical_observations", ios_probe)
    assert discovery.discover_for_onboarding().issues == ()
    assert calls == ["android"]
    snapshot = discovery.discover_for_onboarding("ios")
    assert snapshot.issues == ("ios: iOS physical-device support requires macOS and Xcode",)


def test_discovery_reports_probe_timeout(monkeypatch):
    def failed(**kwargs):
        raise DeviceError("adb devices timed out after 30s")

    monkeypatch.setattr(discovery, "_android_physical_observations", failed)
    assert discovery.discover_for_onboarding("android").issues == (
        "android: adb devices timed out after 30s",
    )


def test_discovery_queries_platforms_concurrently(monkeypatch):
    barrier = threading.Barrier(2, timeout=2)
    monkeypatch.setattr(discovery.sys, "platform", "darwin")

    def probe(**kwargs):
        barrier.wait()
        assert kwargs["timeout"] == 4
        return []

    monkeypatch.setattr(discovery, "_ios_physical_observations", probe)
    monkeypatch.setattr(discovery, "_android_physical_observations", probe)
    snapshot = discovery.discover_for_onboarding(timeout=4)
    assert snapshot.devices == ()
    assert snapshot.issues == ()


@pytest.mark.parametrize("udid", ["unknown", "null", "00000000-00000000", "---", "a\nb"])
def test_ios_invalid_hardware_id_is_not_selectable(monkeypatch, udid):
    monkeypatch.setattr(
        ios, "_devicectl_json", lambda *a, **kw: {"result": {"devices": [iphone(udid)]}}
    )
    observed = ios._ios_physical_observations()[0]
    assert observed.hardware_id is None
    assert observed.selectable is False
    assert observed.state == "identity_unavailable"


def test_android_destination_keeps_mapping_and_optional_transport_token():
    destination = AndroidDestination("Pixel", "wifi:1234", False, "12")
    assert dict(destination) == {
        "kind": "android",
        "name": "Pixel",
        "physical": True,
        "serial": "wifi:1234",
    }
    assert AndroidDestination("Pixel", "PHONE", False).adb_transport_id is None
    converted = as_launch_destination({**dict(destination), "adb_transport_id": "12"})
    assert isinstance(converted, AndroidDestination)
    assert converted.adb_transport_id == "12"


def test_verify_android_checks_original_token_and_current_endpoint(monkeypatch):
    calls = adb_inventory(
        monkeypatch,
        [],
        {
            ("-t", "12", "ro.serialno"): b"PHONE-A",
            ("-s", "wifi:1234", "ro.serialno"): b"PHONE-A",
        },
    )
    android.verify_android_connection(
        AndroidDestination("Pixel", "wifi:1234", False, "12"), "PHONE-A"
    )
    assert [argv[1:3] for argv, _ in calls] == [["-t", "12"], ["-s", "wifi:1234"]]


def test_verify_android_without_token_checks_endpoint_only(monkeypatch):
    calls = adb_inventory(monkeypatch, [], {("-s", "PHONE-A", "ro.serialno"): b"PHONE-A"})
    android.verify_android_connection(AndroidDestination("Pixel", "PHONE-A", False), "PHONE-A")
    assert [argv[1:] for argv, _ in calls] == [["-s", "PHONE-A", "shell", "getprop", "ro.serialno"]]


def test_verify_android_fallback_property_keeps_selected_connection(monkeypatch):
    calls = adb_inventory(
        monkeypatch,
        [],
        {
            ("-s", "wifi:1234", "ro.serialno"): b"unknown",
            ("-s", "wifi:1234", "ro.boot.serialno"): b"PHONE-A",
        },
    )
    android.verify_android_connection(AndroidDestination("Pixel", "wifi:1234", False), "PHONE-A")
    assert [argv[1:] for argv, _ in calls] == [
        ["-s", "wifi:1234", "shell", "getprop", "ro.serialno"],
        ["-s", "wifi:1234", "shell", "getprop", "ro.boot.serialno"],
    ]


def test_verify_android_reused_token_stops_before_endpoint(monkeypatch):
    calls = adb_inventory(monkeypatch, [], {("-t", "12", "ro.serialno"): b"OTHER-PHONE"})
    with pytest.raises(DeviceError, match="identifies OTHER-PHONE, expected PHONE-A"):
        android.verify_android_connection(
            AndroidDestination("Pixel", "wifi:1234", False, "12"), "PHONE-A"
        )
    assert [argv[1:3] for argv, _ in calls] == [["-t", "12"]]


def test_verify_android_reused_endpoint_fails_after_token_check(monkeypatch):
    calls = adb_inventory(
        monkeypatch,
        [],
        {
            ("-t", "12", "ro.serialno"): b"PHONE-A",
            ("-s", "wifi:1234", "ro.serialno"): b"OTHER-PHONE",
        },
    )
    with pytest.raises(DeviceError, match="identifies OTHER-PHONE, expected PHONE-A"):
        android.verify_android_connection(
            AndroidDestination("Pixel", "wifi:1234", False, "12"), "PHONE-A"
        )
    assert [argv[1:3] for argv, _ in calls] == [["-t", "12"], ["-s", "wifi:1234"]]


def test_verify_android_disconnected_token_never_falls_back_to_endpoint(monkeypatch):
    calls = adb_inventory(
        monkeypatch,
        [],
        {
            ("-t", "12", "ro.serialno"): subprocess.CalledProcessError(
                1, "adb", stderr=b"transport not found"
            ),
        },
    )
    with pytest.raises(DeviceError, match=r"transport not found.*Reconnect the intended phone"):
        android.verify_android_connection(
            AndroidDestination("Pixel", "wifi:1234", False, "12"), "PHONE-A"
        )
    assert [argv[1:3] for argv, _ in calls] == [["-t", "12"]]


def test_verify_android_two_selectors_share_one_deadline(monkeypatch):
    calls = adb_inventory(monkeypatch, [], {("-t", "12", "ro.serialno"): b"PHONE-A"})
    times = iter([10.0, 10.4, 11.1])
    monkeypatch.setattr(android.time, "monotonic", lambda: next(times))
    with pytest.raises(DeviceError, match="timed out"):
        android.verify_android_connection(
            AndroidDestination("Pixel", "wifi:1234", False, "12"), "PHONE-A", timeout=1
        )
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == pytest.approx(0.6)


def test_verify_android_subprocess_timeout_is_actionable(monkeypatch):
    monkeypatch.setattr(android, "_android_bin", lambda name: "/fake/adb")
    calls = []

    def expired(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(android.subprocess, "check_output", expired)
    with pytest.raises(DeviceError, match=r"timed out.*Reconnect the intended phone"):
        android.verify_android_connection(
            AndroidDestination("Pixel", "wifi:1234", False), "PHONE-A"
        )
    assert calls == [["/fake/adb", "-s", "wifi:1234", "shell", "getprop", "ro.serialno"]]


def ios_inventory(monkeypatch, devices):
    calls = []
    monkeypatch.setattr(ios, "require_macos", lambda operation: None)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return json.dumps({"result": {"devices": devices}}).encode()

    monkeypatch.setattr(ios, "check_output_finite", run)
    return calls


@pytest.mark.parametrize("tunnel", ["connected", "disconnected"])
def test_verify_ios_accepts_paired_current_device_and_idle_tunnel(monkeypatch, tunnel):
    calls = ios_inventory(monkeypatch, [iphone("PHONE-A", tunnel=tunnel)])
    ios.verify_ios_connection(IOSDestination("iPhone", "PHONE-A", False), "PHONE-A", timeout=4)
    assert calls[0][0] == ["xcrun", "devicectl", "list", "devices", "--json-output", "-"]
    assert calls[0][1]["timeout"] == 4


@pytest.mark.parametrize(
    "devices,detail",
    [
        ([], "no longer lists"),
        ([iphone("OTHER-PHONE")], "no longer lists"),
        ([iphone("PHONE-A", pairing="unpaired")], "finish pairing"),
        ([iphone("PHONE-A", tunnel="unavailable")], "unavailable"),
    ],
)
def test_verify_ios_rejects_missing_changed_or_blocked_device(monkeypatch, devices, detail):
    calls = ios_inventory(monkeypatch, devices)
    with pytest.raises(DeviceError, match=detail):
        ios.verify_ios_connection(IOSDestination("iPhone", "PHONE-A", False), "PHONE-A")
    assert len(calls) == 1


def test_verify_ios_expected_identity_mismatch_stops_before_discovery(monkeypatch):
    monkeypatch.setattr(
        ios, "_ios_physical_observations", lambda **kwargs: pytest.fail("discovery reached")
    )
    with pytest.raises(DeviceError, match="does not match"):
        ios.verify_ios_connection(IOSDestination("iPhone", "OTHER-PHONE", False), "PHONE-A")


def test_verify_ios_subprocess_timeout_is_actionable(monkeypatch):
    monkeypatch.setattr(ios, "require_macos", lambda operation: None)
    calls = []

    def expired(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(ios.subprocess, "check_output", expired)
    with pytest.raises(DeviceError, match=r"timed out.*Check its connection in Xcode"):
        ios.verify_ios_connection(IOSDestination("iPhone", "PHONE-A", False), "PHONE-A", timeout=4)
    assert calls == [["xcrun", "devicectl", "list", "devices", "--json-output", "-"]]
