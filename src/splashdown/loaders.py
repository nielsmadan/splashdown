from __future__ import annotations

import json
import re
from pathlib import Path

# ---------- loaders ----------
# A Loader wires the shell-env tool (mise / direnv / devbox) so it sources
# splashdown.env when the user enters the project directory. Each loader uses
# sentinel-wrapped blocks so wire is idempotent and visually obvious.


class Loader:
    """Abstract base. Subclasses set `name` and override `detect` and `wire`."""

    name: str = ""

    def detect(self, cwd: Path) -> bool:
        raise NotImplementedError

    def wire(self, cwd: Path) -> None:
        """Idempotently configure the loader to source splashdown.env."""
        raise NotImplementedError


class MiseLoader(Loader):
    name = "mise"

    def detect(self, cwd: Path) -> bool:
        return (cwd / "mise.toml").exists() or (cwd / ".mise.toml").exists()

    def wire(self, cwd: Path) -> None:
        # Reuses the existing helper that already handles new-file creation,
        # existing [env] table append, and idempotent re-runs.
        from .commands import _ensure_mise_file_directive  # noqa: PLC0415

        _ensure_mise_file_directive(cwd)


_DIRENV_BEGIN = "# >>> splashdown-managed dotenv >>>"
_DIRENV_END = "# <<< splashdown-managed dotenv <<<"
_DIRENV_BLOCK = f"""{_DIRENV_BEGIN}
dotenv splashdown.env
{_DIRENV_END}
"""
_DIRENV_BLOCK_RE = re.compile(
    re.escape(_DIRENV_BEGIN) + r".*?" + re.escape(_DIRENV_END) + r"\n?",
    re.DOTALL,
)


class DirenvLoader(Loader):
    name = "direnv"

    def detect(self, cwd: Path) -> bool:
        return (cwd / ".envrc").exists() or (cwd / ".envrc.local").exists()

    def wire(self, cwd: Path) -> None:
        path = cwd / ".envrc"
        existing = path.read_text() if path.exists() else ""
        if _DIRENV_BLOCK_RE.search(existing):
            return  # already wired
        text = existing.rstrip()
        if text:
            text += "\n\n"
        text += _DIRENV_BLOCK
        path.write_text(text)


# Marker baked into the init_hook string so we can find-and-replace idempotently
# without parsing JSON ASTs.
_DEVBOX_HOOK_MARKER = "# splashdown-managed"
_DEVBOX_HOOK_CMD = f"{_DEVBOX_HOOK_MARKER}\nset -a; source splashdown.env; set +a"


class DevboxLoader(Loader):
    name = "devbox"

    def detect(self, cwd: Path) -> bool:
        return (cwd / "devbox.json").exists()

    def wire(self, cwd: Path) -> None:
        path = cwd / "devbox.json"
        if not path.exists():
            path.write_text("{}")
        data = json.loads(path.read_text())
        shell = data.setdefault("shell", {})
        hooks = shell.setdefault("init_hook", [])
        if isinstance(hooks, str):
            hooks = [hooks]
        # Replace any existing splashdown-managed entry; else append.
        new_hooks = [h for h in hooks if isinstance(h, str) and _DEVBOX_HOOK_MARKER not in h]
        new_hooks.append(_DEVBOX_HOOK_CMD)
        if new_hooks == hooks:
            return  # nothing changed
        shell["init_hook"] = new_hooks
        path.write_text(json.dumps(data, indent=2) + "\n")


LOADERS: dict[str, Loader] = {
    "mise": MiseLoader(),
    "direnv": DirenvLoader(),
    "devbox": DevboxLoader(),
}
