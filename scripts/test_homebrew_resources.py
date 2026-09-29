import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import homebrew_resources as brew


class HomebrewResourcesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent

    def test_runtime_graph_is_dependency_ordered_and_omits_development_packages(self):
        self.assertEqual(
            [item["name"] for item in brew.runtime_packages(self.root)],
            ["argcomplete", "tomlkit", "pyflyrail"],
        )

    def test_pyflyrail_uses_pypi_source_archive(self):
        package = brew.runtime_packages(self.root)[-1]
        self.assertEqual(package["source"], {"registry": "https://pypi.org/simple"})
        self.assertTrue(package["sdist"]["url"].endswith(f"/pyflyrail-{package['version']}.tar.gz"))
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "wrong.tar.gz"
            archive.write_bytes(b"wrong source")
            overrides = {
                package["sdist"]["url"]: {
                    "path": str(archive),
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                }
            }
            with (
                patch.object(brew, "runtime_packages", return_value=[package]),
                self.assertRaisesRegex(ValueError, "locked source checksum mismatch: pyflyrail"),
            ):
                brew.prepare_resources(self.root, Path(temp) / "cache", overrides)

    def test_staging_verifies_bytes_and_selects_source_project_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as stream:
                content = b'[project]\nname = "pyflyrail"\nversion = "0.1.0"\n'
                info = tarfile.TarInfo("pyflyrail-0.1.0/pyproject.toml")
                info.size = len(content)
                stream.addfile(info, io.BytesIO(content))
            resource = brew.Resource(
                "pyflyrail",
                "https://example.com/source.tar.gz",
                hashlib.sha256(archive.read_bytes()).hexdigest(),
                archive,
            )
            project = brew.stage_resource(resource, root / "staged")
            self.assertEqual(project, root / "staged/pyflyrail-0.1.0")
            self.assertEqual((project / "pyproject.toml").read_bytes(), content)
            archive.write_bytes(b"changed transport")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                brew.stage_resource(resource, root / "rejected")

    def test_runtime_downloads_reject_wrong_locked_or_transport_checksums(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "wrong.tar.gz"
            archive.write_bytes(b"wrong source")
            package = brew.runtime_packages(self.root)[0]
            url = package["sdist"]["url"]
            overrides = {url: {"path": str(archive), "sha256": "0" * 64}}
            with self.assertRaisesRegex(ValueError, "transport checksum mismatch"):
                brew.prepare_resources(self.root, root / "transport", overrides)
            overrides[url]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "locked source checksum mismatch"):
                brew.prepare_resources(self.root, root / "locked", overrides)
            with self.assertRaises(KeyError):
                brew.prepare_resources(self.root, root / "missing", {})

    def test_formula_stages_each_resource_at_its_installable_root(self):
        resources = (
            brew.Resource(
                "tomlkit", "https://example.com/tomlkit.tar.gz", "1" * 64, Path("archive")
            ),
            brew.Resource(
                "pyflyrail", "https://example.com/pyflyrail.tar.gz", "2" * 64, Path("archive")
            ),
        )
        text = brew.render_formula("1.2.3", "3" * 64, resources)
        self.assertIn('resource("tomlkit").stage { venv.pip_install Pathname.pwd }', text)
        self.assertIn('resource("pyflyrail").stage { venv.pip_install Pathname.pwd }', text)
        self.assertIn('sha256 "' + "3" * 64 + '"', text)
        self.assertIn('sha256 "' + "2" * 64 + '"', text)
        self.assertIn("venv.pip_install_and_link buildpath", text)


if __name__ == "__main__":
    unittest.main()
