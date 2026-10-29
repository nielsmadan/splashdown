from pathlib import Path

import pytest

from homebrew_resources import Resource, render_formula, runtime_packages
from render_formula import installed_version, render, render_file

ROOT = Path(__file__).resolve().parent.parent


def verified_formula(tmp_path: Path, version: str, checksum: str) -> Path:
    resources = tuple(
        Resource(
            package["name"],
            package["sdist"]["url"],
            package["sdist"]["hash"].removeprefix("sha256:"),
            tmp_path / f"{package['name']}.tar.gz",
        )
        for package in runtime_packages(ROOT)
    )
    path = tmp_path / f"verified-{version}.rb"
    path.write_text(render_formula(version, checksum, resources))
    return path


def test_formula_preserves_verified_locked_runtime_resources(tmp_path: Path) -> None:
    source = verified_formula(tmp_path, "1.2.3", "a" * 64)
    formula = render("1.2.3", "a" * 64, source)
    assert formula == source.read_text()
    assert "tags/v1.2.3.tar.gz" in formula
    assert 'depends_on "python@3.13"' in formula
    for package in runtime_packages(ROOT):
        assert f'resource "{package["name"]}" do' in formula
        assert package["sdist"]["url"] in formula
        assert package["sdist"]["hash"].removeprefix("sha256:") in formula
        assert f'resource("{package["name"]}").stage' in formula
    assert "venv.pip_install_and_link buildpath" in formula
    assert "generate_completions_from_executable" in formula


@pytest.mark.parametrize("version", ["v1.2.3", "1.2", "01.2.3", "1.2.3;bad"])
def test_formula_rejects_invalid_versions(tmp_path: Path, version: str) -> None:
    with pytest.raises(ValueError, match="Invalid release version"):
        render(version, "a" * 64, tmp_path / "formula.rb")


def test_formula_rejects_placeholder_checksum(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="64 lowercase"):
        render("1.2.3", "SHA256", tmp_path / "formula.rb")


def test_formula_version_parser_requires_one_trusted_release_url() -> None:
    assert installed_version(
        '  url "https://github.com/nielsmadan/splashdown/archive/refs/tags/v2.3.4.tar.gz"\n'
    ) == (2, 3, 4)
    with pytest.raises(ValueError, match="unique trusted"):
        installed_version('  version "2.3.4"\n')


def test_older_release_rerun_preserves_newer_formula(tmp_path: Path) -> None:
    output = tmp_path / "splashdown.rb"
    output.write_bytes(verified_formula(tmp_path, "2.0.0", "b" * 64).read_bytes())
    original = output.read_bytes()
    source = verified_formula(tmp_path, "1.9.9", "a" * 64)
    assert render_file("1.9.9", "a" * 64, source, output) is False
    assert output.read_bytes() == original


@pytest.mark.parametrize("current", ["1.2.3", "1.2.2"])
def test_same_or_newer_release_installs_verified_formula(tmp_path: Path, current: str) -> None:
    output = tmp_path / "splashdown.rb"
    output.write_bytes(verified_formula(tmp_path, current, "b" * 64).read_bytes())
    source = verified_formula(tmp_path, "1.2.3", "a" * 64)
    assert render_file("1.2.3", "a" * 64, source, output) is True
    assert output.read_bytes() == source.read_bytes()


@pytest.mark.parametrize(
    ("version", "checksum", "message"),
    [("1.2.4", "a" * 64, "version differs"), ("1.2.3", "b" * 64, "checksum differs")],
)
def test_incorrect_bundle_preserves_existing_formula(
    tmp_path: Path, version: str, checksum: str, message: str
) -> None:
    output = tmp_path / "splashdown.rb"
    output.write_bytes(verified_formula(tmp_path, "1.2.2", "c" * 64).read_bytes())
    original = output.read_bytes()
    source = verified_formula(tmp_path, version, checksum)
    with pytest.raises(ValueError, match=message):
        render_file("1.2.3", "a" * 64, source, output)
    assert output.read_bytes() == original


def test_existing_untrusted_formula_is_not_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "splashdown.rb"
    output.write_text("class Splashdown < Formula\nend\n")
    original = output.read_bytes()
    source = verified_formula(tmp_path, "1.2.3", "a" * 64)
    with pytest.raises(ValueError, match="unique trusted"):
        render_file("1.2.3", "a" * 64, source, output)
    assert output.read_bytes() == original
