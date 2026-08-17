from __future__ import annotations

from . import profile_core as _profile_core
from . import profiles_compose as _profiles_compose
from . import profiles_mobile as _profiles_mobile
from . import profiles_server as _profiles_server
from . import profiles_web as _profiles_web
from .catalog import PROFILES
from .profile_core import Profile, _manual_port_guidance, _markdown_command, _profile_port
from .profiles_compose import compose_project_resources, compose_wiring_checks
from .profiles_mobile import (
    AndroidNativeProfile,
    ExpoProfile,
    FlutterProfile,
    IosNativeProfile,
    ReactNativeProfile,
)
from .profiles_server import (
    AspNetCoreProfile,
    DjangoProfile,
    FastApiProfile,
    FlaskProfile,
    RailsProfile,
    SpringBootProfile,
)
from .profiles_web import (
    AngularProfile,
    AstroProfile,
    DenoProfile,
    LaravelProfile,
    NextJsProfile,
    NodeBackendProfile,
    NuxtProfile,
    ViteProfile,
)

__all__ = [
    "PROFILES",
    "AndroidNativeProfile",
    "AngularProfile",
    "AspNetCoreProfile",
    "AstroProfile",
    "DenoProfile",
    "DjangoProfile",
    "ExpoProfile",
    "FastApiProfile",
    "FlaskProfile",
    "FlutterProfile",
    "IosNativeProfile",
    "LaravelProfile",
    "NextJsProfile",
    "NodeBackendProfile",
    "NuxtProfile",
    "Profile",
    "RailsProfile",
    "ReactNativeProfile",
    "SpringBootProfile",
    "ViteProfile",
    "_manual_port_guidance",
    "_markdown_command",
    "_profile_port",
    "compose_project_resources",
    "compose_wiring_checks",
]

_BUILTIN_PROFILES = (
    ("astro", AstroProfile()),
    ("laravel", LaravelProfile()),
    ("nuxt", NuxtProfile()),
    ("angular", AngularProfile()),
    ("vite", ViteProfile()),
    ("node-backend", NodeBackendProfile()),
    ("deno", DenoProfile()),
    ("nextjs", NextJsProfile()),
    ("django", DjangoProfile()),
    ("fastapi", FastApiProfile()),
    ("flask", FlaskProfile()),
    ("springboot", SpringBootProfile()),
    ("aspnetcore", AspNetCoreProfile()),
    ("rails", RailsProfile()),
    ("flutter", FlutterProfile()),
    ("expo", ExpoProfile()),
    ("react-native", ReactNativeProfile()),
    ("ios-native", IosNativeProfile()),
    ("android-native", AndroidNativeProfile()),
)

for _name, _profile in _BUILTIN_PROFILES:
    PROFILES[_name] = _profile

_COMPAT_MODULES = (
    _profile_core,
    _profiles_compose,
    _profiles_web,
    _profiles_server,
    _profiles_mobile,
)


def __getattr__(name: str) -> object:
    for module in _COMPAT_MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    names = set(globals())
    for module in _COMPAT_MODULES:
        names.update(name for name in vars(module) if not name.startswith("__"))
    return sorted(names)
