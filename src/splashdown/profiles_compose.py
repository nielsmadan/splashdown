from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .errors import DeviceError
from .wiring import WiringCheck, _strip_hash_comments, _yaml_key_regions

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
            "template": "{{ slug(parent) }}-{{ slug(cwd) }}-{{ truncate(hash(cwd_abs), 8) }}",
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
