"""End-to-end: a per-app `writer = "envfile=..."` resource is delivered into the
app's own dotenv file, a real consumer reads that file and binds the port, and
`splash deinit` later strips splashdown's key while leaving the user's lines.

This is the monorepo per-app delivery path (the mechanism apps like Vite, which
read their own `.env`, depend on). Python only — no Node/network.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import splashdown as sd

# A stand-in dev server that reads its port from a dotenv FILE (argv[1]), the way
# Vite's loadEnv / dotenv-based apps read their app-local .env — then binds it.
_DOTENV_SERVER_SRC = (
    "import sys, socket, time;"
    "port=None;"
    "lines=open(sys.argv[1]).read().splitlines();"
    "port=next(int(x.split('=',1)[1].strip().strip(chr(34)).strip(chr(39)))"
    " for x in lines if x.strip().startswith('WEB_PORT='));"
    "s=socket.socket(socket.AF_INET, socket.SOCK_STREAM);"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1);"
    "s.bind(('127.0.0.1', port));"
    "s.listen(16);"
    "time.sleep(30)"
)

_RECIPE = """\
[project]
loader = "none"

[apps.web]
path = "apps/web"
profile = "vite"
resources = ["WEB_PORT"]

[resources.WEB_PORT]
type   = "port"
range  = [5174, 5200]
writer = "envfile=apps/web/.env"
"""


def _read_web_env(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _wait_listening(port: int, proc: subprocess.Popen[str], deadline_s: float = 10.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(f"consumer exited early rc={proc.returncode}: {err!r}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"nothing listening on port {port} within {deadline_s}s")


def test_envfile_writer_delivers_to_app_then_deinit_strips(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    root = tmp_path / "repo"
    web = root / "apps" / "web"
    web.mkdir(parents=True)
    (root / "splashdown.toml").write_text(_RECIPE)
    # A user-owned line already in the app's .env — must survive both provision
    # (splashdown merges its key in) and deinit (splashdown removes only its key).
    env_file = web / ".env"
    env_file.write_text("USER_ONLY=keep-me\n")

    # Provision → splashdown injects WEB_PORT into apps/web/.env.
    assert sd.main(["--cwd", str(root)]) == 0
    after = _read_web_env(env_file)
    assert after["USER_ONLY"] == "keep-me"
    assert "WEB_PORT" in after
    port = int(after["WEB_PORT"])

    # A real consumer reads the app's own .env and binds the delivered port.
    proc = subprocess.Popen(
        [sys.executable, "-c", _DOTENV_SERVER_SRC, str(env_file)],
        cwd=str(web),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_listening(port, proc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    # deinit strips splashdown's key but preserves the user's, leaving the file.
    assert sd.main(["--cwd", str(root), "deinit"]) == 0
    remaining = env_file.read_text()
    assert "WEB_PORT" not in remaining
    assert "USER_ONLY=keep-me" in remaining
