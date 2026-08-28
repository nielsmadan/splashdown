"""Dependency-free shared exceptions kept below recipe and device layers to prevent import cycles."""

from __future__ import annotations


class ApplicationError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 1, is_error: bool = True) -> None:
        self.exit_code = exit_code
        self.is_error = is_error
        super().__init__(message)


class UsageError(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2, is_error=False)


class MissingRecipeError(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=0, is_error=False)


class SetupError(ApplicationError):
    pass


class DeviceError(RuntimeError):
    pass


class CapabilityError(DeviceError):
    def __init__(self, capability: str, message: str) -> None:
        self.capability = capability
        super().__init__(message)
