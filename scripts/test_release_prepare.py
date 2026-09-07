import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PreparationTests(unittest.TestCase):
    def test_updates_only_the_project_version(self):
        root = Path(__file__).resolve().parent.parent
        original = (root / "pyproject.toml").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pyproject.toml"
            path.write_text(original)
            subprocess.run(
                [sys.executable, str(root / "scripts/prepare_release.py"), "9.8.7"],
                cwd=temporary,
                check=True,
            )
            expected = "\n".join(
                'version = "9.8.7"' if line.startswith("version = ") else line
                for line in original.split("\n")
            )
            self.assertEqual(path.read_text(), expected)


if __name__ == "__main__":
    unittest.main()
