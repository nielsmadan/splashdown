from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import ClassVar, Literal, TypeAlias


@dataclass(frozen=True)
class SimulatorRecord:
    checkout: str
    variant: str
    identifier: str
    model: str
    runtime: str
    created_at: str

    dtype: ClassVar[Literal["simulator"]] = "simulator"
    platform: ClassVar[Literal["ios"]] = "ios"
    owned: ClassVar[bool] = True

    @property
    def name(self) -> None:
        return None

    @property
    def udid(self) -> str:
        return self.identifier

    @property
    def ios(self) -> str:
        return self.runtime


@dataclass(frozen=True)
class EmulatorRecord:
    checkout: str
    variant: str
    name: str
    device: str
    image: str
    created_at: str

    dtype: ClassVar[Literal["emulator"]] = "emulator"
    platform: ClassVar[Literal["android"]] = "android"
    owned: ClassVar[bool] = True

    @property
    def identifier(self) -> str:
        return self.name

    @property
    def udid(self) -> str:
        return self.name

    @property
    def model(self) -> str:
        return self.device

    @property
    def ios(self) -> str:
        return self.image


ManagedDevice: TypeAlias = SimulatorRecord | EmulatorRecord


class _DestinationMapping(Mapping[str, str | bool]):
    platform: ClassVar[str]
    name: str
    identifier: str | None
    owned: bool

    @property
    def physical(self) -> bool:
        return not self.owned

    @property
    def kind(self) -> str:
        return self.platform

    def _mapping(self) -> dict[str, str | bool]:
        key = "udid" if self.platform == "ios" else "serial"
        return {
            "kind": self.platform,
            "name": self.name,
            "physical": self.physical,
            key: self.identifier or "",
        }

    def __getitem__(self, key: str) -> str | bool:
        return self._mapping()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping())

    def __len__(self) -> int:
        return len(self._mapping())


@dataclass(frozen=True, eq=False)
class IOSDestination(_DestinationMapping):
    name: str
    identifier: str
    owned: bool

    platform: ClassVar[Literal["ios"]] = "ios"

    @property
    def udid(self) -> str:
        return self.identifier


@dataclass(frozen=True, eq=False)
class AndroidDestination(_DestinationMapping):
    name: str
    identifier: str | None
    owned: bool

    platform: ClassVar[Literal["android"]] = "android"

    @property
    def serial(self) -> str:
        return self.identifier or ""


LaunchDestination: TypeAlias = IOSDestination | AndroidDestination
DestinationLike: TypeAlias = LaunchDestination | Mapping[str, object]


def as_launch_destination(value: DestinationLike) -> LaunchDestination:
    if isinstance(value, (IOSDestination, AndroidDestination)):
        return value
    platform = str(value.get("platform") or value.get("kind") or "")
    name = str(value.get("name") or "")
    owned = not bool(value.get("physical", False))
    if platform == "ios":
        return IOSDestination(name, str(value.get("udid") or value.get("identifier") or ""), owned)
    if platform == "android":
        identifier = value.get("serial") or value.get("identifier")
        return AndroidDestination(name, str(identifier) if identifier else None, owned)
    raise ValueError(f"unknown launch destination platform `{platform}`")
