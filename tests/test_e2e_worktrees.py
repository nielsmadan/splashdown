"""End-to-end: two git worktrees of one project each get a distinct dev port,
and a real process reading that port from splashdown's env can bind it — both
concurrently, proving no cross-worktree collision.

Unlike the rest of the suite (which mocks device/app launches), this drives the
real pipeline: git init → worktree add → `splash` provisions each checkout →
a subprocess reads PORT from that worktree's splashdown.env and listens on it.
Python + git only — no Node/npm/network, so it runs on CI.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import splashdown as sd

# A stand-in dev server: read PORT from the environment (as mise would export
# splashdown.env), bind it, and listen. The OS completes handshakes from the
# listen backlog without accept(), so a connect() readiness probe succeeds.
_SERVER_SRC = (
    "import os, socket, time;"
    "s=socket.socket(socket.AF_INET, socket.SOCK_STREAM);"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1);"
    "s.bind(('127.0.0.1', int(os.environ['PORT'])));"
    "s.listen(16);"
    "time.sleep(30)"
)

# nextjs-shaped project; the [resources.PORT] block is what `splash` provisions.
_RECIPE = """\
[project]
loader = "none"

[apps.web]
path = "."
profile = "nextjs"
resources = ["PORT"]

[resources.PORT]
type  = "port"
range = [19700, 19710]
"""


def _write_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text('{"dependencies": {"next": "15"}}')
    (root / "next.config.js").write_text("module.exports = {}")
    (root / "splashdown.toml").write_text(_RECIPE)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _git_init_commit(root: Path) -> None:
    # Per-repo identity: CI has no global git identity, so commit would fail.
    _git(root, "-c", "init.defaultBranch=main", "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")


def _read_env(checkout: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in (checkout / "splashdown.env").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _spawn_frontend(checkout: Path, env_vars: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _SERVER_SRC],
        cwd=str(checkout),
        env={**os.environ, **env_vars},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_listening(port: int, proc: subprocess.Popen[str], deadline_s: float = 10.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(f"frontend exited early rc={proc.returncode}: {err!r}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"nothing listening on port {port} within {deadline_s}s")


def _terminate(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_two_worktrees_get_distinct_ports_and_run_concurrently(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    main = tmp_path / "main"
    _write_project(main)
    _git_init_commit(main)
    wt2 = tmp_path / "wt2"
    _git(main, "worktree", "add", "--detach", str(wt2))

    # Provision each checkout through the real CLI.
    assert sd.main(["--cwd", str(main)]) == 0
    assert sd.main(["--cwd", str(wt2)]) == 0

    port_main = int(_read_env(main)["PORT"])
    port_wt2 = int(_read_env(wt2)["PORT"])
    assert port_main != port_wt2, "two worktrees must get distinct ports"

    # The machine-wide registry has one pinned row per checkout.
    ports_tsv = tmp_path / "state" / "splashdown" / "ports.tsv"
    rows = [ln for ln in ports_tsv.read_text().splitlines() if ln.strip()]
    assert len(rows) == 2
    registered = {int(ln.split("\t", 1)[0]) for ln in rows}
    assert registered == {port_main, port_wt2}

    # Spin up both "frontends" concurrently, each reading PORT from its own
    # splashdown.env; both must bind their assigned port at once (no collision).
    procs: list[subprocess.Popen[str]] = []
    try:
        procs.append(_spawn_frontend(main, _read_env(main)))
        procs.append(_spawn_frontend(wt2, _read_env(wt2)))
        _wait_listening(port_main, procs[0])
        _wait_listening(port_wt2, procs[1])
    finally:
        for pr in procs:
            _terminate(pr)


def test_post_checkout_hook_provisions_new_worktree(tmp_path, monkeypatch):
    # The linchpin: adding a worktree must auto-provision it via the installed
    # git post-checkout hook — no manual `splash` call in the new checkout.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    main = tmp_path / "main"
    main.mkdir()
    (main / "package.json").write_text('{"dependencies": {"next": "15"}}')
    (main / "next.config.js").write_text("module.exports = {}")
    _git(main, "-c", "init.defaultBranch=main", "init", "-q")
    _git(main, "config", "user.email", "test@example.com")
    _git(main, "config", "user.name", "Test")

    # Real init installs .githooks/post-checkout + sets core.hooksPath.
    assert sd.main(["--cwd", str(main), "init"]) == 0
    # Commit the hook + recipe so a fresh worktree checks them out and the hook
    # resolves. (splashdown.env / .local.toml are gitignored, so not committed.)
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")

    wt2 = tmp_path / "wt2"
    # `git worktree add` performs a checkout, firing the committed hook, which
    # runs `splash sync` (splash is on PATH under uv; XDG_STATE_HOME is inherited
    # from os.environ). We never invoke splash in wt2 ourselves.
    _git(main, "worktree", "add", "--detach", str(wt2))

    env_file = wt2 / "splashdown.env"
    assert env_file.exists(), "post-checkout hook did not provision the new worktree"
    assert "PORT=" in env_file.read_text()


def test_worktree_provision_trusts_each_worktrees_own_mise_toml(tmp_path, monkeypatch):
    # Regression for the highest-value case: mise trusts by ABSOLUTE PATH, so a
    # new worktree's inherited (committed) mise.toml is untrusted at its new path
    # even though the main checkout was trusted. Each provision must `mise trust`
    # that worktree's own config. Records approvals in-process (we call `splash`
    # via sd.main, not the hook subprocess) rather than shelling out to real mise.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    calls: list[list[str]] = []
    monkeypatch.setattr(sd.loaders, "_run_ok", lambda argv, cwd: calls.append(list(argv)) or True)

    main = tmp_path / "main"
    main.mkdir()
    (main / "mise.toml").write_text("[env]\n")
    (main / "splashdown.toml").write_text(
        '[project]\nloader = "mise"\n\n[resources.PORT]\ntype = "port"\nrange = [19720, 19730]\n'
    )
    _git_init_commit(main)

    wt2 = tmp_path / "wt2"
    _git(main, "worktree", "add", "--detach", str(wt2))

    assert sd.main(["--cwd", str(main)]) == 0
    assert sd.main(["--cwd", str(wt2)]) == 0

    trusted = [argv[2] for argv in calls if argv[:2] == ["mise", "trust"]]
    assert str(main / "mise.toml") in trusted
    assert str(wt2 / "mise.toml") in trusted
