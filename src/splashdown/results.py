from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]
OutputFormat = Literal["text", "json"]
ResultStatus = Literal["success", "error", "partial"]


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str


@dataclass(frozen=True)
class CommandResult:
    command: str | None
    status: ResultStatus
    exit_code: int
    data: JsonObject | None = None
    error: Diagnostic | None = None
    warnings: tuple[Diagnostic, ...] = ()
    next_steps: tuple[str, ...] = ()


def _validate_json(value: object) -> None:
    pending = [(value, False)]
    active: set[int] = set()
    while pending:
        current, leaving = pending.pop()
        if leaving:
            active.remove(id(current))
            continue
        if current is None or type(current) in (str, bool, int):
            continue
        if type(current) is float and math.isfinite(current):
            continue
        children: Iterable[object]
        if isinstance(current, list):
            children = current
        elif isinstance(current, dict) and all(isinstance(key, str) for key in current):
            children = current.values()
        else:
            raise ValueError("result data must contain JSON-native values")
        if id(current) in active:
            raise ValueError("result data must contain JSON-native values")
        active.add(id(current))
        pending.append((current, True))
        pending.extend((item, False) for item in children)


def result_payload(result: CommandResult) -> JsonObject:
    if result.status not in {"success", "error", "partial"}:
        raise ValueError("invalid result status")
    if type(result.exit_code) is not int or result.exit_code < 0:
        raise ValueError("invalid result exit code")
    if result.status == "success":
        if result.exit_code != 0 or result.error is not None:
            raise ValueError("success requires exit 0 and no error")
    elif result.exit_code == 0 or result.error is None:
        raise ValueError("failure requires a nonzero exit and error")
    if result.status == "partial" and result.data is None:
        raise ValueError("partial requires result data")
    if result.data is not None and not isinstance(result.data, dict):
        raise ValueError("result data must be an object")
    payload: JsonObject = {
        "command": result.command,
        "status": result.status,
        "exit_code": result.exit_code,
        "data": result.data,
        "error": {"code": result.error.code, "message": result.error.message}
        if result.error
        else None,
        "warnings": [
            {"code": warning.code, "message": warning.message}
            for warning in dict.fromkeys(result.warnings)
        ],
        "next_steps": list(dict.fromkeys(result.next_steps)),
    }
    _validate_json(payload)
    return payload
