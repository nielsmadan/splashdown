from __future__ import annotations

import os
import select
import sys
import termios
import time
import tty
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


def choose(title: str, options: Sequence[str]) -> int | None:
    if not options:
        raise ValueError("a picker needs at least one option")
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    selected = 0
    redraw = False
    try:
        tty.setcbreak(fd)
        while True:
            if redraw:
                print(f"\x1b[{len(options) + 2}A\x1b[J", end="", file=sys.stderr)
            print(title, file=sys.stderr)
            for index, label in enumerate(options):
                marker = ">" if index == selected else " "
                shortcut = SHORTCUTS[index] if index < len(SHORTCUTS) else " "
                print(f"{marker} [{shortcut}] {terminal_text(label)}", file=sys.stderr)
            print("Letters select · ↑/↓ then Enter · Esc cancels", file=sys.stderr, flush=True)
            key = _read_key(fd).lower()
            if key == "cancel":
                return None
            if key == "enter":
                return selected
            if key and key in SHORTCUTS[: len(options)]:
                return SHORTCUTS.index(key)
            if key in {"up", "down"}:
                selected = (selected + (1 if key == "down" else -1)) % len(options)
            redraw = True
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)


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
