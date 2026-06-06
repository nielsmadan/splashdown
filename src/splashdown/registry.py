from __future__ import annotations

import errno
import fcntl
import os
import socket
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple


# ---------- registry ----------

class DeviceRow(NamedTuple):
    checkout: str
    dtype: str
    variant: str
    udid: str
    model: str
    ios: str
    created_at: str


class Registry:
    """Machine-local registry. TSV files protected by flock.

    ports.tsv:    port\tabspath\tkey
    kv.tsv:       abspath\tkey\tvalue
    devices.tsv:  abspath\tdtype\tvariant\tudid\tmodel\tios\tcreated_at
    """

    def __init__(
        self,
        port_file: Path | None = None,
        kv_file: Path | None = None,
        device_file: Path | None = None,
    ):
        # Resolve defaults at instantiation time (not import time) so tests can
        # monkeypatch.setenv("XDG_STATE_HOME", ...) and have it take effect.
        state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        registry_dir = state_home / "splashdown"
        self.port_file = port_file or (registry_dir / "ports.tsv")
        self.kv_file = kv_file or (registry_dir / "kv.tsv")
        self.device_file = device_file or (registry_dir / "devices.tsv")
        self.port_file.parent.mkdir(parents=True, exist_ok=True)
        self.port_file.touch(exist_ok=True)
        self.kv_file.touch(exist_ok=True)
        self.device_file.touch(exist_ok=True)

    @contextmanager
    def _lock(self, path: Path):
        # Lock a sidecar `.lock` file rather than the TSV itself so the
        # registry can be freely truncated and rewritten without releasing
        # or invalidating the held flock fd.
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.touch(exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # --- ports ---

    def _read_ports(self) -> list[tuple[int, str, str]]:
        out: list[tuple[int, str, str]] = []
        for line in self.port_file.read_text().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            try:
                port = int(parts[0])
            except ValueError:
                continue
            out.append((port, parts[1], parts[2]))
        return out

    def _write_ports(self, rows: Iterable[tuple[int, str, str]]) -> None:
        lines = [f"{p}\t{path}\t{key}" for (p, path, key) in rows]
        self.port_file.write_text("\n".join(lines) + ("\n" if lines else ""))

    def get_port(self, abspath: str, key: str) -> int | None:
        for port, path, k in self._read_ports():
            if path == abspath and k == key:
                return port
        return None

    def busy_ports(self, *, gc: bool = True) -> set[int]:
        rows = self._read_ports()
        live = set()
        kept: list[tuple[int, str, str]] = []
        for port, path, key in rows:
            if gc and not Path(path).exists():
                continue
            live.add(port)
            kept.append((port, path, key))
        if gc and len(kept) != len(rows):
            self._write_ports(kept)
        return live

    def allocate_port(self, abspath: str, key: str, lo: int, hi: int) -> int:
        with self._lock(self.port_file):
            existing = self.get_port(abspath, key)
            if existing is not None and lo <= existing <= hi:
                if not _port_in_use(existing):
                    return existing
                # Someone else grabbed it — fall through and reallocate.
                self._remove_port(abspath, key)
            busy = self.busy_ports(gc=True)
            for candidate in range(lo, hi + 1):
                if candidate in busy:
                    continue
                if _port_in_use(candidate):
                    continue
                self._append_port(candidate, abspath, key)
                return candidate
            raise RuntimeError(f"no free port in range {lo}-{hi} for {key}")

    def _append_port(self, port: int, abspath: str, key: str) -> None:
        rows = self._read_ports()
        rows.append((port, abspath, key))
        self._write_ports(rows)

    def _remove_port(self, abspath: str, key: str) -> None:
        rows = [r for r in self._read_ports() if not (r[1] == abspath and r[2] == key)]
        self._write_ports(rows)

    def release(self, abspath: str) -> int:
        """Remove all registry entries for abspath. Returns count removed."""
        removed = 0
        with self._lock(self.port_file):
            rows = self._read_ports()
            kept = [r for r in rows if r[1] != abspath]
            removed += len(rows) - len(kept)
            self._write_ports(kept)
        with self._lock(self.kv_file):
            kv_rows = self._read_kv()
            kept_kv = [r for r in kv_rows if r[0] != abspath]
            removed += len(kv_rows) - len(kept_kv)
            self._write_kv(kept_kv)
        with self._lock(self.device_file):
            dev_rows = self._read_devices()
            kept_dev = [r for r in dev_rows if r.checkout != abspath]
            removed += len(dev_rows) - len(kept_dev)
            self._write_devices(kept_dev)
        return removed

    # --- key/value (uuids, template results, set values) ---

    def _read_kv(self) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for line in self.kv_file.read_text().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            out.append((parts[0], parts[1], parts[2]))
        return out

    def _write_kv(self, rows: Iterable[tuple[str, str, str]]) -> None:
        lines = [f"{path}\t{key}\t{value}" for (path, key, value) in rows]
        self.kv_file.write_text("\n".join(lines) + ("\n" if lines else ""))

    def get_kv(self, abspath: str, key: str) -> str | None:
        for path, k, value in self._read_kv():
            if path == abspath and k == key:
                return value
        return None

    def set_kv(self, abspath: str, key: str, value: str) -> None:
        with self._lock(self.kv_file):
            rows = [r for r in self._read_kv() if not (r[0] == abspath and r[1] == key)]
            rows.append((abspath, key, value))
            self._write_kv(rows)

    def remove_kv(self, abspath: str, key: str) -> None:
        with self._lock(self.kv_file):
            rows = [r for r in self._read_kv() if not (r[0] == abspath and r[1] == key)]
            self._write_kv(rows)

    def all_for(self, abspath: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for port, path, key in self._read_ports():
            if path == abspath:
                out[key] = str(port)
        for path, key, value in self._read_kv():
            if path == abspath:
                out[key] = value
        return out

    # --- devices (sim / AVD instances we created) ---

    def _read_devices(self) -> list[DeviceRow]:
        out: list[DeviceRow] = []
        for line in self.device_file.read_text().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            out.append(DeviceRow(*parts))
        return out

    def _write_devices(self, rows: Iterable[DeviceRow]) -> None:
        lines = ["\t".join(r) for r in rows]
        self.device_file.write_text("\n".join(lines) + ("\n" if lines else ""))

    def get_device(self, abspath: str, dtype: str, variant: str) -> DeviceRow | None:
        for r in self._read_devices():
            if r.checkout == abspath and r.dtype == dtype and r.variant == variant:
                return r
        return None

    def set_device(
        self, abspath: str, dtype: str, variant: str,
        udid: str, model: str, ios: str,
    ) -> None:
        with self._lock(self.device_file):
            rows = [
                r for r in self._read_devices()
                if not (r.checkout == abspath and r.dtype == dtype and r.variant == variant)
            ]
            rows.append(DeviceRow(
                abspath, dtype, variant, udid, model, ios,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ))
            self._write_devices(rows)

    def remove_device(self, abspath: str, dtype: str, variant: str) -> None:
        with self._lock(self.device_file):
            rows = [
                r for r in self._read_devices()
                if not (r.checkout == abspath and r.dtype == dtype and r.variant == variant)
            ]
            self._write_devices(rows)

    def all_devices(self) -> list[DeviceRow]:
        return self._read_devices()

    def devices_for(self, abspath: str) -> list[DeviceRow]:
        return [r for r in self._read_devices() if r.checkout == abspath]

    def managed_udids(self) -> set[str]:
        return {r.udid for r in self._read_devices()}

    def gc_devices(self) -> int:
        """Drop device rows whose checkout dir no longer exists OR whose
        sim/AVD has been deleted out from under us. Returns count removed."""
        # Lazy import to avoid circular: registry ← devices ← registry.
        from .devices import _is_orphan_device as _orphan_check  # noqa: PLC0415
        with self._lock(self.device_file):
            rows = self._read_devices()
            kept = [
                r for r in rows
                if Path(r.checkout).exists() and not _orphan_check(r)
            ]
            self._write_devices(kept)
            return len(rows) - len(kept)

    def all_checkouts(self) -> list[str]:
        """Every checkout path the registry knows about across ports.tsv,
        kv.tsv, and devices.tsv. Deduped + sorted."""
        seen: set[str] = set()
        for row in self._read_ports():
            seen.add(row[1])
        for row in self._read_kv():
            seen.add(row[0])
        for row in self._read_devices():
            seen.add(row.checkout)
        return sorted(seen)

    def summary_for(self, abspath: str) -> dict[str, int]:
        """Per-checkout row counts grouped by source. Always returns all four
        keys (`port`, `kv`, `simulator`, `emulator`) even when zero, so callers
        can format without key-existence checks."""
        counts = {"port": 0, "kv": 0, "simulator": 0, "emulator": 0}
        for row in self._read_ports():
            if row[1] == abspath:
                counts["port"] += 1
        for row in self._read_kv():
            if row[0] == abspath:
                counts["kv"] += 1
        for row in self._read_devices():
            if row.checkout == abspath and row.dtype in counts:
                counts[row.dtype] += 1
        return counts

    def reconcile_with_recipes(self) -> int:
        """Drop port/kv entries for live checkouts whose current recipe no
        longer declares the key (e.g. a `DART_PORT` left over after the Flutter
        profile stopped emitting one). Checkouts whose recipe is missing or
        won't parse are left untouched — a recipe we can't load must never be
        read as "declares nothing" and nuke live entries. Returns count
        removed."""
        # Lazy import to avoid circular: registry ← recipe ← registry.
        from .recipe import RECIPE_NAME, Recipe  # noqa: PLC0415

        cache: dict[str, set[str] | None] = {}

        def declared(path: str) -> set[str] | None:
            """Resource names the checkout's recipe declares, or None when the
            recipe is absent/unloadable (signal: skip pruning this checkout)."""
            if path not in cache:
                result: set[str] | None = None
                recipe_path = Path(path) / RECIPE_NAME
                if recipe_path.exists():
                    try:
                        result = set(Recipe.load(recipe_path).resources)
                    except Exception:  # noqa: BLE001 — bad recipe shouldn't prune
                        result = None
                cache[path] = result
            return cache[path]

        removed = 0
        with self._lock(self.port_file):
            rows = self._read_ports()
            kept = [r for r in rows if (d := declared(r[1])) is None or r[2] in d]
            removed += len(rows) - len(kept)
            self._write_ports(kept)
        with self._lock(self.kv_file):
            kv_rows = self._read_kv()
            kept_kv = [r for r in kv_rows if (d := declared(r[0])) is None or r[1] in d]
            removed += len(kv_rows) - len(kept_kv)
            self._write_kv(kept_kv)
        return removed

    def gc(self) -> int:
        """Drop entries whose abspath no longer exists, then reconcile live
        checkouts against their current recipes. Returns count removed."""
        removed = 0
        with self._lock(self.port_file):
            rows = self._read_ports()
            kept = [r for r in rows if Path(r[1]).exists()]
            removed += len(rows) - len(kept)
            self._write_ports(kept)
        with self._lock(self.kv_file):
            rows_kv = self._read_kv()
            kept_kv = [r for r in rows_kv if Path(r[0]).exists()]
            removed += len(rows_kv) - len(kept_kv)
            self._write_kv(kept_kv)
        removed += self.gc_devices()
        removed += self.reconcile_with_recipes()
        return removed


def _port_in_use(port: int) -> bool:
    """Best-effort live check. Tries to bind on loopback."""
    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)), (socket.AF_INET6, ("::1", port))):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(addr)
            except OSError as e:
                if e.errno in (errno.EADDRINUSE, errno.EACCES):
                    return True
            finally:
                s.close()
        except OSError:
            continue
    return False


