"""Git-hook, mise, and gitignore wiring kept below loaders and wiring to preserve acyclic imports."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .constants import LOCAL_NAME, newline_for
from .hook_configs import (
    OVERCOMMIT_CONFIG_NAMES,
    PRE_COMMIT_CONFIG_NAMES,
    PREK_CONFIG_NAMES,
    SIMPLE_GIT_HOOKS_DYNAMIC_NAMES,
    SIMPLE_GIT_HOOKS_JSON_NAMES,
    SIMPLE_GIT_HOOKS_PACKAGE_KEY,
    SPLASH_GUARD,
    existing_config,
    pre_commit_config_path,
    pre_commit_state,
    prek_config_path,
    prek_state,
    simple_git_hooks_state,
    simple_git_hooks_target,
    wire_pre_commit,
    wire_prek,
    wire_simple_git_hooks,
)
from .package_json import package_dependencies, read_package_json
from .safe_files import (
    atomic_write_text,
    read_optional_editable_bytes,
    read_optional_editable_text,
)

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
    f'\'{SPLASH_GUARD}"$SPLASH" hook post-checkout "{{1}}" "{{2}}" "{{3}}" >&2 || true\''
)
_OWNED_HOOKS = {LEGACY_POST_CHECKOUT_HOOK, POST_CHECKOUT_HOOK}


@dataclass(frozen=True)
class HookReadiness:
    manager: str
    ready: bool
    detail: str
    active: bool = False


@dataclass(frozen=True)
class HookDetection:
    """Which manager owns this checkout's post-checkout event, and why."""

    manager: str
    reason: str
    candidates: tuple[str, ...] = ()


GITIGNORE_NAME = ".gitignore"
GITIGNORE_BEGIN = "# >>> splashdown >>>"
GITIGNORE_END = "# <<< splashdown <<<"
_GLOB_ESCAPES = str.maketrans({character: f"\\{character}" for character in "\\*?[]"})
_TRAILING_SPACES = re.compile(r" +$")


class _AmbiguousBlock(ValueError):
    """The managed markers are missing a partner or appear more than once."""


@dataclass(frozen=True)
class _IgnoreMatch:
    source: str
    line: int
    pattern: str

    @property
    def ignores(self) -> bool:
        return not self.pattern.startswith("!")


def gitignore_rule(relpath: str) -> str:
    """A `.gitignore` line matching exactly `relpath` next to that file. Anchored
    with a leading slash so `.env` never also silences a nested `apps/x/.env`, and
    glob characters are escaped so a literal `[`, `*` or trailing space in a name
    is matched as itself."""
    escaped = _TRAILING_SPACES.sub(
        lambda match: "\\ " * len(match.group(0)), relpath.translate(_GLOB_ESCAPES)
    )
    if escaped[:1] in {"#", "!"}:
        escaped = "\\" + escaped
    return "/" + escaped


def gitignore_rule_path(rule: str) -> str:
    """The literal path a rule written by `gitignore_rule` covers."""
    return re.sub(r"\\(.)", r"\1", rule.strip("\r\n").removeprefix("/"))


def _check_ignore(cwd: Path, paths: Sequence[str]) -> tuple[dict[str, _IgnoreMatch], str | None]:
    """Which of `paths` Git already matches, and the rule that matched. The second
    element explains why Git could not answer at all, leaving the map empty."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-v", "-z", "--stdin"],
            cwd=cwd,
            input="\0".join(paths).encode(),
            capture_output=True,
            check=False,
        )
    except OSError:
        return {}, "`git` is not available"
    if result.returncode not in {0, 1}:
        detail = result.stderr.decode(errors="replace").strip().splitlines()
        reason = detail[0].removeprefix("fatal: ") if detail else "git check-ignore failed"
        if reason.startswith("not a git repository"):
            reason = "not a git repository"
        return {}, reason
    fields = result.stdout.decode().split("\0")
    matches: dict[str, _IgnoreMatch] = {}
    for index in range(0, len(fields) - 3, 4):
        source, line, pattern, pathname = fields[index : index + 4]
        if pattern:
            matches[pathname] = _IgnoreMatch(source, int(line), pattern)
    return matches, None


def _tracked(cwd: Path, paths: Sequence[str]) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "--literal-pathspecs", "ls-files", "-z", "--", *paths],
            cwd=cwd,
            capture_output=True,
            check=False,
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    return {name for name in result.stdout.decode().split("\0") if name}


def _block_bounds(lines: list[str]) -> tuple[int, int] | None:
    starts = [i for i, line in enumerate(lines) if line.strip() == GITIGNORE_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == GITIGNORE_END]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise _AmbiguousBlock(
            f"{len(starts)} `{GITIGNORE_BEGIN}` and {len(ends)} `{GITIGNORE_END}` markers"
        )
    return starts[0], ends[0]


def _gitignore_text(
    existing: str, lines: list[str], bounds: tuple[int, int] | None, owned: list[str]
) -> str | None:
    """The `.gitignore` text whose managed block holds exactly `owned`, or None
    when the file already says that."""
    newline = newline_for(existing)
    current = list(lines)
    if current and current[-1] == "":
        current.pop()
    block = [GITIGNORE_BEGIN, *owned, GITIGNORE_END] if owned else []
    if bounds is None:
        separator = [""] if current and current[-1].strip() else []
        updated = [*current, *separator, *block] if block else current
    else:
        start, end = bounds
        head, tail = current[:start], current[end + 1 :]
        if not block:
            if head and not head[-1].strip():
                head = head[:-1]
            elif tail and not tail[0].strip():
                tail = tail[1:]
        updated = [*head, *block, *tail]
    text = newline.join([*updated, ""]) if updated else ""
    return None if text == existing else text


def _managed_ignore_lines(
    cwd: Path, wanted: Sequence[str], lines: list[str], bounds: tuple[int, int] | None
) -> tuple[list[str], str | None, dict[str, _IgnoreMatch]]:
    """The rules splashdown must own: one per wanted path that no rule outside the
    managed block already covers, plus the user negations those rules are about to
    outrank. Git decides coverage; when it cannot answer, a line already spelling
    the path out keeps us from writing a duplicate."""
    matches, unavailable = _check_ignore(cwd, wanted)
    outside = _lines_outside_block(lines, bounds)
    owned: list[str] = []
    outranked: dict[str, _IgnoreMatch] = {}
    for relpath in wanted:
        match = matches.get(relpath)
        if match is not None and not _matched_inside(cwd, match, bounds):
            if match.ignores:
                continue
            outranked[relpath] = match
        if unavailable is not None and {relpath, gitignore_rule(relpath)} & outside:
            continue
        owned.append(gitignore_rule(relpath))
    return owned, unavailable, outranked


def _lines_outside_block(lines: list[str], bounds: tuple[int, int] | None) -> set[str]:
    if bounds is None:
        return {line.strip() for line in lines}
    return {line.strip() for line in [*lines[: bounds[0]], *lines[bounds[1] + 1 :]]}


def _matched_inside(cwd: Path, match: _IgnoreMatch, bounds: tuple[int, int] | None) -> bool:
    """True when the matching rule is one splashdown wrote in the managed block,
    so it proves nothing about what the user's own rules already cover."""
    if bounds is None:
        return False
    source = cwd / match.source
    if source.resolve() != (cwd / GITIGNORE_NAME).resolve():
        return False
    return bounds[0] < match.line - 1 < bounds[1]


def _ensure_gitignore(cwd: Path, paths: Sequence[str]) -> None:
    """Ensure the checkout ignores its local config and generated env outputs.
    Adds only rules Git does not already apply, inside the managed block, and
    leaves every other line of the file untouched."""
    wanted = list(dict.fromkeys([LOCAL_NAME, *paths]))
    path = cwd / GITIGNORE_NAME
    try:
        existing = read_optional_editable_text(path, root=cwd) or ""
        lines = existing.split(newline_for(existing)) if existing else []
        bounds = _block_bounds(lines)
        owned, unavailable, outranked = _managed_ignore_lines(cwd, wanted, lines, bounds)
        previous = lines[bounds[0] + 1 : bounds[1]] if bounds else []
        text = _gitignore_text(existing, lines, bounds, owned)
        if text is not None:
            atomic_write_text(path, text, root=cwd, create=True)
            _report_ignore_change(previous, owned)
    except _AmbiguousBlock as error:
        print(
            f"warning: left {GITIGNORE_NAME} alone: {error}; add "
            f"{', '.join(gitignore_rule(entry) for entry in wanted)} yourself if needed",
            file=sys.stderr,
        )
        return
    except ValueError as error:
        print(f"warning: left {GITIGNORE_NAME} alone: {error}", file=sys.stderr)
        return
    if unavailable is not None:
        print(
            f"  note: git could not report ignore status ({unavailable}), "
            f"so {GITIGNORE_NAME} coverage is unverified",
            file=sys.stderr,
        )
    else:
        _report_uncovered(cwd, wanted, outranked)
    for relpath in sorted(_tracked(cwd, wanted)):
        print(
            f"  note: {relpath} is tracked by git, so its generated values will show up as "
            f"tracked changes; an ignore rule does not untrack it "
            f"(`git rm --cached {relpath}` does)",
            file=sys.stderr,
        )


def _report_uncovered(
    cwd: Path,
    wanted: Sequence[str],
    outranked: Mapping[str, _IgnoreMatch] | None = None,
) -> None:
    """Ask Git which of `wanted` it ends up ignoring and say where that disagrees
    with the user: a managed rule still loses to a later `!` negation, and it beats
    an earlier one. Reads only, so callers on the post-checkout path stay safe."""
    if not wanted:
        return
    final, unavailable = _check_ignore(cwd, wanted)
    if unavailable is not None:
        return
    for relpath in wanted:
        match = final.get(relpath)
        if match is None or not match.ignores:
            reason = (
                f"`{match.pattern}` in {match.source} un-ignores it" if match else "no rule matches"
            )
            print(f"  note: {relpath} is still not ignored ({reason})", file=sys.stderr)
            continue
        negation = (outranked or {}).get(relpath)
        if negation is not None:
            print(
                f"  note: {relpath} is now ignored by the managed block, which overrides "
                f"`{negation.pattern}` in {negation.source}",
                file=sys.stderr,
            )


def _report_ignore_change(previous: list[str], owned: list[str]) -> None:
    added = [rule for rule in owned if rule not in previous]
    dropped = [rule for rule in previous if rule not in owned]
    changes = [f"+{rule}" for rule in added] + [f"-{rule}" for rule in dropped]
    if changes:
        print(f"updated {GITIGNORE_NAME} ({', '.join(changes)})", file=sys.stderr)


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
    """Drop the managed rules for files teardown removed and keep the rules for
    files it left behind. Only the managed block is touched: rules the user wrote
    outside it were never splashdown's to remove."""
    path = cwd / GITIGNORE_NAME
    try:
        existing = read_optional_editable_text(path, root=cwd)
        if existing is None:
            return
        lines = existing.split(newline_for(existing))
        bounds = _block_bounds(lines)
        if bounds is None:
            return
        previous = lines[bounds[0] + 1 : bounds[1]]
        kept = [
            rule for rule in previous if rule.strip() and (cwd / gitignore_rule_path(rule)).exists()
        ]
        text = _gitignore_text(existing, lines, bounds, kept)
    except ValueError as error:
        print(f"warning: left {GITIGNORE_NAME} alone: {error}", file=sys.stderr)
        return
    if text is not None:
        atomic_write_text(path, text, root=cwd, create=True)
        _report_ignore_change(previous, kept)


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


_LEFTHOOK_CONFIG_NAMES = ("lefthook.yml", "lefthook.yaml", ".lefthook.yml", ".lefthook.yaml")

_MANAGER_CONFIG_NAMES: dict[str, tuple[str, ...]] = {
    "lefthook": _LEFTHOOK_CONFIG_NAMES,
    "pre-commit": PRE_COMMIT_CONFIG_NAMES,
    "prek": PREK_CONFIG_NAMES,
    "simple-git-hooks": SIMPLE_GIT_HOOKS_DYNAMIC_NAMES + SIMPLE_GIT_HOOKS_JSON_NAMES,
    "overcommit": OVERCOMMIT_CONFIG_NAMES,
}
_MANAGER_PACKAGES = {
    "lefthook": "lefthook",
    "husky": "husky",
    "simple-git-hooks": "simple-git-hooks",
}
# Markers each manager writes into the hooks it installs. All but overcommit's were
# read off a generated hook and recorded in docs/tests/qa-1.0/runs/.
_INSTALLED_HOOK_SIGNATURES = (
    ("pre-commit", "File generated by pre-commit"),
    ("prek", "File generated by prek"),
    ("lefthook", "call_lefthook"),
    ("simple-git-hooks", "SKIP_SIMPLE_GIT_HOOKS"),
    ("overcommit", "overcommit"),
)
_EVIDENCE_REASON = {
    4: "the installed post-checkout hook was generated by it",
    3: "an installed Git hook was generated by it",
    2: "its configuration file is in the checkout",
    1: "package.json declares it",
}


def _configured_hooks_path(cwd: Path) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "config", "--get", "core.hooksPath"],
                cwd=cwd,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (subprocess.CalledProcessError, OSError):
        return ""


def _git_common_dir(cwd: Path) -> Path | None:
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
    return common_dir if common_dir.is_absolute() else cwd / common_dir


def _effective_hooks_dir(cwd: Path) -> Path | None:
    """The directory Git actually runs hooks from, honoring `core.hooksPath`."""
    configured = _configured_hooks_path(cwd)
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else cwd / path
    common_dir = _git_common_dir(cwd)
    return None if common_dir is None else common_dir / "hooks"


def _hook_signature(path: Path) -> str | None:
    try:
        raw = read_optional_editable_bytes(path)
    except ValueError:
        return None
    if raw is None:
        return None
    text = raw.decode(errors="replace")
    if text in _OWNED_HOOKS:
        return None
    return next(
        (manager for manager, marker in _INSTALLED_HOOK_SIGNATURES if marker in text),
        None,
    )


def _installed_hook_evidence(cwd: Path) -> tuple[set[str], set[str]]:
    """`(managers owning post-checkout, managers owning any other hook)`."""
    directory = _effective_hooks_dir(cwd)
    if directory is None or not directory.is_dir():
        return set(), set()
    owns_event: set[str] = set()
    owns_other: set[str] = set()
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return set(), set()
    for entry in entries:
        if entry.name.endswith(".sample") or not entry.is_file():
            continue
        manager = _hook_signature(entry)
        if manager is None:
            continue
        (owns_event if entry.name == "post-checkout" else owns_other).add(manager)
    return owns_event, owns_other


def _declared_managers(cwd: Path) -> set[str]:
    dependencies = package_dependencies(cwd)
    declared = {
        manager for manager, package in _MANAGER_PACKAGES.items() if package in dependencies
    }
    if SIMPLE_GIT_HOOKS_PACKAGE_KEY in read_package_json(cwd):
        declared.add("simple-git-hooks")
    return declared


def _hook_evidence(cwd: Path) -> dict[int, set[str]]:
    owns_event, owns_other = _installed_hook_evidence(cwd)
    configured = {
        manager
        for manager, names in _MANAGER_CONFIG_NAMES.items()
        if existing_config(cwd, names) is not None
    }
    if (cwd / ".husky").is_dir():
        configured.add("husky")
    return {4: owns_event, 3: owns_other, 2: configured, 1: _declared_managers(cwd)}


def _husky_hooks_dir(cwd: Path, directory: Path) -> bool:
    husky = cwd / ".husky"
    return directory == husky or husky in directory.parents


def detect_hook_configuration(cwd: Path) -> HookDetection:
    """Identify who owns this checkout's post-checkout event so we coexist instead
    of clobber.

    Precedence runs from what Git enforces down to what a project merely declares.
    `core.hooksPath` decides first, because Git runs those hooks and no others. Then
    the installed post-checkout hook, then any other installed hook, then a manager's
    configuration file, then a package.json declaration. Ties inside one level are
    reported as a conflict rather than resolved by guessing.
    """
    configured_path = _configured_hooks_path(cwd)
    if configured_path:
        path = Path(configured_path)
        directory = path if path.is_absolute() else cwd / path
        if _husky_hooks_dir(cwd, directory):
            return HookDetection("husky", f"core.hooksPath is `{configured_path}`", ("husky",))
        return HookDetection("core-hookspath-other", f"core.hooksPath is `{configured_path}`")
    evidence = _hook_evidence(cwd)
    for level in (4, 3, 2, 1):
        found = tuple(sorted(evidence[level]))
        if not found:
            continue
        if len(found) == 1:
            return HookDetection(found[0], _EVIDENCE_REASON[level], found)
        return HookDetection("conflict", _EVIDENCE_REASON[level], found)
    return HookDetection("none", "no hook manager is configured")


def _detect_hook_manager(cwd: Path) -> str:
    return detect_hook_configuration(cwd).manager


def _lefthook_config_path(cwd: Path) -> Path:
    # Default when lefthook was detected only through package.json.
    return existing_config(cwd, _LEFTHOOK_CONFIG_NAMES) or cwd / "lefthook.yml"


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


def _run_manager_install(cwd: Path, argv: list[str], label: str) -> bool:
    """Best-effort: let the project's own hook manager regenerate its Git hooks.
    Silent on success, and never installs the manager itself."""
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=30,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    print(
        f"note: could not run `{label}` automatically — run it yourself "
        "to register the post-checkout hook",
        file=sys.stderr,
    )
    return False


def _run_lefthook_install(cwd: Path) -> bool:
    return _run_manager_install(cwd, ["lefthook", "install"], "lefthook install")


def _run_pre_commit_install(cwd: Path) -> bool:
    argv = ["pre-commit", "install", "--hook-type", "post-checkout"]
    return _run_manager_install(cwd, argv, " ".join(argv))


def _run_prek_install(cwd: Path) -> bool:
    argv = ["prek", "install", "--hook-type", "post-checkout"]
    return _run_manager_install(cwd, argv, " ".join(argv))


def _run_simple_git_hooks_install(cwd: Path) -> bool:
    """simple-git-hooks ships no global executable, so only the copy the project
    already installed is run. Nothing is fetched."""
    local = cwd / "node_modules" / ".bin" / "simple-git-hooks"
    if not local.is_file():
        print(
            "note: simple-git-hooks is not installed in this checkout — run your "
            "package manager's install, then `npx simple-git-hooks`",
            file=sys.stderr,
        )
        return False
    return _run_manager_install(cwd, [str(local)], "npx simple-git-hooks")


def _husky_is_installed(cwd: Path) -> bool:
    directory = _effective_hooks_dir(cwd)
    return (
        directory is not None
        and _husky_hooks_dir(cwd, directory)
        and (directory / "post-checkout").is_file()
    )


def _activate_husky(cwd: Path) -> bool:
    if _husky_is_installed(cwd):
        return True
    print(
        "note: husky's hooks are not installed in this checkout — run your package "
        "manager's install (husky's `prepare` script) here",
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
    if _husky_state(cwd) == "ok":
        return True
    atomic_write_text(hook, POST_CHECKOUT_HOOK, root=cwd, create=True, mode=0o755)
    print("wrote .husky/post-checkout (husky)", file=sys.stderr)
    return True


def _native_hook_path(cwd: Path) -> Path | None:
    common_dir = _git_common_dir(cwd)
    return None if common_dir is None else common_dir.resolve() / "hooks" / "post-checkout"


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


def _lefthook_state(cwd: Path) -> str:
    path = _lefthook_config_path(cwd)
    lines = path.read_text().splitlines() if path.exists() else []
    owned = _lefthook_splashdown_job(lines)
    if owned is None:
        return "missing"
    _, _, run_index = owned
    value = _lefthook_run_value(lines[run_index]) if run_index is not None else None
    if value == _LEFTHOOK_RUN:
        return "ok"
    return "legacy" if value == _LEFTHOOK_LEGACY_RUN else "modified"


def _husky_state(cwd: Path) -> str:
    hook = cwd / ".husky" / "post-checkout"
    if not hook.exists():
        return "missing"
    if hook.read_text() != POST_CHECKOUT_HOOK or not hook.stat().st_mode & 0o111:
        return "modified"
    return "ok"


def _husky_hook_path(cwd: Path) -> Path:
    return cwd / ".husky" / "post-checkout"


def _simple_git_hooks_path(cwd: Path) -> Path:
    return simple_git_hooks_target(cwd)[1]


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
_PRE_COMMIT_MANUAL = (
    "Forward the event from a local hook in .pre-commit-config.yaml, invoking a\n"
    "trusted absolute splash executable. post-checkout hooks need `always_run: true`,\n"
    "and pre-commit supplies the event in the environment rather than as arguments:\n"
    "    entry: sh -c '/trusted/path/splash hook post-checkout "
    '"$PRE_COMMIT_FROM_REF" "$PRE_COMMIT_TO_REF" "$PRE_COMMIT_CHECKOUT_TYPE" >&2 || true\'\n'
    "Then run `pre-commit install --hook-type post-checkout`."
)
_PREK_MANUAL = (
    "Forward the event from a local hook in prek.toml, invoking a trusted absolute\n"
    "splash executable. post-checkout hooks need `always_run = true`, and prek\n"
    "supplies the event in the environment rather than as arguments:\n"
    "    entry = \"sh -c '/trusted/path/splash hook post-checkout "
    '\\"$PRE_COMMIT_FROM_REF\\" \\"$PRE_COMMIT_TO_REF\\" \\"$PRE_COMMIT_CHECKOUT_TYPE\\" '
    ">&2 || true'\"\n"
    "Then run `prek install --hook-type post-checkout`."
)
_SIMPLE_GIT_HOOKS_MANUAL = (
    "Forward the event from the post-checkout entry in your simple-git-hooks\n"
    "configuration, invoking a trusted absolute splash executable. The command is\n"
    "copied into the hook script verbatim, so shell positionals carry the event:\n"
    '    "post-checkout": "/trusted/path/splash hook post-checkout '
    '\\"$1\\" \\"$2\\" \\"$3\\" >&2 || true"\n'
    "Then run `npx simple-git-hooks`."
)
_OVERCOMMIT_MANUAL = (
    "Splashdown has no automatic adapter for it, so .overcommit.yml is left untouched.\n"
    "Add an Overcommit PostCheckout hook that runs a trusted absolute splash executable\n"
    "and forwards the three event values:\n"
    '    /trusted/path/splash hook post-checkout "$1" "$2" "$3" >&2 || true'
)
_HOOKSPATH_MANUAL = (
    "Forward the event from the post-checkout hook in your core.hooksPath\n"
    "directory, invoking a trusted absolute splash executable:\n"
    '    /trusted/path/splash hook post-checkout "$1" "$2" "$3" >&2 || true\n'
    "Splashdown never writes into a custom hooks path, so add that line yourself."
)
_CONFLICT_MANUAL = (
    "Splashdown will not guess which one owns post-checkout, so nothing was changed.\n"
    "Add splashdown's forwarding entry to the manager you actually use, invoking a\n"
    "trusted absolute splash executable with the three event values, or remove the\n"
    "configuration you no longer use and run `splash doctor --fix` again."
)
_NATIVE_MANUAL = (
    "Forward the event from the hook this project owns, by\n"
    "invoking a trusted absolute splash executable:\n"
    '    /trusted/path/splash hook post-checkout "$1" "$2" "$3" >&2 || true\n'
    "`splash trust` installs that line for you when the hook is Splashdown's own."
)


@dataclass(frozen=True)
class _Adapter:
    """One hook manager splashdown integrates with automatically. `configure`
    writes project-owned configuration; `install` is the local activation the
    trust flow performs."""

    manager: str
    configure: Callable[[Path], bool]
    state: Callable[[Path], str]
    install: Callable[[Path], bool]
    config_path: Callable[[Path], Path]
    manual: str


_ADAPTERS: dict[str, _Adapter] = {
    "lefthook": _Adapter(
        "lefthook",
        _wire_post_checkout_lefthook,
        _lefthook_state,
        _run_lefthook_install,
        _lefthook_config_path,
        _LEFTHOOK_MANUAL,
    ),
    "husky": _Adapter(
        "husky",
        _wire_post_checkout_husky,
        _husky_state,
        _activate_husky,
        _husky_hook_path,
        _HUSKY_MANUAL,
    ),
    "pre-commit": _Adapter(
        "pre-commit",
        wire_pre_commit,
        pre_commit_state,
        _run_pre_commit_install,
        pre_commit_config_path,
        _PRE_COMMIT_MANUAL,
    ),
    "prek": _Adapter(
        "prek",
        wire_prek,
        prek_state,
        _run_prek_install,
        prek_config_path,
        _PREK_MANUAL,
    ),
    "simple-git-hooks": _Adapter(
        "simple-git-hooks",
        wire_simple_git_hooks,
        simple_git_hooks_state,
        _run_simple_git_hooks_install,
        _simple_git_hooks_path,
        _SIMPLE_GIT_HOOKS_MANUAL,
    ),
}

_UNMANAGED_MANUAL = {
    "core-hookspath-other": _HOOKSPATH_MANUAL,
    "overcommit": _OVERCOMMIT_MANUAL,
    "conflict": _CONFLICT_MANUAL,
}
_MANAGER_MANUAL = {
    **{manager: adapter.manual for manager, adapter in _ADAPTERS.items()},
    **_UNMANAGED_MANUAL,
}

_STATE_DETAIL = {
    "missing": "{manager} post-checkout is missing",
    "modified": "{manager} post-checkout was modified",
    "legacy": "{manager} post-checkout is sync-only",
    "unrecognized": "{manager} configuration is in a shape splashdown cannot edit",
}


def _manager_hook_installed(cwd: Path, manager: str) -> bool:
    directory = _effective_hooks_dir(cwd)
    if directory is None:
        return False
    hook = directory / "post-checkout"
    if not hook.is_file():
        return False
    return True if manager == "husky" else _hook_signature(hook) == manager


def _unmanaged_readiness(cwd: Path, detection: HookDetection) -> HookReadiness:
    if detection.manager == "conflict":
        names = ", ".join(detection.candidates)
        return HookReadiness(
            detection.manager, False, f"several hook managers are configured ({names})"
        )
    if detection.manager == "core-hookspath-other":
        return HookReadiness(detection.manager, False, detection.reason)
    if detection.manager == "overcommit":
        return HookReadiness(detection.manager, False, "overcommit owns this repository's hooks")
    native_hook = _native_hook_path(cwd)
    active = (
        native_hook is not None
        and native_hook.exists()
        and native_hook.read_text() == POST_CHECKOUT_HOOK
        and bool(native_hook.stat().st_mode & 0o111)
    )
    detail = (
        "native hook forwards post-checkout events"
        if active
        else "native post-checkout is missing or modified"
    )
    return HookReadiness(detection.manager, active, detail, active)


def post_checkout_readiness(cwd: Path) -> HookReadiness:
    """Whether splashdown's own entry is in place for whoever owns this checkout's
    post-checkout event. `ready` covers the project-owned configuration; `active`
    covers whether that manager's hook is installed locally too."""
    detection = detect_hook_configuration(cwd)
    adapter = _ADAPTERS.get(detection.manager)
    if adapter is None:
        return _unmanaged_readiness(cwd, detection)
    state = adapter.state(cwd)
    active = _manager_hook_installed(cwd, adapter.manager)
    if state == "ok":
        detail = (
            f"{adapter.manager} forwards post-checkout events"
            if active
            else f"{adapter.manager} forwards post-checkout events once its hooks are installed"
        )
        return HookReadiness(adapter.manager, True, detail, active)
    template = _STATE_DETAIL.get(state, _STATE_DETAIL["missing"])
    return HookReadiness(adapter.manager, False, template.format(manager=adapter.manager), active)


def post_checkout_files(cwd: Path) -> tuple[Path, ...]:
    """The project files this checkout's hook integration writes, so a caller can
    report what changed without parsing each adapter's message."""
    detection = detect_hook_configuration(cwd)
    adapter = _ADAPTERS.get(detection.manager)
    if adapter is not None:
        if adapter.state(cwd) == "unrecognized":
            return ()
        return (adapter.config_path(cwd),)
    if detection.manager in _UNMANAGED_MANUAL:
        return ()
    native = _native_hook_path(cwd)
    return () if native is None else (native,)


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
    current = _configured_hooks_path(cwd) or "?"
    print(
        f"warning: core.hooksPath is `{current}` — not wiring automatically. "
        "Use a trusted absolute splash path and forward `$1`, `$2`, `$3`.",
        file=sys.stderr,
    )


def _report_conflicting_managers(detection: HookDetection) -> None:
    names = ", ".join(detection.candidates)
    print(
        f"warning: several hook managers are configured ({names}) — not wiring "
        "automatically. Add splashdown's post-checkout entry to the one you use.",
        file=sys.stderr,
    )


def _configure_post_checkout_hook(cwd: Path) -> None:
    """Write the project-owned hook configuration only. The native wrapper lives
    in the local `.git` directory, so it is installed by trusted activation."""
    detection = detect_hook_configuration(cwd)
    adapter = _ADAPTERS.get(detection.manager)
    if adapter is not None:
        adapter.configure(cwd)
        return
    if detection.manager == "core-hookspath-other":
        _warn_custom_hooks_path(cwd)
    elif detection.manager == "conflict":
        _report_conflicting_managers(detection)
    elif detection.manager == "overcommit":
        _report_manual_hook_activation(cwd)
    elif _native_hook_path(cwd) is None:
        print("note: not a Git checkout; post-checkout hook not installed", file=sys.stderr)
    else:
        print(
            "note: the local post-checkout hook is installed by `splash trust`",
            file=sys.stderr,
        )


def _ensure_post_checkout_hook(cwd: Path) -> None:
    detection = detect_hook_configuration(cwd)
    adapter = _ADAPTERS.get(detection.manager)
    if adapter is not None:
        if adapter.configure(cwd):
            adapter.install(cwd)
        return
    if detection.manager == "core-hookspath-other":
        _warn_custom_hooks_path(cwd)
    elif detection.manager == "conflict":
        _report_conflicting_managers(detection)
    elif detection.manager == "overcommit":
        _report_manual_hook_activation(cwd)
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
    detection = detect_hook_configuration(cwd)
    adapter = _ADAPTERS.get(detection.manager)
    if adapter is not None:
        if adapter.state(cwd) != "ok":
            _report_manual_hook_activation(cwd)
            return False
        return adapter.install(cwd)
    if detection.manager in _UNMANAGED_MANUAL:
        _report_manual_hook_activation(cwd)
        return False
    return _wire_post_checkout_native(cwd)
