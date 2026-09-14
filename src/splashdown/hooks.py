"""Git-hook, mise, and gitignore wiring kept below loaders and wiring to preserve acyclic imports."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .constants import ENV_FILE_NAME, LOCAL_NAME
from .package_json import package_dependencies
from .safe_files import atomic_write_text, read_optional_editable_text

LEGACY_POST_CHECKOUT_HOOK = """\
#!/bin/sh
# Splashdown per-checkout provisioning. Fires on checkout and worktree add.
set -e
TOP=$(git rev-parse --show-toplevel) || exit 0
cd "$TOP"
[ -f splashdown.toml ] || exit 0
if command -v splash >/dev/null 2>&1; then
    splash sync >&2 || true
else
    echo "post-checkout: \\`splash\\` not on PATH — install splashdown" >&2
fi
exit 0
"""

POST_CHECKOUT_HOOK = """\
#!/bin/sh
# Splashdown per-checkout provisioning. Fires on checkout and worktree add.
set -e
TOP=$PWD
[ -f splashdown.toml ] || exit 0
SPLASH=$(command -v splash) || {
    echo "post-checkout: \\`splash\\` not on PATH — install splashdown" >&2
    exit 0
}
case "$SPLASH" in
    /*) ;;
    *) SPLASH="$TOP/$SPLASH" ;;
esac
case "$SPLASH" in
    "$TOP"/*)
        echo "post-checkout: refusing checkout-controlled splash executable" >&2
        exit 0
        ;;
esac
"$SPLASH" hook post-checkout "$1" "$2" "$3" >&2 || true
exit 0
"""

_LEFTHOOK_LEGACY_RUN = "splash"
_LEFTHOOK_RUN = (
    "'TOP=$PWD; SPLASH=$(command -v splash) || exit 0; "
    'case "$SPLASH" in /*) ;; *) SPLASH="$TOP/$SPLASH";; esac; '
    'case "$SPLASH" in "$TOP"/*) echo "post-checkout: refusing checkout-controlled '
    'splash executable" >&2; exit 0;; esac; '
    '"$SPLASH" hook post-checkout "{1}" "{2}" "{3}" >&2 || true\''
)
_OWNED_HOOKS = {LEGACY_POST_CHECKOUT_HOOK, POST_CHECKOUT_HOOK}


@dataclass(frozen=True)
class HookReadiness:
    manager: str
    ready: bool
    detail: str


def _ensure_gitignore(cwd: Path) -> None:
    path = cwd / ".gitignore"
    existing = path.read_text() if path.exists() else ""
    present = set(existing.splitlines())
    additions = [entry for entry in (ENV_FILE_NAME, LOCAL_NAME) if entry not in present]
    if not additions:
        return
    prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
    path.write_text(prefix + "\n".join(additions) + "\n")
    print(f"updated .gitignore (+{', '.join(additions)})", file=sys.stderr)


def mise_config_path(cwd: Path) -> Path:
    """The mise config splashdown reads and edits. Prefers an existing file so we
    edit the one the user already has instead of scaffolding a second one; falls
    back to `mise.toml` as the path to create."""
    if (cwd / "mise.toml").exists():
        return cwd / "mise.toml"
    if (cwd / ".mise.toml").exists():
        return cwd / ".mise.toml"
    return cwd / "mise.toml"


def _revert_gitignore(cwd: Path) -> None:
    """Inverse of _ensure_gitignore: drop splashdown's exact lines if present.
    Matches the exact lines _ensure_gitignore writes (no strip), so a user's
    differently-formatted line (padding, comment) is left alone. Never delete
    .gitignore — we only ever appended to it."""
    path = cwd / ".gitignore"
    if not path.exists():
        return
    lines = path.read_text().splitlines()
    managed = {ENV_FILE_NAME, LOCAL_NAME}
    reported = [entry for entry in (ENV_FILE_NAME, LOCAL_NAME) if entry in lines]
    kept = [ln for ln in lines if ln not in managed]
    if len(kept) == len(lines):
        return
    path.write_text("\n".join(kept) + ("\n" if kept else ""))
    print(f"updated .gitignore (-{', '.join(reported)})", file=sys.stderr)


def _git_worktree_root(cwd: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    root = result.stdout.strip()
    if result.returncode != 0 or not root:
        return None
    return Path(root).resolve()


def _nested_worktree(cwd: Path, worktree_root: Path | None) -> bool:
    return worktree_root is not None and worktree_root != cwd.resolve()


def _nested_project(cwd: Path) -> bool:
    return _nested_worktree(cwd, _git_worktree_root(cwd))


def _print_nested_checkout_hook_note(cwd: Path) -> None:
    print(
        "note: post-checkout hook not installed for nested project; "
        f"run `splash --cwd {cwd.resolve()} sync` after checkout",
        file=sys.stderr,
    )


def _detect_hook_manager(cwd: Path) -> str:
    """Identify the project's existing hook manager so we coexist instead of clobber.

    Returns one of: "lefthook", "husky", "core-hookspath-other", "none".
    """
    if any((cwd / n).exists() for n in ("lefthook.yml", "lefthook.yaml", ".lefthook.yml")):
        return "lefthook"
    if "lefthook" in package_dependencies(cwd):
        return "lefthook"
    if (cwd / ".husky").is_dir():
        return "husky"
    try:
        out = (
            subprocess.check_output(
                ["git", "config", "--get", "core.hooksPath"],
                cwd=cwd,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
        if out:
            return "core-hookspath-other"
    except (subprocess.CalledProcessError, OSError):
        pass
    return "none"


def _lefthook_config_path(cwd: Path) -> Path:
    for name in ("lefthook.yml", "lefthook.yaml", ".lefthook.yml"):
        path = cwd / name
        if path.exists():
            return path
    return cwd / "lefthook.yml"  # default if lefthook detected only via package.json


def _yaml_block_end(lines: list[str], start: int) -> int:
    indent = len(lines[start]) - len(lines[start].lstrip())
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            return index
    return len(lines)


def _lefthook_splashdown_job(
    lines: list[str],
) -> tuple[int, int, int | None] | None:
    post = next(
        (index for index, line in enumerate(lines) if re.fullmatch(r"post-checkout:\s*", line)),
        None,
    )
    if post is None:
        return None
    post_end = _yaml_block_end(lines, post)
    commands = next(
        (
            index
            for index in range(post + 1, post_end)
            if lines[index].strip() == "commands:" and not lines[index].lstrip().startswith("#")
        ),
        None,
    )
    if commands is None:
        return None
    commands_indent = len(lines[commands]) - len(lines[commands].lstrip())
    commands_end = min(_yaml_block_end(lines, commands), post_end)
    job = next(
        (
            index
            for index in range(commands + 1, commands_end)
            if lines[index].strip() == "splashdown:"
            and not lines[index].lstrip().startswith("#")
            and len(lines[index]) - len(lines[index].lstrip()) == commands_indent + 2
        ),
        None,
    )
    if job is None:
        return None
    job_indent = len(lines[job]) - len(lines[job].lstrip())
    job_end = min(_yaml_block_end(lines, job), commands_end)
    run = next(
        (
            index
            for index in range(job + 1, job_end)
            if re.match(r"^\s+run:\s*", lines[index])
            and not lines[index].lstrip().startswith("#")
            and len(lines[index]) - len(lines[index].lstrip()) == job_indent + 2
        ),
        None,
    )
    return (job, job_end, run)


def _lefthook_run_value(line: str) -> str | None:
    match = re.fullmatch(r"\s*run:\s*(.*?)\s*", line)
    return match.group(1) if match else None


def _wire_post_checkout_lefthook(cwd: Path) -> bool:
    """Write splashdown's job into the project's lefthook configuration. Returns
    True when the configuration carries our current job afterwards. Installing
    the local hooks is activation, not configuration, and happens elsewhere."""
    path = _lefthook_config_path(cwd)
    text = read_optional_editable_text(path, root=cwd) or ""
    lines = text.splitlines()
    owned = _lefthook_splashdown_job(lines)
    if owned is not None:
        _, _, run_index = owned
        value = _lefthook_run_value(lines[run_index]) if run_index is not None else None
        if value == _LEFTHOOK_RUN:
            return True
        if value == _LEFTHOOK_LEGACY_RUN and run_index is not None:
            run_indent = lines[run_index][: len(lines[run_index]) - len(lines[run_index].lstrip())]
            lines[run_index] = f"{run_indent}run: {_LEFTHOOK_RUN}"
            atomic_write_text(
                path,
                "\n".join(lines) + ("\n" if text.endswith("\n") else ""),
                root=cwd,
                create=True,
            )
            print(f"updated post-checkout in {path.name} (lefthook)", file=sys.stderr)
            return True
        print(
            f"existing splashdown job in {path.name} was modified — leaving it untouched",
            file=sys.stderr,
        )
        return False
    pc_idx = next(
        (i for i, ln in enumerate(lines) if re.match(r"^post-checkout:\s*$", ln)),
        None,
    )
    if pc_idx is None:
        sep = "" if not text or text.endswith("\n") else "\n"
        text = (
            text
            + sep
            + (f"\npost-checkout:\n  commands:\n    splashdown:\n      run: {_LEFTHOOK_RUN}\n")
        )
        atomic_write_text(path, text, root=cwd, create=True)
    else:
        end_idx = len(lines)
        for j in range(pc_idx + 1, len(lines)):
            ln = lines[j]
            if ln and not ln[0].isspace() and not ln.startswith("#"):
                end_idx = j
                break
        cmds_idx = next(
            (j for j in range(pc_idx + 1, end_idx) if re.match(r"^\s+commands:\s*$", lines[j])),
            None,
        )
        if cmds_idx is not None:
            commands_indent = len(lines[cmds_idx]) - len(lines[cmds_idx].lstrip())
            addition = [
                " " * (commands_indent + 2) + "splashdown:",
                " " * (commands_indent + 4) + f"run: {_LEFTHOOK_RUN}",
            ]
            lines = lines[: cmds_idx + 1] + addition + lines[cmds_idx + 1 :]
        else:
            addition = ["  commands:", "    splashdown:", f"      run: {_LEFTHOOK_RUN}"]
            lines = lines[: pc_idx + 1] + addition + lines[pc_idx + 1 :]
        atomic_write_text(
            path,
            "\n".join(lines) + ("\n" if text.endswith("\n") or text == "" else ""),
            root=cwd,
            create=True,
        )
    print(f"wired post-checkout in {path.name} (lefthook)", file=sys.stderr)
    return True


def _run_lefthook_install(cwd: Path) -> bool:
    """Best-effort: regenerate the lefthook-managed git hooks. Silent if unavailable."""
    try:
        r = subprocess.run(
            ["lefthook", "install"],
            cwd=cwd,
            capture_output=True,
            timeout=30,
            text=True,
            check=False,
        )
        if r.returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    print(
        "note: could not run `lefthook install` automatically — run it yourself "
        "to register the post-checkout hook",
        file=sys.stderr,
    )
    return False


def _wire_post_checkout_husky(cwd: Path) -> bool:
    husky_dir = cwd / ".husky"
    husky_dir.mkdir(exist_ok=True)
    hook = husky_dir / "post-checkout"
    existing = read_optional_editable_text(hook, root=cwd)
    # Only overwrite hooks whose full contents match a Splashdown template.
    if existing is not None and existing not in _OWNED_HOOKS:
        print(
            "existing .husky/post-checkout is not splashdown's — leaving it "
            "untouched; use a trusted absolute splash path and forward `$1`, `$2`, `$3`",
            file=sys.stderr,
        )
        return False
    atomic_write_text(hook, POST_CHECKOUT_HOOK, root=cwd, create=True, mode=0o755)
    print("wrote .husky/post-checkout (husky)", file=sys.stderr)
    return True


def _native_hook_path(cwd: Path) -> Path | None:
    try:
        raw = (
            subprocess.check_output(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=cwd,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    common_dir = Path(raw)
    if not common_dir.is_absolute():
        common_dir = cwd / common_dir
    return common_dir.resolve() / "hooks" / "post-checkout"


def _wire_post_checkout_native(cwd: Path) -> bool:
    if _nested_project(cwd):
        _print_nested_checkout_hook_note(cwd)
        return False
    hook = _native_hook_path(cwd)
    if hook is None:
        print("note: not a Git checkout; post-checkout hook not installed", file=sys.stderr)
        return False
    hook.parent.mkdir(parents=True, exist_ok=True)
    existing = read_optional_editable_text(hook, root=hook.parent)
    if existing is not None and existing not in _OWNED_HOOKS:
        print(
            f"existing {hook} is not splashdown's — leaving it untouched; "
            "use a trusted absolute splash path and forward `$1`, `$2`, `$3`",
            file=sys.stderr,
        )
        return False
    atomic_write_text(
        hook,
        POST_CHECKOUT_HOOK,
        root=hook.parent,
        create=True,
        mode=0o755,
    )
    print(f"wrote {hook}", file=sys.stderr)
    return True


def post_checkout_readiness(cwd: Path) -> HookReadiness:
    manager = _detect_hook_manager(cwd)
    if manager == "lefthook":
        path = _lefthook_config_path(cwd)
        lines = path.read_text().splitlines() if path.exists() else []
        owned = _lefthook_splashdown_job(lines)
        if owned is not None:
            _, _, run_index = owned
            value = _lefthook_run_value(lines[run_index]) if run_index is not None else None
            if value == _LEFTHOOK_RUN:
                return HookReadiness(manager, True, "lefthook forwards post-checkout events")
            if value == _LEFTHOOK_LEGACY_RUN:
                return HookReadiness(manager, False, "lefthook post-checkout is sync-only")
        return HookReadiness(manager, False, "lefthook post-checkout is missing or modified")
    if manager == "husky":
        hook = cwd / ".husky" / "post-checkout"
        if (
            hook.exists()
            and hook.read_text() == POST_CHECKOUT_HOOK
            and bool(hook.stat().st_mode & 0o111)
        ):
            return HookReadiness(manager, True, "husky forwards post-checkout events")
        return HookReadiness(manager, False, "husky post-checkout is missing or modified")
    if manager == "core-hookspath-other":
        return HookReadiness(manager, False, "core.hooksPath points to a custom directory")
    native_hook = _native_hook_path(cwd)
    if (
        native_hook is not None
        and native_hook.exists()
        and native_hook.read_text() == POST_CHECKOUT_HOOK
        and bool(native_hook.stat().st_mode & 0o111)
    ):
        return HookReadiness(manager, True, "native hook forwards post-checkout events")
    return HookReadiness(manager, False, "native post-checkout is missing or modified")


_LEFTHOOK_MANUAL = (
    "Forward the event from the post-checkout job in your lefthook configuration,\n"
    "invoking a trusted absolute splash executable. lefthook substitutes `{1}`,\n"
    "`{2}` and `{3}`; shell positionals expand to nothing there:\n"
    '    run: /trusted/path/splash hook post-checkout "{1}" "{2}" "{3}" >&2 || true\n'
    "`splash doctor --fix` writes that job when the existing one is Splashdown's own."
)
_HUSKY_MANUAL = (
    "Forward the event from .husky/post-checkout, invoking a trusted absolute\n"
    "splash executable:\n"
    '    /trusted/path/splash hook post-checkout "$1" "$2" "$3" >&2 || true\n'
    "`splash doctor --fix` writes that hook when the existing one is Splashdown's own."
)
_HOOKSPATH_MANUAL = (
    "Forward the event from the post-checkout hook in your core.hooksPath\n"
    "directory, invoking a trusted absolute splash executable:\n"
    '    /trusted/path/splash hook post-checkout "$1" "$2" "$3" >&2 || true\n'
    "Splashdown never writes into a custom hooks path, so add that line yourself."
)
_NATIVE_MANUAL = (
    "Forward the event from the hook this project owns, by\n"
    "invoking a trusted absolute splash executable:\n"
    '    /trusted/path/splash hook post-checkout "$1" "$2" "$3" >&2 || true\n'
    "`splash trust` installs that line for you when the hook is Splashdown's own."
)
_MANAGER_MANUAL = {
    "lefthook": _LEFTHOOK_MANUAL,
    "husky": _HUSKY_MANUAL,
    "core-hookspath-other": _HOOKSPATH_MANUAL,
}


def post_checkout_manual_instructions(cwd: Path) -> str:
    readiness = post_checkout_readiness(cwd)
    body = _MANAGER_MANUAL.get(readiness.manager, _NATIVE_MANUAL)
    head, *rest = body.splitlines()
    return "\n".join(
        [
            f"{readiness.detail}. {head}",
            *rest,
            "Otherwise run `splash bootstrap` manually after creating a worktree.",
        ]
    )


def _warn_custom_hooks_path(cwd: Path) -> None:
    try:
        current = (
            subprocess.check_output(
                ["git", "config", "--get", "core.hooksPath"],
                cwd=cwd,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (subprocess.CalledProcessError, OSError):
        current = "?"
    print(
        f"warning: core.hooksPath is `{current}` — not wiring automatically. "
        "Use a trusted absolute splash path and forward `$1`, `$2`, `$3`.",
        file=sys.stderr,
    )


def _configure_post_checkout_hook(cwd: Path) -> None:
    """Write the project-owned hook configuration only. The native wrapper lives
    in the local `.git` directory, so it is installed by trusted activation."""
    manager = _detect_hook_manager(cwd)
    if manager == "lefthook":
        _wire_post_checkout_lefthook(cwd)
    elif manager == "husky":
        _wire_post_checkout_husky(cwd)
    elif manager == "core-hookspath-other":
        _warn_custom_hooks_path(cwd)
    elif _native_hook_path(cwd) is None:
        print("note: not a Git checkout; post-checkout hook not installed", file=sys.stderr)
    else:
        print(
            "note: the local post-checkout hook is installed by `splash trust`",
            file=sys.stderr,
        )


def _ensure_post_checkout_hook(cwd: Path) -> None:
    manager = _detect_hook_manager(cwd)
    if manager == "lefthook":
        if _wire_post_checkout_lefthook(cwd):
            _run_lefthook_install(cwd)
    elif manager == "husky":
        _wire_post_checkout_husky(cwd)
    elif manager == "core-hookspath-other":
        _warn_custom_hooks_path(cwd)
    else:
        _wire_post_checkout_native(cwd)


def _report_manual_hook_activation(cwd: Path) -> None:
    """The manager's own configuration is tracked project content, so activation
    never rewrites it. Name the edit that finishes the integration instead."""
    first, *rest = post_checkout_manual_instructions(cwd).splitlines()
    print(f"note: {first}", file=sys.stderr)
    for line in rest:
        print(f"      {line}", file=sys.stderr)


def _activate_post_checkout_hook(cwd: Path) -> bool:
    readiness = post_checkout_readiness(cwd)
    if readiness.ready:
        if readiness.manager == "lefthook":
            return _run_lefthook_install(cwd)
        return True
    if readiness.manager in {"lefthook", "husky"}:
        _report_manual_hook_activation(cwd)
        return False
    if readiness.manager == "core-hookspath-other":
        print(
            "note: core.hooksPath prevents event-aware automatic handling; "
            "use `splash bootstrap` manually",
            file=sys.stderr,
        )
        return False
    return _wire_post_checkout_native(cwd)
