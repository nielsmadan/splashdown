from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

from .errors import CapabilityError


def require_macos(operation: str) -> None:
    if sys.platform != "darwin":
        raise CapabilityError("ios", f"iOS {operation} requires macOS and Xcode")


@contextmanager
def translate_tool_errors(capability: str, tool: str, hint: str) -> Iterator[None]:
    try:
        yield
    except OSError as error:
        detail = error.strerror or str(error)
        raise CapabilityError(capability, f"{tool} is unavailable ({detail}); {hint}") from error


def warn_capability(error: CapabilityError, warned: set[str]) -> None:
    if error.capability in warned:
        return
    warned.add(error.capability)
    label = {"ios": "iOS", "android": "Android"}.get(error.capability, error.capability)
    print(f"warning: skipping {label}: {error}", file=sys.stderr)
