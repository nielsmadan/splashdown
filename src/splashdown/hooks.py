"""Git-hook and env-loader wiring: install/remove the managed post-checkout hook
(coexisting with lefthook / husky / core.hooksPath), edit mise's `_.file`
directive, and manage the project `.gitignore` entries.

Extracted from commands.py so wiring.py and loaders.py can depend on it directly
instead of reaching back into commands via function-local imports (which existed
only to dodge an import cycle)."""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import sys
from pathlib import Path

from . import ENV_FILE_NAME, LOCAL_NAME

POST_CHECKOUT_HOOK = """\
#!/bin/sh
# Splashdown per-checkout provisioning. Fires on git checkout / clone / worktree add.
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


def _ensure_gitignore(cwd: Path) -> None:
    path = cwd / ".gitignore"
    existing = path.read_text() if path.exists() else ""
    present = set(existing.splitlines())
    additions = [e for e in (ENV_FILE_NAME, LOCAL_NAME) if e not in present]
    if not additions:
        return
    prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
    path.write_text(prefix + "\n".join(additions) + "\n")
    print(f"updated .gitignore (+{', '.join(additions)})", file=sys.stderr)


def _ensure_mise_file_directive(cwd: Path) -> None:
    """Ensure mise's config has `_.file = "splashdown.env"` under [env].

    Targets an existing `.mise.toml` when that is the only config present, so we
    edit the file the user already has instead of scaffolding a second one.
    """
    from .tomlio import ensure_mise_file_directive_text  # noqa: PLC0415

    directive = f'_.file = "{ENV_FILE_NAME}"'
    if (cwd / "mise.toml").exists():
        path = cwd / "mise.toml"
    elif (cwd / ".mise.toml").exists():
        path = cwd / ".mise.toml"
    else:
        path = cwd / "mise.toml"
    text = path.read_text() if path.exists() else None
    new_text = ensure_mise_file_directive_text(text)
    if new_text is None:
        return  # directive already present
    path.write_text(new_text)
    verb = "updated" if text is not None else "created"
    print(f"{verb} {path.name} (+{directive})", file=sys.stderr)


def _remove_mise_file_directive(cwd: Path) -> None:
    """Inverse of _ensure_mise_file_directive: drop `_.file = "splashdown.env"`.
    If that empties the `[env]` table it's dropped too; if the whole file is left
    empty it's deleted. Other keys/tables are preserved. Targets `.mise.toml`
    when that's the only config present (mirrors _ensure_mise_file_directive)."""
    from .tomlio import remove_mise_file_directive_text  # noqa: PLC0415

    if (cwd / "mise.toml").exists():
        path = cwd / "mise.toml"
    elif (cwd / ".mise.toml").exists():
        path = cwd / ".mise.toml"
    else:
        return
    new_text = remove_mise_file_directive_text(path.read_text())
    if new_text is None:
        return  # nothing of ours to remove
    if new_text.strip():
        path.write_text(new_text)
        print(f"updated {path.name} (-splashdown env directive)", file=sys.stderr)
    else:
        path.unlink()
        print(f"removed {path.name}", file=sys.stderr)


def _revert_gitignore(cwd: Path) -> None:
    """Inverse of _ensure_gitignore: drop the two splashdown lines if present.
    Matches the exact lines _ensure_gitignore writes (no strip), so a user's
    differently-formatted line (padding, comment) is left alone. Never delete
    .gitignore — we only ever appended to it."""
    path = cwd / ".gitignore"
    if not path.exists():
        return
    managed = {ENV_FILE_NAME, LOCAL_NAME}
    lines = path.read_text().splitlines()
    kept = [ln for ln in lines if ln not in managed]
    if len(kept) == len(lines):
        return
    path.write_text("\n".join(kept) + ("\n" if kept else ""))
    print(f"updated .gitignore (-{', '.join(sorted(managed))})", file=sys.stderr)


def _detect_hook_manager(cwd: Path) -> str:
    """Identify the project's existing hook manager so we coexist instead of clobber.

    Returns one of: "lefthook", "husky", "core-hookspath-other", "none".
    """
    if any((cwd / n).exists() for n in ("lefthook.yml", "lefthook.yaml", ".lefthook.yml")):
        return "lefthook"
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        if "lefthook" in deps:
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
        if out and out != ".githooks":
            return "core-hookspath-other"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return "none"


def _lefthook_config_path(cwd: Path) -> Path:
    for name in ("lefthook.yml", "lefthook.yaml", ".lefthook.yml"):
        path = cwd / name
        if path.exists():
            return path
    return cwd / "lefthook.yml"  # default if lefthook detected only via package.json


def _wire_post_checkout_lefthook(cwd: Path) -> None:
    """Idempotently add a `post-checkout` -> `splash sync` entry to the lefthook config."""
    path = _lefthook_config_path(cwd)
    text = path.read_text() if path.exists() else ""
    if "splashdown" in text and "run: splash" in text:
        _run_lefthook_install(cwd)
        return
    lines = text.splitlines()
    pc_idx = next(
        (i for i, ln in enumerate(lines) if re.match(r"^post-checkout:\s*$", ln)),
        None,
    )
    if pc_idx is None:
        sep = "" if not text or text.endswith("\n") else "\n"
        text = text + sep + ("\npost-checkout:\n  commands:\n    splashdown:\n      run: splash\n")
        path.write_text(text)
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
            indent = len(lines[cmds_idx]) - len(lines[cmds_idx].lstrip())
            addition = [
                " " * (indent + 2) + "splashdown:",
                " " * (indent + 4) + "run: splash",
            ]
            lines = lines[: cmds_idx + 1] + addition + lines[cmds_idx + 1 :]
        else:
            addition = ["  commands:", "    splashdown:", "      run: splash"]
            lines = lines[: pc_idx + 1] + addition + lines[pc_idx + 1 :]
        path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") or text == "" else ""))
    _run_lefthook_install(cwd)
    print(f"wired post-checkout in {path.name} (lefthook)", file=sys.stderr)


def _run_lefthook_install(cwd: Path) -> None:
    """Best-effort: regenerate the lefthook-managed git hooks. Silent if unavailable."""
    candidates: list[list[str]] = []
    if (cwd / "yarn.lock").exists():
        candidates.append(["yarn", "lefthook", "install"])
    if (cwd / "package.json").exists():
        candidates.append(["npx", "--no-install", "lefthook", "install"])
    candidates.append(["lefthook", "install"])
    for cmd in candidates:
        try:
            r = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                timeout=30,
                text=True,
                check=False,
            )
            if r.returncode == 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    print(
        "note: could not run `lefthook install` automatically — run it yourself "
        "to register the post-checkout hook",
        file=sys.stderr,
    )


def _wire_post_checkout_husky(cwd: Path) -> None:
    """Drop a husky post-checkout hook invoking `splash sync`."""
    husky_dir = cwd / ".husky"
    husky_dir.mkdir(exist_ok=True)
    hook = husky_dir / "post-checkout"
    if hook.exists():
        existing = hook.read_text()
        # `.husky/post-checkout` is the user's file (unlike our own `.githooks/`).
        # Never clobber a real hook — only (re)write one that's already ours.
        if existing != POST_CHECKOUT_HOOK and "splash sync" not in existing:
            print(
                "existing .husky/post-checkout is not splashdown's — leaving it "
                "untouched; add `splash sync >&2 || true` to it to enable provisioning",
                file=sys.stderr,
            )
            return
    hook.write_text(POST_CHECKOUT_HOOK)
    hook.chmod(0o755)
    print("wrote .husky/post-checkout (husky)", file=sys.stderr)


def _wire_post_checkout_corehookspath(cwd: Path) -> None:
    """Default path: own .githooks/ and set core.hooksPath."""
    hooks_dir = cwd / ".githooks"
    hooks_dir.mkdir(exist_ok=True)
    hook = hooks_dir / "post-checkout"
    hook.write_text(POST_CHECKOUT_HOOK)
    hook.chmod(0o755)
    with contextlib.suppress(FileNotFoundError):
        subprocess.run(
            ["git", "config", "core.hooksPath", ".githooks"],
            cwd=cwd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print("wrote .githooks/post-checkout, set core.hooksPath", file=sys.stderr)


def _ensure_post_checkout_hook(cwd: Path) -> None:
    """Wire `post-checkout -> splash sync`, coexisting with any existing hook manager."""
    manager = _detect_hook_manager(cwd)
    if manager == "lefthook":
        _wire_post_checkout_lefthook(cwd)
    elif manager == "husky":
        _wire_post_checkout_husky(cwd)
    elif manager == "core-hookspath-other":
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
        except (subprocess.CalledProcessError, FileNotFoundError):
            current = "?"
        print(
            f"warning: core.hooksPath is `{current}` — not wiring automatically. "
            f"Add a post-checkout hook there that runs `splash sync`.",
            file=sys.stderr,
        )
    else:
        _wire_post_checkout_corehookspath(cwd)


def _remove_post_checkout_hook(cwd: Path) -> None:
    """Inverse of _ensure_post_checkout_hook. Surgical: remove only the entry we
    added, keeping unrelated hooks and leaving a user-modified hook in place.

    Unlike `_ensure_*`, this does NOT dispatch on the currently-detected manager:
    the manager can change between init and deinit (e.g. a project gains lefthook
    after init wired `.githooks`), which would otherwise orphan the old hook. Each
    removal is content/marker-guarded, so trying all three only ever touches
    splashdown-owned content."""
    _unwire_post_checkout_lefthook(cwd)
    _unwire_post_checkout_husky(cwd)
    _unwire_post_checkout_corehookspath(cwd)


def _unwire_post_checkout_husky(cwd: Path) -> None:
    hook = cwd / ".husky" / "post-checkout"
    if not hook.exists():
        return
    if hook.read_text() == POST_CHECKOUT_HOOK:
        hook.unlink()
        print("removed .husky/post-checkout", file=sys.stderr)
    else:
        print("note: .husky/post-checkout was modified — left in place", file=sys.stderr)


def _unwire_post_checkout_corehookspath(cwd: Path) -> None:
    hooks_dir = cwd / ".githooks"
    hook = hooks_dir / "post-checkout"
    if hook.exists():
        if hook.read_text() != POST_CHECKOUT_HOOK:
            print("note: .githooks/post-checkout was modified — left in place", file=sys.stderr)
            return
        hook.unlink()
        print("removed .githooks/post-checkout", file=sys.stderr)
    # If .githooks is now empty and core.hooksPath still points at it, unset the
    # config and drop the directory.
    if hooks_dir.is_dir() and not any(hooks_dir.iterdir()):
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
        except (subprocess.CalledProcessError, FileNotFoundError):
            current = ""
        if current == ".githooks":
            with contextlib.suppress(FileNotFoundError):
                subprocess.run(
                    ["git", "config", "--unset", "core.hooksPath"],
                    cwd=cwd,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        with contextlib.suppress(OSError):
            hooks_dir.rmdir()


def _unwire_post_checkout_lefthook(cwd: Path) -> None:
    path = _lefthook_config_path(cwd)
    if not path.exists():
        return
    lines = path.read_text().splitlines()
    # Operate ONLY within the top-level `post-checkout:` block — a job named
    # `splashdown` under some other hook (e.g. pre-commit) must not be touched.
    pc_idx = next((i for i, ln in enumerate(lines) if re.match(r"^post-checkout:\s*$", ln)), None)
    if pc_idx is None:
        return
    end_idx = len(lines)
    for j in range(pc_idx + 1, len(lines)):
        ln = lines[j]
        if ln and not ln[0].isspace() and not ln.startswith("#"):
            end_idx = j
            break
    block = lines[pc_idx:end_idx]
    if not any(ln.strip() == "splashdown:" for ln in block):
        return
    block = _remove_indented_block(block, "splashdown:")
    block = _remove_empty_yaml_block(block, "commands:")
    # If our removal emptied the post-checkout block (splashdown created it from
    # scratch), drop the whole block; otherwise keep the user's other jobs.
    if not any(ln.strip() for ln in block[1:]):
        block = []
    new_lines = lines[:pc_idx] + block + lines[end_idx:]
    text = "\n".join(new_lines).rstrip()
    path.write_text(text + "\n" if text else "")
    _run_lefthook_install(cwd)
    print("removed splashdown post-checkout (lefthook)", file=sys.stderr)


def _remove_indented_block(lines: list[str], key: str) -> list[str]:
    """Drop a `<indent>key` line and every following line indented deeper (plus
    any blank lines between them)."""
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.strip() == key:
            indent = len(ln) - len(ln.lstrip())
            i += 1
            while i < n and (
                not lines[i].strip() or (len(lines[i]) - len(lines[i].lstrip())) > indent
            ):
                i += 1
            continue
        out.append(ln)
        i += 1
    return out


def _remove_empty_yaml_block(lines: list[str], key: str) -> list[str]:
    """Drop a `<indent>key` line that has no deeper-indented body following it."""
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.strip() == key:
            indent = len(ln) - len(ln.lstrip())
            has_body = False
            for j in range(i + 1, n):
                if not lines[j].strip():
                    continue
                has_body = (len(lines[j]) - len(lines[j].lstrip())) > indent
                break
            if not has_body:
                i += 1
                continue
        out.append(ln)
        i += 1
    return out
