from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .constants import RECIPE_NAME, split_lines
from .errors import DeviceError
from .recipe import Recipe
from .safe_files import read_editable_text, read_optional_editable_text
from .wiring import WiringCheck
from .yamltext import _strip_hash_comments, _yaml_key_regions

# A compose file is infrastructure spanning apps, not an app, so it is not matched
# per-directory by the scanner. Its resources are emitted once for the repo and its
# check runs alongside whatever framework doctor resolved.

_COMPOSE_FILE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
_COMPOSE_LONG_SYNTAX_RE = re.compile(
    r"(?:^|[{,\n])[ \t]*(?:target|published|protocol|mode|host_ip|name)[ \t]*:"
)
_COMPOSE_PORT_NUMBER_RE = re.compile(r"^[0-9]+(?:-[0-9]+)?$")
# A `${VAR:-5432}` contains a colon, so port slots can only be split after the
# variables are masked out. The sentinel is deliberately colon-free.
_COMPOSE_VAR_RE = re.compile(r"\$\{[^}]*\}|\$\w+")
_VAR_MASK = "\x00"
# Short syntax tops out at host_ip:host:container.
_COMPOSE_MAX_PORT_FIELDS = 3
_COMPOSE_MAX_VOLUME_FIELDS = 3


def _compose_file_path(root: Path) -> Path | None:
    for name in _COMPOSE_FILE_NAMES:
        candidate = root / name
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
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
        description="Compose service slots have no recognized fixed sharing risks",
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
    region = region.strip()
    if not region or region[0] not in "[{" or region[-1] != {"[": "]", "{": "}"}[region[0]]:
        raise ValueError(f"unrecognized flow collection: {region}")
    entries: list[str] = []
    start, quote, escaped = 1, "", False
    stack: list[str] = []
    for i, ch in enumerate(region[1:-1], 1):
        if escaped:
            escaped = False
        elif quote:
            if ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            stack.append({"[": "]", "{": "}"}[ch])
        elif ch in "]}":
            if not stack or stack.pop() != ch:
                raise ValueError(f"unbalanced flow collection: {region}")
        elif ch == "," and not stack:
            entry = region[start:i].strip()
            if not entry:
                raise ValueError(f"empty flow entry: {region}")
            entries.append(entry)
            start = i + 1
    if quote or stack:
        raise ValueError(f"unterminated flow collection: {region}")
    final = region[start:-1].strip()
    return [*entries, final] if final else entries


def _compose_mapping(region: str) -> dict[str, str]:
    text = _strip_hash_comments(region).strip("\n")
    if text.lstrip().startswith("{"):
        entries = _split_flow_entries(text)
        keys = []
        for entry in entries:
            match = re.match(r"([A-Za-z0-9_.-]+)[ \t]*:(?=[ \t]|$)", entry)
            if match is None:
                raise ValueError(f"unrecognized mapping entry: {entry}")
            keys.append(match.group(1))
        indent = None
    else:
        lines = [line for line in split_lines(text) if line.strip()]
        if not lines:
            raise ValueError("expected a mapping, found an empty value")
        indent = min(len(line) - len(line.lstrip()) for line in lines)
        keys = []
        sequence_allowed = False
        for line in lines:
            if len(line) - len(line.lstrip()) != indent:
                if not sequence_allowed:
                    owner = f"{keys[-1]} inline value" if keys else "mapping start"
                    raise ValueError(f"unsupported continuation after {owner}")
                continue
            if line.lstrip().startswith("-") and sequence_allowed:
                continue
            match = re.fullmatch(r"[ ]*([A-Za-z0-9_.-]+)[ \t]*:(.*)", line)
            if match is None or (match.group(2) and not match.group(2)[0].isspace()):
                raise ValueError(f"unrecognized mapping entry: {line.strip()}")
            keys.append(match.group(1))
            value = match.group(2).strip()
            sequence_allowed = not value or re.fullmatch(r"&[A-Za-z0-9_.-]+", value) is not None
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate mapping keys")
    result = {}
    for key in keys:
        values = _yaml_key_regions(text, key, indent=indent)
        if len(values) != 1:
            raise ValueError(f"cannot locate {key} value exactly")
        result[key] = values[0]
    return result


def _compose_scalar(value: str) -> str:
    value = value.strip()
    if not value or "\n" in value or value[0] in "[{*&!>|":
        raise ValueError(f"unrecognized scalar: {value!r}")
    if value[0] in "\"'":
        if re.fullmatch(r""""[^"\\]*"|'[^']*'""", value) is None:
            raise ValueError(f"unsupported quoted scalar: {value}")
        unquoted = _unquote(value)
        if not unquoted:
            raise ValueError("empty scalar")
        return unquoted
    if value.lower() in {"null", "~", "true", "false"} or re.search(r":[ \t]|:$", value):
        raise ValueError(f"unrecognized scalar: {value}")
    if any(ch in value for ch in "[]{}") and not _COMPOSE_VAR_RE.search(value):
        raise ValueError(f"unrecognized scalar: {value}")
    return value


def _compose_sequence(region: str) -> list[str]:
    if region.lstrip().startswith("["):
        return _split_flow_entries(region)
    if not region.lstrip().startswith("- ") and region.strip() != "-":
        raise ValueError(f"unrecognized sequence: {region.strip()!r}")
    return _split_block_entries(region)


def _split_block_entries(region: str) -> list[str]:
    """One string per `- …` item; a long-syntax item keeps its continuation lines."""
    entries: list[str] = []
    for line in region.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "-" or stripped.startswith("- "):
            entries.append(stripped[1:].strip())
        elif entries:
            entries[-1] = f"{entries[-1]}\n{stripped}"
        else:  # no list item at all (an alias, say) — report it verbatim
            entries.append(stripped)
    if any(not entry for entry in entries):
        raise ValueError("empty sequence item")
    return entries


def _classify_long_port_entry(entry: str) -> tuple[str | None, bool]:
    try:
        fields = _compose_mapping(entry)
        if not set(fields) <= {
            "target",
            "published",
            "protocol",
            "mode",
            "host_ip",
            "name",
            "app_protocol",
        }:
            return None, False
        target = _compose_scalar(fields.get("target", ""))
        if not (_COMPOSE_PORT_NUMBER_RE.fullmatch(target) or _COMPOSE_VAR_RE.search(target)):
            return None, False
        if "published" not in fields:
            return None, True
        value = _compose_scalar(fields["published"])
    except ValueError:
        return None, False
    if _COMPOSE_VAR_RE.search(value):
        return None, True
    return (value, True) if _COMPOSE_PORT_NUMBER_RE.fullmatch(value) else (None, False)


def _classify_port_entry(entry: str) -> tuple[str | None, bool]:
    """(pinned host port or None, whether the entry was understood at all).

    Only the *host* slot decides. Testing the whole entry for `$` passed
    `"5432:${CONTAINER_PORT}"` as templated while the host side stayed pinned, so
    variables are masked to a colon-free sentinel first and each slot judged alone."""
    if _COMPOSE_LONG_SYNTAX_RE.search(entry):
        return _classify_long_port_entry(entry)
    try:
        scalar = _compose_scalar(entry)
    except ValueError:
        return None, False
    spec = _COMPOSE_VAR_RE.sub(_VAR_MASK, scalar.split("/", 1)[0])
    # Remove a bracketed IPv6 host before splitting host/container port slots.
    if spec.startswith("["):
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


def _mount_source(entry: str) -> str | None:
    if entry.lstrip().startswith("{") or re.match(r"(?:type|source|target):", entry):
        fields = _compose_mapping(entry)
        kind = _compose_scalar(fields.get("type", ""))
        if kind not in {"bind", "volume", "tmpfs", "image", "npipe", "cluster"}:
            raise ValueError(f"unrecognized volume type: {kind}")
        if "target" not in fields:
            raise ValueError("volume is missing target")
        _compose_scalar(fields["target"])
        if kind != "bind":
            return None
        return _compose_scalar(fields.get("source", ""))
    spec = _compose_scalar(entry)
    masked = _COMPOSE_VAR_RE.sub(_VAR_MASK, spec)
    parts = masked.split(":")
    if len(parts) > _COMPOSE_MAX_VOLUME_FIELDS or any(not part for part in parts):
        raise ValueError(f"unrecognized volume entry: {entry}")
    if len(parts) == 1:
        if not parts[0].startswith("/"):
            raise ValueError(f"unrecognized anonymous volume target: {entry}")
        return None
    source = spec[: spec.find(":")] if _VAR_MASK not in parts[0] else None
    if source is not None and source.startswith("~"):
        raise ValueError(f"home-relative bind source needs manual review: {source}")
    return source


def _compose_service_findings(service: str, body: str) -> tuple[list[str], list[str]]:
    fields = _compose_mapping(body)
    findings: list[str] = []
    incomplete: list[str] = []
    if "extends" in fields:
        incomplete.append(
            f"services.{service}: inherited service configuration needs manual review"
        )
    for key in ("ports", "container_name", "volumes"):
        if key not in fields:
            continue
        slot = f"services.{service}.{key}"
        try:
            if key == "container_name":
                name = _compose_scalar(fields[key])
                if "$" not in name:
                    findings.append(
                        f"{slot} = {name!r}: fixed container name can collide across checkouts"
                    )
                continue
            for index, entry in enumerate(_compose_sequence(fields[key])):
                entry_slot = f"{slot}[{index}]"
                if key == "ports":
                    host, understood = _classify_port_entry(entry)
                    if not understood:
                        raise ValueError(f"unrecognized port entry: {entry}")
                    if host:
                        findings.append(
                            f"{entry_slot}: fixed host port {host} can collide across checkouts"
                        )
                else:
                    source = _mount_source(entry)
                    if source and "$" not in source and source.startswith("/"):
                        findings.append(
                            f"{entry_slot}.source = {source!r}: absolute bind source can share data across checkouts even with distinct ports and COMPOSE_PROJECT_NAME values"
                        )
        except ValueError as error:
            incomplete.append(f"{slot}: {error}")
    return findings, incomplete


def _compose_hardcoded_detect(cwd: Path) -> tuple[str, str]:
    cfg = _compose_file_path(cwd)
    if cfg is None:
        raise DeviceError("compose file not found")
    try:
        text = read_editable_text(cfg, root=cwd)
        if "\t" in text:
            raise ValueError("tabbed YAML is unsupported")
        document = _compose_mapping(text)
        if "include" in document:
            raise ValueError("included Compose files need manual inspection")
        if "services" not in document:
            raise ValueError("services mapping not found")
        services = _compose_mapping(document["services"])
    except (OSError, ValueError) as error:
        return "incomplete", f"{cfg.name}: Compose inspection incomplete: {error}"
    findings: list[str] = []
    incomplete: list[str] = []
    for service, body in services.items():
        try:
            observed, errors = _compose_service_findings(service, body)
            findings.extend(observed)
            incomplete.extend(errors)
        except ValueError as error:
            incomplete.append(f"services.{service}: {error}")
    details = [f"{cfg.name}: {detail}" for detail in [*findings, *incomplete]]
    if incomplete:
        return "incomplete", "; ".join(details)
    if findings:
        return "warning", "; ".join(details)
    return (
        "ok",
        f"{cfg.name}: inspected service port/name/bind slots have no recognized fixed sharing risks; interpolated values and runtime configuration are unverified",
    )


def _compose_hardcoded_manual(cwd: Path) -> str:
    cfg = _compose_file_path(cwd)
    name = cfg.name if cfg else "compose.yaml"
    guidance = [
        f"Review the reported service/value slots in {name}; intentional sharing is valid.",
        "For isolated bind data, use a checkout-relative source or an ordinary project-scoped named volume.",
        "For isolated host ports, declare the intended port resource in splashdown.toml and reference its actual key in the published/host slot. No service-to-resource association was inferred.",
        "Drop fixed container_name when project-scoped Compose names are intended.",
    ]
    raw = read_optional_editable_text(cwd / RECIPE_NAME, root=cwd)
    if raw is None:
        guidance.append("No recipe was available to verify declared Compose resources.")
    else:
        recipe = Recipe.parse(raw, cwd / RECIPE_NAME)
        ports = sorted(name for name, spec in recipe.resources.items() if spec["type"] == "port")
        guidance.append(
            f"Declared port resources: {', '.join(ports) if ports else 'none'}; stored allocations were not inspected."
        )
        guidance.append(
            "COMPOSE_PROJECT_NAME is declared; its stored value was not inspected."
            if "COMPOSE_PROJECT_NAME" in recipe.resources
            else "COMPOSE_PROJECT_NAME is not declared; declare and provision it if project names should differ per checkout."
        )
    guidance.append(
        "Pass the intended values to Compose explicitly or through your chosen loader; this check does not verify the invoking shell environment."
    )
    return "\n".join(guidance)
