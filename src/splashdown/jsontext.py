"""Byte-preserving edits to project-owned JSON documents. A whole-document
`json.dumps` reflows every nested literal the project hand-formatted and changes
its indentation, so splashdown splices only the bytes of the member it owns and
leaves the rest of the file exactly as written. Rendering a whole document is for
one splashdown creates; a caller that cannot locate the member's bytes says so in
its own terms rather than reflowing behind the user's back.

It imports only the `constants.py` seam, so the hook-config editors, the
framework-wiring autofixes, and the loader wiring share one JSON editor without
importing each other."""

from __future__ import annotations

import json
import re
from typing import Any

from .constants import newline_for

_CLOSING = {"{": "}", "[": "]"}


def _json_indent(text: str) -> str | int:
    match = re.search(r"\n([ \t]+)\S", text)
    if match is None:
        return 2
    return "\t" if match.group(1).startswith("\t") else len(match.group(1))


def _line_indent(text: str, index: int) -> str:
    line = text[text.rfind("\n", 0, index) + 1 : index]
    return line[: len(line) - len(line.lstrip())]


def _matching_bracket(text: str, start: int) -> int | None:
    """The offset of the `}` or `]` closing the collection opened at `start`."""
    closing = _CLOSING[text[start]]
    depth, quote, index = 0, False, start
    while index < len(text):
        character = text[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == '"':
                quote = False
        elif character == '"':
            quote = True
        elif character == text[start]:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _value_end(text: str, start: int) -> int | None:
    """One past the last byte of the JSON value beginning at `start`."""
    character = text[start] if start < len(text) else ""
    if character in _CLOSING:
        close = _matching_bracket(text, start)
        return None if close is None else close + 1
    if character == '"':
        index = start + 1
        while index < len(text):
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == '"':
                return index + 1
            index += 1
        return None
    index = start
    while index < len(text) and text[index] not in ",}] \t\r\n":
        index += 1
    return index if index > start else None


def _object_span(text: str, value: Any, key: str | None) -> tuple[int, int] | None:
    """Offsets of the `{` and `}` of the object holding `value`: the document's own
    outermost object when `key` is None, otherwise the one that member names."""
    if key is None:
        starts = [text.find("{")]
    else:
        starts = [m.end() for m in re.finditer(rf'"{re.escape(key)}"[ \t]*:\s*', text)]
    for start in starts:
        if start < 0 or start >= len(text) or text[start] != "{":
            continue
        close = _matching_bracket(text, start)
        if close is None:
            continue
        try:
            if json.loads(text[start : close + 1]) == value:
                return start, close
        except json.JSONDecodeError:
            continue
    return None


def _member_span(text: str, span: tuple[int, int], name: str) -> tuple[int, int, int] | None:
    """`(name start, value start, value end)` for the direct member `name` of the
    object at `span`, or `None` when the object does not spell it out at its own
    depth. Nested objects are skipped, so an inner member of the same name loses."""
    open_index, close_index = span
    pattern = re.compile(rf'"{re.escape(name)}"[ \t]*:[ \t\r\n]*')
    index = open_index + 1
    while index < close_index:
        character = text[index]
        if character in _CLOSING:
            close = _matching_bracket(text, index)
            if close is None:
                return None
            index = close + 1
            continue
        if character == '"':
            match = pattern.match(text, index)
            if match is None:
                end = _value_end(text, index)
                if end is None:
                    return None
                index = end
                continue
            value_end = _value_end(text, match.end())
            return None if value_end is None else (index, match.end(), value_end)
        index += 1
    return None


def _render_json_value(value: Any, text: str) -> str:
    """`value` as JSON at `text`'s own indent step, newline-separated. The splice
    re-indents it to the column it lands on."""
    step = _json_indent(text)
    pad = "\t" if step == "\t" else " " * int(step)
    return json.dumps(value, indent=pad, ensure_ascii=False)


def _at_column(rendered: str, newline: str, column: str) -> str:
    return rendered.replace("\n", newline + column)


def _member_text(name: str, rendered: str) -> str:
    return f"{json.dumps(name, ensure_ascii=False)}: {rendered}"


def _insert_json_member(text: str, span: tuple[int, int], name: str, rendered: str) -> str:
    """Splice one member into an existing JSON object, touching nothing else."""
    open_index, close_index = span
    body = text[open_index + 1 : close_index]
    newline = newline_for(text)
    if not body.strip():
        step = _json_indent(text)
        pad = "\t" if step == "\t" else " " * int(step)
        base = _line_indent(text, open_index)
        member = _member_text(name, _at_column(rendered, newline, base + pad))
        head, tail = text[: open_index + 1], text[close_index:]
        return f"{head}{newline}{base}{pad}{member}{newline}{base}{tail}"
    cut = close_index - (len(body) - len(body.rstrip()))
    if "\n" not in body:
        return f"{text[:cut]}, {_member_text(name, _at_column(rendered, newline, ''))}{text[cut:]}"
    indent = _line_indent(text, cut)
    member = _member_text(name, _at_column(rendered, newline, indent))
    return f"{text[:cut]},{newline}{indent}{member}{text[cut:]}"


def _set_json_member(text: str, span: tuple[int, int], name: str, rendered: str) -> str:
    """`text` with `name` carrying `rendered`, replacing the value bytes in place
    when the object already spells the member out and appending it when not."""
    member = _member_span(text, span, name)
    if member is None:
        return _insert_json_member(text, span, name, rendered)
    _, value_start, value_end = member
    placed = _at_column(rendered, newline_for(text), _line_indent(text, value_start))
    return text[:value_start] + placed + text[value_end:]


def _remove_json_member(text: str, span: tuple[int, int], name: str) -> str | None:
    """`text` without the member `name`, or `None` when the object does not have it.
    The separating comma and the member's own line go with it."""
    member = _member_span(text, span, name)
    if member is None:
        return None
    open_index, close_index = span
    start, _, end = member
    after = end
    while after < close_index and text[after] in " \t\r\n":
        after += 1
    if after < close_index and text[after] == ",":
        end = after + 1
    else:
        before = start
        while before > open_index + 1 and text[before - 1] in " \t\r\n":
            before -= 1
        if before > open_index + 1 and text[before - 1] == ",":
            start = before - 1
        else:
            return text[: open_index + 1] + text[close_index:]
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip():
        while end < len(text) and text[end] in " \t":
            end += 1
        return text[:start] + text[end:]
    start = line_start
    while end < len(text) and text[end] in " \t":
        end += 1
    if text.startswith("\r\n", end):
        end += 2
    elif text[end : end + 1] in {"\n", "\r"}:
        end += 1
    return text[:start] + text[end:]


def _new_json_document(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
