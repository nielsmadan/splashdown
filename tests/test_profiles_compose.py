from __future__ import annotations

import os

import pytest

from splashdown import profiles_compose as compose


def detect(root, body):
    (root / "compose.yaml").write_text(body)
    return compose.compose_wiring_checks(root)[0].detect(root)


@pytest.mark.parametrize(
    "mount",
    [
        '"/srv/shared/db:/var/lib/db"',
        '"/srv/shared/db:/var/lib/db:ro"',
        "{type: bind, source: /srv/shared/db, target: /var/lib/db}",
        "type: bind\n        source: /srv/shared/db\n        target: /var/lib/db",
    ],
)
def test_absolute_mount_identifies_service_source_and_sharing(tmp_path, mount):
    state, detail = detect(tmp_path, f"services:\n  database:\n    volumes:\n      - {mount}\n")
    assert state == "warning"
    assert "compose.yaml: services.database.volumes[0].source = '/srv/shared/db'" in detail
    assert "even with distinct ports and COMPOSE_PROJECT_NAME values" in detail


@pytest.mark.parametrize(
    "mount",
    [
        "./data:/var/lib/db",
        "../data:/var/lib/db",
        "database:/var/lib/db",
        "/var/lib/db",
        "${DATA_DIR:-/srv/shared/db}:/var/lib/db",
        "{type: volume, source: database, target: /var/lib/db}",
        "{type: bind, source: ./data, target: /var/lib/db}",
        '{type: bind, source: "${DATA_DIR}", target: /var/lib/db}',
    ],
)
def test_relative_named_and_interpolated_mounts_are_not_fixed_absolute_sources(tmp_path, mount):
    state, detail = detect(tmp_path, f"services:\n  database:\n    volumes: [{mount}]\n")
    assert state == "ok"
    assert "interpolated values and runtime configuration are unverified" in detail


def test_flow_mapping_mount_and_unrelated_service_slots(tmp_path):
    state, detail = detect(
        tmp_path,
        'services: {api: {ports: ["8080:80"], volumes: ["/srv/api:/data"]}, db: {container_name: shared}}\n',
    )
    assert state == "warning"
    assert "services.api.ports[0]: fixed host port 8080" in detail
    assert "services.api.volumes[0].source = '/srv/api'" in detail
    assert "services.db.container_name = 'shared'" in detail


@pytest.mark.parametrize(
    "body, cause",
    [
        ("services: *services\n", "unrecognized mapping"),
        ("services:\n  db: *service\n", "services.db"),
        ("services:\n  db:\n    volumes: *mounts\n", "services.db.volumes"),
        ("services:\n  db:\n    volumes: [{type: bind, target: /data}]\n", "services.db.volumes"),
        ('services:\n  db:\n    ports: ["8080:80"\n', "unrecognized flow collection"),
        ("services:\n  db:\n    ports: [{target: *port}]\n", "unrecognized port"),
        ("services:\n  db:\n    container_name: *name\n", "unrecognized scalar"),
        ("services:\n  db:\n    extends: other.yaml\n", "inherited service"),
        ("services:\n  db:\n    <<: *base\n", "unrecognized mapping"),
        ('services:\n  db:\n    "ports": ["8080:80"]\n', "unrecognized mapping"),
        ("services: {}\ninclude: other.yaml\n", "included Compose"),
        ("hello YAML\n", "unrecognized mapping"),
    ],
)
def test_unknown_or_malformed_yaml_is_incomplete(tmp_path, body, cause):
    state, detail = detect(tmp_path, body)
    assert state == "incomplete"
    assert cause in detail


def test_independent_service_warning_survives_unknown_service(tmp_path):
    state, detail = detect(tmp_path, 'services:\n  db: *unknown\n  api: {ports: ["8080:80"]}\n')
    assert state == "incomplete"
    assert "services.db" in detail
    assert "services.api.ports[0]: fixed host port 8080" in detail


@pytest.mark.parametrize("kind", ["fifo", "dangling", "directory"])
def test_nonregular_compose_is_detected_without_opening_or_following(tmp_path, kind):
    path = tmp_path / "compose.yaml"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "dangling":
        path.symlink_to(tmp_path / "missing")
    else:
        path.mkdir()
    check = compose.compose_wiring_checks(tmp_path)[0]
    assert check.applies(tmp_path)
    state, detail = check.detect(tmp_path)
    assert state == "incomplete"
    assert "symlink" in detail if kind == "dangling" else "not a regular file" in detail


@pytest.mark.parametrize("declared", [False, True])
def test_guidance_uses_recipe_declarations_without_allocated_or_parent_shell_claims(
    tmp_path, declared
):
    (tmp_path / "compose.yaml").write_text('services: {db: {ports: ["5432:5432"]}}\n')
    (tmp_path / "splashdown.toml").write_text(
        '[project]\nloader = "none"\n'
        + (
            '[resources.ODD_NAME]\ntype = "port"\nrange = [4500, 4600]\n'
            '[resources.COMPOSE_PROJECT_NAME]\ntype = "template"\ntemplate = "checkout"\n'
            if declared
            else ""
        )
    )
    guidance = compose._compose_hardcoded_manual(tmp_path)
    assert (
        f"Declared port resources: {'ODD_NAME' if declared else 'none'}; stored allocations were not inspected."
        in guidance
    )
    assert f"COMPOSE_PROJECT_NAME is {'declared' if declared else 'not declared'}" in guidance
    assert "does not verify the invoking shell environment" in guidance
    assert "No service-to-resource association was inferred" in guidance
    assert "Intentional sharing" in guidance or "intentional sharing" in guidance


def test_empty_services_is_only_a_static_slot_observation(tmp_path):
    assert detect(tmp_path, "services: {}\n")[0] == "ok"


@pytest.mark.parametrize("slot", ["ports", "volumes"])
def test_empty_block_item_is_incomplete(tmp_path, slot):
    state, detail = detect(tmp_path, f"services:\n  db:\n    {slot}:\n      -\n")
    assert state == "incomplete"
    assert "empty sequence item" in detail


@pytest.mark.parametrize(
    "body",
    [
        'services:{db: {ports: ["8080:80"]}}\n',
        'services:\n  db:\n    ports:["8080:80"]\n',
        "services:\n  db:\n    container_name: null\n",
        'services:\n  db:\n    container_name: ""\n',
        "services:\n  db:\n    container_name: name: bad\n",
    ],
)
def test_malformed_mapping_or_scalar_does_not_establish_a_completed_inspection(tmp_path, body):
    state, detail = detect(tmp_path, body)
    assert state == "incomplete"
    assert "compose.yaml" in detail
