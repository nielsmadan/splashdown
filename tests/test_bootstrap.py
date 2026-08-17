from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import splashdown as sd
from conftest import _git_init


def _write_recipe(cwd: Path, run: str | list[str], *, with_resource: bool = False) -> None:
    resource = '[resources.RUN_ID]\ntype = "cwd"\n\n' if with_resource else ""
    (cwd / sd.RECIPE_NAME).write_text(f"{resource}[bootstrap]\nrun = {json.dumps(run)}\n")


def _commit_recipe(cwd: Path) -> str:
    subprocess.run(["git", "add", sd.RECIPE_NAME], cwd=cwd, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Splashdown Tests",
            "-c",
            "user.email=splashdown@example.invalid",
            "commit",
            "-qm",
            "recipe",
        ],
        cwd=cwd,
        check=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()


def _wait_for(path: Path, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.01)


def _instrumented_lifecycle_process(
    cwd: Path,
    env: dict[str, str],
    signal: Path,
    argv: list[str],
) -> subprocess.Popen[str]:
    code = textwrap.dedent(
        f"""
        from contextlib import contextmanager
        from pathlib import Path
        from splashdown import cli, commands

        original = commands.lifecycle_lock

        @contextmanager
        def observed(*args, **kwargs):
            Path({str(signal)!r}).touch()
            with original(*args, **kwargs) as value:
                yield value

        commands.lifecycle_lock = observed
        raise SystemExit(cli.main({argv!r}))
        """
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _instrumented_untrust_process(
    cwd: Path,
    env: dict[str, str],
    signal: Path,
) -> subprocess.Popen[str]:
    code = textwrap.dedent(
        f"""
        from pathlib import Path
        from splashdown import cli, commands

        original = commands.revoke_trust

        def observed(dirs):
            Path({str(signal)!r}).touch()
            return original(dirs)

        commands.revoke_trust = observed
        raise SystemExit(cli.main(["--cwd", {str(cwd)!r}, "untrust"]))
        """
    )
    return subprocess.Popen([sys.executable, "-c", code], cwd=cwd, env=env)


def test_trust_displays_commands_but_does_not_run_them(tmp_path, capsys):
    _git_init(tmp_path)
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[bootstrap]\nrun = "printf first\\nprintf second > bootstrap-ran"\n'
    )

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    output = capsys.readouterr().err
    assert '1. "printf first\\nprintf second > bootstrap-ran"' in output
    assert "current and future refs" in output
    assert "next: run `splash bootstrap`" in output
    assert not (tmp_path / "bootstrap-ran").exists()
    dirs = sd.git_dirs(tmp_path)
    assert sd.is_trusted(dirs)
    trust_path = dirs.common / "splashdown" / "trust-v1.json"
    assert stat.S_IMODE(trust_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(trust_path.parent.stat().st_mode) == 0o700


def test_trust_without_bootstrap_authorizes_only_automatic_sync(tmp_path, capsys):
    _git_init(tmp_path)
    (tmp_path / sd.RECIPE_NAME).write_text("")

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    dirs = sd.git_dirs(tmp_path)
    assert sd.is_trusted(dirs, bootstrap=False)
    assert not sd.is_trusted(dirs)
    output = capsys.readouterr().err
    assert "bootstrap commands: none" in output
    assert "bootstrap execution is not authorized" in output
    assert "next: run `splash bootstrap`" not in output


def test_untrusted_bootstrap_fails_before_outputs_or_registry(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    _git_init(tmp_path)
    _write_recipe(tmp_path, "touch bootstrap-ran", with_resource=True)

    assert sd.main(["--cwd", str(tmp_path), "bootstrap"]) == 1

    assert not (tmp_path / "bootstrap-ran").exists()
    assert not (tmp_path / sd.ENV_FILE_NAME).exists()
    assert not state.exists()


def test_bootstrap_runs_once_and_rerun_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _git_init(tmp_path)
    _write_recipe(tmp_path, "printf x >> bootstrap.log")
    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    assert sd.main(["--cwd", str(tmp_path), "bootstrap"]) == 0
    assert sd.main(["--cwd", str(tmp_path), "bootstrap"]) == 0
    assert (tmp_path / "bootstrap.log").read_text() == "x"

    assert sd.main(["--cwd", str(tmp_path), "bootstrap", "--rerun"]) == 0
    assert (tmp_path / "bootstrap.log").read_text() == "xx"
    assert sd.bootstrap_complete(sd.git_dirs(tmp_path))


def test_failed_bootstrap_retries_from_the_first_command(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _git_init(tmp_path)
    _write_recipe(tmp_path, ["printf a >> bootstrap.log", "exit 3"])
    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    assert sd.main(["--cwd", str(tmp_path), "bootstrap"]) == 1
    assert not sd.bootstrap_complete(sd.git_dirs(tmp_path))

    _write_recipe(tmp_path, ["printf a >> bootstrap.log", "true"])
    assert sd.main(["--cwd", str(tmp_path), "bootstrap"]) == 0
    assert (tmp_path / "bootstrap.log").read_text() == "aa"


def test_failed_rerun_preserves_previous_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _git_init(tmp_path)
    _write_recipe(tmp_path, "printf x >> bootstrap.log")
    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0
    assert sd.main(["--cwd", str(tmp_path), "bootstrap"]) == 0

    _write_recipe(tmp_path, "exit 4")
    assert sd.main(["--cwd", str(tmp_path), "bootstrap", "--rerun"]) == 1
    assert sd.bootstrap_complete(sd.git_dirs(tmp_path))
    assert sd.main(["--cwd", str(tmp_path), "bootstrap"]) == 0
    assert (tmp_path / "bootstrap.log").read_text() == "x"


def test_corrupt_completion_requires_explicit_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _git_init(tmp_path)
    _write_recipe(tmp_path, "printf x >> bootstrap.log")
    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0
    dirs = sd.git_dirs(tmp_path)
    completion = dirs.private / "splashdown" / "bootstrap-v1.json"
    completion.write_text("not json\n")

    assert sd.main(["--cwd", str(tmp_path), "bootstrap"]) == 1
    assert not (tmp_path / "bootstrap.log").exists()
    assert sd.main(["--cwd", str(tmp_path), "bootstrap", "--rerun"]) == 0
    assert (tmp_path / "bootstrap.log").read_text() == "x"


def test_nested_sync_fails_without_deadlocking_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _git_init(tmp_path)
    _write_recipe(tmp_path, f"{sys.executable} -m splashdown sync")
    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    assert sd.main(["--cwd", str(tmp_path), "bootstrap"]) == 1
    assert not sd.bootstrap_complete(sd.git_dirs(tmp_path))


@pytest.mark.parametrize("command", ["deinit", "trust", "untrust"])
def test_nested_locking_commands_fail_without_deadlock(tmp_path, monkeypatch, command):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    _git_init(tmp_path)
    _write_recipe(tmp_path, f"{sys.executable} -m splashdown {command}")
    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    result = subprocess.run(
        [sys.executable, "-m", "splashdown", "--cwd", str(tmp_path), "bootstrap"],
        env={**os.environ, "XDG_STATE_HOME": str(state)},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 1
    assert "may not invoke splashdown lifecycle commands" in result.stderr


def test_git_checkout_inside_bootstrap_does_not_deadlock(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    _git_init(tmp_path)
    _write_recipe(tmp_path, "git checkout --detach HEAD")
    _commit_recipe(tmp_path)
    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    result = subprocess.run(
        [sys.executable, "-m", "splashdown", "--cwd", str(tmp_path), "bootstrap"],
        env={**os.environ, "XDG_STATE_HOME": str(state)},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0


def test_hook_bootstraps_only_a_linked_worktree_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    primary = tmp_path / "primary"
    primary.mkdir()
    _git_init(primary)
    _write_recipe(primary, "printf x >> bootstrap.log", with_resource=True)
    head = _commit_recipe(primary)
    sd.record_trust(sd.git_dirs(primary))
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(linked), head], cwd=primary, check=True
    )

    assert sd.main(["--cwd", str(linked), "hook", "post-checkout", head, head, "1"]) == 0
    assert not (linked / "bootstrap.log").exists()
    assert (linked / sd.ENV_FILE_NAME).exists()

    assert sd.main(["--cwd", str(linked), "hook", "post-checkout", "0" * 40, head, "1"]) == 0
    assert (linked / "bootstrap.log").read_text() == "x"

    primary_dirs = sd.git_dirs(primary)
    linked_dirs = sd.git_dirs(linked)
    assert not sd.is_worktree_creation(primary_dirs, "0" * 40, head, "1")
    assert sd.is_worktree_creation(linked_dirs, "0" * 64, "a" * 64, "1")
    assert not sd.is_worktree_creation(linked_dirs, "0" * 40, head, "0")


def test_untrusted_worktree_hook_writes_nothing_and_runs_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    primary = tmp_path / "primary"
    primary.mkdir()
    _git_init(primary)
    _write_recipe(primary, "touch bootstrap-ran", with_resource=True)
    head = _commit_recipe(primary)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(linked), head], cwd=primary, check=True
    )

    assert sd.main(["--cwd", str(linked), "hook", "post-checkout", "0" * 40, head, "1"]) == 0

    assert not (linked / sd.ENV_FILE_NAME).exists()
    assert not (linked / "bootstrap-ran").exists()
    assert not sd.bootstrap_complete(sd.git_dirs(linked))
    assert "automatic handling skipped" in capsys.readouterr().err


@pytest.mark.parametrize(
    "payload",
    ["not json\n", '{"version": 99, "sync": true, "bootstrap": true}\n'],
)
def test_corrupt_trust_fails_closed_for_direct_and_automatic_bootstrap(
    tmp_path, monkeypatch, payload
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    primary = tmp_path / "primary"
    primary.mkdir()
    _git_init(primary)
    _write_recipe(primary, "touch bootstrap-ran", with_resource=True)
    head = _commit_recipe(primary)
    sd.record_trust(sd.git_dirs(primary))
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(linked), head], cwd=primary, check=True
    )
    dirs = sd.git_dirs(linked)
    trust_path = dirs.common / "splashdown" / "trust-v1.json"
    trust_path.write_text(payload)

    assert sd.main(["--cwd", str(linked), "bootstrap"]) == 1
    assert sd.main(["--cwd", str(linked), "hook", "post-checkout", "0" * 40, head, "1"]) == 0

    assert not (linked / sd.ENV_FILE_NAME).exists()
    assert not (linked / "bootstrap-ran").exists()
    assert not sd.bootstrap_complete(dirs)


def test_bootstrap_receives_resolved_resources(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _git_init(tmp_path)
    _write_recipe(
        tmp_path,
        'printf "%s:%s" "$RUN_ID" "$SPLASHDOWN_LIFECYCLE_ACTIVE" > observed',
        with_resource=True,
    )
    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    assert sd.main(["--cwd", str(tmp_path), "bootstrap"]) == 0

    assert (tmp_path / "observed").read_text() == f"{tmp_path.name}:1"


def test_linked_worktrees_share_trust_but_separate_clones_do_not(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    _git_init(primary)
    _write_recipe(primary, "true")
    head = _commit_recipe(primary)
    sd.record_trust(sd.git_dirs(primary))
    linked = tmp_path / "linked"
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(linked), head], cwd=primary, check=True
    )
    subprocess.run(["git", "clone", "-q", str(primary), str(clone)], check=True)

    assert sd.is_trusted(sd.git_dirs(linked))
    assert not sd.is_trusted(sd.git_dirs(clone))


def test_trust_does_not_edit_tracked_lefthook_configuration(tmp_path, capsys):
    _git_init(tmp_path)
    _write_recipe(tmp_path, "true")
    path = tmp_path / "lefthook.yml"
    legacy = "post-checkout:\n  commands:\n    splashdown:\n      run: splash\n"
    path.write_text(legacy)

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    assert path.read_text() == legacy
    assert "doctor --fix" in capsys.readouterr().err
    assert sd.is_trusted(sd.git_dirs(tmp_path))


def test_trust_does_not_edit_tracked_husky_hook(tmp_path, capsys):
    _git_init(tmp_path)
    _write_recipe(tmp_path, "true")
    husky = tmp_path / ".husky"
    husky.mkdir()
    hook = husky / "post-checkout"
    hook.write_text(sd.LEGACY_POST_CHECKOUT_HOOK)
    hook.chmod(0o755)

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    assert hook.read_text() == sd.LEGACY_POST_CHECKOUT_HOOK
    assert "doctor --fix" in capsys.readouterr().err


def test_doctor_fix_migrates_exact_legacy_lefthook_job(tmp_path):
    _git_init(tmp_path)
    _write_recipe(tmp_path, "true")
    path = tmp_path / "lefthook.yml"
    path.write_text(
        "post-checkout:\n  commands:\n    splashdown:\n      run: splash\n      tags: local\n"
    )

    assert sd.cmd_doctor(tmp_path, fix=True) == 0

    text = path.read_text()
    assert '"$SPLASH" hook post-checkout' in text
    assert "tags: local" in text


def test_doctor_fix_preserves_modified_lefthook_job(tmp_path):
    _git_init(tmp_path)
    _write_recipe(tmp_path, "true")
    path = tmp_path / "lefthook.yml"
    custom = "post-checkout:\n  commands:\n    splashdown:\n      run: splash sync && notify\n"
    path.write_text(custom)

    assert sd.cmd_doctor(tmp_path, fix=True) == 1

    assert path.read_text() == custom


def test_doctor_rejects_custom_hook_that_does_not_forward_events(tmp_path):
    _git_init(tmp_path)
    _write_recipe(tmp_path, "true")
    husky = tmp_path / ".husky"
    husky.mkdir()
    hook = husky / "post-checkout"
    custom = "#!/bin/sh\necho splash hook post-checkout\n"
    hook.write_text(custom)
    hook.chmod(0o755)

    assert sd.cmd_doctor(tmp_path, fix=True) == 1

    assert hook.read_text() == custom
    assert not sd.post_checkout_readiness(tmp_path).ready


def test_bootstrap_reuses_react_native_hook_check(tmp_path, capsys):
    _git_init(tmp_path)
    _write_recipe(tmp_path, "true")
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')

    assert sd.cmd_doctor(tmp_path) == 1

    output = capsys.readouterr().err
    assert sum("  ✗  hook:" in line for line in output.splitlines()) == 1


def test_trust_upgrades_an_exact_legacy_native_hook(tmp_path):
    _git_init(tmp_path)
    _write_recipe(tmp_path, "true")
    hook = sd._native_hook_path(tmp_path)
    assert hook is not None
    hook.write_text(sd.LEGACY_POST_CHECKOUT_HOOK)
    hook.chmod(0o755)

    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0

    assert hook.read_text() == sd.hooks.POST_CHECKOUT_HOOK


def test_new_native_hook_forwards_events_once_and_swallows_failure(tmp_path):
    _git_init(tmp_path)
    (tmp_path / sd.RECIPE_NAME).write_text("")
    sd._wire_post_checkout_native(tmp_path)
    hook = sd._native_hook_path(tmp_path)
    assert hook is not None
    bin_dir = tmp_path.parent / f"{tmp_path.name}-trusted-bin"
    bin_dir.mkdir()
    fake = bin_dir / "splash"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SPLASH_CALLS"\nexit 7\n')
    fake.chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SPLASH_CALLS": str(calls),
    }

    subprocess.run([str(hook), "0" * 40, "a" * 40, "1"], cwd=tmp_path, env=env, check=True)

    assert calls.read_text().splitlines() == [f"hook post-checkout {'0' * 40} {'a' * 40} 1"]


def test_native_hook_rejects_checkout_controlled_splash(tmp_path):
    _git_init(tmp_path)
    (tmp_path / sd.RECIPE_NAME).write_text("")
    sd._wire_post_checkout_native(tmp_path)
    hook = sd._native_hook_path(tmp_path)
    assert hook is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "splash"
    fake.write_text('#!/bin/sh\ntouch "$SPLASH_CALLED"\n')
    fake.chmod(0o755)
    called = tmp_path / "called"

    result = subprocess.run(
        [str(hook), "0" * 40, "a" * 40, "1"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SPLASH_CALLED": str(called),
        },
        capture_output=True,
        text=True,
        check=True,
    )

    assert not called.exists()
    assert "refusing checkout-controlled splash executable" in result.stderr


def test_deinit_keeps_clone_trust_and_shared_hook_but_clears_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _git_init(tmp_path)
    _write_recipe(tmp_path, "true")
    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0
    assert sd.main(["--cwd", str(tmp_path), "bootstrap"]) == 0
    dirs = sd.git_dirs(tmp_path)
    hook = sd._native_hook_path(tmp_path)
    assert hook is not None

    assert sd.main(["--cwd", str(tmp_path), "deinit"]) == 0

    assert sd.is_trusted(dirs)
    assert hook.exists()
    assert not sd.bootstrap_complete(dirs)


def test_deinit_clears_only_one_linked_worktree_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    primary = tmp_path / "primary"
    primary.mkdir()
    _git_init(primary)
    _write_recipe(primary, "true")
    head = _commit_recipe(primary)
    sd.record_trust(sd.git_dirs(primary))
    first = tmp_path / "first"
    second = tmp_path / "second"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(first), head], cwd=primary, check=True
    )
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(second), head], cwd=primary, check=True
    )
    assert sd.main(["--cwd", str(first), "bootstrap"]) == 0
    assert sd.main(["--cwd", str(second), "bootstrap"]) == 0

    assert sd.main(["--cwd", str(first), "deinit"]) == 0

    assert not sd.bootstrap_complete(sd.git_dirs(first))
    assert sd.bootstrap_complete(sd.git_dirs(second))
    assert sd.is_trusted(sd.git_dirs(second))


def test_untrust_works_without_a_recipe(tmp_path):
    _git_init(tmp_path)
    dirs = sd.git_dirs(tmp_path)
    sd.record_trust(dirs)

    assert sd.main(["--cwd", str(tmp_path), "untrust"]) == 0

    assert not sd.is_trusted(dirs)


def test_concurrent_bootstrap_processes_execute_once(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    _git_init(tmp_path)
    _write_recipe(
        tmp_path,
        "printf x >> runs; touch started; while [ ! -f release ]; do sleep 0.01; done",
    )
    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0
    env = {**os.environ, "XDG_STATE_HOME": str(state)}
    argv = [sys.executable, "-m", "splashdown", "--cwd", str(tmp_path), "bootstrap"]
    first = subprocess.Popen(
        argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        _wait_for(tmp_path / "started")
        second_ready = tmp_path / "second-ready"
        second = _instrumented_lifecycle_process(
            tmp_path,
            env,
            second_ready,
            ["--cwd", str(tmp_path), "bootstrap"],
        )
        _wait_for(second_ready)
        (tmp_path / "release").touch()
        first_result = first.communicate(timeout=5)
        second_result = second.communicate(timeout=5)
    finally:
        (tmp_path / "release").touch(exist_ok=True)
        if first.poll() is None:
            first.kill()
    assert first.returncode == 0, first_result
    assert second.returncode == 0, second_result
    assert (tmp_path / "runs").read_text() == "x"


def test_untrust_waits_for_running_bootstrap_then_revokes(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    _git_init(tmp_path)
    _write_recipe(tmp_path, "touch started; while [ ! -f release ]; do sleep 0.01; done")
    assert sd.main(["--cwd", str(tmp_path), "trust"]) == 0
    env = {**os.environ, "XDG_STATE_HOME": str(state)}
    bootstrap = subprocess.Popen(
        [sys.executable, "-m", "splashdown", "--cwd", str(tmp_path), "bootstrap"],
        env=env,
    )
    try:
        _wait_for(tmp_path / "started")
        untrust_ready = tmp_path / "untrust-ready"
        untrust = _instrumented_untrust_process(tmp_path, env, untrust_ready)
        _wait_for(untrust_ready)
        assert untrust.poll() is None
        (tmp_path / "release").touch()
        assert bootstrap.wait(timeout=5) == 0
        assert untrust.wait(timeout=5) == 0
    finally:
        (tmp_path / "release").touch(exist_ok=True)
        for process in (bootstrap, locals().get("untrust")):
            if process is not None and process.poll() is None:
                process.kill()
    assert not sd.is_trusted(sd.git_dirs(tmp_path))
