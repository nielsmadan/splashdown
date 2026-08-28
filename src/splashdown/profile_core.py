from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from .inventory import AppInventory
from .wiring import WiringCheck

# Built-in profiles register framework resources and wiring checks in PROFILES; there is no plugin API.


class Profile:
    """Abstract base. Subclasses set `name` and override the integration rules
    they support. Runnable profiles additionally satisfy `RunnableProfile`."""

    name: str = ""

    # True only when an empty check list means the framework reads its port directly from the environment.
    env_only: bool = False

    # Whether envfile delivery is expected to reach the app when no shell loader is available.
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
