from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from dataclasses import replace as replace_result
from pathlib import Path
from typing import TYPE_CHECKING

from .catalog import PROFILES
from .inventory import AppInventory
from .recipe import Recipe
from .safe_files import atomic_write_text, read_optional_editable_text

if TYPE_CHECKING:
    import flyrail as fr

_GUIDANCE_START = "<!-- >>> splashdown-managed agent-guidance >>> -->"
_GUIDANCE_END = "<!-- <<< splashdown-managed agent-guidance <<< -->"
_AGENT_FILES = ("AGENTS.md", "CLAUDE.md")
_AGENTS_IMPORT_RE = re.compile(r"(?<![A-Za-z0-9_])@(?:\./)?AGENTS\.md(?![A-Za-z0-9_.])")
_GENERATED_HEADER_RE = re.compile(
    r"@generated\b"
    r"|\b(?:auto[-\s]?)?generated\s+(?:by|from|with)\s+\S"
    r"|\bdo\s+not\s+edit\b"
    r"|\bdon'?t\s+edit\b",
    re.IGNORECASE,
)
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n.*?^---[ \t]*(?:\r?\n|\Z)", re.DOTALL | re.MULTILINE)
_MARKER_SUMMARY_LIMIT = 160


def render_agent_guidance(cwd: Path, recipe: Recipe) -> str:
    apps: list[tuple[str, dict[str, object], list[str]]] = []
    for name, spec in recipe.apps.items():
        profile_name = str(spec["profile"])
        profile = PROFILES.get(profile_name)
        if profile is None:
            continue
        port_names = [
            resource_name
            for resource_name in spec["resources"]
            if recipe.resources[resource_name].get("type") == "port"
        ]
        if port_names:
            apps.append((name, spec, port_names))

    if not apps:
        return ""

    env_file = _markdown_code(recipe.env_file)
    lines = [
        _GUIDANCE_START,
        "## Splashdown",
        "",
        f"Splashdown assigns this checkout's ports and writes the resolved values to {env_file}.",
        "Ranges in `splashdown.toml` are allocation pools, not the assigned values.",
        "Never hardcode numeric port values or add numeric port overrides. Read one value",
        "with `splash env get KEY` and list the variable names with `splash env`.",
        f"Run `splash sync` when {env_file} is missing or out of date, and prefer the",
        "project's existing scripts when they already consume the Splashdown environment.",
        "Run any manual commands below from the checkout root.",
    ]
    for name, spec, port_names in apps:
        profile_name = str(spec["profile"])
        path = str(spec["path"])
        app = AppInventory(
            name=name,
            path=(cwd / path).resolve(),
            profile=profile_name,
            project_path=Path(path),
        )
        ports = ", ".join(f"`{port}`" for port in port_names)
        lines.extend(
            [
                "",
                f"### App {_markdown_code(name)} ({_markdown_code(path)})",
                "",
                (
                    f"Framework: `{profile_name}`. Allocated port variable"
                    f"{'s' if len(port_names) != 1 else ''}: {ports}."
                ),
            ]
        )
        specific = PROFILES[profile_name].agent_guidance(app, port_names)
        if specific:
            lines.extend(["", *specific])
    lines.extend(["", _GUIDANCE_END])
    return "\n".join(lines)


@dataclass(frozen=True)
class GuidanceResult:
    file: str
    status: str
    content_current: bool = False
    detail: str = ""
    desired_current: bool | None = None

    @property
    def successful(self) -> bool:
        return self.status not in {"failed", "partial", "incomplete"}


_BUNDLE_ID = "splashdown"
_STATE_DIRECTORY = ".splashdown-ai"
_METADATA_IGNORES = ("/.splashdown-ai/", "/.flyrail-*.state/")


def _target(cwd: Path, name: str) -> fr.InstallationTarget:
    import flyrail as fr  # noqa: PLC0415

    return fr.InstallationTarget(cwd / _STATE_DIRECTORY / name.removesuffix(".md").lower())


def _rendered(path: Path, block: str) -> tuple[fr.Bundle, fr.RenderedBundle]:
    import flyrail as fr  # noqa: PLC0415

    body = block[len(_GUIDANCE_START) + 1 : -len(_GUIDANCE_END)]
    bundle = fr.Bundle.from_artifacts(
        fr.BundleIdentity(_BUNDLE_ID, "guidance-1"),
        [fr.InstructionArtifact("guidance", body)],
    )
    rendered = fr.RenderedBundle(
        [
            fr.RenderedArtifact(
                "guidance",
                fr.Family.INSTRUCTIONS,
                path,
                fr.SectionContent(
                    "guidance", body, fr.SectionBoundaries(_GUIDANCE_START, _GUIDANCE_END)
                ),
                require_existing=True,
            ),
        ]
    )
    return bundle, rendered


def _ensure_metadata_ignored(cwd: Path) -> None:
    path = cwd / ".gitignore"
    text = read_optional_editable_text(path, root=cwd) or ""
    additions = [line for line in _METADATA_IGNORES if line not in text.splitlines()]
    if additions:
        separator = "" if not text or text.endswith("\n") else "\n"
        atomic_write_text(
            path, text + separator + "\n".join(additions) + "\n", root=cwd, create=True
        )


def _imports_agents(text: str) -> bool:
    visible: list[str] = []
    fence = ""
    for raw_line in text.splitlines():
        line = re.sub(r"^(?: {0,3}> ?)+", "", raw_line.expandtabs(4))
        boundary = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if boundary:
            marker, suffix = boundary.groups()
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not suffix.strip():
                fence = ""
            continue
        if not fence and not line.startswith("    "):
            visible.append(line)
    prose = "\n".join(visible)
    parts: list[str] = []
    while match := re.search(r"`+", prose):
        parts.append(prose[: match.start()])
        end = re.search(r"(?<!`)" + re.escape(match.group()) + r"(?!`)", prose[match.end() :])
        if end is None:
            prose = ""
            break
        parts.append(" ")
        prose = prose[match.end() + end.end() :]
    return bool(_AGENTS_IMPORT_RE.search("".join([*parts, prose])))


def _has_markers(text: str) -> bool:
    return _GUIDANCE_START in text or _GUIDANCE_END in text


def _generated_marker(text: str) -> str | None:
    remainder = _skip_frontmatter(text.lstrip("\ufeff").lstrip())
    while True:
        remainder = remainder.lstrip()
        if not remainder.startswith("<!--"):
            return None
        end = remainder.find("-->")
        if end < 0:
            return None
        if _GENERATED_HEADER_RE.search(remainder[len("<!--") : end]):
            marker = remainder[: end + len("-->")]
            collapsed = "".join(char for char in " ".join(marker.split()) if char.isprintable())
            if len(collapsed) <= _MARKER_SUMMARY_LIMIT:
                return collapsed
            return f"{collapsed[: _MARKER_SUMMARY_LIMIT - 3]}..."
        remainder = remainder[end + len("-->") :]


def _skip_frontmatter(text: str) -> str:
    match = _FRONTMATTER_RE.match(text)
    return text[match.end() :] if match else text


def _generated_action(text: str, block: str, *, remove: bool) -> str | None:
    malformed = (
        text.count(_GUIDANCE_START) != text.count(_GUIDANCE_END)
        or text.count(_GUIDANCE_START) > 1
        or (
            _GUIDANCE_START in text
            and _GUIDANCE_END in text
            and text.index(_GUIDANCE_START) >= text.index(_GUIDANCE_END)
        )
    )
    if remove or not block:
        if not _has_markers(text):
            return None
        return "remove-malformed" if malformed else "remove"
    if not _has_markers(text):
        return "add"
    error = _legacy_error(text, block)
    if not error:
        return None
    return "repair" if error.startswith("malformed") else "replace"


def _report_generated_file(name: str, marker: str, block: str, action: str) -> None:
    condition = (
        "its Splashdown guidance markers are malformed and it is generated by another tool"
        if action in {"repair", "remove-malformed"}
        else "it is generated by another tool"
    )
    print(f"warning: left {name} alone: {condition}", file=sys.stderr)
    print(f"  marker: {marker}", file=sys.stderr)
    verb = {
        "add": "add the Splashdown guidance block to",
        "replace": "replace the Splashdown guidance block in",
        "remove": "remove the Splashdown guidance block from",
        "remove-malformed": "remove the Splashdown guidance block from",
        "repair": "repair the Splashdown guidance markers in",
    }[action]
    print(f"  {verb} the source that generates {name}, then re-run that generator", file=sys.stderr)
    if block and action != "remove":
        print("  ----- begin Splashdown guidance block -----", file=sys.stderr)
        print(block, file=sys.stderr)
        print("  ----- end Splashdown guidance block -----", file=sys.stderr)


def _legacy_error(text: str, block: str) -> str:
    if not _has_markers(text):
        return ""
    newline = _newline_for(text)
    if (
        text.count(_GUIDANCE_START) != 1
        or text.count(_GUIDANCE_END) != 1
        or _GUIDANCE_START not in text.removeprefix("\ufeff").splitlines()
        or _GUIDANCE_END not in text.splitlines()
        or text.index(_GUIDANCE_START) >= text.index(_GUIDANCE_END)
    ):
        return "malformed Splashdown guidance markers; repair manually"
    expected = block.replace("\n", newline) + newline if block else ""
    if not expected or expected not in text:
        return "unverified legacy guidance; current generated block does not match"
    return ""


def _observation_result(
    name: str, observation: fr.InstallationObservation, *, allow_pending: bool = False
) -> GuidanceResult:
    errors = [observation.error, *(resource.error for resource in observation.resources)]
    detail = next((f"{error.code}: {error.message}" for error in errors if error), "")
    if (observation.pending and not allow_pending) or any(
        resource.recovery_paths for resource in observation.resources
    ):
        return GuidanceResult(name, "incomplete", detail=detail or "recovery required")
    if detail:
        return GuidanceResult(name, "failed", detail=detail)
    if observation.version is not None:
        return GuidanceResult(
            name, "current" if observation.is_current else "modified", observation.is_current
        )
    return GuidanceResult(name, "unmanaged")


def _apply(
    name: str, proposal: fr.LifecyclePreview, expected_text: str | None = None
) -> GuidanceResult:
    import flyrail as fr  # noqa: PLC0415

    for resource in proposal.resources:
        if expected_text is not None and resource.before.data != expected_text.encode("utf-8"):
            return GuidanceResult(
                name, "failed", detail="instruction file changed after import selection; retry"
            )
    return _mutation_result(name, fr.apply_preview(proposal))


def _mutation_result(name: str, result: fr.InstallationResult) -> GuidanceResult:
    observation = _observation_result(name, result.observation)
    detail = observation.detail
    if result.error:
        detail = f"{result.error.code}: {result.error.message}"
    return GuidanceResult(name, str(result.status), result.observation.is_current, detail)


def _recover_file(cwd: Path, name: str) -> GuidanceResult | None:
    import flyrail as fr  # noqa: PLC0415

    try:
        result = _mutation_result(name, fr.recover_installation(_BUNDLE_ID, _target(cwd, name)))
        return None if result.successful else result
    except (OSError, ValueError) as error:
        return GuidanceResult(name, "failed", detail=f"symlink or unreadable/unsafe file; {error}")


def _update_file(cwd: Path, name: str, block: str, replace: bool) -> GuidanceResult:
    import flyrail as fr  # noqa: PLC0415

    path = cwd / name
    target = _target(cwd, name)
    bundle, rendered = _rendered(path, block)
    proposal = fr.preview(
        bundle,
        rendered,
        target,
        acquisition=fr.Acquisition.TAKEOVER if replace else fr.Acquisition.ADOPT,
        replace_modified=replace,
    )
    if not replace and proposal.index is None:
        for resource in proposal.resources:
            text = (resource.before.data or b"").decode("utf-8")
            if (
                _has_markers(text)
                and resource.resource.state_root.exists()
                and (
                    resource.previous is None
                    or any(claim.bundle_id == _BUNDLE_ID for claim in resource.previous.claims)
                )
            ):
                return GuidanceResult(
                    name,
                    "failed",
                    detail="guidance metadata exists without its index; restore metadata before retrying",
                )
            error = _legacy_error(text, block)
            if error:
                return GuidanceResult(name, "failed", detail=error)
    return _apply(name, proposal)


def _remove_file(
    cwd: Path, name: str, block: str, expected_text: str | None = None
) -> GuidanceResult:
    import flyrail as fr  # noqa: PLC0415

    target = _target(cwd, name)
    observation = fr.inspect_installation(_BUNDLE_ID, target)
    if (
        observation.version is None
        and not observation.resources
        and not observation.error
        and not observation.pending
    ):
        text = read_optional_editable_text(cwd / name, root=cwd)
        if text is None:
            return GuidanceResult(name, "absent")
        if not _has_markers(text):
            return GuidanceResult(name, "unmanaged")
        error = _legacy_error(text, block)
        if error:
            return GuidanceResult(name, "failed", detail=error)
        adopted = _update_file(cwd, name, block, False)
        if not adopted.successful:
            return adopted
    return _apply(name, fr.preview_removal(_BUNDLE_ID, target), expected_text)


def _file_operation(
    cwd: Path,
    name: str,
    block: str,
    *,
    remove: bool,
    replace: bool = False,
    expected_text: str | None = None,
) -> GuidanceResult:
    import flyrail as fr  # noqa: PLC0415

    try:
        observation = fr.inspect_installation(_BUNDLE_ID, _target(cwd, name))
        text = read_optional_editable_text(cwd / name, root=cwd)
        if (
            text is None
            and observation.version is None
            and not observation.resources
            and not observation.error
            and not observation.pending
        ):
            return GuidanceResult(name, "absent")
        observed = _observation_result(name, observation, allow_pending=True)
        if not observed.successful:
            return observed
        marker = _generated_marker(text or "")
        action = _generated_action(text or "", block, remove=remove) if marker else None
        if marker and action is None:
            return GuidanceResult(name, "generated", content_current=bool(block))
        if marker and action:
            _report_generated_file(name, marker, block, action)
            return GuidanceResult(name, "generated")
        _ensure_metadata_ignored(cwd)
        return (
            _remove_file(cwd, name, block, expected_text)
            if remove
            else _update_file(cwd, name, block, replace)
        )
    except (OSError, ValueError) as error:
        detail = (
            "malformed Splashdown guidance markers; repair manually"
            if "boundary" in str(error)
            else f"symlink or unreadable/unsafe file; {error}"
        )
        return GuidanceResult(name, "failed", detail=detail)


def _announce(results: tuple[GuidanceResult, ...]) -> None:
    for result in results:
        if not result.successful:
            print(
                f"warning: {result.file}: {result.detail or result.status}; left unchanged or recovery required",
                file=sys.stderr,
            )
        elif result.status == "applied":
            print(f"updated guidance in {result.file}", file=sys.stderr)


def sync_agent_guidance(
    cwd: Path, recipe: Recipe, *, replace: bool = False, announce: bool = True
) -> tuple[GuidanceResult, ...]:
    block = render_agent_guidance(cwd, recipe)
    agents = _recover_file(cwd, "AGENTS.md") or _file_operation(
        cwd, "AGENTS.md", block, remove=not block, replace=replace
    )
    try:
        claude = _recover_file(cwd, "CLAUDE.md")
        if claude is None:
            claude_text = read_optional_editable_text(cwd / "CLAUDE.md", root=cwd) or ""
            imported = (
                bool(block)
                and agents.successful
                and agents.content_current
                and _imports_agents(claude_text)
            )
            claude = _file_operation(
                cwd,
                "CLAUDE.md",
                block,
                remove=not block or imported,
                replace=replace,
                expected_text=claude_text if imported else None,
            )
    except (OSError, ValueError) as error:
        claude = GuidanceResult("CLAUDE.md", "failed", detail=f"symlink or unreadable; {error}")
    results = (agents, claude)
    if announce:
        _announce(results)
    return results


def remove_agent_guidance(
    cwd: Path, recipe: Recipe | None = None, *, announce: bool = True
) -> tuple[GuidanceResult, ...]:
    block = render_agent_guidance(cwd, recipe) if recipe is not None else ""
    results = tuple(
        _recover_file(cwd, name) or _file_operation(cwd, name, block, remove=True)
        for name in _AGENT_FILES
    )
    if announce:
        _announce(results)
    return results


def _status_source(cwd: Path) -> str | None:
    try:
        return render_agent_guidance(cwd, Recipe.load(cwd / "splashdown.toml"))
    except (OSError, ValueError):
        return None


def _compare_desired(
    cwd: Path, result: GuidanceResult, observation: fr.InstallationObservation, block: str | None
) -> GuidanceResult:
    if block is None:
        status = "recorded-current" if result.status == "current" else result.status
        return replace_result(result, status=status)
    if block and result.status != "absent":
        bundle, rendered = _rendered(cwd / result.file, block)
        matches = observation.matches(bundle, rendered)
    else:
        matches = observation.version is None
    status = "outdated" if result.status == "current" and not matches else result.status
    return replace_result(result, status=status, desired_current=matches)


def inspect_agent_guidance(cwd: Path) -> tuple[GuidanceResult, ...]:
    import flyrail as fr  # noqa: PLC0415

    block = _status_source(cwd)
    results: list[GuidanceResult] = []
    for name in _AGENT_FILES:
        try:
            observation = fr.inspect_installation(_BUNDLE_ID, _target(cwd, name))
            result = _observation_result(name, observation)
            text = read_optional_editable_text(cwd / name, root=cwd)
            if result.status == "unmanaged":
                result = GuidanceResult(
                    name,
                    "absent" if text is None else "unmanaged",
                    detail="unverified legacy guidance" if text and _has_markers(text) else "",
                )
            desired = block
            if (
                name == "CLAUDE.md"
                and block
                and results[0].content_current
                and results[0].desired_current
                and _imports_agents(text or "")
            ):
                desired = ""
            results.append(_compare_desired(cwd, result, observation, desired))
        except (OSError, ValueError) as error:
            results.append(GuidanceResult(name, "failed", detail=f"symlink or unreadable; {error}"))
    return tuple(results)


def _markdown_code(value: str) -> str:
    escaped = (
        value.replace("<", "&lt;").replace(">", "&gt;").replace("\r", r"\r").replace("\n", r"\n")
    )
    longest = max((len(match.group()) for match in re.finditer(r"`+", escaped)), default=0)
    fence = "`" * max(1, longest + 1)
    padding = " " if escaped.startswith("`") or escaped.endswith("`") else ""
    return f"{fence}{padding}{escaped}{padding}{fence}"


def _newline_for(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text and "\n" not in text:
        return "\r"
    return "\n"
