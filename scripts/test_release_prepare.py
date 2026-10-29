import subprocess
import sys
from pathlib import Path


def test_prepare_updates_only_project_version(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    original = (root / "pyproject.toml").read_text()
    path = tmp_path / "pyproject.toml"
    path.write_text(original)
    subprocess.run(
        [sys.executable, str(root / "scripts/prepare_release.py"), "9.8.7"],
        cwd=tmp_path,
        check=True,
    )
    expected = "\n".join(
        'version = "9.8.7"' if line.startswith("version = ") else line
        for line in original.split("\n")
    )
    assert path.read_text() == expected
