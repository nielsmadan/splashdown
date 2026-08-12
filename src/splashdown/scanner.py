from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .loaders import LOADERS
from .recipe import _TEMPLATE_NAMES, Recipe, template_refs


@dataclass
class AppInventory:
    """One consumer (app) inside the project that splashdown will wire."""

    name: str  # e.g. "api"
    path: Path  # absolute path to the app's root directory
    profile: str  # the Profile name that matched, or "unknown"
    project_path: Path | None = None


@dataclass
class ProjectInventory:
    """What the Scanner found about the repo as a whole."""

    workspace: str  # "pnpm" | "yarn" | "npm" | "cargo" | "gradle" | "single"
    apps: list[AppInventory]
    loader: str  # "mise" | "direnv" | "devbox" | "none"


@runtime_checkable
class RunnableProfile(Protocol):
    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int: ...


# profiles.py populates this at import time.
PROFILES: dict[str, Any] = {}


def _detect_workspace(cwd: Path) -> str:
    """Identify the workspace manager. Returns one of: pnpm, yarn, npm, cargo, gradle, single."""
    if (cwd / "pnpm-workspace.yaml").exists():
        return "pnpm"
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
        except json.JSONDecodeError:
            data = {}
        if data.get("workspaces"):
            if (cwd / "yarn.lock").exists():
                return "yarn"
            if (cwd / "package-lock.json").exists():
                return "npm"
            # Default for workspace-shaped package.json without a lockfile signal.
            return "npm"
    if (cwd / "Cargo.toml").exists():
        try:
            text = (cwd / "Cargo.toml").read_text()
        except OSError:
            text = ""
        if "[workspace]" in text:
            return "cargo"
    for settings in ("settings.gradle", "settings.gradle.kts"):
        if (cwd / settings).exists():
            return "gradle"
    return "single"


def _enumerate_apps(cwd: Path, workspace: str) -> list[tuple[str, Path]]:
    """Return [(name, path), ...] for every app the workspace manager declares.
    `single`-workspace returns one entry pointing at cwd itself, named 'main'."""
    if workspace == "single":
        return [("main", cwd)]
    if workspace == "pnpm":
        # Parse pnpm-workspace.yaml's `packages:` glob list with a minimal reader
        # (no yaml dep). Each glob is a relative shell-style path; expand it.
        text = (cwd / "pnpm-workspace.yaml").read_text()
        globs: list[str] = []
        in_packages = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("packages:"):
                in_packages = True
                continue
            if in_packages:
                if stripped.startswith("- "):
                    val = stripped[2:].strip().strip("\"'")
                    if val:
                        globs.append(val)
                elif stripped and not stripped.startswith("#"):
                    # First non-list, non-empty, non-comment line ends the block.
                    break
        return _expand_workspace_globs(cwd, globs)
    if workspace in ("yarn", "npm"):
        data = json.loads((cwd / "package.json").read_text())
        globs = data.get("workspaces") or []
        if isinstance(globs, dict):
            globs = globs.get("packages") or []
        return _expand_workspace_globs(cwd, globs)
    if workspace == "cargo":
        # Minimal TOML extract — Cargo's `[workspace] members = [...]`.
        data = tomllib.loads((cwd / "Cargo.toml").read_text())
        members = (data.get("workspace") or {}).get("members") or []
        return _expand_workspace_globs(cwd, members)
    if workspace == "gradle":
        # `include("api", "web")` or `include 'api', 'web'` in settings.gradle*.
        settings = cwd / "settings.gradle.kts"
        if not settings.exists():
            settings = cwd / "settings.gradle"
        text = settings.read_text() if settings.exists() else ""
        names = re.findall(r"['\"]([A-Za-z0-9_:.\-]+)['\"]", text)
        out: list[tuple[str, Path]] = []
        for n in names:
            # Gradle module names use ':' as a path separator (':api:server' → 'api/server').
            rel = n.replace(":", "/")
            p = cwd / rel
            if p.is_dir():
                out.append((p.name, p))
        return out
    return []


def _expand_workspace_globs(cwd: Path, globs: list[str]) -> list[tuple[str, Path]]:
    """Expand pnpm/yarn-style workspace globs (`apps/*`, `packages/foo`) to
    [(name, path), ...]. Excludes node_modules and hidden dirs."""
    out: list[tuple[str, Path]] = []
    for raw in globs:
        g = raw.rstrip("/")
        if "*" in g:
            base = cwd / g.split("*", 1)[0].rstrip("/")
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                if (
                    child.is_dir()
                    and not child.name.startswith(".")
                    and child.name != "node_modules"
                ):
                    out.append((child.name, child))
        else:
            p = cwd / g
            if p.is_dir():
                out.append((p.name, p))
    return out


def _loader_on_path(name: str) -> bool:
    """Whether a loader's binary is installed. `shutil` is imported lazily — it
    drags in the compression modules and this is only ever reached from `init`,
    never from the git-hook provisioning path."""
    import shutil  # noqa: PLC0415

    return shutil.which(name) is not None


def _detect_loader(cwd: Path) -> str:
    """Detect which shell-env loader to wire, asking each loader in priority order
    (mise → direnv → devbox). A loader configured in the repo always wins; failing
    that, the first one installed on PATH is used, since a fresh clone has no
    config file yet and writing splashdown.env with nothing to source it is a
    silent no-op. Returns "none" only when nothing is installed — `cmd_init` then
    delivers values into a dotenv file or prints instructions. `--loader none`
    remains the explicit opt-out."""
    for name, loader in LOADERS.items():
        if loader.detect(cwd):
            return name
    for name, loader in LOADERS.items():
        if name != "none" and _loader_on_path(loader.name):
            return name
    return "none"


class Scanner:
    """Inspects the repo and produces a ProjectInventory.

    Pure inspection — no writes. Same instance is safe to reuse across calls.
    Profile detection is delegated to the PROFILES registry; if the registry is
    empty (or no profile matches), apps get `profile="unknown"`."""

    def scan(self, cwd: Path) -> ProjectInventory:
        workspace = _detect_workspace(cwd)
        loader = _detect_loader(cwd)
        apps: list[AppInventory] = []
        for name, path in _enumerate_apps(cwd, workspace):
            profile_name = self._match_profile(path)
            # In a real workspace, members with no detected framework are shared
            # libraries, not runnable apps — omit them. The single-app case keeps
            # its lone app even when unmatched (a bare directory is still "the app").
            if workspace != "single" and profile_name == "unknown":
                continue
            apps.append(
                AppInventory(
                    name=name,
                    path=path,
                    profile=profile_name,
                    project_path=path.relative_to(cwd),
                )
            )
        return ProjectInventory(workspace=workspace, apps=apps, loader=loader)

    def _match_profile(self, app_path: Path) -> str:
        for name, profile in PROFILES.items():
            if profile.detect(app_path):
                return name
        return "unknown"


def _merge_app_resources(
    apps: list[AppInventory],
    res_by_app: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Merge per-app resource dicts into one [resources.*] table. When the same
    canonical name appears in more than one app, all instances are mangled with
    the app name (e.g. WEB_DEV_PORT → WEB_DEV_PORT_ADMIN / WEB_DEV_PORT_CUSTOMER).
    Single-owner names are kept canonical."""
    owners: dict[str, list[str]] = {}
    for app_name, res in res_by_app.items():
        for res_name in res:
            owners.setdefault(res_name, []).append(app_name)

    merged: dict[str, dict[str, Any]] = {}
    for app_name, res in res_by_app.items():
        for res_name, spec in res.items():
            if len(owners[res_name]) > 1:
                key = f"{res_name}_{app_name.upper().replace('-', '_')}"
            else:
                key = res_name
            merged[key] = spec
    return merged


def _prune_unresolvable_templates(
    resources: dict[str, dict[str, Any]],
    app_resource_names: dict[str, list[str]],
    extra_known: set[str] | None = None,
) -> list[str]:
    """Drop profile-emitted template resources whose references don't resolve in
    the merged catalog, and un-list them from the apps that claimed them.

    A Profile emits resources for one app and can't see its siblings, so a
    cross-app reference may dangle: Vite emits `API_DEV_PORT = "{{ PORT }}"` for
    any config mentioning a proxy, but `PORT` only exists when the repo also has
    a backend app. Writing that would fail `Recipe` validation and abort init, so
    prune it here instead. Loops to a fixed point — pruning one template can
    strand another that referenced it. Returns the pruned names."""
    pruned: list[str] = []
    while True:
        known = _TEMPLATE_NAMES | set(resources) | (extra_known or set())
        dangling = {
            name
            for name, spec in resources.items()
            if spec.get("type") == "template"
            and not template_refs(str(spec.get("template", ""))) <= known
        }
        if not dangling:
            return sorted(pruned)
        pruned.extend(dangling)
        for name in dangling:
            del resources[name]
        for names in app_resource_names.values():
            names[:] = [n for n in names if n not in dangling]


def _merge_app_targets(
    apps: list[AppInventory],
) -> dict[str, dict[str, dict[str, str]]]:
    """Collect default device targets across apps into one [targets.*] map.
    Targets aren't app-scoped, so the first app declaring a (dtype, variant)
    wins — later mobile apps don't clobber it. `unknown` apps contribute none."""
    merged: dict[str, dict[str, dict[str, str]]] = {}
    for app in apps:
        if app.profile == "unknown":
            continue
        for dtype, variants in PROFILES[app.profile].targets(app).items():
            for variant, fields in variants.items():
                merged.setdefault(dtype, {}).setdefault(variant, fields)
    return merged


def _has_resource_collision(res_by_app: dict[str, dict[str, dict[str, Any]]]) -> bool:
    """True if any canonical resource name is emitted by more than one app —
    the signal that scanner-driven init would mangle names the apps don't read."""
    counts: dict[str, int] = {}
    for res in res_by_app.values():
        for name in res:
            counts[name] = counts.get(name, 0) + 1
    return any(c > 1 for c in counts.values())


def _dir_has_native_project(p: Path) -> bool:
    """True if `p` is the root of an Xcode or gradle project."""
    if any(p.glob("*.xcworkspace")) or any(p.glob("*.xcodeproj")):
        return True
    has_build = (p / "build.gradle").exists() or (p / "build.gradle.kts").exists()
    has_settings = (p / "settings.gradle").exists() or (p / "settings.gradle.kts").exists()
    return has_build and has_settings


def _unclaimed_native_dirs(cwd: Path, apps: list[AppInventory]) -> list[Path]:
    """Immediate subdirs of `cwd` that look like native projects and are not
    covered by any enumerated app. A native dir is 'claimed' if it equals an app
    path or sits inside/outside the same chain (so an RN/Flutter app at the repo
    root claims its own ios/ + android/). Returns [] for single-app repos, where
    the lone app is at cwd and covers everything."""
    app_paths = [a.path for a in apps]

    def claimed(sub: Path) -> bool:
        return any(sub == ap or ap in sub.parents or sub in ap.parents for ap in app_paths)

    found: list[Path] = []
    for sub in sorted(p for p in cwd.iterdir() if p.is_dir()):
        if sub.name.startswith(".") or sub.name == "node_modules":
            continue
        if claimed(sub):
            continue
        if _dir_has_native_project(sub):
            found.append(sub)
    return found


def _should_defer_monorepo(
    cwd: Path, res_by_app: dict[str, dict[str, dict[str, Any]]], apps: list[AppInventory]
) -> bool:
    """True when scanner-driven init should NOT auto-configure: a canonical-name
    collision, or a native project sibling the workspace doesn't claim."""
    return bool(_has_resource_collision(res_by_app) or _unclaimed_native_dirs(cwd, apps))


def _app_resource_names(
    apps: list[AppInventory],
    res_by_app: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, list[str]]:
    """Return {app_name: [resource_names]} after mangling. Mirrors what
    [apps.<name>] `resources` should list."""
    owners: dict[str, list[str]] = {}
    for app_name, res in res_by_app.items():
        for res_name in res:
            owners.setdefault(res_name, []).append(app_name)
    out: dict[str, list[str]] = {}
    for app_name, res in res_by_app.items():
        names = []
        for res_name in res:
            if len(owners[res_name]) > 1:
                names.append(f"{res_name}_{app_name.upper().replace('-', '_')}")
            else:
                names.append(res_name)
        out[app_name] = names
    return out
