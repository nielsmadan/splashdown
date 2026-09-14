from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path


class UneditablePath(ValueError):
    """A destination splashdown will not read or write, carrying the reason apart
    from the sentence naming the file, so a caller that already names the file in
    its own report can print only the cause."""

    def __init__(self, path: Path, reason: str, *, verb: str = "refusing to edit") -> None:
        super().__init__(f"{verb} `{path}`: {reason}")
        self.reason = reason


def refusal_reason(error: ValueError) -> str:
    """Why a destination could not be edited, without repeating its name."""
    return error.reason if isinstance(error, UneditablePath) else str(error)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _named(path: Path, root: Path | None) -> Path:
    """The destination as the user knows it: relative to the checkout when it is
    inside one, so a warning quotes `.gitignore` rather than an absolute path."""
    if root is None:
        return path
    try:
        return Path(os.path.relpath(_absolute(path), _absolute(root)))
    except ValueError:
        return path


def _validate_parent_chain(path: Path, root: Path | None) -> None:
    if root is None:
        return
    absolute_root = _absolute(root)
    absolute_path = _absolute(path)
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as error:
        raise UneditablePath(_named(path, root), f"destination is outside `{root}`") from error
    if not relative.parts:
        raise UneditablePath(_named(path, root), f"destination is not a file below `{root}`")

    current = absolute_root
    for part in relative.parts[:-1]:
        current /= part
        try:
            entry = current.lstat()
        except OSError as error:
            raise UneditablePath(
                _named(path, root),
                f"path component `{_named(current, root)}` could not be inspected: {error}",
                verb="could not edit",
            ) from error
        if stat.S_ISLNK(entry.st_mode):
            raise UneditablePath(
                _named(path, root), f"path component `{_named(current, root)}` is a symlink"
            )
        if not stat.S_ISDIR(entry.st_mode):
            raise UneditablePath(
                _named(path, root), f"path component `{_named(current, root)}` is not a directory"
            )

    try:
        resolved_root = absolute_root.resolve(strict=True)
        resolved_parent = absolute_path.parent.resolve(strict=True)
    except OSError as error:
        raise UneditablePath(
            _named(path, root), f"its parent could not be resolved: {error}", verb="could not edit"
        ) from error
    if not resolved_parent.is_relative_to(resolved_root):
        raise UneditablePath(_named(path, root), f"destination resolves outside `{root}`")


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
        raise UneditablePath(
            _named(path, root), "file does not exist", verb="could not edit"
        ) from None
    except OSError as error:
        raise UneditablePath(
            _named(path, root), f"it could not be inspected: {error}", verb="could not edit"
        ) from error
    if stat.S_ISLNK(entry.st_mode):
        raise UneditablePath(_named(path, root), "destination is a symlink")
    if not stat.S_ISREG(entry.st_mode):
        raise UneditablePath(_named(path, root), "destination is not a regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as file:
            opened = os.fstat(file.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise UneditablePath(_named(path, root), "destination is not a regular file")
            raw = file.read()
    except OSError as error:
        raise UneditablePath(
            _named(path, root), f"it could not be read: {error}", verb="could not edit"
        ) from error
    return raw, stat.S_IMODE(opened.st_mode)


def read_editable_bytes(path: Path, *, root: Path | None = None) -> bytes:
    current = _read_regular_file(path, root=root, missing_ok=False)
    if current is None:
        raise AssertionError("required editable file unexpectedly missing")
    return current[0]


def read_optional_editable_bytes(path: Path, *, root: Path | None = None) -> bytes | None:
    current = _read_regular_file(path, root=root, missing_ok=True)
    return None if current is None else current[0]


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
