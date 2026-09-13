import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

import homebrew_resources as brew


class HomebrewResourcesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent

    def test_runtime_graph_is_dependency_ordered_and_omits_development_packages(self):
        self.assertEqual(
            [item["name"] for item in brew.runtime_packages(self.root)],
            ["argcomplete", "tomlkit", "flyrail"],
        )

    def test_flyrail_archive_uses_exact_public_lock_pin_and_python_subproject(self):
        package = brew.runtime_packages(self.root)[-1]
        commit = package["source"]["git"].split("#")[1]
        self.assertEqual(
            brew.git_archive(package),
            (f"https://github.com/nielsmadan/flyrail/archive/{commit}.tar.gz", "python"),
        )
        package["source"]["git"] = package["source"]["git"].replace(commit, "main")
        with self.assertRaises(ValueError):
            brew.git_archive(package)

    def test_staging_verifies_bytes_and_selects_actual_python_subproject(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as stream:
                content = b'[project]\nname = "flyrail"\nversion = "0.1.0"\n'
                info = tarfile.TarInfo("flyrail-source/python/pyproject.toml")
                info.size = len(content)
                stream.addfile(info, io.BytesIO(content))
            resource = brew.Resource(
                "flyrail",
                "https://example.com/source.tar.gz",
                hashlib.sha256(archive.read_bytes()).hexdigest(),
                archive,
                "python",
            )
            project = brew.stage_resource(resource, root / "staged")
            self.assertEqual(project, root / "staged/flyrail-source/python")
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
                "flyrail", "https://example.com/flyrail.tar.gz", "2" * 64, Path("archive"), "python"
            ),
        )
        text = brew.render_formula("1.2.3", "3" * 64, resources)
        self.assertIn('resource("tomlkit").stage { venv.pip_install Pathname.pwd }', text)
        self.assertIn('resource("flyrail").stage { venv.pip_install Pathname.pwd/"python" }', text)
        self.assertIn('sha256 "' + "3" * 64 + '"', text)
        self.assertIn('sha256 "' + "2" * 64 + '"', text)
        self.assertIn("venv.pip_install_and_link buildpath", text)


if __name__ == "__main__":
    unittest.main()
