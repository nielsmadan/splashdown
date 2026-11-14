from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .errors import DeviceError
from .inventory import AppInventory, ManagedResource
from .jsontext import _member_span, _object_span, _set_json_member
from .package_json import package_dependencies
from .profile_core import (
    Profile,
    _manual_port_guidance,
    _profile_port,
    managed_ports,
    resource_scope_check,
)
from .safe_files import atomic_write_text, read_editable_text
from .wiring import (
    WiringCheck,
    _js_mask_comments,
    _js_mask_literals,
    _js_member,
    _port_reads,
    _PortRead,
    _server_port_span,
    _shell_port_span,
    _shell_port_wired,
    _shell_segments,
    _strip_js_comments,
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
        return _astro_config_path(app_path) is not None or "astro" in package_dependencies(app_path)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        # Skips Astro's own 4321 so an unwired app can't accidentally be handed
        # the default and look wired.
        return {"WEB_DEV_PORT": {"type": "port", "range": [4322, 4400]}}

    def wiring_checks(self, app: AppInventory, env_file: str) -> list[WiringCheck]:
        ports = managed_ports(app, "WEB_DEV_PORT", env_file)
        if len(ports) != 1:
            return [resource_scope_check(ports)]
        return [_astro_port_check(ports[0].name)]

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "WEB_DEV_PORT")
        return _manual_port_guidance("Astro", "npx astro dev --port {port}", port, app.project_path)


def _astro_port_check(port_var: str = "WEB_DEV_PORT") -> WiringCheck:
    return WiringCheck(
        id="astro-config-port",
        description=f"astro.config wires server.port to {port_var}",
        applies=lambda cwd: _astro_config_path(cwd) is not None,
        detect=lambda cwd: _astro_port_detect(cwd, port_var),
        autofix=lambda cwd: _astro_port_autofix(cwd, port_var),
        manual_instructions=lambda cwd: _astro_port_manual(cwd).replace("WEB_DEV_PORT", port_var),
        files=lambda cwd: _config_files(_astro_config_path(cwd)),
    )


def _astro_port_detect(cwd: Path, port_var: str = "WEB_DEV_PORT") -> tuple[str, str]:
    cfg = _astro_config_path(cwd)
    if cfg is None:
        raise DeviceError("astro.config.* not found")
    text = read_editable_text(cfg, root=cwd)
    try:
        span = _server_port_span(text)
        if span is not None:
            reads = _port_reads(_js_mask_comments(text), span, frozenset({port_var}))
            if reads:
                if reads[0].source != "process.env":
                    raise ValueError("unsupported server.port environment source")
                return "ok", f"astro.config server.port reads {port_var}"
        return "problem", f"astro.config server.port does not read {port_var}"
    except ValueError as error:
        return "incomplete", str(error)


def _astro_port_autofix(cwd: Path, port_var: str = "WEB_DEV_PORT") -> None:
    cfg = _astro_config_path(cwd)
    if cfg is None:
        raise DeviceError("astro.config.* not found")
    text = read_editable_text(cfg, root=cwd)
    code = _js_mask_comments(text)
    try:
        span = _server_port_span(code)
    except ValueError:
        return
    if span is not None or _ASTRO_SERVER_BLOCK_RE.search(code):
        return
    match = _ASTRO_DEFAULT_EXPORT_RE.search(code)
    if match is None:
        return
    block = f"\n  server: {{ port: Number(process.env.{port_var}) || 4321 }},"
    atomic_write_text(cfg, text[: match.end()] + block + text[match.end() :], root=cwd)


def _astro_port_manual(cwd: Path) -> str:
    return (
        "Astro does not read PORT from the environment, so the dev port has to be\n"
        "in astro.config. Add it to the top-level config object:\n"
        "  server: { port: Number(process.env.WEB_DEV_PORT) || 4321 }\n"
        "If you already have a `server:` block, put the port there — a block nested\n"
        "under `vite:` configures Vite's server, not Astro's."
    )


_VITE_CONFIG_NAMES = ("vite.config.ts", "vite.config.js", "vite.config.mjs", "vite.config.mts")


def _config_files(path: Path | None) -> tuple[Path, ...]:
    return () if path is None else (path,)


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

    def wiring_checks(self, app: AppInventory, env_file: str) -> list[WiringCheck]:
        ports = managed_ports(app, "WEB_DEV_PORT", env_file)
        if not ports:
            return [resource_scope_check(ports)]
        return _vite_checks(ports)

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "WEB_DEV_PORT")
        return _manual_port_guidance("Vite", "npx vite --port {port}", port, app.project_path)


def _js_call_arguments(text: str, open_paren: int) -> list[str] | None:
    """The top-level arguments of the call whose `(` sits at `open_paren`, or None
    when the parentheses do not balance."""
    depth = 0
    args: list[str] = []
    current: list[str] = []
    quote = ""
    i = open_paren
    while i < len(text):
        ch = text[i]
        if quote and ch == "\\":
            current.append(text[i : i + 2])
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(current))
                return [a.strip() for a in args]
        elif ch == "," and depth == 1:
            args.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    return None


_LOAD_ENV_PREFIX_ARITY = 3


def _load_env_prefixes(code: str, source: str) -> tuple[str, ...]:
    if not re.search(
        r"(?:^|\n)\s*import\s*\{[^}]*\bloadEnv\b(?!\s+as\b)[^}]*\}\s*from\s*['\"]vite['\"]",
        code,
    ) or re.search(r"\b(?:function|const|let|var)\s+loadEnv\b", code):
        raise ValueError("loadEnv is not an unshadowed named import from vite")
    if re.search(rf"\b{re.escape(source)}\s*(?:\.\w+|\[[^]]+\])\s*=", code):
        raise ValueError("loadEnv result is modified before consumption")
    if len(re.findall(rf"\b{re.escape(source)}\s*=(?!=)", code)) != 1:
        raise ValueError("loadEnv binding is reassigned or shadowed")
    bindings = list(re.finditer(rf"\bconst\s+{re.escape(source)}\s*=\s*loadEnv\s*\(", code))
    if len(bindings) != 1:
        raise ValueError(f"cannot establish {source}'s loadEnv binding")
    args = _js_call_arguments(code, bindings[0].end() - 1)
    if args is None or len(args) not in {2, 3}:
        raise ValueError("unsupported loadEnv arguments")
    if len(args) < _LOAD_ENV_PREFIX_ARITY or args[2] == "undefined":
        return ("VITE_",)
    literal = r"[\"']([^\"'\\]*)[\"']"
    if match := re.fullmatch(literal, args[2]):
        return (match.group(1),)
    if args[2].startswith("[") and args[2].endswith("]"):
        body = args[2][1:-1].strip()
        if not body:
            return ()
        parts = _js_call_arguments("(" + body + ")", 0)
        if parts is not None:
            matches = [re.fullmatch(literal, part) for part in parts if part]
            if all(match is not None for match in matches):
                return tuple(match.group(1) for match in matches if match is not None)
    raise ValueError("loadEnv prefix is dynamic; verify its managed-key filter manually")


def _vite_analysis(
    text: str, ports: tuple[ManagedResource, ...]
) -> tuple[str, str, tuple[_PortRead, ...]]:
    code = _js_mask_comments(text)
    try:
        span = _server_port_span(code)
        if span is None:
            labels = ", ".join(resource.name for resource in ports)
            return (
                "problem" if len(ports) == 1 else "incomplete",
                f"server.port does not consume a declared port ({labels})",
                (),
            )
        names = {resource.name for resource in ports}
        reads = _port_reads(code, span, frozenset(names))
        selected = {read.name for read in reads if read.name in names}
        if len(selected) != 1:
            return (
                "problem" if len(ports) == 1 else "incomplete",
                "Cannot identify one managed server port from: " + ", ".join(sorted(names)),
                (),
            )
        name = next(iter(selected))
        rejected = []
        for read in reads:
            if read.name != name:
                raise ValueError("server.port fallback may select an unrelated environment value")
            if read.source == "process.env":
                break
            prefixes = _load_env_prefixes(code, read.source)
            if any(name.startswith(prefix) for prefix in prefixes):
                break
            rejected.append(read)
        else:
            if rejected:
                return (
                    "problem",
                    f"loadEnv's prefix excludes {name} from server.port",
                    tuple(rejected),
                )
        return (
            "ok",
            f"server.port consumes {name} from the inherited environment or native loadEnv",
            (),
        )
    except ValueError as error:
        return "incomplete", str(error), ()


def _vite_checks(ports: tuple[ManagedResource, ...]) -> list[WiringCheck]:
    def detect(cwd: Path) -> tuple[str, str]:
        cfg = _vite_config_path(cwd)
        if cfg is None:
            raise DeviceError("vite.config.* not found")
        state, detail, _ = _vite_analysis(read_editable_text(cfg, root=cwd), ports)
        return state, detail

    def repair(cwd: Path) -> None:
        cfg = _vite_config_path(cwd)
        if cfg is None:
            raise DeviceError("vite.config.* not found")
        text = read_editable_text(cfg, root=cwd)
        _, _, rejected = _vite_analysis(text, ports)
        updated = text
        for read in sorted(set(rejected), key=lambda read: read.start, reverse=True):
            original = text[read.start : read.end]
            updated = (
                updated[: read.start]
                + f"(process.env.{read.name} ?? {original})"
                + updated[read.end :]
            )
        if updated != text:
            atomic_write_text(cfg, updated, root=cwd)

    names = ", ".join(resource.name for resource in ports)
    guidance = f"Wire the intended managed port ({names}) to server.port. Use process.env or loadEnv with a literal prefix admitting that key. Existing fallback expressions can remain. Dynamic configurations require manual verification."
    return [
        WiringCheck(
            id="vite-port-wired",
            description="vite.config consumes the declared server-port resource",
            applies=lambda cwd: _vite_config_path(cwd) is not None,
            detect=detect,
            autofix=repair,
            manual_instructions=lambda _: guidance,
            files=lambda cwd: _config_files(_vite_config_path(cwd)),
        )
    ]


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

    def wiring_checks(self, app: AppInventory, env_file: str) -> list[WiringCheck]:
        # SERVER_PORT is inherited; only the Vite server needs a config check.
        if _vite_config_path(app.path) is None:
            return []
        return ViteProfile().wiring_checks(app, env_file)

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


_NUXT_CONFIG_NAMES = ("nuxt.config.ts", "nuxt.config.js", "nuxt.config.mjs")


class NuxtProfile(Profile):
    name = "nuxt"
    env_only = True
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        if any((app_path / n).exists() for n in _NUXT_CONFIG_NAMES):
            return True
        return "nuxt" in package_dependencies(app_path)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        # NUXT_PORT rather than the also-supported PORT: the specific name can't be
        # claimed by a sibling backend in a monorepo. Skips Nuxt's own 3000.
        return {"NUXT_PORT": {"type": "port", "range": [3001, 3100]}}

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "NUXT_PORT")
        return _manual_port_guidance("Nuxt", "npx nuxt dev --port {port}", port, app.project_path)


class AngularProfile(Profile):
    name = "angular"

    def detect(self, app_path: Path) -> bool:
        return (app_path / "angular.json").exists()

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        # Skips Angular's own 4200 so an unwired app can't look wired.
        return {"WEB_DEV_PORT": {"type": "port", "range": [4201, 4300]}}

    def wiring_checks(self, app: AppInventory, env_file: str) -> list[WiringCheck]:
        ports = managed_ports(app, "WEB_DEV_PORT", env_file)
        return (
            [_angular_pkg_port_check(ports[0].name)]
            if len(ports) == 1
            else [resource_scope_check(ports)]
        )

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "WEB_DEV_PORT")
        return _manual_port_guidance(
            "Angular", "npx ng serve --port {port}", port, app.project_path
        )


_NG_SERVE_RE = re.compile(r"(?:npx\s+)?ng\s+serve\b")


def _command_span(value: str, pattern: re.Pattern[str]) -> tuple[int, int] | None:
    matches = []
    for start, end in _shell_segments(value):
        match = pattern.match(value[start:end].lstrip())
        if match:
            padding = len(value[start:end]) - len(value[start:end].lstrip())
            matches.append((start + padding + match.end(), end))
    return matches[0] if len(matches) == 1 else None


def _angular_serve_scripts(data: Any) -> dict[str, str]:
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {
        k: v
        for k, v in scripts.items()
        if isinstance(v, str) and _command_span(v, _NG_SERVE_RE) is not None
    }


def _angular_pkg_port_check(port_var: str = "WEB_DEV_PORT") -> WiringCheck:
    """Angular reads no environment variable for its dev-server port — only
    `angular.json` or `--port`. Writing the allocated port as a literal into the
    (committed) angular.json would churn it in every worktree, which is the collision
    splashdown exists to remove, so the port is passed through the npm script instead:
    npm runs scripts via a shell, so `$WEB_DEV_PORT` expands from the loader's env."""
    return WiringCheck(
        id="angular-pkg-port",
        description=f"package.json `ng serve` scripts pass --port ${port_var}",
        applies=lambda cwd: (cwd / "package.json").exists() and (cwd / "angular.json").exists(),
        detect=lambda cwd: _angular_pkg_port_detect(cwd, port_var),
        autofix=lambda cwd: _angular_pkg_port_autofix(cwd, port_var),
        manual_instructions=lambda cwd: _angular_pkg_port_manual(cwd).replace(
            "WEB_DEV_PORT", port_var
        ),
        files=lambda cwd: (cwd / "package.json",),
    )


def _angular_pkg_port_detect(cwd: Path, port_var: str = "WEB_DEV_PORT") -> tuple[str, str]:
    try:
        data = json.loads((cwd / "package.json").read_text())
    except (json.JSONDecodeError, OSError) as e:
        return ("incomplete", f"could not read package.json: {e}")
    serving = _angular_serve_scripts(data)
    if not serving:
        # Without an ng serve script, the allocated port cannot reach Angular.
        return (
            "problem",
            f"no `ng serve` script to carry --port; inspect {port_var} manually",
        )
    unwired = [n for n, v in serving.items() if not _angular_script_wired(v, port_var)]
    if unwired:
        return ("problem", f"`ng serve` scripts ignore {port_var}: {', '.join(unwired)}")
    return ("ok", f"`ng serve` scripts pass {port_var}")


def _angular_script_wired(value: str, port_var: str) -> bool:
    span = _command_span(value, _NG_SERVE_RE)
    return span is not None and _shell_port_wired(value[span[0] : span[1]], port_var)


def _replace_shell_port(flags: str, port_var: str) -> str | None:
    try:
        span = _shell_port_span(flags)
    except ValueError:
        return None
    if span is None:
        return f" --port ${port_var}" + flags
    start, end = span
    value = flags[start:end]
    if re.fullmatch(r"\d+", value):
        return flags[:start] + f"${{{port_var}:-{value}}}" + flags[end:]
    return None


def _angular_wire_serve_script(value: str, port_var: str = "WEB_DEV_PORT") -> str:
    """Insert `--port $WEB_DEV_PORT` into the `ng serve` command itself, replacing any
    port flag already on it. Appending to the end of the script instead put the flag
    on whatever came after a `&&`, and left a second `--port` behind when the existing
    one was a variable rather than a literal."""
    span = _command_span(value, _NG_SERVE_RE)
    if span is None:
        return value
    start, end = span
    flags = _replace_shell_port(value[start:end], port_var)
    return value if flags is None else value[:start] + flags + value[end:]


def _angular_pkg_port_autofix(cwd: Path, port_var: str = "WEB_DEV_PORT") -> None:
    path = cwd / "package.json"
    text = read_editable_text(path, root=cwd)
    data = json.loads(text)
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return
    changed = False
    for name, value in _angular_serve_scripts(data).items():
        if _angular_script_wired(value, port_var):
            continue
        root = _object_span(text, data, None)
        member = _member_span(text, root, "scripts") if root is not None else None
        if member is None:
            return
        updated = _angular_wire_serve_script(value, port_var)
        if updated == value:
            continue
        text = _set_json_member(text, (member[1], member[2] - 1), name, json.dumps(updated))
        scripts[name] = updated
        changed = True
    if changed and json.loads(text) == data:
        atomic_write_text(path, text, root=cwd)
        print(f"patched package.json (ng serve consumes {port_var})", file=sys.stderr)


def _angular_pkg_port_manual(cwd: Path) -> str:
    return (
        "Angular exposes no env var for the dev-server port, so pass it in the script:\n"
        '  "start": "ng serve --port $WEB_DEV_PORT"\n'
        "This wires `npm start`; a bare `ng serve` still uses angular.json's default."
    )


_NODE_BACKEND_DEPS = {"hono", "express", "fastify", "koa", "@hapi/hapi", "@nestjs/core"}


class NodeBackendProfile(Profile):
    name = "node-backend"
    env_only = True
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        deps = package_dependencies(app_path)
        return any(dependency in deps for dependency in _NODE_BACKEND_DEPS)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [9081, 9100]}}


_DENO_CONFIG_NAMES = ("deno.json", "deno.jsonc")
_DENO_SERVE_RE = re.compile(r"deno\s+serve\b")


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

    def wiring_checks(self, app: AppInventory, env_file: str) -> list[WiringCheck]:
        ports = managed_ports(app, "PORT", env_file)
        return (
            [_deno_port_check(ports[0].name)] if len(ports) == 1 else [resource_scope_check(ports)]
        )

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


def _deno_port_check(port_var: str = "PORT") -> WiringCheck:
    """Deno is the one runtime here with no port convention at all: neither
    `deno serve` nor `Deno.serve()` consults an environment variable, so an allocated
    PORT reaches nothing until either a task passes `--port $PORT` or the server code
    reads it. Only the first is mechanical, so autofix handles `deno serve` tasks and
    everything else falls through to instructions."""
    return WiringCheck(
        id="deno-port-wired",
        description=f"a deno task or server port expression consumes {port_var}",
        applies=lambda cwd: _deno_config_path(cwd) is not None,
        detect=lambda cwd: _deno_port_detect(cwd, port_var),
        autofix=lambda cwd: _deno_port_autofix(cwd, port_var),
        manual_instructions=lambda cwd: _deno_port_manual(cwd).replace("PORT", port_var),
        files=lambda cwd: _config_files(_deno_config_path(cwd)),
    )


def _deno_sources_read_port(cwd: Path, port_var: str = "PORT") -> bool:
    for path in sorted([*cwd.glob("*.ts"), *cwd.glob("*.js"), *cwd.glob("*.tsx")]):
        text = _js_mask_comments(read_editable_text(path, root=cwd))
        calls = list(re.finditer(r"\bDeno\.serve\s*\(\s*\{", _js_mask_literals(text)))
        for call in calls:
            span = _js_member(text, call.end() - 1, "port")
            if span is None:
                continue
            value = text[span[0] : span[1]]
            read = re.compile(r"\bDeno\.env\.get\(\s*[\"']" + re.escape(port_var) + r"[\"']\s*\)")
            visible = _js_mask_literals(value)
            matches = [match for match in read.finditer(value) if visible[match.start()].strip()]
            if not matches:
                continue
            replacement = f"process.env.{port_var}"
            deno_reads = set()
            offset = 0
            for match in matches:
                start = match.start() + offset
                value = value[:start] + replacement + value[match.end() + offset :]
                deno_reads.add(start)
                offset += len(replacement) - (match.end() - match.start())
            try:
                reads = _port_reads(value, (0, len(value)), frozenset({port_var}))
                if reads and reads[0].start in deno_reads:
                    return True
            except ValueError:
                continue
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
_DENO_TRAILING_PORT_RE = re.compile(r"""\s+--port[=\s]+["']?\$\{?PORT\}?["']?""")
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


def _deno_serve_port_wired(task: str, port_var: str = "PORT") -> bool:
    """Whether a `deno serve` task passes PORT *to deno*. Position is the whole
    point: everything after the script argument is handed to the script, so an
    appended `--port $PORT` is silently dropped and the server keeps binding 8000."""
    span = _command_span(task, _DENO_SERVE_RE)
    if span is None:
        return False
    rest = task[span[0] : span[1]]
    return _shell_port_wired(rest[: _deno_flag_span(rest)], port_var)


def _deno_serve_port_misplaced(task: str, port_var: str = "PORT") -> bool:
    """A `--port $PORT` that landed after the script argument, where deno never
    sees it. Distinct from "no port flag at all" so the two get different advice."""
    span = _command_span(task, _DENO_SERVE_RE)
    if span is None:
        return False
    rest = task[span[0] : span[1]]
    return (
        re.compile(_DENO_TRAILING_PORT_RE.pattern.replace("PORT", port_var)).search(
            rest[_deno_flag_span(rest) :]
        )
        is not None
    )


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
    """Return string tasks, `{}` when absent, or `None` when the config is unreadable."""
    data = _deno_config_data(cfg)
    if data is None:
        return None
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        return {}
    return {k: v for k, v in tasks.items() if isinstance(v, str)}


def _deno_port_detect(cwd: Path, port_var: str = "PORT") -> tuple[str, str]:
    cfg = _deno_config_path(cwd)
    if cfg is None:
        raise DeviceError("deno.json* not found")
    tasks = _deno_tasks(cfg)
    if tasks is None:
        return ("incomplete", f"{cfg.name} cannot be parsed as JSON; inspect {port_var} manually")
    serve = {n: v for n, v in tasks.items() if _command_span(v, _DENO_SERVE_RE) is not None}
    if serve and all(_deno_serve_port_wired(v, port_var) for v in serve.values()):
        return ("ok", f"{cfg.name} passes {port_var} to the server")
    misordered = [n for n, v in serve.items() if _deno_serve_port_misplaced(v, port_var)]
    if misordered:
        return (
            "problem",
            (
                f"--port comes after the script argument in: {', '.join(sorted(misordered))}; "
                "deno passes it to the script, not to itself"
            ),
        )
    if not serve and _deno_sources_read_port(cwd, port_var):
        return ("ok", f"server code reads {port_var} from the environment")
    return (
        "problem" if serve else "incomplete",
        f"No recognized consumer of {port_var}; inspect Deno.serve's port or deno serve flags manually",
    )


def _deno_wire_serve_task(task: str, port_var: str = "PORT") -> str:
    """Put `--port $PORT` directly after `deno serve`, never at the end: everything
    following the script argument is passed to the script, so an appended flag is
    silently ignored and the server keeps binding 8000.

    The rewrite is scoped to deno's own flag run. A blanket substitution over the
    whole string deleted `--port` from chained sidecar commands (`&& psql --port
    5432`), so past the script argument only splashdown's own `$PORT` is lifted."""
    span = _command_span(task, _DENO_SERVE_RE)
    if span is None:
        return task
    head, rest = task[: span[0]], task[span[0] : span[1]]
    cut = _deno_flag_span(rest)
    flags = _replace_shell_port(rest[:cut], port_var)
    if flags is None:
        return task
    argv = re.compile(_DENO_TRAILING_PORT_RE.pattern.replace("PORT", port_var)).sub("", rest[cut:])
    return f"{head}{flags}{argv}" + task[span[1] :]


def _deno_port_autofix(cwd: Path, port_var: str = "PORT") -> None:
    cfg = _deno_config_path(cwd)
    # jsonc may carry comments that a json round-trip would delete.
    if cfg is None or cfg.name.endswith(".jsonc"):
        return
    text = read_editable_text(cfg, root=cwd)
    data = json.loads(text)
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        return
    changed = False
    for name, value in tasks.items():
        if not isinstance(value, str) or _command_span(value, _DENO_SERVE_RE) is None:
            continue
        if _deno_serve_port_wired(value, port_var):
            continue
        root = _object_span(text, data, None)
        member = _member_span(text, root, "tasks") if root is not None else None
        if member is None:
            return
        updated = _deno_wire_serve_task(value, port_var)
        if updated == value:
            continue
        text = _set_json_member(text, (member[1], member[2] - 1), name, json.dumps(updated))
        tasks[name] = updated
        changed = True
    if changed and json.loads(text) == data:
        atomic_write_text(cfg, text, root=cwd)
        print(f"patched {cfg.name} (deno serve consumes {port_var})", file=sys.stderr)


def _deno_port_manual(cwd: Path) -> str:
    return (
        "Deno reads no PORT env var of its own. Either pass it from the deno.json task:\n"
        '  "dev": "deno serve --port $PORT main.ts"\n'
        "or read it where the server starts:\n"
        '  Deno.serve({ port: Number(Deno.env.get("PORT")) || 8000 }, handler)'
    )


_NEXTJS_CONFIG_NAMES = ("next.config.js", "next.config.ts", "next.config.mjs")


class NextJsProfile(Profile):
    name = "nextjs"
    env_only = True
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        for name in _NEXTJS_CONFIG_NAMES:
            if (app_path / name).exists():
                return True
        return "next" in package_dependencies(app_path)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [3001, 3100]}}

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "PORT")
        return _manual_port_guidance(
            "Next.js", "npx next dev --port {port}", port, app.project_path
        )
