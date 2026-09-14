from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import normalized_env_reference
from .errors import LoaderConflictError
from .hooks import mise_config_path
from .safe_files import atomic_write_text, read_optional_editable_text

CREATED = "created"
UPDATED = "updated"
CONFIGURED = "configured"
REUSED = "reused"
NOTHING = "nothing"


@dataclass(frozen=True)
class WirePlan:
    """A validated, not-yet-committed loader edit. `text` is `None` for the
    no-write outcomes, so a plan can be reported before anything is written."""

    loader: str
    status: str
    note: str
    path: Path | None = None
    text: str | None = None
    root: Path | None = None
    hint: str = ""

    @property
    def writes(self) -> bool:
        return self.text is not None


def _run_ok(argv: list[str], cwd: Path) -> bool:
    """Run a loader approval command, swallowing every failure. Never raises:
    a missing binary, non-zero exit, or timeout must not break `splash` or the
    git hook that calls it."""
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=10, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _quoted_argument(raw: str) -> str:
    quote = raw[:1]
    if quote in ("'", '"') and raw.endswith(quote) and raw != quote:
        return raw[1:-1]
    return raw


def _names_env_file(raw: str, env_file: str) -> bool:
    return normalized_env_reference(_quoted_argument(raw)) == env_file


def _read_config(path: Path, cwd: Path, loader: str) -> str | None:
    try:
        return read_optional_editable_text(path, root=cwd)
    except ValueError as error:
        raise LoaderConflictError(f"cannot wire {loader}: {error}") from error


class Loader:
    """Abstract base. Subclasses set `name` and override `detect` and `plan`."""

    name: str = ""
    # Completes "trust also approves the <name> configuration splashdown wired, so …".
    approval_detail: str = ""

    def detect(self, cwd: Path) -> bool:
        raise NotImplementedError

    def config_paths(self, cwd: Path) -> list[Path]:
        """Every configuration file of this loader that exists at `cwd`."""
        return []

    def plan(self, cwd: Path, env_file: str) -> WirePlan:
        """Parse and validate the edit that would make this loader load
        `env_file`, without writing. Raises LoaderConflictError when the existing
        configuration cannot be extended without discarding settings."""
        raise NotImplementedError

    def wire(self, cwd: Path, env_file: str) -> WirePlan:
        """Idempotently configure the loader to source the env destination."""
        return apply_wire_plan(self.plan(cwd, env_file))

    def owns_config(self, cwd: Path, env_file: str) -> bool:
        """True when the loader's configuration holds splashdown's integration
        and nothing else, so approving it cannot authorize user-authored code."""
        return False

    def approve(self, cwd: Path, *, env_file: str, announce: bool = False) -> bool:
        """Run the loader's trust/allow step so it will actually load the env
        destination. No-op by default (only mise/direnv gate on trust).
        Never raises. `announce` prints a one-line result."""
        return False

    def unwire(self, cwd: Path, env_file: str) -> None:
        """Inverse of wire: remove splashdown's loading directive. Surgical —
        leave unrelated content, and delete a file only when nothing but our
        content remains. No-op by default so unknown loaders are harmless."""


def apply_wire_plan(plan: WirePlan) -> WirePlan:
    if plan.text is None or plan.path is None:
        return plan
    atomic_write_text(plan.path, plan.text, root=plan.root, create=True)
    return plan


class MiseLoader(Loader):
    name = "mise"
    approval_detail = "mise trusts that path permanently and loads whatever the file holds later"

    def detect(self, cwd: Path) -> bool:
        return bool(self.config_paths(cwd))

    def config_paths(self, cwd: Path) -> list[Path]:
        return [cwd / name for name in ("mise.toml", ".mise.toml") if (cwd / name).exists()]

    def plan(self, cwd: Path, env_file: str) -> WirePlan:
        from .tomlio import (  # noqa: PLC0415
            ensure_mise_file_directive_text,
            mise_file_directive_is_managed,
        )

        path = mise_config_path(cwd)
        existing = _read_config(path, cwd, self.name)
        try:
            new_text = ensure_mise_file_directive_text(existing, env_file)
        except ValueError as error:
            raise LoaderConflictError(
                f"cannot wire mise: in {path.name}, {error}. "
                f"Point `_.file` at {env_file} yourself, or re-run with `--loader none`."
            ) from error
        if new_text is None:
            if mise_file_directive_is_managed(existing):
                return WirePlan(self.name, CONFIGURED, f"{path.name} already loads {env_file}")
            return WirePlan(
                self.name,
                REUSED,
                f"reusing the {path.name} directive for {env_file}",
                hint=(
                    f"that directive is unmarked, so `splash deinit` will leave it. "
                    f"If splashdown wrote it, remove the {env_file} entry from "
                    f"`_.file` in {path.name} (the whole line when it names nothing "
                    f"else) and re-run `splash init` to have it marked."
                ),
            )
        verb = UPDATED if existing is not None else CREATED
        return WirePlan(
            self.name,
            verb,
            f'{verb} {path.name} (+_.file = "{env_file}")',
            path=path,
            text=new_text,
            root=cwd,
        )

    def owns_config(self, cwd: Path, env_file: str) -> bool:
        from .tomlio import remove_mise_file_directive_text  # noqa: PLC0415

        path = mise_config_path(cwd)
        try:
            existing = read_optional_editable_text(path, root=cwd)
        except (OSError, ValueError):
            return False
        if existing is None:
            return False
        try:
            remainder = remove_mise_file_directive_text(existing, env_file)
        except ValueError:
            return False
        return remainder is not None and not remainder.strip()

    def approve(self, cwd: Path, *, env_file: str, announce: bool = False) -> bool:
        path = mise_config_path(cwd)
        if not path.exists():
            return False
        ok = _run_ok(["mise", "trust", str(path)], cwd)
        if announce:
            msg = f"trusted {path.name}" if ok else f"run `mise trust` to load {env_file}"
            print(msg, file=sys.stderr)
        return ok

    def unwire(self, cwd: Path, env_file: str) -> None:
        from .tomlio import remove_mise_file_directive_text  # noqa: PLC0415

        path = mise_config_path(cwd)
        existing = read_optional_editable_text(path, root=cwd)
        if existing is None:
            return
        new_text = remove_mise_file_directive_text(existing, env_file)
        if new_text is None:
            return
        if new_text.strip():
            atomic_write_text(path, new_text, root=cwd)
            print(f"updated {path.name} (-splashdown env directive)", file=sys.stderr)
        else:
            path.unlink()
            print(f"removed {path.name}", file=sys.stderr)


_DIRENV_BEGIN = "# >>> splashdown-managed dotenv >>>"
_DIRENV_END = "# <<< splashdown-managed dotenv <<<"


def _direnv_block(env_file: str) -> str:
    # `dotenv_if_exists` (not `dotenv`) so a fresh checkout doesn't hard-error
    # before the destination has been generated.
    return f"{_DIRENV_BEGIN}\ndotenv_if_exists {env_file}\n{_DIRENV_END}\n"


_DIRENV_BLOCK_RE = re.compile(
    re.escape(_DIRENV_BEGIN) + r".*?" + re.escape(_DIRENV_END) + r"\n?",
    re.DOTALL,
)
# A `#` only starts a comment at the start of a word, so `file.env#x` names a file.
_SHELL_COMMENT_RE = re.compile(r"(?:^|(?<=[ \t]))#.*$")
# Column 0 only: an indented directive sits inside a function or conditional,
# which is not proof that the file is loaded.
_DIRENV_DOTENV_RE = re.compile(
    r"^(?:dotenv|dotenv_if_exists)[ \t]+(?P<arg>'[^']*'|\"[^\"]*\"|[^\s#'\"]+)[ \t]*$"
)


def _direnv_user_directive(text: str, env_file: str) -> bool:
    """Whether `.envrc` already loads the destination outside splashdown's block."""
    for line in _DIRENV_BLOCK_RE.sub("", text).split("\n"):
        match = _DIRENV_DOTENV_RE.match(_SHELL_COMMENT_RE.sub("", line))
        if match is not None and _names_env_file(match["arg"], env_file):
            return True
    return False


class DirenvLoader(Loader):
    name = "direnv"
    approval_detail = (
        "direnv loads the file as it stands now and prompts again after any later edit"
    )

    def detect(self, cwd: Path) -> bool:
        return bool(self.config_paths(cwd))

    def config_paths(self, cwd: Path) -> list[Path]:
        return [cwd / name for name in (".envrc", ".envrc.local") if (cwd / name).exists()]

    def plan(self, cwd: Path, env_file: str) -> WirePlan:
        path = cwd / ".envrc"
        existing = _read_config(path, cwd, self.name)
        if existing is not None and _direnv_user_directive(existing, env_file):
            return WirePlan(self.name, REUSED, f"reusing the .envrc directive for {env_file}")
        block = _direnv_block(env_file)
        base = existing or ""
        if _DIRENV_BLOCK_RE.search(base):
            new_text = _DIRENV_BLOCK_RE.sub(block, base, count=1)
        else:
            head = base.rstrip()
            new_text = (head + "\n\n" if head else "") + block
        if new_text == existing:
            return WirePlan(self.name, CONFIGURED, f".envrc already loads {env_file}")
        verb = UPDATED if existing is not None else CREATED
        return WirePlan(
            self.name,
            verb,
            f"{verb} .envrc (+dotenv_if_exists {env_file})",
            path=path,
            text=new_text,
            root=cwd,
            # Editing an existing .envrc invalidates its trust hash, and auto-allowing
            # user commands would be unsafe.
            hint="" if existing is None else f"run `direnv allow` to load {env_file}",
        )

    def owns_config(self, cwd: Path, env_file: str) -> bool:
        path = cwd / ".envrc"
        try:
            text = read_optional_editable_text(path, root=cwd)
        except (OSError, ValueError):
            return False
        if text is None:
            return False
        if not _DIRENV_BLOCK_RE.search(text):
            return False
        return not _DIRENV_BLOCK_RE.sub("", text).strip()

    def approve(self, cwd: Path, *, env_file: str, announce: bool = False) -> bool:
        if not (cwd / ".envrc").exists():
            return False
        ok = _run_ok(["direnv", "allow", str(cwd)], cwd)
        if announce:
            msg = "allowed .envrc" if ok else f"run `direnv allow` to load {env_file}"
            print(msg, file=sys.stderr)
        return ok

    def unwire(self, cwd: Path, env_file: str) -> None:
        path = cwd / ".envrc"
        existing = read_optional_editable_text(path, root=cwd)
        if existing is None:
            return
        new = _DIRENV_BLOCK_RE.sub("", existing)
        if new == existing:
            return
        if new.strip():
            atomic_write_text(path, new, root=cwd)
        else:
            path.unlink()


# Marker baked into the init_hook string so we can find-and-replace idempotently
# without parsing JSON ASTs.
_DEVBOX_HOOK_MARKER = "# splashdown-managed"


def _devbox_hook_cmd(env_file: str) -> str:
    return f"{_DEVBOX_HOOK_MARKER}\nset -a; source {env_file}; set +a"


_DEVBOX_ALLEXPORT_RE = re.compile(r"^set[ \t]+(?P<sign>[-+])(?:a|o[ \t]+allexport)$")
_DEVBOX_SOURCE_RE = re.compile(r"^(?:source|\.)[ \t]+(?P<arg>'[^']*'|\"[^\"]*\"|[^\s#'\"]+)$")


def _devbox_statements(hook: str) -> list[str]:
    """The hook's top-level statements. Only a newline ends a command in sh, so
    the split is on `\n` alone. Comments go first, so a `;` inside one cannot
    split it into code; an indented line is dropped because it sits inside a
    function body or a conditional."""
    statements: list[str] = []
    for line in hook.split("\n"):
        if line[:1] in (" ", "\t"):
            continue
        for segment in _SHELL_COMMENT_RE.sub("", line).split(";"):
            statements.append(segment.strip(" \t"))
    return statements


def _devbox_hook_loads_env_file(hook: str, env_file: str) -> bool:
    """Whether one init hook sources the destination as a top-level statement of
    its own while allexport is on. `set -a` turns allexport on and `set +a`
    turns it back off, so only the last one before the `source` counts. A
    chained (`&&`) statement, a comment, and anything indented under a
    conditional or function are not proof that the file is loaded."""
    exported = False
    for statement in _devbox_statements(hook):
        allexport = _DEVBOX_ALLEXPORT_RE.match(statement)
        if allexport is not None:
            exported = allexport["sign"] == "-"
            continue
        match = _DEVBOX_SOURCE_RE.match(statement)
        if exported and match is not None and _names_env_file(match["arg"], env_file):
            return True
    return False


def _devbox_hooks(data: Any) -> list[Any]:
    if not isinstance(data, dict):
        raise LoaderConflictError("cannot wire devbox: devbox.json is not a JSON object")
    shell = data.get("shell", {})
    if not isinstance(shell, dict):
        raise LoaderConflictError("cannot wire devbox: `shell` in devbox.json is not an object")
    hooks = shell.get("init_hook", [])
    if isinstance(hooks, str):
        return [hooks]
    if not isinstance(hooks, list):
        raise LoaderConflictError(
            "cannot wire devbox: `shell.init_hook` in devbox.json is neither a string nor a list"
        )
    return list(hooks)


def _is_managed_hook(hook: Any) -> bool:
    return isinstance(hook, str) and _DEVBOX_HOOK_MARKER in hook


class DevboxLoader(Loader):
    name = "devbox"

    def detect(self, cwd: Path) -> bool:
        return bool(self.config_paths(cwd))

    def config_paths(self, cwd: Path) -> list[Path]:
        path = cwd / "devbox.json"
        return [path] if path.exists() else []

    def plan(self, cwd: Path, env_file: str) -> WirePlan:
        path = cwd / "devbox.json"
        existing = _read_config(path, cwd, self.name)
        try:
            data: Any = json.loads(existing) if existing is not None else {}
        except json.JSONDecodeError as error:
            raise LoaderConflictError(
                f"cannot wire devbox: devbox.json is not valid JSON ({error}). "
                "Fix the file, or re-run with `--loader none`."
            ) from error
        hooks = _devbox_hooks(data)
        reusable = any(
            isinstance(hook, str)
            and not _is_managed_hook(hook)
            and _devbox_hook_loads_env_file(hook, env_file)
            for hook in hooks
        )
        kept = [hook for hook in hooks if not _is_managed_hook(hook)]
        new_hooks = kept if reusable else [*kept, _devbox_hook_cmd(env_file)]
        if new_hooks == hooks:
            if reusable:
                return WirePlan(
                    self.name, REUSED, f"reusing the devbox.json init hook for {env_file}"
                )
            return WirePlan(self.name, CONFIGURED, f"devbox.json already loads {env_file}")
        data.setdefault("shell", {})["init_hook"] = new_hooks
        verb = UPDATED if existing is not None else CREATED
        note = (
            f"updated devbox.json (-duplicate init_hook for {env_file})"
            if reusable
            else f"{verb} devbox.json (+init_hook sourcing {env_file})"
        )
        return WirePlan(
            self.name,
            REUSED if reusable else verb,
            note,
            path=path,
            text=json.dumps(data, indent=2) + "\n",
            root=cwd,
        )

    def unwire(self, cwd: Path, env_file: str) -> None:
        path = cwd / "devbox.json"
        existing = read_optional_editable_text(path, root=cwd)
        if existing is None:
            return
        data = json.loads(existing)
        shell = data.get("shell")
        if not isinstance(shell, dict):
            return
        hooks = shell.get("init_hook")
        if isinstance(hooks, str):
            hooks = [hooks]
        if not isinstance(hooks, list):
            return
        new_hooks = [hook for hook in hooks if not _is_managed_hook(hook)]
        if new_hooks == hooks:
            return
        if new_hooks:
            shell["init_hook"] = new_hooks
        else:
            del shell["init_hook"]
            if not shell:
                del data["shell"]
        if data:
            atomic_write_text(path, json.dumps(data, indent=2) + "\n", root=cwd)
        else:
            path.unlink()


class NoneLoader(Loader):
    """Explicit opt-out: wires nothing. `detect` is always False, so `none` is
    only ever reached through `--loader none` or an unconfigured checkout."""

    name = "none"

    def detect(self, cwd: Path) -> bool:
        return False

    def plan(self, cwd: Path, env_file: str) -> WirePlan:
        return WirePlan(self.name, NOTHING, "")


LOADERS: dict[str, Loader] = {
    "mise": MiseLoader(),
    "direnv": DirenvLoader(),
    "devbox": DevboxLoader(),
    "none": NoneLoader(),
}
