"""Shared exception types with no intra-package dependencies.

`DeviceError` lives here (rather than in devices.py) so lower-level modules like
recipe.py can raise/catch it without importing the device layer — which would be
a cycle, since devices.py imports recipe.py."""

from __future__ import annotations


class DeviceError(RuntimeError):
    pass
