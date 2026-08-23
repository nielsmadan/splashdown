from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CANARY = ROOT / "tests" / "smoke" / "first-use-vite.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -u\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_toolchain(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    fake_bin.mkdir()
    state.mkdir()

    _write_executable(
        fake_bin / "uv",
        f"state={str(state)!r}\nfake_splash={str(fake_bin / 'splash')!r}\n"
        + """
        printf '%s\n' "$*" >> "$state/uv.log"
        if [[ "$#" -eq 4 && "$1" == "venv" && "$2" == "--python" && "$3" == "3.13" ]]; then
            venv="$4"
            mkdir -p "$venv/bin"
            touch "$venv/bin/python"
        elif [[ "$#" -eq 6 && "$1 $2 $3" == "pip install --python" && "$5" == "--no-cache" ]]; then
            [[ -e "$4" && -d "$6" ]] || exit 51
            venv="${4%/bin/python}"
            cp "$fake_splash" "$venv/bin/splash"
            chmod +x "$venv/bin/splash"
        else
            printf 'unexpected uv invocation: %s\n' "$*" >&2
            exit 52
        fi
        """,
    )
    _write_executable(
        fake_bin / "splash",
        f"""
        state={str(state)!r}
        if [[ "${{1:-}}" == "--version" ]]; then
            printf 'splashdown 0.test\n'
            exit 0
        fi
        if [[ "${{1:-}}" == "init" ]]; then
            printf '[apps.app]\nprofile = "vite"\nresources = ["WEB_DEV_PORT"]\n\n[resources.WEB_DEV_PORT]\ntype = "port"\n' > splashdown.toml
            printf '[targets]\n' > splashdown.local.toml
            printf 'WEB_DEV_PORT=5174\n' > splashdown.env
            printf 'splashdown.local.toml\n' > .gitignore
            printf '  → vite\n'
            if [[ -f "$state/loader-none" ]]; then
                printf '  shell loader\t→ none\n'
                printf 'no shell loader detected — wrote splashdown.env but nothing sources it.\n'
            else
                printf '  shell loader\t→ mise\n'
                printf '_.file = "splashdown.env"\n' > mise.toml
            fi
            exit 0
        fi
        checkout="$2"
        case "$checkout" in
            */worktree) printf '5175\n' ;;
            *) printf '5174\n' ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "node",
        f"state={str(state)!r}\n"
        + """
        if [[ -f "$state/check-node-env" ]]; then
            case "${HOME:-}" in
                */splashdown-first-use.*/node-home) ;;
                *) printf 'unsafe Node HOME: %s\n' "${HOME:-unset}" >&2; exit 61 ;;
            esac
            case "${TMPDIR:-}" in
                */splashdown-first-use.*/node-tmp) ;;
                *) printf 'unsafe Node TMPDIR: %s\n' "${TMPDIR:-unset}" >&2; exit 62 ;;
            esac
            [[ -z "${CANARY_SECRET_SHOULD_NOT_LEAK+x}" ]] || exit 63
        fi
        case "${1:-}" in
            --version) printf 'v24.test\n' ;;
            -e) printf '5999\n' ;;
            -p)
                if [[ -f "$state/fail-vite-version" ]]; then
                    printf 'fake Vite version failure\n' >&2
                    exit 17
                fi
                printf '9.1.2\n'
                ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "npm",
        f"state={str(state)!r}\n"
        + """
        if [[ -f "$state/check-npm-env" ]]; then
            case "${HOME:-}" in
                */splashdown-first-use.*/node-home) ;;
                *) printf 'unsafe HOME: %s\n' "${HOME:-unset}" >&2; exit 71 ;;
            esac
            [[ -z "${CANARY_SECRET_SHOULD_NOT_LEAK+x}" ]] || exit 72
            [[ "${npm_config_userconfig:-}" == "/dev/null" ]] || exit 73
            case "${npm_config_globalconfig:-}" in
                */splashdown-first-use.*/node-home/global.npmrc)
                    [[ -f "$npm_config_globalconfig" && ! -s "$npm_config_globalconfig" ]] || exit 76
                    ;;
                *) exit 78 ;;
            esac
            case "${npm_config_cache:-}" in
                */splashdown-first-use.*/npm-cache) ;;
                *) printf 'unsafe npm cache: %s\n' "${npm_config_cache:-unset}" >&2; exit 74 ;;
            esac
            case "${TMPDIR:-}" in
                */splashdown-first-use.*/node-tmp) ;;
                *) printf 'unsafe npm TMPDIR: %s\n' "${TMPDIR:-unset}" >&2; exit 77 ;;
            esac
        fi
        printf '%s\n' "$*" >> "$state/npm.log"
        if [[ "${1:-}" == "--version" ]]; then
            printf '11.test\n'
        elif [[ "${1:-} ${2:-} ${3:-}" == "view create-vite version" ]]; then
            printf '9.1.2\n'
        elif [[ "${1:-}" == "create" ]]; then
            if [[ -f "$state/require-fixed-create" && "${2:-}" != "vite@9.1.2" ]]; then
                printf 'create-vite version was not fixed for this run\n' >&2
                exit 75
            fi
            mkdir -p "$3"
            printf '{{"scripts":{{"dev":"vite"}}}}\n' > "$3/package.json"
            printf 'export default {{}}\n' > "$3/vite.config.js"
            if [[ -f "$state/scaffold-git-entry" ]]; then
                mkdir -p "$3/.git/hooks"
            fi
        elif [[ "${1:-}" == "install" || "${1:-}" == "ci" ]]; then
            mkdir -p node_modules/vite
            printf '{{"version":"9.1.2"}}\n' > node_modules/vite/package.json
            printf '{{"lockfileVersion":3}}\n' > package-lock.json
        elif [[ "${1:-} ${2:-}" == "run dev" ]]; then
            if [[ -f "$state/check-launch-contract" ]]; then
                [[ "$#" -eq 8 ]] || exit 86
                [[ "$3" == "--" ]] || exit 87
                [[ "$4" == "--host" && "$5" == "127.0.0.1" ]] || exit 88
                [[ "$6" == "--port" && "$7" =~ ^[0-9]+$ ]] || exit 89
                [[ "$8" == "--strictPort" ]] || exit 90
                [[ "${WEB_DEV_PORT:-}" == "$7" ]] || exit 91
            fi
            printf 'fake Vite server from %s\n' "$PWD"
            if [[ -f "$state/record-server-pids" ]]; then
                printf '%s\n' "$$" >> "$state/server-pids.log"
            fi
            if [[ -f "$state/server-exit" ]]; then
                exit 23
            fi
            sleep 3600 &
            child=$!
            trap 'kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; exit 0' TERM INT
            wait "$child"
        else
            printf 'unexpected npm invocation: %s\n' "$*" >&2
            exit 92
        fi
        """,
    )
    _write_executable(
        fake_bin / "curl",
        f"""
        state={str(state)!r}
        printf '%s\n' "$PWD" >> "$state/curl-pwd.log"
        [[ ! -f "$state/server-exit" ]] || exit 7
        [[ ! -f "$state/curl-never-ready" ]] || exit 7
        count=0
        [[ ! -f "$state/curl-count" ]] || count="$(<"$state/curl-count")"
        count=$((count + 1))
        printf '%s\n' "$count" > "$state/curl-count"
        if [[ -f "$state/bad-second-body" && "$count" -eq 2 ]]; then
            printf '<html><body>wrong app</body></html>\n'
        else
            printf '<html><body><div id="root"></div></body></html>\n'
        fi
        """,
    )
    _write_executable(
        fake_bin / "git",
        f"""
        state={str(state)!r}
        if [[ "${{1:-}} ${{2:-}}" == "rev-parse --local-env-vars" ]]; then
            printf '%s\n' \
                GIT_ALTERNATE_OBJECT_DIRECTORIES \
                GIT_CONFIG \
                GIT_CONFIG_PARAMETERS \
                GIT_CONFIG_COUNT \
                GIT_OBJECT_DIRECTORY \
                GIT_DIR \
                GIT_WORK_TREE \
                GIT_IMPLICIT_WORK_TREE \
                GIT_GRAFT_FILE \
                GIT_INDEX_FILE \
                GIT_NO_REPLACE_OBJECTS \
                GIT_REPLACE_REF_BASE \
                GIT_PREFIX \
                GIT_SHALLOW_FILE \
                GIT_COMMON_DIR
            exit 0
        fi
        checkout=""
        if [[ "${{1:-}}" == "-C" ]]; then
            checkout="$2"
            shift 2
        fi
        is_init=0
        template=""
        for value in "$@"; do
            [[ "$value" != "init" ]] || is_init=1
            case "$value" in --template=*) template="${{value#--template=}}" ;; esac
        done
        if [[ "$is_init" -eq 1 ]]; then
            if [[ -f "$state/check-git-env" ]]; then
                [[ -z "${{GIT_TEMPLATE_DIR:-}}" ]] || exit 77
                [[ -z "${{GIT_CONFIG_COUNT:-}}" ]] || exit 78
                [[ -z "${{GIT_CONFIG_PARAMETERS:-}}" ]] || exit 79
                [[ -n "$template" && -d "$template" ]] || exit 80
                [[ -z "${{GIT_DIR:-}}" ]] || exit 81
                [[ -z "${{GIT_WORK_TREE:-}}" ]] || exit 82
                [[ -z "${{GIT_INDEX_FILE:-}}" ]] || exit 83
                [[ -z "${{GIT_OBJECT_DIRECTORY:-}}" ]] || exit 84
                [[ -z "${{GIT_COMMON_DIR:-}}" ]] || exit 85
            fi
            mkdir -p "$checkout/.git"
        elif [[ " $* " == *" worktree add "* ]]; then
            for value in "$@"; do worktree="$value"; done
            mkdir -p "$worktree"
            cp -R "$checkout/." "$worktree/"
            printf 'WEB_DEV_PORT=5175\n' > "$worktree/splashdown.env"
        fi
        """,
    )
    return fake_bin, state


def _run_canary(
    tmp_path: Path,
    *,
    markers: tuple[str, ...] = (),
    loader_none: bool = False,
    runner_timeout: float = 20,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    fake_bin, state = _fake_toolchain(tmp_path)
    for marker in markers:
        (state / marker).touch()
    if loader_none:
        (state / "loader-none").touch()
    run_tmp = tmp_path / "run-tmp"
    run_tmp.mkdir()
    hostile_template = tmp_path / "host-template"
    hostile_template.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "TMPDIR": str(run_tmp),
            "SPLASH_SMOKE_KEEP": "1",
            "CANARY_SECRET_SHOULD_NOT_LEAK": "sentinel",
            "GIT_TEMPLATE_DIR": str(hostile_template),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(tmp_path / "host-hooks"),
            "GIT_CONFIG_PARAMETERS": "'alias.host=!false'",
            "GIT_DIR": str(tmp_path / "host-git-dir"),
            "GIT_WORK_TREE": str(tmp_path / "host-work-tree"),
            "GIT_INDEX_FILE": str(tmp_path / "host-index"),
            "GIT_OBJECT_DIRECTORY": str(tmp_path / "host-objects"),
            "GIT_COMMON_DIR": str(tmp_path / "host-common-dir"),
        }
    )
    command = ["bash", str(CANARY)]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=runner_timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        stderr += f"\ntest runner timed out after {runner_timeout} seconds\n"
        result = subprocess.CompletedProcess(command, 124, stdout, stderr)
    else:
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    return result, state


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.fixture(scope="module")
def successful_canary(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    return _run_canary(
        tmp_path_factory.mktemp("successful-canary"),
        markers=(
            "check-git-env",
            "check-launch-contract",
            "check-node-env",
            "check-npm-env",
            "require-fixed-create",
        ),
        loader_none=True,
    )


def test_canary_accepts_the_no_loader_fallback(
    successful_canary: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    result, _state = successful_canary

    assert result.returncode == 0, _output(result)
    assert "first-use canary passed: loader=none main=5174 worktree=5175" in result.stdout


def test_canary_installs_the_current_checkout_with_uv(
    successful_canary: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    result, state = successful_canary

    assert result.returncode == 0, _output(result)
    assert (state / "uv.log").is_file()
    commands = (state / "uv.log").read_text().splitlines()
    assert len(commands) == 2
    assert commands[0].startswith("venv --python 3.13 ")
    assert commands[0].endswith("/venv")
    assert commands[1].startswith("pip install --python ")
    assert commands[1].endswith(f"/venv/bin/python --no-cache {ROOT}")


def test_canary_isolates_network_fetched_npm_code(
    successful_canary: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    result, _state = successful_canary

    assert result.returncode == 0, _output(result)


def test_canary_isolates_direct_node_commands(
    successful_canary: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    result, _state = successful_canary

    assert result.returncode == 0, _output(result)


def test_canary_ignores_inherited_git_configuration(
    successful_canary: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    result, _state = successful_canary

    assert result.returncode == 0, _output(result)


def test_canary_rejects_a_repository_created_by_the_scaffolder(tmp_path: Path) -> None:
    result, _state = _run_canary(tmp_path, markers=("scaffold-git-entry",))

    assert result.returncode == 1
    assert "refusing to initialize scaffold with pre-existing .git" in _output(result)


def test_directory_commands_do_not_change_the_harness_directory(
    successful_canary: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    result, state = successful_canary

    assert result.returncode == 0, _output(result)
    assert set((state / "curl-pwd.log").read_text().splitlines()) == {str(ROOT)}


def test_create_vite_uses_the_version_recorded_for_the_run(
    successful_canary: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    result, _state = successful_canary

    assert result.returncode == 0, _output(result)


def test_vite_launches_with_the_allocated_port_contract(
    successful_canary: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    result, _state = successful_canary

    assert result.returncode == 0, _output(result)


def test_early_vite_exit_reports_status_and_server_log(tmp_path: Path) -> None:
    result, _state = _run_canary(tmp_path, markers=("server-exit",))
    output = _output(result)

    assert result.returncode == 1
    assert "Vite exited with status 23 before becoming ready" in output
    assert "fake Vite server" in output


def test_readiness_timeout_reports_canary_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPLASH_SMOKE_VITE_TIMEOUT", "1")
    result, _state = _run_canary(tmp_path, markers=("curl-never-ready",))
    output = _output(result)

    assert result.returncode == 1
    assert "Vite did not become ready" in output
    assert "within 1 seconds" in output


def test_test_runner_gracefully_cleans_servers_on_outer_timeout(tmp_path: Path) -> None:
    result, state = _run_canary(
        tmp_path,
        markers=("curl-never-ready", "record-server-pids"),
        runner_timeout=3.0,
    )

    assert result.returncode == 124
    assert "test runner timed out after 3.0 seconds" in result.stderr
    assert (state / "server-pids.log").is_file(), _output(result)
    for value in (state / "server-pids.log").read_text().splitlines():
        with pytest.raises(ProcessLookupError):
            os.kill(int(value), 0)


def test_unexpected_http_body_reports_the_server_log(tmp_path: Path) -> None:
    result, _state = _run_canary(tmp_path, markers=("bad-second-body",))
    output = _output(result)

    assert result.returncode == 1
    assert "vite-main.log" in output


def test_logged_version_failure_selects_the_versions_log(tmp_path: Path) -> None:
    result, _state = _run_canary(tmp_path, markers=("fail-vite-version",))
    output = _output(result)

    assert result.returncode == 1
    assert "Last 80 lines of" in output
    assert "versions.log:" in output
    assert "fake Vite version failure" in output
