from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class PortOwner:
    pid: int
    command: str


def listening_processes() -> dict[int, tuple[PortOwner, ...]] | None:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or result.stderr.strip():
        return None
    owners: dict[int, set[PortOwner]] = {}
    pid: int | None = None
    command = ""
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
            command = ""
        elif line.startswith("c"):
            command = line[1:]
        elif line.startswith("n") and pid is not None and command:
            match = re.search(r":(\d+)$", line)
            if match:
                port = int(match[1])
                owners.setdefault(port, set()).add(PortOwner(pid, command))
    return {port: tuple(sorted(entries)) for port, entries in owners.items()}
