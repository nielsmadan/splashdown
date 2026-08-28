from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _validate_parent_chain(path: Path, root: Path | None) -> None:
    if root is None:
        return
    absolute_root = _absolute(root)
    absolute_path = _absolute(path)
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as error:
        raise ValueError(f"refusing to edit `{path}`: destination is outside `{root}`") from error
    if not relative.parts:
        raise ValueError(f"refusing to edit `{path}`: destination is not a file below `{root}`")

    current = absolute_root
    for part in relative.parts[:-1]:
        current /= part
        try:
            entry = current.lstat()
        except OSError as error:
            raise ValueError(f"could not inspect path component `{current}`: {error}") from error
        if stat.S_ISLNK(entry.st_mode):
            raise ValueError(f"refusing to edit `{path}`: path component `{current}` is a symlink")
        if not stat.S_ISDIR(entry.st_mode):
            raise ValueError(
                f"refusing to edit `{path}`: path component `{current}` is not a directory"
            )

    try:
        resolved_root = absolute_root.resolve(strict=True)
        resolved_parent = absolute_path.parent.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"could not resolve destination parent for `{path}`: {error}") from error
    if not resolved_parent.is_relative_to(resolved_root):
        raise ValueError(f"refusing to edit `{path}`: destination resolves outside `{root}`")


def _read_regular_file(
    path: Path,
    *,
    root: Path | None,
    missing_ok: bool,
) -> tuple[bytes, int] | None:
    _validate_parent_chain(path, root)
    try:
        entry = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ValueError(f"could not edit `{path}`: file does not exist") from None
    except OSError as error:
        raise ValueError(f"could not inspect `{path}`: {error}") from error
    if stat.S_ISLNK(entry.st_mode):
        raise ValueError(f"refusing to edit `{path}`: destination is a symlink")
    if not stat.S_ISREG(entry.st_mode):
        raise ValueError(f"refusing to edit `{path}`: destination is not a regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as file:
            opened = os.fstat(file.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"refusing to edit `{path}`: destination is not a regular file")
            raw = file.read()
    except OSError as error:
        raise ValueError(f"could not safely read `{path}`: {error}") from error
    return raw, stat.S_IMODE(opened.st_mode)


def read_editable_bytes(path: Path, *, root: Path | None = None) -> bytes:
    current = _read_regular_file(path, root=root, missing_ok=False)
    if current is None:
        raise AssertionError("required editable file unexpectedly missing")
    return current[0]


def read_editable_text(
    path: Path,
    *,
    root: Path | None = None,
    encoding: str = "utf-8",
) -> str:
    return read_editable_bytes(path, root=root).decode(encoding)


def read_optional_editable_text(
    path: Path,
    *,
    root: Path | None = None,
    encoding: str = "utf-8",
) -> str | None:
    current = _read_regular_file(path, root=root, missing_ok=True)
    return None if current is None else current[0].decode(encoding)


def _create_temporary_file(path: Path) -> tuple[int, Path]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _ in range(10):
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            return os.open(temporary, flags, 0o666), temporary
        except FileExistsError:
            continue
        except OSError as error:
            raise ValueError(
                f"could not create a temporary file beside `{path}`: {error}"
            ) from error
    raise ValueError(f"could not create a unique temporary file beside `{path}`")


def atomic_write_text(
    path: Path,
    text: str,
    *,
    root: Path | None = None,
    create: bool = False,
    text_format: tuple[str, str | None] = ("utf-8", ""),
    mode: int | None = None,
) -> None:
    current = _read_regular_file(path, root=root, missing_ok=create)
    output_mode = mode if mode is not None else (current[1] if current is not None else None)
    fd, temporary = _create_temporary_file(path)
    encoding, newline = text_format
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as file:
            if output_mode is not None:
                os.fchmod(file.fileno(), output_mode)
            file.write(text)
        os.replace(temporary, path)
    except OSError as error:
        raise ValueError(f"could not safely write `{path}`: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)
