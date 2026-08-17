from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .errors import DeviceError
from .inventory import AppInventory
from .package_json import package_dependencies
from .profile_core import Profile, _manual_port_guidance, _profile_port
from .wiring import WiringCheck, _strip_js_comments

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


# Ahead of `vite` for the same reason as laravel: Nuxt is Vite-based, and while the
# minimal template ships no vite.config, a project that adds one must still resolve to
# nuxt — ViteProfile would emit WEB_DEV_PORT that `nuxt dev` never reads.


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
