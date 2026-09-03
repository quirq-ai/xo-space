#!/usr/bin/env bash
# XO Space API local runner and process manager
# Usage: ./cowork-api.sh {dev|install|start|stop|restart|status|logs}
#
# The /tmp basenames match what the Coder claude-code template uses for the
# same process (/tmp/xo-space.{pid,log}), so one name finds it everywhere.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
PID_FILE="/tmp/xo-space.pid"
LOG_FILE="/tmp/xo-space.log"
LOCK_FILE="/tmp/xo-space.lock"
LOCK_PID_FILE="${LOCK_FILE}/pid"

# Read only the simple scalar values needed by this shell wrapper. The server
# still uses python-dotenv for the complete configuration. Avoid sourcing .env:
# it may contain secrets or values that are valid dotenv but unsafe shell.
read_dotenv_value() {
    local key="$1"
    local line
    local value

    [ -f "$ENV_FILE" ] || return 0

    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "$key="*)
                value="${line#*=}"
                value="${value%%[[:space:]]#*}"
                value="${value#"${value%%[![:space:]]*}"}"
                value="${value%"${value##*[![:space:]]}"}"
                case "$value" in
                    \"*\") value="${value#\"}"; value="${value%\"}" ;;
                    \'*\') value="${value#\'}"; value="${value%\'}" ;;
                esac
                printf '%s\n' "$value"
                return 0
                ;;
        esac
    done < "$ENV_FILE"
}

CONFIGURED_HOST="${HOST:-$(read_dotenv_value HOST)}"
CONFIGURED_PORT="${PORT:-$(read_dotenv_value PORT)}"
HOST="${CONFIGURED_HOST:-0.0.0.0}"
PORT="${CONFIGURED_PORT:-5002}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

resolve_python_cmd() {
    # Prefer project virtualenv when available, then system Python.
    if [ -x "$SCRIPT_DIR/venv/bin/python" ]; then
        echo "$SCRIPT_DIR/venv/bin/python"
        return 0
    fi

    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi

    if command -v python >/dev/null 2>&1; then
        command -v python
        return 0
    fi

    return 1
}

LOCK_ACQUIRED=0
cleanup_lock() {
    if [ "$LOCK_ACQUIRED" -eq 1 ]; then
        rm -f "$LOCK_PID_FILE" 2>/dev/null || true
        rmdir "$LOCK_FILE" 2>/dev/null || true
        LOCK_ACQUIRED=0
    fi
}

acquire_lock() {
    if [ "$LOCK_ACQUIRED" -eq 1 ]; then
        return 0
    fi

    # Backward compatibility: older versions used LOCK_FILE as a plain file (flock).
    # If that legacy file exists, remove it so directory-based locking can work.
    if [ -e "$LOCK_FILE" ] && [ ! -d "$LOCK_FILE" ]; then
        rm -f "$LOCK_FILE" 2>/dev/null || true
    fi

    # Portable lock: atomic mkdir works on both macOS and Linux.
    if mkdir "$LOCK_FILE" 2>/dev/null; then
        echo "$$" > "$LOCK_PID_FILE"
        LOCK_ACQUIRED=1
        trap cleanup_lock EXIT
        return 0
    fi

    # If lock exists, check whether owner is stale.
    if [ -f "$LOCK_PID_FILE" ]; then
        local lock_pid
        lock_pid=$(cat "$LOCK_PID_FILE" 2>/dev/null || true)
        if [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; then
            log_warn "Removing stale lock held by dead PID: $lock_pid"
            rm -f "$LOCK_PID_FILE" 2>/dev/null || true
            rmdir "$LOCK_FILE" 2>/dev/null || true
            if mkdir "$LOCK_FILE" 2>/dev/null; then
                echo "$$" > "$LOCK_PID_FILE"
                LOCK_ACQUIRED=1
                trap cleanup_lock EXIT
                return 0
            fi
        fi
    fi

    log_error "Another cowork-api.sh operation is in progress"
    exit 1
}

clean_stale_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || true)
        if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
            log_warn "Removing stale PID file"
            rm -f "$PID_FILE"
        fi
    fi
}

is_running() {
    clean_stale_pid
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || true)
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
    else
        return 1
    fi
}

find_port_pids_for() {
    local target_port="$1"
    # lsof works on macOS/Linux and is the safest way to identify listeners.
    lsof -tiTCP:"$target_port" -sTCP:LISTEN 2>/dev/null || true
}

find_port_pids() {
    find_port_pids_for "$PORT"
}

port_is_in_use() {
    local target_port="$1"

    if command -v lsof >/dev/null 2>&1; then
        [ -n "$(find_port_pids_for "$target_port")" ]
        return
    fi
    if command -v nc >/dev/null 2>&1; then
        nc -z 127.0.0.1 "$target_port" >/dev/null 2>&1
        return
    fi

    # The Python server performs the same bind check before native startup.
    # Docker will still return an actionable bind error when neither probe is
    # available on the host.
    return 1
}

select_local_port() {
    local preferred_port="${1:-5002}"

    if [ "$preferred_port" != "5002" ]; then
        if port_is_in_use "$preferred_port"; then
            log_error "Configured local port $preferred_port is already in use" >&2
            return 1
        fi
        printf '%s\n' "$preferred_port"
        return 0
    fi

    if ! port_is_in_use "5002"; then
        printf '5002\n'
        return 0
    fi

    if ! port_is_in_use "5003"; then
        log_warn "Local port 5002 is already in use; using 5003" >&2
        printf '5003\n'
        return 0
    fi

    log_error "Local ports 5002 and 5003 are both in use; set PORT to another port" >&2
    return 1
}

find_orphan_server_pids() {
    local managed_pid=""
    if [ -f "$PID_FILE" ]; then
        managed_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    fi

    local candidates
    candidates=$(( pgrep -f "python.*server.py" 2>/dev/null; pgrep -f "uvicorn.*server:app" 2>/dev/null ) | sort -u || true)
    [ -z "$candidates" ] && return 0

    for pid in $candidates; do
        [ -n "$managed_pid" ] && [ "$pid" = "$managed_pid" ] && continue
        echo "$pid"
    done
}

kill_pid_graceful() {
    local pid="$1"
    [ -z "$pid" ] && return 0
    kill "$pid" 2>/dev/null || true
}

kill_pid_force_if_alive() {
    local pid="$1"
    [ -z "$pid" ] && return 0
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
}

kill_process_tree() {
    local root_pid="$1"
    [ -z "$root_pid" ] && return 0

    # Kill known children first, then the parent.
    pkill -P "$root_pid" 2>/dev/null || true
    kill_pid_graceful "$root_pid"
}

wait_for_port_release() {
    local max_wait="${1:-10}"
    for _ in $(seq 1 "$max_wait"); do
        if [ -z "$(find_port_pids)" ]; then
            return 0
        fi
        sleep 1
    done
    log_warn "Port $PORT still appears busy after ${max_wait}s"
    return 1
}

kill_hindering_processes() {
    local pids
    pids="$(find_port_pids)"
    if [ -n "$pids" ]; then
        log_warn "Found process(es) listening on port $PORT: $pids"
        for pid in $pids; do
            kill_process_tree "$pid"
        done
        sleep 1
        for pid in $pids; do
            kill_pid_force_if_alive "$pid"
        done
    fi

    local orphans
    orphans="$(find_orphan_server_pids)"
    if [ -n "$orphans" ]; then
        log_warn "Found orphan API process(es): $orphans"
        for pid in $orphans; do
            kill_process_tree "$pid"
        done
        sleep 1
        for pid in $orphans; do
            kill_pid_force_if_alive "$pid"
        done
    fi
}

start_api() {
    acquire_lock
    if is_running; then
        echo -e "${YELLOW}XO Space API is already running (PID: $(cat "$PID_FILE"))${NC}"
        return 0
    fi

    kill_hindering_processes
    wait_for_port_release 10 || true

    local python_cmd
    python_cmd="$(resolve_python_cmd || true)"
    if [ -z "$python_cmd" ]; then
        log_error "No Python interpreter found (tried: venv/bin/python, python3, python)"
        return 1
    fi

    log "Starting XO Space API on ${HOST}:${PORT}..."
    nohup bash -c '
        cd "'"$SCRIPT_DIR"'"
        export HOST="'"$HOST"'"
        export PORT="'"$PORT"'"
        "'"$python_cmd"'" server.py
    ' >> "$LOG_FILE" 2>&1 &

    local pid=$!
    echo "$pid" > "$PID_FILE"
    sleep 1

    if kill -0 "$pid" 2>/dev/null; then
        log_success "XO Space API started (PID: $pid, port: $PORT)"
        echo -e "Logs: ${CYAN}$LOG_FILE${NC}"
    else
        rm -f "$PID_FILE"
        log_error "Failed to start XO Space API. Check logs: $LOG_FILE"
        return 1
    fi
}

stop_api() {
    acquire_lock
    if is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        log "Stopping XO Space API (PID: $pid)..."
        kill_process_tree "$pid"

        for _ in $(seq 1 10); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done

        kill_pid_force_if_alive "$pid"
        rm -f "$PID_FILE"
    else
        log_warn "Managed API process not running"
    fi

    kill_hindering_processes
    wait_for_port_release 10 || true
    log_success "XO Space API stopped"
}

restart_api() {
    acquire_lock
    stop_api
    sleep 1
    start_api
}

status_api() {
    if is_running; then
        echo -e "${GREEN}XO Space API is running${NC} (PID: $(cat "$PID_FILE"))"
    else
        echo -e "${RED}XO Space API is not running${NC}"
    fi

    local listeners
    listeners="$(find_port_pids)"
    if [ -n "$listeners" ]; then
        echo -e "${YELLOW}Port $PORT listener PID(s):${NC} $listeners"
    else
        echo -e "${CYAN}No process currently listening on port $PORT${NC}"
    fi
}

show_logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        echo "No log file found at $LOG_FILE"
    fi
}

install_deps() {
    local venv_dir="$SCRIPT_DIR/venv"
    local req="$SCRIPT_DIR/requirements.txt"

    if [ ! -f "$req" ]; then
        log_error "requirements.txt not found at $req"
        return 1
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        log_error "python3 not found; install Python 3.12 or newer first"
        return 1
    fi

    if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
        log_error "Python 3.12 or newer is required"
        return 1
    fi

    if [ ! -x "$venv_dir/bin/python" ]; then
        log "Creating virtualenv at $venv_dir..."
        if ! python3 -m venv "$venv_dir"; then
            log_error "Failed to create venv. On Debian/Ubuntu try: sudo apt-get install -y python3-venv"
            return 1
        fi
    fi

    if ! "$venv_dir/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
        log_error "Existing venv uses Python older than 3.12; recreate venv with a supported interpreter"
        return 1
    fi

    log "Installing dependencies from requirements.txt..."
    if ! "$venv_dir/bin/python" -m pip install -U pip; then
        log_error "pip upgrade failed"
        return 1
    fi
    if ! "$venv_dir/bin/python" -m pip install -r "$req"; then
        log_error "pip install -r requirements.txt failed"
        return 1
    fi
    log_success "Dependencies installed (venv: $venv_dir)"
}

run_dev() {
    local dev_host="${CONFIGURED_HOST:-127.0.0.1}"
    local preferred_port="${CONFIGURED_PORT:-5002}"
    local dev_port
    local python_cmd

    install_deps || return 1
    dev_port="$(select_local_port "$preferred_port")" || return 1
    python_cmd="$(resolve_python_cmd || true)"
    if [ -z "$python_cmd" ]; then
        log_error "No Python interpreter found after installation"
        return 1
    fi

    log_success "Local environment is ready"
    if [ -f "$ENV_FILE" ]; then
        log "Using project configuration from $ENV_FILE"
    else
        log_warn "No .env found; using safe local defaults"
        log "Copy .env.example to .env when you need runtime or connector settings"
    fi
    log "Starting development server at http://${dev_host}:${dev_port}/space/"
    log "Press Ctrl+C to stop"

    cd "$SCRIPT_DIR" || return 1
    HOST="$dev_host" \
    PORT="$dev_port" \
    STAGE=local \
    UVICORN_RELOAD=true \
    exec "$python_cmd" server.py
}

case "${1:-restart}" in
    dev)     run_dev ;;
    install) install_deps ;;
    start)   start_api ;;
    stop)    stop_api ;;
    restart) restart_api ;;
    status)  status_api ;;
    logs)    show_logs ;;
    *)
        echo "Usage: $0 {dev|install|start|stop|restart|status|logs}"
        exit 1
        ;;
esac
