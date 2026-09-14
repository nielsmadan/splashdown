from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from .constants import ENV_FILE_NAME
from .hooks import _ensure_mise_file_directive, _remove_mise_file_directive, mise_config_path


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
    # Completes "trust also approves the <name> configuration splashdown wired, so …".
    approval_detail: str = ""

    def detect(self, cwd: Path) -> bool:
        raise NotImplementedError

    def wire(self, cwd: Path) -> None:
        """Idempotently configure the loader to source splashdown.env."""
        raise NotImplementedError

    def owns_config(self, cwd: Path) -> bool:
        """True when the loader's configuration holds splashdown's integration
        and nothing else, so approving it cannot authorize user-authored code."""
        return False

    def approve(self, cwd: Path, *, announce: bool = False) -> bool:
        """Run the loader's trust/allow step so it will actually load
        splashdown.env. No-op by default (only mise/direnv gate on trust).
        Never raises. `announce` prints a one-line result."""
        return False

    def unwire(self, cwd: Path) -> None:
        """Inverse of wire: remove splashdown's loading directive. Surgical —
        leave unrelated content, and delete a file only when nothing but our
        content remains. No-op by default so unknown loaders are harmless."""


class MiseLoader(Loader):
    name = "mise"
    approval_detail = "mise trusts that path permanently and loads whatever the file holds later"

    def detect(self, cwd: Path) -> bool:
        return (cwd / "mise.toml").exists() or (cwd / ".mise.toml").exists()

    def wire(self, cwd: Path) -> None:
        _ensure_mise_file_directive(cwd)

    def owns_config(self, cwd: Path) -> bool:
        from .tomlio import remove_mise_file_directive_text  # noqa: PLC0415

        path = mise_config_path(cwd)
        if not path.exists():
            return False
        try:
            remainder = remove_mise_file_directive_text(path.read_text())
        except (OSError, ValueError):
            return False
        return remainder is not None and not remainder.strip()

    def approve(self, cwd: Path, *, announce: bool = False) -> bool:
        path = mise_config_path(cwd)
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
    approval_detail = (
        "direnv loads the file as it stands now and prompts again after any later edit"
    )

    def detect(self, cwd: Path) -> bool:
        return (cwd / ".envrc").exists() or (cwd / ".envrc.local").exists()

    def wire(self, cwd: Path) -> None:
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
            return
        path.write_text(new_text)
        # Editing an existing .envrc invalidates its trust hash, but auto-allowing user commands would be unsafe.
        if not created:
            print("wired .envrc — run `direnv allow` to load splashdown.env", file=sys.stderr)

    def owns_config(self, cwd: Path) -> bool:
        path = cwd / ".envrc"
        if not path.exists():
            return False
        try:
            text = path.read_text()
        except OSError:
            return False
        if not _DIRENV_BLOCK_RE.search(text):
            return False
        return not _DIRENV_BLOCK_RE.sub("", text).strip()

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

    def wire(self, cwd: Path) -> None:
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
            return
        shell["init_hook"] = new_hooks
        path.write_text(json.dumps(data, indent=2) + "\n")

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

    def wire(self, cwd: Path) -> None:
        pass


LOADERS: dict[str, Loader] = {
    "mise": MiseLoader(),
    "direnv": DirenvLoader(),
    "devbox": DevboxLoader(),
    "none": NoneLoader(),
}
