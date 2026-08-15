from __future__ import annotations

from pathlib import Path


def resolve_version() -> str:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version("splashdown")
    except PackageNotFoundError:
        import tomllib  # noqa: PLC0415

        try:
            pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
            return str(tomllib.loads(pyproject.read_text())["project"]["version"])
        except Exception:  # noqa: BLE001
            return "0.0.0+unknown"
