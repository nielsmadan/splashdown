#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PHASE="startup"
CURRENT_LOG=""
TMP_ROOT=""
LOG_DIR=""
KEEP="${SPLASH_SMOKE_KEEP:-0}"
VITE_TIMEOUT="${SPLASH_SMOKE_VITE_TIMEOUT:-30}"
PIDS=()
LAST_PID=""
CREATE_VITE_VERSION=""
NODE_ENV=()

phase() {
    PHASE="$1"
    printf '\n==> %s\n' "$PHASE"
}

fail() {
    printf 'first-use canary failed during %s: %s\n' "$PHASE" "$1" >&2
    printf 'workspace: %s\n' "${TMP_ROOT:-not-created}" >&2
    printf 'loader: %s\n' "${LOADER:-not-detected}" >&2
    printf 'main port: %s\n' "${MAIN_PORT:-not-allocated}" >&2
    printf 'worktree port: %s\n' "${WORKTREE_PORT:-not-allocated}" >&2
    if [[ -n "$LOG_DIR" && -f "$LOG_DIR/versions.log" ]]; then
        printf '\nVersions:\n' >&2
        cat "$LOG_DIR/versions.log" >&2
    fi
    if [[ -n "$CURRENT_LOG" && -f "$CURRENT_LOG" ]]; then
        printf '\nLast 80 lines of %s:\n' "$CURRENT_LOG" >&2
        tail -n 80 "$CURRENT_LOG" >&2
    fi
    exit 1
}

run_logged() {
    local log="$1"
    local status
    shift
    CURRENT_LOG="$log"
    if "$@" >>"$log" 2>&1; then
        return
    else
        status=$?
    fi
    fail "command exited with status $status"
}

capture_logged() {
    local variable="$1"
    local log="$2"
    local output
    local status
    shift 2
    CURRENT_LOG="$log"
    if output="$("$@" 2>&1)"; then
        printf '%s\n' "$output" >>"$log"
        printf -v "$variable" '%s' "$output"
        return
    else
        status=$?
    fi
    printf '%s\n' "$output" >>"$log"
    fail "command exited with status $status"
}

install_splashdown() {
    local venv="$1"
    local source_root="$2"
    uv venv --python 3.13 "$venv" || return
    uv pip install --python "$venv/bin/python" --no-cache "$source_root"
}

capture_versions() {
    printf 'splash: '
    splash --version || return
    printf 'node: '
    run_node_tool node --version || return
    printf 'npm: '
    run_node_tool npm --version || return
    printf 'create-vite: '
    CREATE_VITE_VERSION="$(run_node_tool npm view create-vite version)" || return
    case "$CREATE_VITE_VERSION" in
        ''|*[!0-9A-Za-z.+-]*)
            printf 'unexpected create-vite version: %s\n' "$CREATE_VITE_VERSION" >&2
            return 1
            ;;
    esac
    printf '%s\n' "$CREATE_VITE_VERSION"
}

run_in_directory() {
    local directory="$1"
    shift
    (cd "$directory" && "$@")
}

run_node_tool() {
    "${NODE_ENV[@]}" "$@"
}

record_vite_version() {
    printf 'vite: '
    run_node_tool node -p 'require(process.argv[1]).version' "$1"
}

assert_generated_env_port() {
    local checkout="$1"
    local expected_port="$2"
    if (
        set -a
        . "$checkout/splashdown.env" || exit
        set +a
        [[ "${WEB_DEV_PORT:-}" == "$expected_port" ]] || exit
    ); then
        return
    fi
    fail "$checkout/splashdown.env disagrees with the registry"
}

initialize_git() {
    local checkout="$1"
    if [[ -e "$checkout/.git" || -L "$checkout/.git" ]]; then
        printf 'refusing to initialize scaffold with pre-existing .git\n' >&2
        return 1
    fi
    git -C "$checkout" -c init.defaultBranch=main init -q \
        --template="$EMPTY_GIT_TEMPLATE" || return
    git -C "$checkout" config user.email first-use-canary@example.invalid || return
    git -C "$checkout" config user.name "Splashdown first-use canary" || return
    git -C "$checkout" add -A || return
    git -C "$checkout" commit -q -m "initial Vite app"
}

commit_splashdown() {
    local checkout="$1"
    git -C "$checkout" add -A || return
    git -C "$checkout" commit -q -m "configure Splashdown"
}

require_tool() {
    command -v "$1" >/dev/null 2>&1 || fail "missing prerequisite: $1"
}

stop_server() {
    local pid="$1"
    local attempt
    if kill -0 "$pid" 2>/dev/null; then
        kill -TERM -- "-$pid" 2>/dev/null || true
    fi
    for attempt in {1..50}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL -- "-$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
}

cleanup() {
    local status="$?"
    local pid
    trap - EXIT
    for pid in "${PIDS[@]:-}"; do
        [[ -n "$pid" ]] || continue
        stop_server "$pid"
    done
    if [[ -n "$TMP_ROOT" && -d "$TMP_ROOT" ]]; then
        case "$TMP_ROOT" in
            */splashdown-first-use.*)
                if [[ "$KEEP" == "1" ]]; then
                    printf 'retained first-use workspace: %s\n' "$TMP_ROOT" >&2
                else
                    rm -rf -- "$TMP_ROOT"
                fi
                ;;
            *)
                printf 'refusing to remove unexpected temporary path: %s\n' "$TMP_ROOT" >&2
                status=1
                ;;
        esac
    fi
    exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

case "$VITE_TIMEOUT" in
    ''|*[!0-9]*|0) fail "SPLASH_SMOKE_VITE_TIMEOUT must be a positive integer" ;;
esac

phase "prerequisites"
for tool in git uv node npm curl; do
    require_tool "$tool"
done

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/splashdown-first-use.XXXXXX")"
case "$TMP_ROOT" in
    */splashdown-first-use.*) ;;
    *) fail "mktemp returned an unexpected path: $TMP_ROOT" ;;
esac
LOG_DIR="$TMP_ROOT/logs"
mkdir -p "$LOG_DIR"
VENV="$TMP_ROOT/venv"
NODE_HOME="$TMP_ROOT/node-home"
NODE_TMPDIR="$TMP_ROOT/node-tmp"
NPM_CACHE="$TMP_ROOT/npm-cache"
EMPTY_GIT_TEMPLATE="$TMP_ROOT/git-template"
NPM_GLOBAL_CONFIG="$NODE_HOME/global.npmrc"

export XDG_STATE_HOME="$TMP_ROOT/xdg-state"
export XDG_CONFIG_HOME="$TMP_ROOT/xdg-config"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
while IFS= read -r variable; do unset "$variable"; done < <(git rev-parse --local-env-vars)
unset GIT_TEMPLATE_DIR
for variable in $(compgen -v GIT_CONFIG_KEY_); do unset "$variable"; done
for variable in $(compgen -v GIT_CONFIG_VALUE_); do unset "$variable"; done
mkdir -p \
    "$XDG_STATE_HOME" \
    "$XDG_CONFIG_HOME" \
    "$NODE_HOME" \
    "$NODE_TMPDIR" \
    "$NPM_CACHE" \
    "$EMPTY_GIT_TEMPLATE"
touch "$NPM_GLOBAL_CONFIG"

phase "Splashdown install"
CURRENT_LOG="$LOG_DIR/splashdown-install.log"
run_logged "$CURRENT_LOG" install_splashdown "$VENV" "$SOURCE_ROOT"
export PATH="$VENV/bin:$PATH"
NODE_ENV=(
    /usr/bin/env -i
    "HOME=$NODE_HOME"
    "PATH=$PATH"
    "TMPDIR=$NODE_TMPDIR"
    "npm_config_cache=$NPM_CACHE"
    "npm_config_userconfig=/dev/null"
    "npm_config_globalconfig=$NPM_GLOBAL_CONFIG"
)
[[ "$(command -v splash)" == "$VENV/bin/splash" ]] || fail "PATH did not select the temporary Splashdown install"

phase "version capture"
CURRENT_LOG="$LOG_DIR/versions.log"
run_logged "$CURRENT_LOG" capture_versions

[[ -x "$VENV/bin/splash" ]] || fail "temporary Splashdown executable was not installed"

assert_file() {
    [[ -f "$1" ]] || fail "expected file: $1"
}

assert_contains() {
    local path="$1"
    local expected="$2"
    grep -F -- "$expected" "$path" >/dev/null || fail "$path does not contain: $expected"
}

assert_port() {
    case "$2" in
        ''|*[!0-9]*) fail "$1 is not a numeric port: $2" ;;
    esac
}

assert_loader_wiring() {
    local checkout="$1"
    local loader="$2"
    local init_log="$3"
    local mise_path
    case "$loader" in
        mise)
            mise_path="$checkout/mise.toml"
            [[ -f "$mise_path" ]] || mise_path="$checkout/.mise.toml"
            assert_file "$mise_path"
            assert_contains "$mise_path" "splashdown.env"
            ;;
        direnv)
            assert_file "$checkout/.envrc"
            assert_contains "$checkout/.envrc" "# >>> splashdown-managed dotenv >>>"
            assert_contains "$checkout/.envrc" "dotenv_if_exists splashdown.env"
            ;;
        devbox)
            assert_file "$checkout/devbox.json"
            assert_contains "$checkout/devbox.json" "# splashdown-managed"
            assert_contains "$checkout/devbox.json" "source splashdown.env"
            ;;
        none)
            assert_contains "$init_log" "no shell loader detected"
            ;;
        *)
            fail "plain splash init reported unsupported loader: $loader"
            ;;
    esac
}

free_port() {
    run_node_tool node -e 'const net=require("net"); const server=net.createServer(); server.listen(0,"127.0.0.1",()=>{console.log(server.address().port); server.close();});'
}

start_vite() {
    local checkout="$1"
    local port="$2"
    local log="$3"
    local load_splashdown_env="$4"
    local launch_port="$port"
    set -m
    (
        cd "$checkout"
        if [[ "$load_splashdown_env" == "yes" ]]; then
            set -a
            . ./splashdown.env
            set +a
            [[ "${WEB_DEV_PORT:-}" == "$port" ]]
            launch_port="$WEB_DEV_PORT"
        fi
        exec "${NODE_ENV[@]}" "WEB_DEV_PORT=$launch_port" \
            npm run dev -- --host 127.0.0.1 --port "$launch_port" --strictPort
    ) >"$log" 2>&1 &
    LAST_PID="$!"
    PIDS+=("$LAST_PID")
    set +m
}

wait_for_vite() {
    local port="$1"
    local pid="$2"
    local log="$3"
    local body=""
    local deadline=$((SECONDS + VITE_TIMEOUT))
    local remaining
    local status
    CURRENT_LOG="$log"
    while ((SECONDS < deadline)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            if wait "$pid"; then
                status=0
            else
                status=$?
            fi
            fail "Vite exited with status $status before becoming ready on port $port"
        fi
        remaining=$((deadline - SECONDS))
        if body="$(curl --fail --silent --max-time "$remaining" "http://127.0.0.1:$port/")"; then
            [[ "$body" == *'id="root"'* ]] || fail "port $port did not serve the generated React document"
            return
        fi
        sleep 0.25
    done
    CURRENT_LOG="$log"
    fail "Vite did not become ready on port $port within $VITE_TIMEOUT seconds"
}

APP="$TMP_ROOT/app"

phase "Vite scaffold"
CURRENT_LOG="$LOG_DIR/vite-scaffold.log"
run_logged "$CURRENT_LOG" run_in_directory "$TMP_ROOT" \
    run_node_tool npm create "vite@$CREATE_VITE_VERSION" app -- --template react --no-interactive
assert_file "$APP/package.json"
assert_file "$APP/vite.config.js"

phase "dependency install"
CURRENT_LOG="$LOG_DIR/npm-install-main.log"
run_logged "$CURRENT_LOG" run_in_directory "$APP" run_node_tool npm install
assert_file "$APP/package-lock.json"
run_logged "$LOG_DIR/versions.log" record_vite_version "$APP/node_modules/vite/package.json"

phase "Git initialization"
CURRENT_LOG="$LOG_DIR/git-init.log"
run_logged "$CURRENT_LOG" initialize_git "$APP"

phase "baseline Vite launch"
CURRENT_LOG="$LOG_DIR/vite-baseline.log"
capture_logged BASELINE_PORT "$CURRENT_LOG" free_port
start_vite "$APP" "$BASELINE_PORT" "$CURRENT_LOG" no
BASELINE_PID="$LAST_PID"
wait_for_vite "$BASELINE_PORT" "$BASELINE_PID" "$CURRENT_LOG"
stop_server "$BASELINE_PID"
PIDS=()
printf 'baseline Vite served on %s\n' "$BASELINE_PORT"

phase "plain splash init"
CURRENT_LOG="$LOG_DIR/splash-init.log"
run_logged "$CURRENT_LOG" run_in_directory "$APP" splash init

assert_file "$APP/splashdown.toml"
assert_file "$APP/splashdown.local.toml"
assert_file "$APP/splashdown.env"
assert_contains "$CURRENT_LOG" "→ vite"
assert_contains "$APP/splashdown.toml" 'profile = "vite"'
assert_contains "$APP/splashdown.toml" '[resources.WEB_DEV_PORT]'

capture_logged LOADER "$CURRENT_LOG" \
    awk '/^[[:space:]]*shell loader[[:space:]]*→/ { print $NF; exit }' "$CURRENT_LOG"
[[ -n "$LOADER" ]] || fail "plain splash init did not report a loader"
assert_loader_wiring "$APP" "$LOADER" "$CURRENT_LOG"

git -C "$APP" check-ignore -q -- splashdown.local.toml \
    || fail "splashdown.local.toml is not ignored"

capture_logged MAIN_PORT "$CURRENT_LOG" splash --cwd "$APP" env get WEB_DEV_PORT
assert_port "main WEB_DEV_PORT" "$MAIN_PORT"
assert_generated_env_port "$APP" "$MAIN_PORT"

CURRENT_LOG="$LOG_DIR/git-splashdown-commit.log"
run_logged "$CURRENT_LOG" commit_splashdown "$APP"

WORKTREE="$TMP_ROOT/worktree"

phase "worktree creation and hook provisioning"
CURRENT_LOG="$LOG_DIR/git-worktree-add.log"
run_logged "$CURRENT_LOG" git -C "$APP" worktree add --detach "$WORKTREE"

assert_file "$WORKTREE/splashdown.env"
assert_contains "$WORKTREE/splashdown.env" "WEB_DEV_PORT="

capture_logged WORKTREE_PORT "$CURRENT_LOG" splash --cwd "$WORKTREE" env get WEB_DEV_PORT
assert_port "worktree WEB_DEV_PORT" "$WORKTREE_PORT"
[[ "$MAIN_PORT" != "$WORKTREE_PORT" ]] \
    || fail "main and worktree received the same WEB_DEV_PORT: $MAIN_PORT"
assert_generated_env_port "$WORKTREE" "$WORKTREE_PORT"

phase "worktree dependency install"
CURRENT_LOG="$LOG_DIR/npm-ci-worktree.log"
run_logged "$CURRENT_LOG" run_in_directory "$WORKTREE" run_node_tool npm ci

phase "concurrent Vite launch"
MAIN_LOG="$LOG_DIR/vite-main.log"
WORKTREE_LOG="$LOG_DIR/vite-worktree.log"
start_vite "$APP" "$MAIN_PORT" "$MAIN_LOG" yes
MAIN_PID="$LAST_PID"
start_vite "$WORKTREE" "$WORKTREE_PORT" "$WORKTREE_LOG" yes
WORKTREE_PID="$LAST_PID"

wait_for_vite "$MAIN_PORT" "$MAIN_PID" "$MAIN_LOG"
wait_for_vite "$WORKTREE_PORT" "$WORKTREE_PID" "$WORKTREE_LOG"
kill -0 "$MAIN_PID" 2>/dev/null || fail "main Vite process stopped after readiness"
kill -0 "$WORKTREE_PID" 2>/dev/null || fail "worktree Vite process stopped after readiness"

printf 'first-use canary passed: loader=%s main=%s worktree=%s\n' \
    "$LOADER" "$MAIN_PORT" "$WORKTREE_PORT"
