from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .inventory import AppInventory
from .profile_core import Profile, _manual_port_guidance, _profile_port
from .wiring import WiringCheck, _strip_hash_comments, _yaml_key_regions


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
