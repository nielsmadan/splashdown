from __future__ import annotations

import sys

from .errors import ApplicationError
from .results import OutputFormat


def require_confirmation(
    prompt: str, *, yes: bool, fmt: OutputFormat = "text", next_step: str
) -> None:
    if yes:
        return
    if fmt == "json" or not sys.stdin.isatty():
        raise ApplicationError(
            "Confirmation required.", code="confirmation_required", next_steps=(next_step,)
        )
    print(f"{prompt} [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        answer = ""
    if answer not in {"y", "yes"}:
        raise ApplicationError("Confirmation declined.", code="confirmation_declined")
