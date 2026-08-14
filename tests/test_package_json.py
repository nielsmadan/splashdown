from splashdown.package_json import package_dependencies, read_package_json


def test_read_package_json_returns_object(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"demo"}')

    assert read_package_json(tmp_path) == {"name": "demo"}


def test_read_package_json_rejects_non_object(tmp_path):
    (tmp_path / "package.json").write_text('["electron"]')

    assert read_package_json(tmp_path) == {}


def test_package_dependencies_merges_only_object_tables(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"electron":"43"},"devDependencies":["vite"]}'
    )

    assert package_dependencies(tmp_path) == {"electron": "43"}
