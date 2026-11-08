from __future__ import annotations

import os
import select
import sys
import termios
import time
import tty
import unicodedata
from collections.abc import Sequence

from .terminal import terminal_text

SHORTCUTS = "qwertyuiopasdfghjklzxcvbnm"


def _read_escape(fd: int) -> str:
    deadline = time.monotonic() + 0.1
    if not select.select([fd], [], [], 0.1)[0]:
        return "cancel"
    if os.read(fd, 1) not in {b"[", b"O"}:
        return "cancel"
    plain = True
    while (remaining := deadline - time.monotonic()) > 0:
        if not select.select([fd], [], [], remaining)[0]:
            break
        byte = os.read(fd, 1)
        if byte in {b"", b"\x04", b"\x1b"}:
            return "cancel"
        if byte == b"\x03":
            raise KeyboardInterrupt
        if b"@" <= byte <= b"~":
            return {b"A": "up", b"B": "down"}.get(byte, "") if plain else ""
        plain = False
    return ""


def _read_key(fd: int) -> str:
    key = os.read(fd, 1)
    if key == b"\x1b":
        return _read_escape(fd)
    if key in {b"", b"\x04"}:
        return "cancel"
    if key == b"\x03":
        raise KeyboardInterrupt
    if key in {b"\r", b"\n"}:
        return "enter"
    return key.decode("ascii", errors="ignore")


def _terminal_size() -> os.terminal_size:
    try:
        size = os.get_terminal_size(sys.stderr.fileno())
    except OSError:
        return os.terminal_size((80, 24))
    return os.terminal_size((size.columns or 80, size.lines or 24))


def _cell_width(text: str) -> int:
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"W", "F"}
        else 1
        for character in text
    )


def _fit_line(text: str, columns: int) -> str:
    text = terminal_text(text)
    width = max(1, columns - 1)
    if _cell_width(text) <= width:
        return text
    end = 0
    used = 0
    for index, character in enumerate(text):
        used += _cell_width(character)
        if used >= width:
            break
        end = index + 1
    return text[:end] + "…"


def _clear_lines(lines: Sequence[str], size: os.terminal_size) -> None:
    rows = sum(max(1, (_cell_width(line) + size.columns - 1) // size.columns) for line in lines)
    if rows:
        print(f"\x1b[{min(rows, max(1, size.lines - 1))}A\r\x1b[J", end="", file=sys.stderr)


def _menu_lines(
    title: str, options: Sequence[str], selected: int, size: os.terminal_size
) -> list[str]:
    count = max(1, size.lines - 4)
    start = min(max(0, selected - count // 2), max(0, len(options) - count))
    visible = options[start : start + count]
    lines = [title]
    for index, label in enumerate(visible, start=start):
        marker = ">" if index == selected else " "
        shortcut = f"[{SHORTCUTS[index]}]" if index < len(SHORTCUTS) else "   "
        lines.append(f"{marker} {shortcut} {label}")
    if len(visible) < len(options):
        lines.append(f"{start + 1}-{start + len(visible)} of {len(options)} · ↑/↓ to scroll")
    lines.append("Letters select · ↑/↓ then Enter · Esc cancels")
    return [_fit_line(line, size.columns) for line in lines]


def choose(
    title: str, options: Sequence[str], *, summaries: Sequence[str] | None = None
) -> int | None:
    if not options:
        raise ValueError("a picker needs at least one option")
    if summaries is not None and len(summaries) != len(options):
        raise ValueError("each picker option needs a summary")
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    selected = 0
    lines: list[str] = []
    result = None
    try:
        tty.setcbreak(fd)
        while True:
            size = _terminal_size()
            _clear_lines(lines, size)
            lines = _menu_lines(title, options, selected, size)
            print("\n".join(lines), file=sys.stderr, flush=True)
            key = _read_key(fd).lower()
            if key == "cancel":
                break
            if key == "enter":
                result = selected
                break
            if key and key in SHORTCUTS[: len(options)]:
                result = SHORTCUTS.index(key)
                break
            if key in {"up", "down"}:
                selected = (selected + (1 if key == "down" else -1)) % len(options)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)
        _clear_lines(lines, _terminal_size())
        summary = (
            "Cancelled"
            if result is None
            else summaries[result]
            if summaries is not None
            else f"{title}: {options[result]}"
        )
        print(terminal_text(summary), file=sys.stderr, flush=True)
    return result


def read_name(prompt: str) -> str | None:
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    characters: list[str] = []
    try:
        tty.setcbreak(fd)
        print(prompt, end="", file=sys.stderr, flush=True)
        while True:
            key = _read_key(fd)
            if key == "cancel":
                print(file=sys.stderr)
                return None
            if key == "enter":
                print(file=sys.stderr)
                return "".join(characters)
            if key in {"\x7f", "\x08"}:
                if characters:
                    characters.pop()
                    print("\b \b", end="", file=sys.stderr, flush=True)
            elif len(key) == 1 and key.isprintable():
                characters.append(key)
                print(key, end="", file=sys.stderr, flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)
