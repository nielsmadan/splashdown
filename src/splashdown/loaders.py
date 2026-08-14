from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from . import ENV_FILE_NAME
from .hooks import _ensure_mise_file_directive, _remove_mise_file_directive

# A Loader wires the shell-env tool (mise / direnv / devbox) so it sources
# splashdown.env when the user enters the project directory. Each loader uses
# sentinel-wrapped blocks so wire is idempotent and visually obvious. mise and
# direnv also gate loading behind a trust/allow step, so init approves only a
# config file it created itself.


def _run_ok(argv: list[str], cwd: Path) -> bool:
    """Run a loader approval command, swallowing every failure. Never raises:
    a missing binary, non-zero exit, or timeout must not break `splash` or the
    git hook that calls it."""
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=10, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


class Loader:
    """Abstract base. Subclasses set `name` and override `detect` and `wire`."""

    name: str = ""

    def detect(self, cwd: Path) -> bool:
        raise NotImplementedError

    def wire(self, cwd: Path) -> bool:
        """Idempotently configure the loader to source splashdown.env. Returns
        True only when this call created the loader config from nothing — the
        signal `cmd_init` uses to decide whether to auto-approve it."""
        raise NotImplementedError

    def approve(self, cwd: Path, *, announce: bool = False) -> bool:
        """Run the loader's trust/allow step so it will actually load
        splashdown.env. No-op by default (only mise/direnv gate on trust).
        Never raises. `announce` prints a one-line result on the init path."""
        return False

    def unwire(self, cwd: Path) -> None:
        """Inverse of wire: remove splashdown's loading directive. Surgical —
        leave unrelated content, and delete a file only when nothing but our
        content remains. No-op by default so unknown loaders are harmless."""


class MiseLoader(Loader):
    name = "mise"

    def detect(self, cwd: Path) -> bool:
        return (cwd / "mise.toml").exists() or (cwd / ".mise.toml").exists()

    def wire(self, cwd: Path) -> bool:
        return _ensure_mise_file_directive(cwd)

    def approve(self, cwd: Path, *, announce: bool = False) -> bool:
        path = cwd / "mise.toml" if (cwd / "mise.toml").exists() else cwd / ".mise.toml"
        if not path.exists():
            return False
        ok = _run_ok(["mise", "trust", str(path)], cwd)
        if announce:
            msg = f"trusted {path.name}" if ok else f"run `mise trust` to load {ENV_FILE_NAME}"
            print(msg, file=sys.stderr)
        return ok

    def unwire(self, cwd: Path) -> None:
        _remove_mise_file_directive(cwd)


_DIRENV_BEGIN = "# >>> splashdown-managed dotenv >>>"
_DIRENV_END = "# <<< splashdown-managed dotenv <<<"
# `dotenv_if_exists` (not `dotenv`) so a fresh checkout doesn't hard-error before
# splashdown.env has been generated.
_DIRENV_BLOCK = f"""{_DIRENV_BEGIN}
dotenv_if_exists splashdown.env
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

    def wire(self, cwd: Path) -> bool:
        path = cwd / ".envrc"
        created = not path.exists()
        existing = path.read_text() if path.exists() else ""
        if _DIRENV_BLOCK_RE.search(existing):
            new_text = _DIRENV_BLOCK_RE.sub(_DIRENV_BLOCK, existing, count=1)
        else:
            text = existing.rstrip()
            if text:
                text += "\n\n"
            new_text = text + _DIRENV_BLOCK
        if new_text == existing:
            return False  # already wired
        path.write_text(new_text)
        # A file we created gets auto-`direnv allow`ed by the caller; but for a
        # pre-existing .envrc we won't auto-approve the user's own commands, so
        # tell them to re-allow (editing it invalidated direnv's trust hash).
        if not created:
            print("wired .envrc — run `direnv allow` to load splashdown.env", file=sys.stderr)
        return created

    def approve(self, cwd: Path, *, announce: bool = False) -> bool:
        if not (cwd / ".envrc").exists():
            return False
        ok = _run_ok(["direnv", "allow", str(cwd)], cwd)
        if announce:
            msg = "allowed .envrc" if ok else f"run `direnv allow` to load {ENV_FILE_NAME}"
            print(msg, file=sys.stderr)
        return ok

    def unwire(self, cwd: Path) -> None:
        path = cwd / ".envrc"
        if not path.exists():
            return
        text = path.read_text()
        new = _DIRENV_BLOCK_RE.sub("", text)
        if new == text:
            return
        if new.strip():
            path.write_text(new)
        else:
            path.unlink()


# Marker baked into the init_hook string so we can find-and-replace idempotently
# without parsing JSON ASTs.
_DEVBOX_HOOK_MARKER = "# splashdown-managed"
_DEVBOX_HOOK_CMD = f"{_DEVBOX_HOOK_MARKER}\nset -a; source splashdown.env; set +a"


class DevboxLoader(Loader):
    name = "devbox"

    def detect(self, cwd: Path) -> bool:
        return (cwd / "devbox.json").exists()

    def wire(self, cwd: Path) -> bool:
        path = cwd / "devbox.json"
        if not path.exists():
            path.write_text("{}")
        data = json.loads(path.read_text())
        shell = data.setdefault("shell", {})
        hooks = shell.setdefault("init_hook", [])
        if isinstance(hooks, str):
            hooks = [hooks]
        new_hooks = [h for h in hooks if isinstance(h, str) and _DEVBOX_HOOK_MARKER not in h]
        new_hooks.append(_DEVBOX_HOOK_CMD)
        if new_hooks == hooks:
            return False
        shell["init_hook"] = new_hooks
        path.write_text(json.dumps(data, indent=2) + "\n")
        # devbox has no trust gate, so the create/edit distinction is unused.
        return False

    def unwire(self, cwd: Path) -> None:
        path = cwd / "devbox.json"
        if not path.exists():
            return
        data = json.loads(path.read_text())
        shell = data.get("shell")
        if not isinstance(shell, dict):
            return
        hooks = shell.get("init_hook")
        if isinstance(hooks, str):
            hooks = [hooks]
        if not isinstance(hooks, list):
            return
        new_hooks = [h for h in hooks if not (isinstance(h, str) and _DEVBOX_HOOK_MARKER in h)]
        if new_hooks == hooks:
            return
        if new_hooks:
            shell["init_hook"] = new_hooks
        else:
            del shell["init_hook"]
            if not shell:
                del data["shell"]
        if data:
            path.write_text(json.dumps(data, indent=2) + "\n")
        else:
            path.unlink()


class NoneLoader(Loader):
    """Fallback when no shell-env loader is present. Wires nothing — `cmd_init`
    decides whether to route values into a dotenv file or print instructions.
    `detect` is always False; this loader is only ever selected as the fallback."""

    name = "none"

    def detect(self, cwd: Path) -> bool:
        return False

    def wire(self, cwd: Path) -> bool:
        return False


LOADERS: dict[str, Loader] = {
    "mise": MiseLoader(),
    "direnv": DirenvLoader(),
    "devbox": DevboxLoader(),
    "none": NoneLoader(),
}
