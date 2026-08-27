from __future__ import annotations

import os
import re
from pathlib import Path


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
