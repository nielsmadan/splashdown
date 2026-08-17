from __future__ import annotations

import subprocess
from typing import Any, cast

from .errors import DeviceError

DISCOVERY_TIMEOUT = 30
MUTATION_TIMEOUT = 120


def run_finite(
    argv: list[str],
    *,
    operation: str,
    timeout: int,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    check = bool(kwargs.pop("check", False))
    try:
        return subprocess.run(argv, timeout=timeout, check=check, **kwargs)
    except subprocess.TimeoutExpired as error:
        raise DeviceError(f"{operation} timed out after {timeout}s") from error


def check_output_finite(
    argv: list[str],
    *,
    operation: str,
    timeout: int,
    **kwargs: Any,
) -> bytes:
    try:
        return cast(bytes, subprocess.check_output(argv, timeout=timeout, **kwargs))
    except subprocess.TimeoutExpired as error:
        raise DeviceError(f"{operation} timed out after {timeout}s") from error


def call_finite(
    argv: list[str],
    *,
    operation: str,
    timeout: int,
    **kwargs: Any,
) -> int:
    return int(run_finite(argv, operation=operation, timeout=timeout, **kwargs).returncode)
