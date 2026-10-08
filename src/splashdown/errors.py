"""Dependency-free shared exceptions kept below recipe and device layers to prevent import cycles."""

from __future__ import annotations

from .results import Diagnostic, JsonObject


class ApplicationError(RuntimeError):
    def __init__(  # noqa: PLR0913 — shared exception contract
        self,
        message: str,
        *,
        exit_code: int = 1,
        is_error: bool = True,
        code: str = "operation_failed",
        data: JsonObject | None = None,
        partial: bool = False,
        next_steps: tuple[str, ...] = (),
        warnings: tuple[Diagnostic, ...] = (),
    ) -> None:
        if partial and data is None:
            raise ValueError("partial requires result data")
        self.code = code
        self.data = data
        self.partial = partial
        self.next_steps = next_steps
        self.warnings = warnings
        self.exit_code = exit_code
        self.is_error = is_error
        super().__init__(message)


class UsageError(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2, is_error=False, code="invalid_arguments")


class MissingRecipeError(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=0, is_error=False)


class SetupError(ApplicationError):
    pass


class LoaderConflictError(ApplicationError):
    """A loader's configuration cannot be edited without discarding settings
    splashdown does not own."""


class DeviceError(RuntimeError):
    pass


class CapabilityError(DeviceError):
    def __init__(self, capability: str, message: str) -> None:
        self.capability = capability
        super().__init__(message)
