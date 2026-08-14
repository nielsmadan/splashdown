from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_package_json(app_path: Path) -> dict[str, Any]:
    path = app_path / "package.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def package_dependencies(app_path: Path) -> dict[str, Any]:
    data = read_package_json(app_path)
    dependencies = data.get("dependencies")
    dev_dependencies = data.get("devDependencies")
    return {
        **(dependencies if isinstance(dependencies, dict) else {}),
        **(dev_dependencies if isinstance(dev_dependencies, dict) else {}),
    }
