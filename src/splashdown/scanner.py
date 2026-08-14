from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .loaders import LOADERS
from .package_json import package_dependencies, read_package_json
from .recipe import _TEMPLATE_NAMES, Recipe, template_refs


@dataclass
class AppInventory:
    """One consumer (app) inside the project that splashdown will wire."""

    name: str  # e.g. "api"
    path: Path  # absolute path to the app's root directory
    profile: str  # the Profile name that matched, or "unknown"
    project_path: Path | None = None
    capabilities: tuple[str, ...] = ()


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
    data = read_package_json(cwd)
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
        data = read_package_json(cwd)
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


def _detect_capabilities(app_path: Path) -> tuple[str, ...]:
    return ("electron",) if "electron" in package_dependencies(app_path) else ()


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
            capabilities = _detect_capabilities(path)
            # In a real workspace, members with no detected framework are shared
            # libraries, not runnable apps — omit them. The single-app case keeps
            # its lone app even when unmatched (a bare directory is still "the app").
            if workspace != "single" and profile_name == "unknown" and not capabilities:
                continue
            apps.append(
                AppInventory(
                    name=name,
                    path=path,
                    profile=profile_name,
                    project_path=path.relative_to(cwd),
                    capabilities=capabilities,
                )
            )
        return ProjectInventory(workspace=workspace, apps=apps, loader=loader)

    def _match_profile(self, app_path: Path) -> str:
        for name, profile in PROFILES.items():
            if profile.detect(app_path):
                return name
        return "unknown"


def _build_resource_catalog(
    res_by_app: dict[str, dict[str, dict[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    resolved_names = _scoped_resource_names(res_by_app)
    merged: dict[str, dict[str, Any]] = {}
    app_resource_names: dict[str, list[str]] = {}
    sources: dict[str, tuple[str, str]] = {}
    for app_name, resources in res_by_app.items():
        names: list[str] = []
        for resource_name, spec in resources.items():
            resolved_name = resolved_names[app_name, resource_name]
            if previous := sources.get(resolved_name):
                previous_app, previous_resource = previous
                raise ValueError(
                    f"resource name collision after app-name normalization: "
                    f"`{previous_app}.{previous_resource}` and `{app_name}.{resource_name}` "
                    f"both resolve to `{resolved_name}`; rename one app"
                )
            sources[resolved_name] = (app_name, resource_name)
            merged[resolved_name] = spec
            names.append(resolved_name)
        app_resource_names[app_name] = names
    return merged, app_resource_names


def _scoped_resource_names(
    res_by_app: dict[str, dict[str, dict[str, Any]]],
) -> dict[tuple[str, str], str]:
    owners: dict[str, list[str]] = {}
    for app_name, resources in res_by_app.items():
        for resource_name in resources:
            owners.setdefault(resource_name, []).append(app_name)

    names: dict[tuple[str, str], str] = {}
    for resource_name, app_names in owners.items():
        if len(app_names) == 1:
            names[app_names[0], resource_name] = resource_name
            continue
        suffixes = {
            app_name: re.sub(r"[^A-Z0-9_]", "_", app_name.upper()) for app_name in app_names
        }
        counts = {suffix: list(suffixes.values()).count(suffix) for suffix in suffixes.values()}
        for app_name in app_names:
            suffix = suffixes[app_name]
            if counts[suffix] > 1:
                digest = hashlib.sha256(app_name.encode()).hexdigest()[:8].upper()
                suffix = f"{suffix}_{digest}"
            names[app_name, resource_name] = f"{resource_name}_{suffix}"
    return names


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
