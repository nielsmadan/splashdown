"""Line-preserving lexical helpers for YAML-shaped text. It imports only the
`constants.py` seam, so the framework-wiring checks and the hook-configuration
editors read the same value regions without importing each other."""

from __future__ import annotations

import re

from .constants import split_lines


def _strip_hash_comment_lines(lines: list[str]) -> list[str]:
    """`lines` with `#` comments dropped, one output line per input line, so a
    caller can map an index in the stripped view back onto the original bytes."""
    out: list[str] = []
    for line in lines:
        kept: list[str] = []
        quote = ""
        for ch in line:
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "\"'":
                quote = ch
            elif ch == "#" and (not kept or kept[-1] in " \t"):
                break
            kept.append(ch)
        out.append("".join(kept).rstrip())
    return out


def _strip_hash_comments(text: str) -> str:
    """Drop `#` comments from YAML / .properties / shell text, honouring quotes.
    Indentation is preserved: YAML block structure is read off it."""
    return "\n".join(_strip_hash_comment_lines(split_lines(text)))


def _yaml_flow_value(text: str, start: int) -> str:
    """The flow scalar or bracketed collection at `start`, ending at the first
    top-level `,`/`}`/`]` or end of line. Quote- and depth-aware so a `,` inside
    `['5432:5432', ...]` or a `:` inside `${PORT:-5432}` doesn't terminate it."""
    depth, quote = 0, ""
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and ch in ",\n":
            break
        i += 1
    return text[start:i]


def _yaml_key_regions(text: str, key: str, *, indent: int | None = None) -> list[str]:
    """The value text of every `<key>:` mapping entry: the flow value on the same
    line, or the indented block beneath it. Anchoring a regex on block layout is
    what silently missed every flow-style spelling, so the key is matched both at
    line start and inside a flow mapping (`db: { ports: [...] }`). Pass `indent` to
    accept only keys at that column — spring's `server:` must not match
    `management:`'s nested one. Comments are stripped here so no caller can forget."""
    text = _strip_hash_comments(text)
    pattern = re.compile(
        rf"(?:^|(?<=[{{,]))([ \t]*)(?:-[ \t]+)?{re.escape(key)}[ \t]*:", re.MULTILINE
    )
    regions: list[str] = []
    for m in pattern.finditer(text):
        at_line_start = m.start() == text.rfind("\n", 0, m.start()) + 1
        if indent is not None and not (at_line_start and len(m.group(1)) == indent):
            continue
        inline = _yaml_flow_value(text, m.end()).strip()
        if inline:
            regions.append(inline)
            continue
        if not at_line_start:
            continue
        key_indent = len(m.group(1))
        block: list[str] = []
        for line in text[m.end() :].split("\n")[1:]:
            stripped = line.lstrip()
            if stripped:
                line_indent = len(line) - len(stripped)
                # A block sequence may sit at its key's own indent, which is both
                # legal and common (`ports:` then `- "5432:5432"` at the same column).
                if line_indent < key_indent:
                    break
                if line_indent == key_indent and not stripped.startswith("-"):
                    break
            block.append(line)
        while block and not block[-1].strip():
            block.pop()
        regions.append("\n".join(block))
    return regions
