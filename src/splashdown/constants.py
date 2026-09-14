from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath


def state_directory() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state_home / "splashdown"


REGISTRY_DIR = state_directory()
STATE_HOME = REGISTRY_DIR.parent
PORT_REGISTRY = REGISTRY_DIR / "ports.tsv"
KV_REGISTRY = REGISTRY_DIR / "kv.tsv"
DEVICE_REGISTRY = REGISTRY_DIR / "devices.tsv"
CLAIM_REGISTRY = REGISTRY_DIR / "claims.tsv"
CLAIM_NOTICE_REGISTRY = REGISTRY_DIR / "claim-notices.tsv"
CLAIM_NOTICE_DAYS = 30

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TARGET_VARIANT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
TARGET_TYPES = ("simulator", "emulator", "device")
RECIPE_NAME = "splashdown.toml"
LOCAL_NAME = "splashdown.local.toml"
GLOBAL_CONFIG_NAME = "config.toml"
ENV_FILE_NAME = "splashdown.env"


def normalized_env_reference(value: str) -> str:
    """One canonical spelling for an env destination, so a persisted `env_file`,
    an `envfile=` writer, and a loader directive that name the same file compare
    equal: `./.env`, `a/./.env` and `.env ` collapse to `.env` and `a/.env`."""
    return PurePosixPath(os.path.normpath(value.strip())).as_posix()


def newline_for(text: str) -> str:
    """The line ending a text file already uses, so a rewrite keeps it."""
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text and "\n" not in text:
        return "\r"
    return "\n"


def split_lines(text: str) -> list[str]:
    """Split on the text's own line ending only, keeping the trailing empty element
    a final newline produces. `str.splitlines` also breaks on vertical tab, form
    feed, the information separators, NEL and the Unicode separators, none of which
    ends a line in the formats splashdown edits, so an editor that rewrote from it
    would promote any of them to a real line break."""
    return text.split(newline_for(text))
