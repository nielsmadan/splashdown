from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from .devices import DeviceError
from .recipe import Recipe
from .runners import (
    _android_native_run,
    _expo_run,
    _flutter_run,
    _ios_native_run,
    _rn_run,
)
from .scanner import PROFILES, AppInventory
from .wiring import (
    _HOOK_WIRING_CHECK,
    _RN_WIRING_CHECKS,
    WiringCheck,
    _strip_hash_comments,
    _strip_js_comments,
    _yaml_key_regions,
)

# Named scaffolds for `splash init NAME`. Framework detection, wiring checks,
# and `splash run` logic live on `Profile` subclasses (below).


def _detect_flutter(cwd: Path) -> bool:
    return (cwd / "pubspec.yaml").exists()


def _read_pkg_deps(cwd: Path) -> dict[str, Any]:
    pkg = cwd / "package.json"
    if not pkg.exists():
        return {}
    try:
        data = json.loads(pkg.read_text())
    except json.JSONDecodeError:
        return {}
    return {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}


def _detect_expo(cwd: Path) -> bool:
    deps = _read_pkg_deps(cwd)
    return "expo" in deps and (cwd / "app.json").exists()


def _detect_rn(cwd: Path) -> bool:
    return "react-native" in _read_pkg_deps(cwd)


# `[project] run` (or a `[project.run]` table) overrides the framework's built-in
# launcher — the escape hatch for a specific package manager (yarn/pnpm), a
# monorepo subdir, or any non-standard invocation. Mobile-only (that's the only
# place `splash run` exists). See _resolve_custom_run / run_custom_command.


def _has_js_or_flutter(cwd: Path) -> bool:
    return _detect_flutter(cwd) or _detect_expo(cwd) or _detect_rn(cwd)


def _pbxproj_targets_ios(project: Path) -> bool:
    """Whether an .xcodeproj builds for iOS. Fails open unless the pbxproj says
    macOS and nothing says iOS — projects that keep deployment targets in an
    .xcconfig name neither, and must not be excluded on that silence."""
    try:
        text = (project / "project.pbxproj").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    if "IPHONEOS_DEPLOYMENT_TARGET" in text or "SDKROOT = iphoneos" in text:
        return True
    return "MACOSX_DEPLOYMENT_TARGET" not in text and "SDKROOT = macosx" not in text


def _detect_ios_native(cwd: Path) -> bool:
    if _has_js_or_flutter(cwd):
        return False
    # A macOS-only app matches the same globs but has no simulator to build for.
    # The workspace usually wraps a sibling project, so let the projects decide.
    projects = sorted(cwd.glob("*.xcodeproj"))
    if projects:
        return any(_pbxproj_targets_ios(p) for p in projects)
    return any(cwd.glob("*.xcworkspace"))


def _detect_android_native(cwd: Path) -> bool:
    if _has_js_or_flutter(cwd):
        return False
    has_build = (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists()
    has_settings = (cwd / "settings.gradle").exists() or (cwd / "settings.gradle.kts").exists()
    return has_build and has_settings


# A Profile encodes how splashdown integrates with one framework. The Scanner
# matches each app to a Profile by filesystem detection; Profiles contribute
# resources (which end up in [resources.*]) and wiring checks (which the doctor
# runs to patch consumer configs). Built-in only — to add a Profile, ship it
# upstream as a new subclass + entry in PROFILES.


class Profile:
    """Abstract base. Subclasses set `name` and override `detect`, `resources`,
    and (where relevant) `wiring_checks` / `run`."""

    name: str = ""

    # Whether an empty `wiring_checks()` means "healthy, nothing to patch" rather
    # than "nobody has written checks yet". Only True where the framework reads
    # its port straight from the environment — doctor turns this into a positive
    # verdict, so a framework that actually needs config patching (expo runs
    # Metro) must leave it False or it gets reported green while broken.
    env_only: bool = False

    # Whether this framework reads values from a plain dotenv file (`.env` /
    # `.env.local`) on its own. True for server frameworks where loading a
    # dotenv file is the conventional setup (Next.js natively; Django/FastAPI/
    # Node via the near-ubiquitous dotenv libraries). False for frameworks that
    # only see values already exported into the process environment — Vite
    # (config rewritten to read `process.env`), Spring Boot (`${PORT}`
    # placeholder), and the mobile build systems. Used to decide, when no shell
    # loader is detected, whether a dotenv-file fallback can actually reach the app.
    reads_dotenv: bool = False

    def detect(self, app_path: Path) -> bool:
        raise NotImplementedError

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        """Return a {resource_name: {type, range, ...}} dict to merge into the
        recipe's [resources.*] tables. Resource names should be canonical; the
        Scanner mangles them with the app name when more than one app of the
        same profile is present."""
        return {}

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        """Return default device targets keyed {dtype: {variant: {field: value}}}
        to emit as [targets.*] tables in a scanner-driven `splash init`. Mobile
        profiles override this; non-device profiles return {} (no targets)."""
        return {}

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        """Return WiringCheck instances for consumer-side config patches. The
        existing doctor flow runs these."""
        return []

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        """Return framework-specific Markdown appended to common port guidance."""
        return []

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        """Build + install + launch the app on the given device. Mobile
        Profiles override; web/backend Profiles raise (no `splash run` semantics
        for them — those use `pnpm dev` / `gradle bootRun` / etc. directly)."""
        raise DeviceError(f"don't know how to run framework `{self.name}`")


def _manual_port_guidance(
    label: str, command: str, port_name: str, app_path: Path | None
) -> list[str]:
    app_path = app_path or Path(".")
    env_command = command.format(port=f'"${port_name}"')
    if str(app_path) in {"", "."}:
        lookup_command = command.format(port=f'"$(splash env get {port_name})"')
    else:
        quoted_path = shlex.quote(str(app_path))
        env_command = f"(cd {quoted_path} && {env_command})"
        lookup_launch = command.format(port='"$port"')
        lookup_command = (
            f'(port="$(splash env get {port_name})" && cd {quoted_path} && {lookup_launch})'
        )
    return [
        f"- Manual {label} launch: {_markdown_command(env_command)}.",
        f"- Without a loaded environment: {_markdown_command(lookup_command)}.",
    ]


def _markdown_command(command: str) -> str:
    escaped = (
        command.replace("<", "&lt;").replace(">", "&gt;").replace("\r", r"\r").replace("\n", r"\n")
    )
    longest = max((len(match.group()) for match in re.finditer(r"`+", escaped)), default=0)
    fence = "`" * max(1, longest + 1)
    padding = " " if escaped.startswith("`") or escaped.endswith("`") else ""
    return f"{fence}{padding}{escaped}{padding}{fence}"


def _profile_port(port_names: list[str], canonical: str) -> str:
    return next(
        (name for name in port_names if name == canonical or name.startswith(f"{canonical}_")),
        port_names[0],
    )


# A compose file is infrastructure spanning apps, not an app, so it is not matched
# per-directory by the scanner. Its resources are emitted once for the repo and its
# check runs alongside whatever framework doctor resolved.

_COMPOSE_FILE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
# `ports:` and `container_name:` are found without anchoring on block layout, because
# both keys are equally legal in flow style (`ports: ['5432:5432']`, `{ container_name: x }`)
# and a line-anchored pattern silently reports those files clean.
_COMPOSE_CONTAINER_NAME_RE = re.compile(
    r"(?:^|[{,])[ \t]*container_name[ \t]*:[ \t]*([^,}\n]+)", re.MULTILINE
)
# Long port syntax (`- {target: 80, published: 8080}`); only `published` pins a host port.
_COMPOSE_LONG_SYNTAX_RE = re.compile(
    r"(?:^|[{,\n])[ \t]*(?:target|published|protocol|mode|host_ip|name)[ \t]*:"
)
_COMPOSE_PUBLISHED_RE = re.compile(r"(?:^|[{,\n])[ \t]*published[ \t]*:[ \t]*([^,}\n]+)")
_COMPOSE_PORT_NUMBER_RE = re.compile(r"^[0-9]+(?:-[0-9]+)?$")
# A `${VAR:-5432}` contains a colon, so port slots can only be split after the
# variables are masked out. The sentinel is deliberately colon-free.
_COMPOSE_VAR_RE = re.compile(r"\$\{[^}]*\}|\$\w+")
_VAR_MASK = "\x00"
# Short syntax tops out at host_ip:host:container.
_COMPOSE_MAX_PORT_FIELDS = 3


def _compose_file_path(root: Path) -> Path | None:
    for name in _COMPOSE_FILE_NAMES:
        candidate = root / name
        if candidate.exists():
            return candidate
    return None


def compose_project_resources(root: Path) -> dict[str, dict[str, Any]]:
    """Resources every compose repo wants, emitted once for the project.

    `COMPOSE_PROJECT_NAME` is the load-bearing one: it namespaces containers,
    networks and volumes per checkout, which is what keeps two worktrees of the
    same repo from fighting. Host ports are deliberately *not* invented here —
    which service deserves a pinned port is a judgement call, so the wiring check
    reports them and the user declares the ones they want."""
    if _compose_file_path(root) is None:
        return {}
    return {
        "COMPOSE_PROJECT_NAME": {
            "type": "template",
            "template": "{{ slug(parent) }}-{{ slug(cwd) }}",
        }
    }


def compose_wiring_checks(root: Path) -> list[WiringCheck]:
    return [_compose_hardcoded_check()] if _compose_file_path(root) is not None else []


def _compose_hardcoded_check() -> WiringCheck:
    """Report-only. Compose files are YAML with significant whitespace and
    splashdown ships no YAML parser, so a mechanical rewrite would be regex over
    indentation-sensitive text — the check names what to change instead."""
    return WiringCheck(
        id="compose-hardcoded-ports",
        description="compose file templates its host ports and container names",
        applies=lambda cwd: _compose_file_path(cwd) is not None,
        detect=_compose_hardcoded_detect,
        autofix=None,
        manual_instructions=_compose_hardcoded_manual,
    )


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_flow_entries(region: str) -> list[str]:
    inner = region.strip().removeprefix("[").removesuffix("]")
    entries: list[str] = []
    cur: list[str] = []
    depth, quote = 0, ""
    for ch in inner:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            entries.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    entries.append("".join(cur))
    return [e.strip() for e in entries if e.strip()]


def _split_block_entries(region: str) -> list[str]:
    """One string per `- …` item; a long-syntax item keeps its continuation lines."""
    entries: list[str] = []
    for line in region.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "-" or stripped.startswith("- "):
            entries.append(stripped[1:].strip())
        elif entries:
            entries[-1] = f"{entries[-1]}\n{stripped}"
        else:  # no list item at all (an alias, say) — report it verbatim
            entries.append(stripped)
    return [e for e in entries if e]


def _classify_long_port_entry(entry: str) -> tuple[str | None, bool]:
    """Long syntax (`- {target: 80, published: 8080}`); only `published` pins a host
    port, and its absence means docker picks one."""
    published = _COMPOSE_PUBLISHED_RE.search(entry)
    if published is None:
        return (None, True)
    value = _unquote(published.group(1))
    if "$" in value:
        return (None, True)
    return (value, True) if _COMPOSE_PORT_NUMBER_RE.match(value) else (None, False)


def _classify_port_entry(entry: str) -> tuple[str | None, bool]:
    """(pinned host port or None, whether the entry was understood at all).

    Only the *host* slot decides. Testing the whole entry for `$` passed
    `"5432:${CONTAINER_PORT}"` as templated while the host side stayed pinned, so
    variables are masked to a colon-free sentinel first and each slot judged alone."""
    if _COMPOSE_LONG_SYNTAX_RE.search(entry):
        return _classify_long_port_entry(entry)
    spec = _COMPOSE_VAR_RE.sub(_VAR_MASK, _unquote(entry).split("/", 1)[0])
    if spec.startswith("["):  # ipv6 host_ip
        spec = spec.partition("]:")[2]
    parts = spec.split(":")
    if len(parts) > _COMPOSE_MAX_PORT_FIELDS:
        return (None, False)
    if len(parts) == 1:  # container port only; docker picks the host side
        return (None, _COMPOSE_PORT_NUMBER_RE.match(parts[0]) is not None or _VAR_MASK in parts[0])
    host, container = parts[-2], parts[-1]
    if _VAR_MASK in host or host == "":  # templated, or `127.0.0.1::5432`
        return (None, True)
    if not _COMPOSE_PORT_NUMBER_RE.match(host):
        return (None, False)
    if not (_COMPOSE_PORT_NUMBER_RE.match(container) or _VAR_MASK in container):
        return (None, False)
    return (host, True)


def _compose_hardcoded_detect(cwd: Path) -> tuple[str, str]:
    cfg = _compose_file_path(cwd)
    if cfg is None:  # applies() guarantees the compose file exists
        raise DeviceError("compose file not found")
    text = _strip_hash_comments(cfg.read_text())
    ports: set[str] = set()
    unreadable: set[str] = set()
    for region in _yaml_key_regions(text, "ports"):
        flow = region.lstrip().startswith("[")
        entries = _split_flow_entries(region) if flow else _split_block_entries(region)
        if not entries:
            if region.strip() not in ("", "[]"):
                unreadable.add(region.strip())
            continue
        for entry in entries:
            host, understood = _classify_port_entry(entry)
            if host:
                ports.add(host)
            elif not understood:
                unreadable.add(" ".join(entry.split()))
    names = {_unquote(m.group(1)) for m in _COMPOSE_CONTAINER_NAME_RE.finditer(text)}
    names = {n for n in names if n and "$" not in n}
    hardcoded = []
    if ports:
        hardcoded.append(f"host ports {', '.join(sorted(ports))}")
    if names:
        hardcoded.append(f"container_name {', '.join(sorted(names))}")
    problems = [f"{cfg.name} hardcodes {'; '.join(hardcoded)}"] if hardcoded else []
    if unreadable:
        problems.append(
            f"{cfg.name} has port entries splashdown can't read, "
            f"check them by hand: {', '.join(sorted(unreadable))}"
        )
    if problems:
        return ("problem", "; ".join(problems))
    return ("ok", f"{cfg.name} has no hardcoded host ports or container names")


def _compose_hardcoded_manual(cwd: Path) -> str:
    cfg = _compose_file_path(cwd)
    name = cfg.name if cfg else "compose.yaml"
    return (
        f"Templatize {name} so each checkout gets its own ports and containers:\n"
        '  ports:\n    - "${DB_PORT:-5432}:5432"   # declare DB_PORT in splashdown.toml\n'
        "Drop `container_name:` and let COMPOSE_PROJECT_NAME namespace them.\n"
        "COMPOSE_PROJECT_NAME is already allocated per checkout; compose reads it\n"
        "from the environment, so run `docker compose up` in a shell your loader\n"
        "has populated (any shell you have cd'd into)."
    )


_ASTRO_CONFIG_NAMES = (
    "astro.config.mjs",
    "astro.config.ts",
    "astro.config.js",
    "astro.config.mts",
    "astro.config.cjs",
)
# An existing `server:` block may be nested under `vite:`, where a port would not
# reach Astro's dev server. Detecting one anywhere is the signal to stop and let
# the user place it, rather than guess at nesting with a regex.
_ASTRO_SERVER_BLOCK_RE = re.compile(r"\bserver\s*:\s*\{")
_ASTRO_DEFAULT_EXPORT_RE = re.compile(r"export\s+default\s+(?:defineConfig\s*\(\s*)?\{")


def _astro_config_path(app_path: Path) -> Path | None:
    for name in _ASTRO_CONFIG_NAMES:
        candidate = app_path / name
        if candidate.exists():
            return candidate
    return None


class AstroProfile(Profile):
    name = "astro"

    def detect(self, app_path: Path) -> bool:
        return _astro_config_path(app_path) is not None or "astro" in _read_pkg_deps(app_path)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        # Skips Astro's own 4321 so an unwired app can't accidentally be handed
        # the default and look wired.
        return {"WEB_DEV_PORT": {"type": "port", "range": [4322, 4400]}}

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_astro_port_check()]

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "WEB_DEV_PORT")
        return _manual_port_guidance("Astro", "npx astro dev --port {port}", port, app.project_path)


def _astro_port_check() -> WiringCheck:
    return WiringCheck(
        id="astro-config-port",
        description="astro.config wires server.port to WEB_DEV_PORT",
        applies=lambda cwd: _astro_config_path(cwd) is not None,
        detect=_astro_port_detect,
        autofix=_astro_port_autofix,
        manual_instructions=_astro_port_manual,
    )


def _astro_port_detect(cwd: Path) -> tuple[str, str]:
    cfg = _astro_config_path(cwd)
    if cfg is None:  # applies() guarantees the astro config exists
        raise DeviceError("astro.config.* not found")
    if "WEB_DEV_PORT" in _strip_js_comments(cfg.read_text()):
        return ("ok", "astro.config reads WEB_DEV_PORT")
    # Unlike Next.js, Astro never reads PORT from the environment — the port has
    # to be in the config or on the command line.
    return ("problem", "astro.config never reads WEB_DEV_PORT; Astro ignores $PORT")


def _astro_port_autofix(cwd: Path) -> None:
    cfg = _astro_config_path(cwd)
    if cfg is None:  # applies() guarantees the astro config exists
        raise DeviceError("astro.config.* not found")
    text = cfg.read_text()
    if "WEB_DEV_PORT" in text or _ASTRO_SERVER_BLOCK_RE.search(text):
        return
    m = _ASTRO_DEFAULT_EXPORT_RE.search(text)
    if m is None:  # unrecognized shape — manual_instructions covers it
        return
    block = "\n  server: { port: Number(process.env.WEB_DEV_PORT) || 4321 },"
    cfg.write_text(text[: m.end()] + block + text[m.end() :])
    print(f"patched {cfg.name} (server.port → WEB_DEV_PORT)", file=sys.stderr)


def _astro_port_manual(cwd: Path) -> str:
    return (
        "Astro does not read PORT from the environment, so the dev port has to be\n"
        "in astro.config. Add it to the top-level config object:\n"
        "  server: { port: Number(process.env.WEB_DEV_PORT) || 4321 }\n"
        "If you already have a `server:` block, put the port there — a block nested\n"
        "under `vite:` configures Vite's server, not Astro's."
    )


PROFILES["astro"] = AstroProfile()


_VITE_CONFIG_NAMES = ("vite.config.ts", "vite.config.js", "vite.config.mjs", "vite.config.mts")
# Matches `env.VAR_NAME` access (the loadEnv idiom). The wiring autofix rewrites
# these to `process.env.VAR_NAME` so splashdown.env + mise loading works.
# Negative lookbehind on `process.` ensures already-fixed `process.env.VAR` is
# not re-matched.
_VITE_ENV_ACCESS_RE = re.compile(r"(?<!process\.)(?<!\.)env\.([A-Z][A-Z0-9_]*)\b")


def _vite_unfixed_env_matches(text: str) -> list[re.Match[str]]:
    """`env.X` accesses with no matching `process.env.X` read anywhere in the file.
    `process.env.X || env.X` is a deliberate shell-then-dotenv fallback chain, so
    rewriting its second term would silently delete the dotenv layer."""
    covered = set(re.findall(r"process\.env\.([A-Z][A-Z0-9_]*)\b", text))
    return [m for m in _VITE_ENV_ACCESS_RE.finditer(text) if m.group(1) not in covered]


def _vite_config_path(app_path: Path) -> Path | None:
    for name in _VITE_CONFIG_NAMES:
        candidate = app_path / name
        if candidate.exists():
            return candidate
    return None


class ViteProfile(Profile):
    name = "vite"

    def detect(self, app_path: Path) -> bool:
        return _vite_config_path(app_path) is not None

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {
            "WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]},
        }
        # Only emit API_DEV_PORT if the config has a server.proxy block —
        # otherwise this app doesn't need to know the api's port at all.
        cfg = _vite_config_path(app.path)
        if cfg and "proxy" in cfg.read_text():
            out["API_DEV_PORT"] = {"type": "template", "template": "{{ PORT }}"}
        return out

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        ports = [name for name, spec in self.resources(app).items() if spec.get("type") == "port"]
        return [_vite_process_env_check(), *(_vite_port_wired_check(p) for p in ports)]

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "WEB_DEV_PORT")
        return _manual_port_guidance("Vite", "npx vite --port {port}", port, app.project_path)


def _vite_port_wired_check(port_var: str) -> WiringCheck:
    """Report-only. A vite.config that never names the allocated port var leaves it
    unused, but adding a `server.port` block to an arbitrary config is not safely
    mechanical, so print the snippet instead of guessing (as springboot does)."""

    def detect(cwd: Path) -> tuple[str, str]:
        cfg = _vite_config_path(cwd)
        if cfg is None:  # applies() guarantees the vite config exists
            raise DeviceError("vite.config.* not found")
        if port_var in _strip_js_comments(cfg.read_text()):
            return ("ok", f"vite.config references {port_var}")
        return ("problem", f"vite.config never reads {port_var}; allocated port goes unused")

    return WiringCheck(
        id="vite-port-wired",
        description=f"vite.config wires its dev-server port to {port_var}",
        applies=lambda cwd: _vite_config_path(cwd) is not None,
        detect=detect,
        autofix=None,
        manual_instructions=lambda cwd: (
            f"Wire the dev server port in vite.config:\n"
            f"  server: {{ port: Number(process.env.{port_var}) || 5173 }}"
        ),
    )


def _vite_process_env_check() -> WiringCheck:
    return WiringCheck(
        id="vite-config-process-env",
        description="vite.config reads env vars from process.env, not loadEnv",
        applies=lambda cwd: _vite_config_path(cwd) is not None,
        detect=_vite_process_env_detect,
        autofix=_vite_process_env_autofix,
        manual_instructions=_vite_process_env_manual,
    )


def _vite_process_env_detect(cwd: Path) -> tuple[str, str]:
    cfg = _vite_config_path(cwd)
    if cfg is None:  # applies() guarantees the vite config exists
        raise DeviceError("vite.config.* not found")
    text = _strip_js_comments(cfg.read_text())
    if "loadEnv" in text and _vite_unfixed_env_matches(text):
        return ("problem", "vite.config uses loadEnv; should read process.env")
    return ("ok", "vite.config reads process.env")


def _vite_process_env_autofix(cwd: Path) -> None:
    cfg = _vite_config_path(cwd)
    if cfg is None:  # applies() guarantees the vite config exists
        raise DeviceError("vite.config.* not found")
    text = cfg.read_text()
    # Rewrite every `env.VAR` access to `process.env.VAR`, skipping names already
    # read from process.env elsewhere. Keep loadEnv lines untouched (the user may
    # want them for other purposes) — the new access path just bypasses them.
    new_text = text
    for m in reversed(_vite_unfixed_env_matches(text)):
        new_text = new_text[: m.start()] + f"process.env.{m.group(1)}" + new_text[m.end() :]
    if new_text != text:
        cfg.write_text(new_text)
        print(f"patched {cfg.name} (env.X → process.env.X)", file=sys.stderr)


def _vite_process_env_manual(cwd: Path) -> str:
    return (
        "Edit vite.config so any `env.VAR_NAME` access reads `process.env.VAR_NAME`\n"
        "instead. Splashdown.env is loaded into the parent shell via your shell-env\n"
        "loader (mise/direnv/devbox), so process.env carries the values."
    )


class LaravelProfile(Profile):
    name = "laravel"
    env_only = True
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        # Lumen also ships an `artisan`, so the composer requirement is what
        # distinguishes a Laravel app (lumen pulls laravel/lumen-framework).
        if not (app_path / "artisan").exists():
            return False
        composer = app_path / "composer.json"
        return bool(composer.exists() and "laravel/framework" in composer.read_text())

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        # A Laravel app runs two dev servers that both collide across worktrees:
        # `php artisan serve` (SERVER_PORT, read straight from the environment) and,
        # since Laravel 9, Vite for assets. Only claim the Vite port when the app
        # actually ships a vite config — API-only Laravel apps don't.
        out: dict[str, dict[str, Any]] = {"SERVER_PORT": {"type": "port", "range": [8001, 8100]}}
        if _vite_config_path(app.path) is not None:
            out["WEB_DEV_PORT"] = {"type": "port", "range": [5174, 5200]}
        return out

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        # SERVER_PORT needs no patching; the Vite half does. With no vite config
        # the empty list plus env_only gives the green "env-only" verdict.
        if _vite_config_path(app.path) is None:
            return []
        return [_vite_port_wired_check("WEB_DEV_PORT")]

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        server_port = _profile_port(port_names, "SERVER_PORT")
        lines = _manual_port_guidance(
            "Laravel", "php artisan serve --port={port}", server_port, app.project_path
        )
        web_ports = [name for name in port_names if "WEB_DEV_PORT" in name]
        if web_ports:
            lines.extend(
                _manual_port_guidance(
                    "Vite", "npx vite --port {port}", web_ports[0], app.project_path
                )
            )
        return lines


# Registered ahead of `vite`: Laravel has shipped a vite.config since Laravel 9, so
# ViteProfile matches every modern Laravel app and would otherwise claim it — leaving
# the PHP server's port unmanaged. Detection here needs `artisan` *and* the composer
# entry, so it can't steal a plain Vite app.
PROFILES["laravel"] = LaravelProfile()


_NUXT_CONFIG_NAMES = ("nuxt.config.ts", "nuxt.config.js", "nuxt.config.mjs")


class NuxtProfile(Profile):
    name = "nuxt"
    env_only = True
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        if any((app_path / n).exists() for n in _NUXT_CONFIG_NAMES):
            return True
        return "nuxt" in _read_pkg_deps(app_path)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        # NUXT_PORT rather than the also-supported PORT: the specific name can't be
        # claimed by a sibling backend in a monorepo. Skips Nuxt's own 3000.
        return {"NUXT_PORT": {"type": "port", "range": [3001, 3100]}}

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "NUXT_PORT")
        return _manual_port_guidance("Nuxt", "npx nuxt dev --port {port}", port, app.project_path)


# Ahead of `vite` for the same reason as laravel: Nuxt is Vite-based, and while the
# minimal template ships no vite.config, a project that adds one must still resolve to
# nuxt — ViteProfile would emit WEB_DEV_PORT that `nuxt dev` never reads.
PROFILES["nuxt"] = NuxtProfile()


class AngularProfile(Profile):
    name = "angular"

    def detect(self, app_path: Path) -> bool:
        return (app_path / "angular.json").exists()

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        # Skips Angular's own 4200 so an unwired app can't look wired.
        return {"WEB_DEV_PORT": {"type": "port", "range": [4201, 4300]}}

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_angular_pkg_port_check()]

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "WEB_DEV_PORT")
        return _manual_port_guidance(
            "Angular", "npx ng serve --port {port}", port, app.project_path
        )


_NG_SERVE_RE = re.compile(r"\bng\s+serve\b")
_NG_PORT_VAR = "$WEB_DEV_PORT"
# The variable has to reach `--port`. Merely naming it (`WEB_DEV_PORT=4200 ng serve`)
# sets an environment variable that `ng serve` never reads. Anchored on the right so
# `--port=$WEB_DEV_PORT_EXTRA` is not read as a match.
_NG_PORT_ARG_RE = re.compile(r"--port[=\s]+[\"']?\$\{?WEB_DEV_PORT\}?[\"']?(?![\w.-])")
_NG_ANY_PORT_RE = re.compile(r"\s+--port[=\s]+[\"']?\$?\{?[\w.-]+\}?[\"']?")
# `ng serve && echo done` is one script with two commands; the flag belongs to the
# first. Appending to the whole string handed it to `echo`.
_NG_SEGMENT_END_RE = re.compile(r"\s(?:&&|\|\||[;|])")


def _angular_serve_scripts(data: Any) -> dict[str, str]:
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {k: v for k, v in scripts.items() if isinstance(v, str) and _NG_SERVE_RE.search(v)}


def _angular_pkg_port_check() -> WiringCheck:
    """Angular reads no environment variable for its dev-server port — only
    `angular.json` or `--port`. Writing the allocated port as a literal into the
    (committed) angular.json would churn it in every worktree, which is the collision
    splashdown exists to remove, so the port is passed through the npm script instead:
    npm runs scripts via a shell, so `$WEB_DEV_PORT` expands from the loader's env."""
    return WiringCheck(
        id="angular-pkg-port",
        description="package.json `ng serve` scripts pass --port $WEB_DEV_PORT",
        applies=lambda cwd: (cwd / "package.json").exists() and (cwd / "angular.json").exists(),
        detect=_angular_pkg_port_detect,
        autofix=_angular_pkg_port_autofix,
        manual_instructions=_angular_pkg_port_manual,
    )


def _angular_pkg_port_detect(cwd: Path) -> tuple[str, str]:
    try:
        data = json.loads((cwd / "package.json").read_text())
    except (json.JSONDecodeError, OSError) as e:
        return ("problem", f"could not read package.json: {e}")
    serving = _angular_serve_scripts(data)
    if not serving:
        # Not a pass: Angular reads no port env var at all, so with no script to
        # carry --port the allocated WEB_DEV_PORT reaches nothing and the app
        # keeps binding angular.json's default in every checkout.
        return (
            "problem",
            "no `ng serve` script to carry --port; WEB_DEV_PORT reaches nothing (not autofixable)",
        )
    unwired = [n for n, v in serving.items() if not _NG_PORT_ARG_RE.search(v)]
    if unwired:
        return ("problem", f"`ng serve` scripts ignore WEB_DEV_PORT: {', '.join(unwired)}")
    return ("ok", "`ng serve` scripts pass WEB_DEV_PORT")


def _angular_wire_serve_script(value: str) -> str:
    """Insert `--port $WEB_DEV_PORT` into the `ng serve` command itself, replacing any
    port flag already on it. Appending to the end of the script instead put the flag
    on whatever came after a `&&`, and left a second `--port` behind when the existing
    one was a variable rather than a literal."""
    m = _NG_SERVE_RE.search(value)
    if m is None:
        return value
    head, rest = value[: m.end()], value[m.end() :]
    end = _NG_SEGMENT_END_RE.search(rest)
    cut = end.start() if end else len(rest)
    return f"{head} --port {_NG_PORT_VAR}{_NG_ANY_PORT_RE.sub('', rest[:cut])}{rest[cut:]}"


def _angular_pkg_port_autofix(cwd: Path) -> None:
    path = cwd / "package.json"
    data = json.loads(path.read_text())
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return
    changed = False
    for name, value in _angular_serve_scripts(data).items():
        if _NG_PORT_ARG_RE.search(value):
            continue
        scripts[name] = _angular_wire_serve_script(value)
        changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print("patched package.json (ng serve --port $WEB_DEV_PORT)", file=sys.stderr)


def _angular_pkg_port_manual(cwd: Path) -> str:
    return (
        "Angular exposes no env var for the dev-server port, so pass it in the script:\n"
        '  "start": "ng serve --port $WEB_DEV_PORT"\n'
        "This wires `npm start`; a bare `ng serve` still uses angular.json's default."
    )


PROFILES["angular"] = AngularProfile()

PROFILES["vite"] = ViteProfile()

_NODE_BACKEND_DEPS = {"hono", "express", "fastify", "koa", "@hapi/hapi", "@nestjs/core"}


class NodeBackendProfile(Profile):
    name = "node-backend"
    env_only = True
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        pkg = app_path / "package.json"
        if not pkg.exists():
            return False
        try:
            data = json.loads(pkg.read_text())
        except json.JSONDecodeError:
            return False
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        return any(d in deps for d in _NODE_BACKEND_DEPS)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [9081, 9100]}}


PROFILES["node-backend"] = NodeBackendProfile()

_DENO_CONFIG_NAMES = ("deno.json", "deno.jsonc")
_DENO_SERVE_RE = re.compile(r"\bdeno\s+serve\b")


def _deno_config_path(app_path: Path) -> Path | None:
    for name in _DENO_CONFIG_NAMES:
        candidate = app_path / name
        if candidate.exists():
            return candidate
    return None


class DenoProfile(Profile):
    name = "deno"

    def detect(self, app_path: Path) -> bool:
        return _deno_config_path(app_path) is not None

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        # Skips Deno.serve's own 8000. Note Deno reads no PORT of its own (verified
        # against `deno serve` and `Deno.serve()` — both bind 8000 with PORT set), so
        # this is only useful once the wiring check below is satisfied.
        return {"PORT": {"type": "port", "range": [8001, 8100]}}

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_deno_port_check()]

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "PORT")
        lines = _manual_port_guidance(
            "Deno", "deno serve --port {port} SCRIPT_PATH", port, app.project_path
        )
        return [
            "- Prefer the configured `deno task` when it already passes the port.",
            *lines,
            "- Replace `SCRIPT_PATH` with the app's entrypoint.",
            "- Keep `--port` before the entrypoint; later arguments go to the script.",
        ]


def _deno_port_check() -> WiringCheck:
    """Deno is the one runtime here with no port convention at all: neither
    `deno serve` nor `Deno.serve()` consults an environment variable, so an allocated
    PORT reaches nothing until either a task passes `--port $PORT` or the server code
    reads it. Only the first is mechanical, so autofix handles `deno serve` tasks and
    everything else falls through to instructions."""
    return WiringCheck(
        id="deno-port-wired",
        description="a deno task or the server code consumes PORT",
        applies=lambda cwd: _deno_config_path(cwd) is not None,
        detect=_deno_port_detect,
        autofix=_deno_port_autofix,
        manual_instructions=_deno_port_manual,
    )


def _deno_sources_read_port(cwd: Path) -> bool:
    """Whether a top-level source file reads PORT itself. Root-only and non-recursive:
    this is a cheap positive signal, not an audit."""
    for path in sorted([*cwd.glob("*.ts"), *cwd.glob("*.js"), *cwd.glob("*.tsx")]):
        try:
            text = _strip_js_comments(path.read_text())
        except OSError:
            continue
        if 'env.get("PORT")' in text or "env.get('PORT')" in text:
            return True
    return False


# `deno serve`'s script argument is always a module specifier, so the flag run ends
# at the first token that looks like one. Enumerating which flags take a
# space-separated value instead is an allowlist that can't be completed — omitting
# `--host` made `deno serve --host 0.0.0.0 --port $PORT x.ts` read as misordered.
_DENO_MODULE_RE = re.compile(
    r"^(?:\./|\.\./|/|https?:|npm:|jsr:|file:|data:)|\.(?:ts|tsx|js|jsx|mjs|mts|cjs)$"
)
# splashdown's own variable, not merely a name containing "PORT" — `--port $MY_PORT`
# leaves the allocated PORT unused and must not read as wired.
_DENO_OUR_PORT_RE = re.compile(r"""^["']?\$\{?PORT\}?["']?$""")
_DENO_TRAILING_PORT_RE = re.compile(r"""\s+--port[=\s]+["']?\$\{?PORT\}?["']?""")
_DENO_ANY_PORT_RE = re.compile(r"""\s+--port[=\s]+["']?\$?\{?[\w.:-]+\}?["']?""")
_DENO_PORT_FLAG_RE = re.compile(r"^--port(?:=(.*))?$")
_JSONC_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _deno_flag_span(rest: str) -> int:
    """Offset in `rest` (the text after `deno serve`) where deno's own flags stop
    and the script argument begins. Everything past it is the script's argv."""
    for token in re.finditer(r"\S+", rest):
        word = token.group()
        if word == "--":
            return token.end()
        if not word.startswith("-") and _DENO_MODULE_RE.search(word):
            return token.start()
    return len(rest)


def _deno_serve_port_wired(task: str) -> bool:
    """Whether a `deno serve` task passes PORT *to deno*. Position is the whole
    point: everything after the script argument is handed to the script, so an
    appended `--port $PORT` is silently dropped and the server keeps binding 8000."""
    m = _DENO_SERVE_RE.search(task)
    if m is None:
        return False
    rest = task[m.end() :]
    tokens = list(re.finditer(r"\S+", rest[: _deno_flag_span(rest)]))
    for i, token in enumerate(tokens):
        flag = _DENO_PORT_FLAG_RE.match(token.group())
        if flag is None:
            continue
        value = flag.group(1)
        if value is None and i + 1 < len(tokens):
            value = tokens[i + 1].group()
        return _DENO_OUR_PORT_RE.match(value or "") is not None
    return False


def _deno_serve_port_misplaced(task: str) -> bool:
    """A `--port $PORT` that landed after the script argument, where deno never
    sees it. Distinct from "no port flag at all" so the two get different advice."""
    m = _DENO_SERVE_RE.search(task)
    if m is None:
        return False
    rest = task[m.end() :]
    return _DENO_TRAILING_PORT_RE.search(rest[_deno_flag_span(rest) :]) is not None


def _deno_config_data(cfg: Path) -> dict[str, Any] | None:
    """The parsed config, or None when it can't be read. Comments are stripped and
    trailing commas retried, because deno.jsonc legitimately carries both and
    rejecting them reported correctly-wired projects as broken."""
    try:
        text = _strip_js_comments(cfg.read_text())
    except OSError:
        return None
    for candidate in (text, _JSONC_TRAILING_COMMA_RE.sub(r"\1", text)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return data if isinstance(data, dict) else None
    return None


def _deno_tasks(cfg: Path) -> dict[str, str] | None:
    """The `tasks` table, or None when the config can't be read."""
    data = _deno_config_data(cfg)
    if data is None:
        return None
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        return {}
    return {k: v for k, v in tasks.items() if isinstance(v, str)}


def _deno_port_detect(cwd: Path) -> tuple[str, str]:
    cfg = _deno_config_path(cwd)
    if cfg is None:
        raise DeviceError("deno.json* not found")
    tasks = _deno_tasks(cfg)
    if tasks is None:
        return ("problem", f"{cfg.name} can't read as JSON; check the PORT wiring by hand")
    serve = {n: v for n, v in tasks.items() if _DENO_SERVE_RE.search(v)}
    if any(_deno_serve_port_wired(v) for v in serve.values()):
        return ("ok", f"{cfg.name} passes PORT to the server")
    misordered = [n for n, v in serve.items() if _deno_serve_port_misplaced(v)]
    if misordered:
        return (
            "problem",
            f"--port comes after the script argument in: {', '.join(sorted(misordered))}; "
            "deno passes it to the script, not to itself",
        )
    if _deno_sources_read_port(cwd):
        return ("ok", "server code reads PORT from the environment")
    return ("problem", "nothing consumes PORT; Deno has no port env var of its own")


def _deno_wire_serve_task(task: str) -> str:
    """Put `--port $PORT` directly after `deno serve`, never at the end: everything
    following the script argument is passed to the script, so an appended flag is
    silently ignored and the server keeps binding 8000.

    The rewrite is scoped to deno's own flag run. A blanket substitution over the
    whole string deleted `--port` from chained sidecar commands (`&& psql --port
    5432`), so past the script argument only splashdown's own `$PORT` is lifted."""
    m = _DENO_SERVE_RE.search(task)
    if m is None:
        return task
    head, rest = task[: m.end()], task[m.end() :]
    cut = _deno_flag_span(rest)
    flags = _DENO_ANY_PORT_RE.sub("", rest[:cut])
    argv = _DENO_TRAILING_PORT_RE.sub("", rest[cut:])
    return f"{head} --port $PORT{flags}{argv}".rstrip()


def _deno_port_autofix(cwd: Path) -> None:
    cfg = _deno_config_path(cwd)
    # jsonc may carry comments that a json round-trip would delete.
    if cfg is None or cfg.name.endswith(".jsonc"):
        return
    data = json.loads(cfg.read_text())
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        return
    changed = False
    for name, value in tasks.items():
        if not isinstance(value, str) or not _DENO_SERVE_RE.search(value):
            continue
        if _deno_serve_port_wired(value):
            continue
        tasks[name] = _deno_wire_serve_task(value)
        changed = True
    if changed:
        cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print(f"patched {cfg.name} (deno serve --port $PORT)", file=sys.stderr)


def _deno_port_manual(cwd: Path) -> str:
    return (
        "Deno reads no PORT env var of its own. Either pass it from the deno.json task:\n"
        '  "dev": "deno serve --port $PORT main.ts"\n'
        "or read it where the server starts:\n"
        '  Deno.serve({ port: Number(Deno.env.get("PORT")) || 8000 }, handler)'
    )


PROFILES["deno"] = DenoProfile()

_NEXTJS_CONFIG_NAMES = ("next.config.js", "next.config.ts", "next.config.mjs")


class NextJsProfile(Profile):
    name = "nextjs"
    env_only = True
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        for name in _NEXTJS_CONFIG_NAMES:
            if (app_path / name).exists():
                return True
        pkg = app_path / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text())
            except json.JSONDecodeError:
                return False
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
            return "next" in deps
        return False

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [3001, 3100]}}

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "PORT")
        return _manual_port_guidance(
            "Next.js", "npx next dev --port {port}", port, app.project_path
        )


PROFILES["nextjs"] = NextJsProfile()


class DjangoProfile(Profile):
    name = "django"
    env_only = True
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        mp = app_path / "manage.py"
        if not mp.exists():
            return False
        try:
            return "django" in mp.read_text()
        except OSError:
            return False

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [8001, 8100]}}

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "PORT")
        return _manual_port_guidance(
            "Django",
            "python manage.py runserver 127.0.0.1:{port}",
            port,
            app.project_path,
        )


PROFILES["django"] = DjangoProfile()


class FastApiProfile(Profile):
    name = "fastapi"
    env_only = True
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        for f in ("requirements.txt", "requirements-dev.txt"):
            req = app_path / f
            if req.exists() and "fastapi" in req.read_text().lower():
                return True
        pyproject = app_path / "pyproject.toml"
        return bool(pyproject.exists() and "fastapi" in pyproject.read_text().lower())

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [8001, 8100]}}


PROFILES["fastapi"] = FastApiProfile()


class FlaskProfile(Profile):
    name = "flask"
    env_only = True
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        for f in ("requirements.txt", "requirements-dev.txt"):
            req = app_path / f
            if req.exists() and "flask" in req.read_text().lower():
                return True
        pyproject = app_path / "pyproject.toml"
        return bool(pyproject.exists() and "flask" in pyproject.read_text().lower())

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        # Only `flask run` reads this; `python app.py` calling app.run() still
        # hardcodes 5000.
        return {"FLASK_RUN_PORT": {"type": "port", "range": [5001, 5100]}}

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "FLASK_RUN_PORT")
        return _manual_port_guidance("Flask", "flask run --port {port}", port, app.project_path)


# Registered after fastapi so a project carrying both deps resolves to fastapi —
# flask is the more common incidental dependency of the two.
PROFILES["flask"] = FlaskProfile()


class SpringBootProfile(Profile):
    name = "springboot"

    def detect(self, app_path: Path) -> bool:
        pom = app_path / "pom.xml"
        if pom.exists() and "spring-boot-starter" in pom.read_text():
            return True
        for grade in ("build.gradle", "build.gradle.kts"):
            g = app_path / grade
            if g.exists() and "org.springframework.boot" in g.read_text():
                return True
        return False

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [8081, 8180]}}

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_springboot_application_properties_check()]


def _springboot_application_properties_check() -> WiringCheck:
    return WiringCheck(
        id="springboot-application-properties",
        description="application.properties reads server.port from PORT env",
        # Derived from the same enumeration detect uses. Two hardcoded filenames here
        # skipped the check entirely on an `application.yaml`- or profile-only project.
        applies=lambda cwd: bool(_springboot_config_files(cwd)),
        detect=_springboot_app_props_detect,
        autofix=None,  # manual-only — patching Java configs is too risky to auto-rewrite
        manual_instructions=_springboot_app_props_manual,
    )


_SPRING_FLAT_PORT_RE = re.compile(r"^[ \t]*server\.port[ \t]*[:=][ \t]*(.+)$", re.MULTILINE)
_SPRING_NESTED_PORT_RE = re.compile(r"(?:^|[{,\n])[ \t]*port[ \t]*:[ \t]*([^,}\n]+)")


def _springboot_config_files(cwd: Path) -> list[Path]:
    """Every config Spring may load, profile-specific ones included: an
    `application-dev.properties` pinning a literal overrides a wired base file."""
    resources = cwd / "src" / "main" / "resources"
    found = [
        p
        for pattern in ("application*.properties", "application*.yml", "application*.yaml")
        for p in resources.glob(pattern)
    ]
    return sorted(found)


def _springboot_declared_port(path: Path) -> str | None:
    """The effective `server.port` a config declares, or None if it declares none.

    Reads the flat `server.port` spelling and YAML's nested `server:` block, which a
    properties file cannot write. The nested lookup is pinned to column 0 so
    `management.server.port` — Actuator's separate port, and perfectly normal — is not
    mistaken for the app's. The *last* declaration wins, matching Spring: taking the
    first read a wired value that a later line had already overridden."""
    text = _strip_hash_comments(path.read_text())
    values = [m.group(1).strip() for m in _SPRING_FLAT_PORT_RE.finditer(text)]
    for region in _yaml_key_regions(text, "server", indent=0):
        values += [m.group(1).strip() for m in _SPRING_NESTED_PORT_RE.finditer(region)]
    return values[-1] if values else None


def _springboot_app_props_detect(cwd: Path) -> tuple[str, str]:
    declared = {
        p.name: v for p in _springboot_config_files(cwd) if (v := _springboot_declared_port(p))
    }
    if not declared:
        return ("problem", "server.port should read ${PORT:8080} from env")
    pinned = sorted(name for name, value in declared.items() if "${PORT" not in value)
    if pinned:
        return ("problem", f"server.port is pinned to a literal in: {', '.join(pinned)}")
    return ("ok", "server.port uses PORT env placeholder")


def _springboot_app_props_manual(cwd: Path) -> str:
    return (
        "In application.properties: server.port=${PORT:8080}\n"
        "In application.yml:      server:\n                              port: ${PORT:8080}"
    )


PROFILES["springboot"] = SpringBootProfile()

# `dotnet new blazor|mvc|webapi|razor` all emit Microsoft.NET.Sdk.Web; only standalone
# Blazor WebAssembly uses its own SDK. Both ship the same launchSettings.json shape and
# both honour ASPNETCORE_HTTP_PORTS, so one profile covers them.
_CSPROJ_WEB_SDK_RE = re.compile(r'Sdk\s*=\s*"Microsoft\.NET\.Sdk\.(?:Web|BlazorWebAssembly)"')


def _launch_settings_path(app_path: Path) -> Path:
    return app_path / "Properties" / "launchSettings.json"


class AspNetCoreProfile(Profile):
    name = "aspnetcore"

    def detect(self, app_path: Path) -> bool:
        for proj in app_path.glob("*.csproj"):
            try:
                if _CSPROJ_WEB_SDK_RE.search(proj.read_text()):
                    return True
            except OSError:
                continue
        return False

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        # Skips .NET's own 5000/5001 so an unwired app can't accidentally be handed
        # the default and look wired. 5174-5200 is vite's, so start above it.
        return {"ASPNETCORE_HTTP_PORTS": {"type": "port", "range": [5201, 5300]}}

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        if _aspnet_supports_http_ports(app.path):
            return [_aspnet_launch_settings_check()]
        return [_aspnet_legacy_tfm_check()]

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "ASPNETCORE_HTTP_PORTS")
        return _manual_port_guidance(
            "ASP.NET Core",
            "dotnet run --urls http://localhost:{port}",
            port,
            app.project_path,
        )


_TFM_RE = re.compile(r"<TargetFrameworks?>([^<]+)</TargetFrameworks?>")
_NET_VERSION_RE = re.compile(r"^net(\d+)\.(\d+)$")


def _aspnet_supports_http_ports(app_path: Path) -> bool:
    """ASPNETCORE_HTTP_PORTS is .NET 8+. net6.0/net7.0 ignore it outright, so
    dropping their applicationUrl would strand the app on the shared default 5000 —
    the exact collision splashdown exists to prevent. A multi-target project counts
    as modern if any target qualifies; an absent or unparseable TFM is treated as
    modern, which is overwhelmingly the common case."""
    versions: list[tuple[int, int]] = []
    for proj in app_path.glob("*.csproj"):
        try:
            text = proj.read_text()
        except OSError:
            continue
        for group in _TFM_RE.findall(text):
            for tfm in group.split(";"):
                if m := _NET_VERSION_RE.match(tfm.strip()):
                    versions.append((int(m.group(1)), int(m.group(2))))
    return not versions or max(versions) >= (8, 0)


def _aspnet_legacy_tfm_check() -> WiringCheck:
    """Report-only twin of `aspnet-launch-settings` for pre-.NET-8 projects, where
    the autofix would do harm rather than good. Never reports `ok`: splashdown
    genuinely cannot wire this TFM mechanically, and a green verdict on a broken
    wiring is the one outcome these checks exist to prevent."""
    return WiringCheck(
        id="aspnet-launch-settings",
        description="launchSettings.json lets the allocated port reach the app",
        applies=lambda cwd: _launch_settings_path(cwd).exists(),
        detect=lambda cwd: (
            "problem",
            "project targets .NET < 8, which ignores ASPNETCORE_HTTP_PORTS",
        ),
        autofix=None,
        manual_instructions=_aspnet_legacy_manual,
    )


def _aspnet_legacy_manual(cwd: Path) -> str:
    return (
        "ASPNETCORE_HTTP_PORTS needs .NET 8+. Either retarget the project, or derive\n"
        "the URL form this TFM does read by adding to splashdown.toml:\n"
        "  [resources.ASPNETCORE_URLS]\n"
        '  type     = "template"\n'
        '  template = "http://localhost:{{ ASPNETCORE_HTTP_PORTS }}"\n'
        "then remove `applicationUrl` from the Project profiles in\n"
        "Properties/launchSettings.json so the environment wins."
    )


def _read_launch_settings(path: Path) -> tuple[Any, str, str]:
    """Parsed JSON plus the byte conventions to write it back with. The .NET
    templates emit this file with a UTF-8 BOM and CRLF endings — a plain
    `read_text()` leaves the BOM in the string and `json.loads` then rejects a
    perfectly valid file, and a plain `write_text()` would strip both and churn
    every line for Windows-authored projects."""
    raw = path.read_bytes()
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    text = raw.decode(encoding)
    return json.loads(text), encoding, "\r\n" if "\r\n" in text else "\n"


def _aspnet_project_profiles(data: Any) -> dict[str, Any]:
    """The `commandName: "Project"` launch profiles — the ones `dotnet run` uses.
    IISExpress profiles carry their own applicationUrl that only IIS Express reads,
    so touching them would be an edit with no effect on the dev-server port."""
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        return {}
    return {
        k: v
        for k, v in profiles.items()
        if isinstance(v, dict) and v.get("commandName") == "Project"
    }


def _aspnet_launch_settings_check() -> WiringCheck:
    """`dotnet run` reads applicationUrl out of launchSettings.json and it wins over
    an inherited ASPNETCORE_HTTP_PORTS, so the allocated port is silently ignored
    while the file declares one. Dropping the key is the fix, and launchSettings is
    JSON — unlike the compose/Spring cases, the rewrite is safely mechanical."""
    return WiringCheck(
        id="aspnet-launch-settings",
        description="launchSettings.json lets ASPNETCORE_HTTP_PORTS set the port",
        applies=lambda cwd: _launch_settings_path(cwd).exists(),
        detect=_aspnet_launch_settings_detect,
        autofix=_aspnet_launch_settings_autofix,
        manual_instructions=_aspnet_launch_settings_manual,
    )


def _aspnet_launch_settings_detect(cwd: Path) -> tuple[str, str]:
    path = _launch_settings_path(cwd)
    try:
        data, _, _ = _read_launch_settings(path)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ("problem", "launchSettings.json is not valid JSON")
    pinned = [
        name for name, spec in _aspnet_project_profiles(data).items() if spec.get("applicationUrl")
    ]
    if pinned:
        return ("problem", f"launchSettings.json pins applicationUrl in: {', '.join(pinned)}")
    return ("ok", "no launch profile pins applicationUrl")


def _aspnet_launch_settings_autofix(cwd: Path) -> None:
    path = _launch_settings_path(cwd)
    data, encoding, newline = _read_launch_settings(path)
    changed = False
    for spec in _aspnet_project_profiles(data).values():
        changed = spec.pop("applicationUrl", None) is not None or changed
    if changed:
        # Append a bare "\n" — `newline=` translates every \n on write, so passing
        # the CRLF here too would emit a trailing \r\r\n.
        path.write_text(json.dumps(data, indent=2) + "\n", encoding=encoding, newline=newline)
        print("patched Properties/launchSettings.json (dropped applicationUrl)", file=sys.stderr)


def _aspnet_launch_settings_manual(cwd: Path) -> str:
    return (
        'Remove the `applicationUrl` key from each `"commandName": "Project"`\n'
        "profile in Properties/launchSettings.json. With it gone, `dotnet run`\n"
        "falls back to ASPNETCORE_HTTP_PORTS from the environment."
    )


PROFILES["aspnetcore"] = AspNetCoreProfile()

_GEMFILE_RAILS_RE = re.compile(r"""^\s*gem\s+["']rails["']""", re.MULTILINE)


class RailsProfile(Profile):
    name = "rails"
    env_only = True
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        app_rb = app_path / "config" / "application.rb"
        if app_rb.exists() and "Rails::Application" in app_rb.read_text():
            return True
        gemfile = app_path / "Gemfile"
        return bool(gemfile.exists() and _GEMFILE_RAILS_RE.search(gemfile.read_text()))

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [3001, 3100]}}

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "PORT")
        return _manual_port_guidance(
            "Rails", "bin/rails server --port {port}", port, app.project_path
        )


PROFILES["rails"] = RailsProfile()


# Default device targets emitted by scanner-driven `splash init`, kept in sync
# with the preset scaffolds (`_RN_SCAFFOLD` etc.). iOS sim left at implicit
# `ios = "latest"` (auto-recreate on newer iOS); Android emulator on pixel_9.
_DEFAULT_SIM_TARGET: dict[str, dict[str, dict[str, str]]] = {
    "simulator": {"default": {"model": "iPhone 17"}}
}
_DEFAULT_EMULATOR_TARGET: dict[str, dict[str, dict[str, str]]] = {
    "emulator": {"default": {"device": "pixel_9"}}
}
_DEFAULT_MOBILE_TARGETS = {**_DEFAULT_SIM_TARGET, **_DEFAULT_EMULATOR_TARGET}


class ReactNativeProfile(Profile):
    name = "react-native"

    def detect(self, app_path: Path) -> bool:
        return _detect_rn(app_path)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"RCT_METRO_PORT": {"type": "port", "range": [8082, 8200]}}

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_MOBILE_TARGETS

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return list(_RN_WIRING_CHECKS)

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "RCT_METRO_PORT")
        return [
            "- Metro must use the allocated port; do not start it on a numeric default.",
            *_manual_port_guidance(
                "Metro", "npx react-native start --port {port}", port, app.project_path
            ),
            "- Launch with `splash run simulator` or `splash run emulator`.",
        ]

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        return _rn_run(cwd, recipe, info)


class ExpoProfile(Profile):
    name = "expo"

    def detect(self, app_path: Path) -> bool:
        return _detect_expo(app_path)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"RCT_METRO_PORT": {"type": "port", "range": [8082, 8200]}}

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_MOBILE_TARGETS

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "RCT_METRO_PORT")
        return [
            "- Metro must use the allocated port; do not start it on a numeric default.",
            *_manual_port_guidance(
                "Expo Metro", "npx expo start --port {port}", port, app.project_path
            ),
            "- Launch with `splash run simulator` or `splash run emulator`.",
        ]

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        return _expo_run(cwd, recipe, info)


class FlutterProfile(Profile):
    name = "flutter"

    def detect(self, app_path: Path) -> bool:
        return _detect_flutter(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_MOBILE_TARGETS

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        return _flutter_run(cwd, recipe, info)


class IosNativeProfile(Profile):
    name = "ios-native"

    def detect(self, app_path: Path) -> bool:
        return _detect_ios_native(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_SIM_TARGET

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_HOOK_WIRING_CHECK]

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        return _ios_native_run(cwd, recipe, info)


class AndroidNativeProfile(Profile):
    name = "android-native"

    def detect(self, app_path: Path) -> bool:
        return _detect_android_native(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_EMULATOR_TARGET

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_HOOK_WIRING_CHECK]

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        return _android_native_run(cwd, recipe, info)


# Mobile precedence: pubspec.yaml (flutter) wins over an `expo` or `react-native`
# dep in package.json — a flutter project might transitively pull in JS tooling
# but rarely the other way around. Then expo (requires both `expo` dep and
# app.json) before plain react-native.
PROFILES["flutter"] = FlutterProfile()
PROFILES["expo"] = ExpoProfile()
PROFILES["react-native"] = ReactNativeProfile()
PROFILES["ios-native"] = IosNativeProfile()
PROFILES["android-native"] = AndroidNativeProfile()


# Named scaffolds for `splash init NAME`. Decoupled from PROFILES because some
# entries (minimal, electron, server) don't have a detectable framework, and
# some Profiles (vite, springboot, etc.) don't have a stock scaffold.
