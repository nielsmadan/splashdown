from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_STATE_DIR = "splashdown"
_TRUST_FILE = "trust-v1.json"
_TRUST_LOCK = "trust.lock"
_COMPLETION_FILE = "bootstrap-v1.json"
_LIFECYCLE_LOCK = "lifecycle.lock"
_ACTIVE_ENV = "SPLASHDOWN_LIFECYCLE_ACTIVE"
_OID_RE = re.compile(r"[0-9A-Fa-f]+")
_GIT_DIR_LINES = 2


@dataclass(frozen=True)
class GitDirs:
    private: Path
    common: Path

    @property
    def linked(self) -> bool:
        try:
            return not self.private.samefile(self.common)
        except OSError:
            return self.private != self.common


@dataclass(frozen=True)
class TrustState:
    sync: bool
    bootstrap: bool


def git_dirs(cwd: Path) -> GitDirs:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise ValueError("bootstrap trust requires a Git checkout") from error
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != _GIT_DIR_LINES:
        raise ValueError("bootstrap trust requires a Git checkout")
    private = Path(lines[0])
    common = Path(lines[1])
    if not private.is_absolute():
        private = cwd / private
    if not common.is_absolute():
        common = cwd / common
    return GitDirs(private.resolve(), common.resolve())


def _state_dir(git_dir: Path, *, create: bool) -> Path:
    path = git_dir / _STATE_DIR
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    return path


@contextmanager
def _file_lock(path: Path, *, shared: bool) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def lifecycle_lock(cwd: Path, *, require_git: bool) -> Iterator[GitDirs | None]:
    try:
        dirs = git_dirs(cwd)
    except ValueError:
        if require_git:
            raise
        yield None
        return
    path = _state_dir(dirs.private, create=True) / _LIFECYCLE_LOCK
    with _file_lock(path, shared=False):
        yield dirs


@contextmanager
def trusted_execution(dirs: GitDirs) -> Iterator[TrustState]:
    path = _state_dir(dirs.common, create=True) / _TRUST_LOCK
    with _file_lock(path, shared=True):
        yield trust_state(dirs)


def _read_state(path: Path) -> object | None:
    try:
        value: object = json.loads(path.read_text())
        return value
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    fd, raw_tmp = tempfile.mkstemp(prefix=".splashdown-", dir=parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def trust_state(dirs: GitDirs) -> TrustState:
    path = _state_dir(dirs.common, create=False) / _TRUST_FILE
    state = _read_state(path)
    if not isinstance(state, dict) or state.get("version") != 1:
        return TrustState(False, False)
    sync = state.get("sync") is True
    bootstrap = state.get("bootstrap") is True
    return TrustState(sync, sync and bootstrap)


def is_trusted(dirs: GitDirs, *, bootstrap: bool = True) -> bool:
    state = trust_state(dirs)
    return state.bootstrap if bootstrap else state.sync


def record_trust(dirs: GitDirs, *, bootstrap: bool = True) -> None:
    lock = _state_dir(dirs.common, create=True) / _TRUST_LOCK
    with _file_lock(lock, shared=False):
        path = _state_dir(dirs.common, create=True) / _TRUST_FILE
        existing = trust_state(dirs)
        _atomic_write(
            path,
            {
                "version": 1,
                "sync": True,
                "bootstrap": existing.bootstrap or bootstrap,
            },
        )


def revoke_trust(dirs: GitDirs) -> bool:
    lock = _state_dir(dirs.common, create=True) / _TRUST_LOCK
    with _file_lock(lock, shared=False):
        path = _state_dir(dirs.common, create=True) / _TRUST_FILE
        state = trust_state(dirs)
        was_trusted = state.sync or state.bootstrap
        _atomic_write(path, {"version": 1, "sync": False, "bootstrap": False})
        return was_trusted


def bootstrap_complete(dirs: GitDirs) -> bool:
    path = _state_dir(dirs.private, create=False) / _COMPLETION_FILE
    if not path.exists():
        return False
    if _read_state(path) != {"complete": True, "version": 1}:
        raise ValueError(
            "bootstrap completion state is invalid; run `splash bootstrap --rerun` explicitly"
        )
    return True


def mark_bootstrap_complete(dirs: GitDirs) -> None:
    path = _state_dir(dirs.private, create=True) / _COMPLETION_FILE
    _atomic_write(path, {"version": 1, "complete": True})


def clear_bootstrap_completion(dirs: GitDirs) -> bool:
    path = _state_dir(dirs.private, create=False) / _COMPLETION_FILE
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def is_worktree_creation(dirs: GitDirs, old: str, new: str, flag: str) -> bool:
    return (
        dirs.linked
        and flag == "1"
        and bool(old)
        and set(old) == {"0"}
        and bool(_OID_RE.fullmatch(new))
        and set(new) != {"0"}
        and len(old) == len(new)
    )


def lifecycle_active() -> bool:
    return os.environ.get(_ACTIVE_ENV) == "1"


def lifecycle_environment() -> dict[str, str]:
    return {_ACTIVE_ENV: "1"}
