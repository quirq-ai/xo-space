#!/usr/bin/env bash
# ==============================================================
# install.sh — set up and run Quirq natively, without Docker.
#
# Replaces the previous Docker installer. Instead of pulling an image
# and `docker run`ing it, this prepares a virtual environment with uv
# and runs `server.py` directly in the foreground, the way you would
# during normal local development. Docker Desktop is no longer a
# prerequisite; git and a network connection are.
#
# Two ways to use it, chosen automatically:
#
#   From a checkout — uses that working tree in place and never runs
#   a git command against it, so your local edits stay yours:
#       git clone https://github.com/quirq-ai/xo-space
#       cd xo-space && ./install.sh
#
#   Standalone — clones into ./<repo-name> and runs from there.
#   Re-running fast-forwards that checkout, so this doubles as the
#   update path:
#       curl -fsSL <raw-url>/install.sh | bash
#
# The directory you launch from becomes the workspace: the checkout
# lands beside your projects, and machine state goes in ./.quirq
#
# The server runs in the foreground with a quiet terminal: its output
# appends to <state root>/quirq.log, Ctrl-C stops it, and re-running
# updates and restarts. Agents wanting a session-scoped background server
# run this same command as a background task of their harness — process
# lifecycle belongs to whoever launched it, so there is no daemon mode,
# no PID file, and no stop subcommand.
#
# Root directory precedence:
#     XO_PROJECTS_ROOT / QUIRQ_STATE_ROOT exported in the caller's shell
#         → roots.env saved by the Setup tab
#         → the checkout's .env from the first install
#         → the launch directory, and ./.quirq inside it
#
# Every environment value below is overridable from the caller's
# environment, e.g.  PORT=8080 ./install.sh
#
# Note it must be piped to `bash`, not `sh`: it uses BASH_SOURCE and
# `set -o pipefail`, neither of which exists in POSIX sh (dash).
# ==============================================================

set -Eeuo pipefail

SOURCE_REPO="${QUIRQ_SOURCE_REPO:-https://github.com/quirq-ai/xo-space.git}"
# main, not development: this is what the public one-liner clones, so it must
# be the reviewed branch. Set QUIRQ_SOURCE_REF=development to track dev.
SOURCE_REF="${QUIRQ_SOURCE_REF:-main}"
# Captured before anything cd's. Everything Quirq creates hangs off this, so
# a run is self-contained in the directory you launched it from.
LAUNCH_DIR="$PWD"
# Captured before .env is loaded into the environment, so the resolution
# below can tell a root the caller exported (beats everything) apart from
# one that merely came out of the checkout's .env (loses to roots.env —
# the Setup tab's saved roots must actually apply on the next run).
SHELL_XO_PROJECTS_ROOT="${XO_PROJECTS_ROOT:-}"
SHELL_QUIRQ_STATE_ROOT="${QUIRQ_STATE_ROOT:-}"
# Named after the repository, the way a plain `git clone` would name it, so
# the directory follows QUIRQ_SOURCE_REPO instead of hardcoding one name.
REPO_NAME="${SOURCE_REPO##*/}"
REPO_NAME="${REPO_NAME%.git}"
APP_DIR="${QUIRQ_APP_DIR:-${LAUNCH_DIR}/${REPO_NAME}}"
PYTHON_VERSION="${QUIRQ_PYTHON_VERSION:-3.12}"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"
# The canonical one-liner (INSTALLATION.md), echoed back as the update path.
INSTALL_URL="https://quirq.ai/install"

# Set by resolve_repo_dir, once we know whether we are running from a
# checkout or have to create one.
REPO_DIR=""
VENV_DIR=""
VENV_PYTHON=""
MANAGED_CHECKOUT=0

fail() {
    printf '\nQuirq: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "$2"
}

# ==============================================================
# Where the source lives. Two modes, decided by one question: is
# this script sitting inside a Quirq checkout?
#
#   Yes → in-place. That working tree belongs to the user; we read
#         it and never run a git command against it.
#   No  → managed. The script was piped from curl or downloaded on
#         its own, so we own a checkout at APP_DIR and keep it
#         current, the way the old installer owned its container.
#
# The probe is `-f` on BASH_SOURCE rather than a git check, because
# a script read from stdin names no real file — which is exactly
# the case that has to fall through to managed mode.
# ==============================================================
resolve_repo_dir() {
    local source_path="${BASH_SOURCE[0]:-}"
    local script_dir=""

    if [ -n "$source_path" ] && [ -f "$source_path" ]; then
        script_dir="$(cd "$(dirname "$source_path")" && pwd)"
    fi

    if [ -n "$script_dir" ] &&
        [ -f "${script_dir}/server.py" ] &&
        [ -f "${script_dir}/requirements.txt" ]; then
        REPO_DIR="$script_dir"
        MANAGED_CHECKOUT=0
        return
    fi

    # Piped from curl while standing inside a checkout — typically someone
    # re-running the one-liner from ./xo-space instead of from the workspace
    # above it. Nesting a second clone in there would make this checkout the
    # projects root and leave it permanently dirty (so it would never update
    # again). Use it as the managed checkout and its parent as the workspace:
    # exactly what running the same command one level up does. An explicit
    # QUIRQ_APP_DIR still wins.
    if [ -z "${QUIRQ_APP_DIR:-}" ] &&
        [ -f "${LAUNCH_DIR}/server.py" ] &&
        [ -f "${LAUNCH_DIR}/requirements.txt" ]; then
        REPO_DIR="$LAUNCH_DIR"
        LAUNCH_DIR="$(cd "${LAUNCH_DIR}/.." && pwd)"
        MANAGED_CHECKOUT=1
        printf 'Running from inside the Quirq checkout; the workspace is %s\n' "$LAUNCH_DIR"
        return
    fi

    REPO_DIR="$APP_DIR"
    MANAGED_CHECKOUT=1
}

# Clone on first run, fast-forward afterwards. Only ever called in
# managed mode. A dirty tree stops the update instead of discarding
# work: an installer that silently resets someone's edits is worse
# than one that is a version behind.
fetch_repo() {
    if [ -d "${REPO_DIR}/.git" ]; then
        printf 'Quirq is already installed here: %s\n' "$REPO_DIR"
        if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
            printf 'It has local changes — keeping them, skipping the update.\n'
            return
        fi
        # A checkout on some other branch was put there on purpose (a
        # development clone, say); resetting it to SOURCE_REF would silently
        # move it. Managed clones are made with --branch SOURCE_REF, so they
        # always pass this and keep updating.
        local branch
        branch="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
        if [ -n "$branch" ] && [ "$branch" != "HEAD" ] && [ "$branch" != "$SOURCE_REF" ]; then
            printf 'It is on branch %s, not %s — leaving it as is. Set QUIRQ_SOURCE_REF=%s to track that branch instead.\n' \
                "$branch" "$SOURCE_REF" "$branch"
            return
        fi
        printf 'Updating it to the latest %s...\n' "$SOURCE_REF"
        git -C "$REPO_DIR" fetch --quiet --depth 1 origin "$SOURCE_REF" ||
            fail "Could not fetch ${SOURCE_REF} from ${SOURCE_REPO}."
        git -C "$REPO_DIR" reset --hard --quiet FETCH_HEAD ||
            fail "Could not update the checkout in ${REPO_DIR}."
        return
    fi

    [ ! -e "$REPO_DIR" ] || fail \
        "${REPO_DIR} already exists and is not a Quirq checkout. Move it aside, or set QUIRQ_APP_DIR to another path."

    printf 'Downloading Quirq into %s...\n' "$REPO_DIR"
    git clone --quiet --depth 1 --branch "$SOURCE_REF" "$SOURCE_REPO" "$REPO_DIR" ||
        fail "Could not clone ${SOURCE_REPO} (${SOURCE_REF})."
}

# ==============================================================
# uv — the only tool this script installs for you. Everything else
# is either reported (optional runtime tools) or required up front
# (git). uv installs into ~/.local/bin without sudo and brings its
# own managed CPython, so the host needs no system Python.
# ==============================================================
ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return
    fi

    # Installed previously but not on this shell's PATH.
    if [ -x "${HOME}/.local/bin/uv" ]; then
        PATH="${HOME}/.local/bin:${PATH}"
        export PATH
        return
    fi

    require_command curl \
        "curl is required to install uv. Install uv yourself instead: https://docs.astral.sh/uv/getting-started/installation/"

    printf 'Installing uv...\n'
    curl -LsSf "$UV_INSTALL_URL" | sh >/dev/null ||
        fail "Could not install uv. Install it manually: https://docs.astral.sh/uv/getting-started/installation/"

    PATH="${HOME}/.local/bin:${PATH}"
    export PATH
    command -v uv >/dev/null 2>&1 ||
        fail "uv was installed but is not on PATH. Add its install directory to PATH and run this script again."
}

# ==============================================================
# Python environment. Lives at venv/ rather than uv's default
# .venv/ because CLAUDE.md, DEVELOPING.md and compose.local.yml all
# document venv/bin/python as this project's interpreter path.
#
# Only created when absent, so repeat runs skip straight to the
# dependency sync. To rebuild from scratch: rm -rf venv
# ==============================================================
sync_dependencies() {
    if [ ! -x "$VENV_PYTHON" ]; then
        printf 'Creating the Python %s environment in %s...\n' "$PYTHON_VERSION" "$VENV_DIR"
        uv venv "$VENV_DIR" --python "$PYTHON_VERSION" ||
            fail "Could not create the virtual environment at ${VENV_DIR}."
    fi

    printf 'Installing dependencies...\n'
    uv pip install --quiet --python "$VENV_PYTHON" --requirement "${REPO_DIR}/requirements.txt" ||
        fail "Could not install the dependencies in requirements.txt."
}

# ==============================================================
# Optional runtime tools. The Docker image apt-installed these; a
# native run inherits whatever the host has. The server already
# degrades non-fatally without each of them, so this reports and
# continues rather than adding a second, divergent gate.
# ==============================================================
report_tool() {
    local binary="$1"
    local purpose="$2"

    if command -v "$binary" >/dev/null 2>&1; then
        printf '  present  %-7s %s\n' "$binary" "$purpose"
    else
        printf '  MISSING  %-7s %s\n' "$binary" "$purpose"
    fi
}

check_optional_tools() {
    printf '\nOptional tools:\n'
    report_tool node   "installs the agent CLI at boot"
    report_tool npm    "installs the agent CLI at boot"
    report_tool gh     "xo-projects-sync backup repositories"
    report_tool rclone "Google Drive and OneDrive connectors"
    report_tool gpg    "encrypted backup and restore"
    printf '  Anything marked MISSING disables only the feature next to it.\n'
}

# ==============================================================
# Transparency — what this install sends anywhere, printed on
# every run so it is never a surprise. Keep in step with the
# README section "What leaves your machine".
# ==============================================================
print_reporting_notice() {
    local log_file="$1"
    cat <<'NOTICE'

┌─ Usage reporting ───────────────────────────────────────────────────────┐
│ Quirq reports usage to xo-swarm-api ONLY when XO_API_KEY in .env is set │
│ and valid. Empty, missing, or invalid key: nothing is tracked.          │
│                                                                         │
│ Sent once a day when the key is set:      Never sent:                   │
│   • token counts (input/output/cache)       • prompts or responses      │
│   • estimated cost                          • file contents             │
│   • message, session, tool-call counts      • file paths                │
│   • per-model and per-tool breakdown        • anything, without the key │
│   • workspace id/name, project id                                       │
│                                                                         │
│ Details: README → "What leaves your machine".                           │
└─────────────────────────────────────────────────────────────────────────┘
NOTICE
    # The server logs every decision it makes about reporting; this is the
    # one-liner that shows them, since the terminal itself stays quiet.
    printf '  See what it decided:  grep usage_sync %s\n' "$log_file"
}

# ==============================================================
# Root directories — copied from install.sh so the two scripts
# resolve, validate and relocate roots identically.
# ==============================================================
saved_root_from_file() {
    local state_root="$1"
    local key="$2"
    local line
    local found=""
    local config_file="${state_root}/roots.env"

    [ -f "$config_file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "${key}="*)
                found="${line#*=}"
                ;;
        esac
    done < "$config_file"
    printf '%s' "$found"
}

validate_host_root() {
    local label="$1"
    local path="$2"

    case "$path" in
        /*) ;;
        *) fail "${label} must be an absolute path: ${path}" ;;
    esac
    [ "$path" != "/" ] || fail "${label} cannot be the filesystem root."
}

validate_separate_roots() {
    local projects_root="$1"
    local state_root="$2"
    local relative

    case "${state_root}/" in
        "${projects_root}/"*)
            # One nesting is allowed: the state root as a hidden directory
            # directly inside the projects root, which is the default layout
            # (./ and ./.quirq). quirq_catalog skips dot-prefixed entries when
            # it enumerates projects, so ".quirq" there can never be mistaken
            # for one. Anything deeper, or not hidden, would be — and equal
            # roots fall through to the failure too.
            relative="${state_root#"${projects_root}/"}"
            case "$relative" in
                */*) fail "Quirq root cannot be nested inside XO root." ;;
                .?*) ;;
                *) fail "Quirq root cannot be inside XO root." ;;
            esac
            ;;
    esac
    case "${projects_root}/" in
        "${state_root}/"*) fail "XO root cannot be inside Quirq root." ;;
    esac
}

prepare_state_root() {
    local previous_state_root="$1"
    local state_root="$2"

    mkdir -p "$state_root"
    if [ -z "$previous_state_root" ] || [ "$previous_state_root" = "$state_root" ]; then
        return
    fi
    [ -d "$previous_state_root" ] || return
    if [ -z "$(find "$state_root" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
        printf 'Copying existing Quirq state to %s...\n' "$state_root"
        cp -R -p "$previous_state_root/." "$state_root/"
    else
        printf 'Using existing Quirq state at %s (no files were merged).\n' "$state_root"
    fi
}

# ==============================================================
# Port. utils/local_port.py only auto-falls-back for the historical
# default 5002, so an explicit port is handed straight to Uvicorn.
# Check it here to fail with something actionable instead of a
# traceback — reusing the app's own bind check rather than a second
# implementation that could disagree with it.
# ==============================================================
ensure_port_available() {
    local host="$1"
    local port="$2"
    local status=0

    QUIRQ_CHECK_HOST="$host" QUIRQ_CHECK_PORT="$port" \
        "$VENV_PYTHON" - <<'PY' >/dev/null 2>&1 || status=$?
import os
import sys

from utils.local_port import is_port_available

host = os.environ["QUIRQ_CHECK_HOST"]
port = int(os.environ["QUIRQ_CHECK_PORT"])
sys.exit(0 if is_port_available(host, port) else 1)
PY

    case "$status" in
        0) return 0 ;;
        1) fail "Port ${port} is already in use — Quirq may already be running here. Stop it with Ctrl-C in the terminal running it (find a stray one with: lsof -i :${port}), or set PORT=<port> to start a second one." ;;
        *) printf 'Could not verify that port %s is free; starting anyway.\n' "$port" ;;
    esac
}

# ==============================================================
# Environment contract. This is install.sh's `docker run --env`
# block, minus everything that existed only to bridge the container
# boundary:
#
#   QUIRQ_HOST_HOME / _PROJECTS_ROOT / _STATE_ROOT  translated
#       container paths back to host paths. Running natively they
#       are the same path, and runtime_config renders the single
#       true path when they are unset.
#   QUIRQ_MANAGED_CONTAINER / QUIRQ_ALLOW_SELF_RESTART  both
#       default to false, so the Setup tab correctly reports that
#       it cannot restart this process instead of offering a
#       control that would fail.
#
# AGENT_NAME defaults to claude_code here, where the Docker installer
# left it unset. Without it the server still boots — agent_registry safe-boots
# to openclaw — but _run_agent_setup reads AGENT_NAME directly with no
# fallback, so nothing would install a CLI and no backend would work.
# Setting it makes the Docker-free path self-sufficient on first run.
# It stays overridable, and runtime.env (loaded with override=True)
# still wins, so the Setup tab remains authoritative.
#
# AI_PROVIDER is left alone: server.py already defaults it to claude.
# ==============================================================
# ==============================================================
# .env — the checkout's own configuration file.
#
# Read before anything is resolved so your edits actually drive the run,
# then written once on first install so there is something to edit.
#
# Precedence, highest first:
#     1. variables exported in your shell   (PORT=8080 ./install.sh)
#     2. this .env
#     3. the defaults below
#
# which matches how the server itself loads it: python-dotenv's plain
# load_dotenv() fills only what the environment left unset. The Setup tab's
# runtime.env and secrets.env are loaded with override=True and beat all
# three, so the tab stays authoritative for anything it manages.
# ==============================================================

# Keys install.sh exports. Only these can shadow the file, so only these have
# to be read back out of it; anything else in .env reaches the server
# untouched through load_dotenv().
ENV_KEYS="HOST PORT STAGE AGENT_NAME UVICORN_RELOAD QUIRQ_SKIP_BOOT_INSTALL
XO_PROJECTS_ROOT AI_WORKSPACE_ROOT QUIRQ_STATE_ROOT QUIRQ_WATCHER_SOURCE_MODE
QUIRQ_PUBLIC_URL QUIRQ_RUNTIME_FILE QUIRQ_SECRETS_FILE STARTUP_WARMUP_URL"

read_env_value() {
    local key="$1"
    local file="${REPO_DIR}/.env"
    local line
    local found=""

    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "${key}="*)
                found="${line#*=}"
                found="${found%%[[:space:]]#*}"                  # trailing comment
                found="${found#"${found%%[![:space:]]*}"}"       # trim left
                found="${found%"${found##*[![:space:]]}"}"       # trim right
                case "$found" in
                    \"*\") found="${found#\"}"; found="${found%\"}" ;;
                    \'*\') found="${found#\'}"; found="${found%\'}" ;;
                esac
                ;;
        esac
    done < "$file"
    printf '%s' "$found"
}

load_env_file() {
    local key
    local value

    for key in $ENV_KEYS; do
        # Already set in the caller's environment — leave it alone, so an
        # explicit `PORT=8080 ./install.sh` still wins over the file.
        [ -z "${!key:-}" ] || continue
        value="$(read_env_value "$key")"
        [ -n "$value" ] || continue
        export "${key}=${value}"
    done
}

# Written once, then never touched again: it is the user's file after that,
# and an installer that rewrites it every run would discard hand-edited
# credentials on the next update.
#
# Credentials are written commented out rather than empty on purpose. An
# empty assignment is not the same as absent — `CHAT_API_BASE_URL=` would
# make os.getenv return "" instead of falling back to server.py's default,
# silently pointing the server at nothing.
write_env_file() {
    local env_file="${REPO_DIR}/.env"

    [ ! -f "$env_file" ] || return 0

    umask 077
    cat > "$env_file" <<ENVEOF
# Quirq configuration, created by install.sh on first run.
#
# Edit, then re-run ./install.sh to apply. Variables exported in your shell
# still win over this file, and the Setup tab's runtime.env / secrets.env
# override both.
#
# Gitignored, and never rewritten once it exists.

# --- Server ---
HOST=${HOST}
PORT=${PORT}
STAGE=${STAGE}
UVICORN_RELOAD=${UVICORN_RELOAD}

# --- Agent backend ---
AGENT_NAME=${AGENT_NAME}

# Install nothing beyond requirements.txt. Set to 0 to let the boot hooks
# apt-install, fetch Node via nvm, and npm -g the agent CLI.
QUIRQ_SKIP_BOOT_INSTALL=${QUIRQ_SKIP_BOOT_INSTALL}

# --- Paths ---
XO_PROJECTS_ROOT=${XO_PROJECTS_ROOT}
AI_WORKSPACE_ROOT=${AI_WORKSPACE_ROOT}
QUIRQ_STATE_ROOT=${QUIRQ_STATE_ROOT}
QUIRQ_WATCHER_SOURCE_MODE=${QUIRQ_WATCHER_SOURCE_MODE}

# --- Credentials ---
# Uncomment and fill in, or configure them through the Setup tab instead.
# Leave them commented rather than blank: an empty value overrides the
# server's own default with an empty string.
# ANTHROPIC_API_KEY=
# CLAUDE_CODE_OAUTH_TOKEN=
# Usage is reported to xo-swarm-api ONLY when this key is set and valid:
# token counts, costs and session/tool counts once a day — never prompts or
# file contents. Empty, missing or invalid: nothing is tracked. See the
# README section "What leaves your machine".
# XO_API_KEY=
# CHAT_API_BASE_URL=https://api-swarm-beta.xo.builders
ENVEOF
    chmod 600 "$env_file"
    printf 'Wrote %s (mode 600) — edit it to change this install.\n' "$env_file"
}

start_server() {
    local projects_root="$1"
    local state_root="$2"

    export HOST
    export PORT
    export STAGE="${STAGE:-local}"
    export AGENT_NAME="${AGENT_NAME:-claude_code}"
    # This runs on someone's own machine, not a disposable container, so the
    # boot hooks must not apt-install, curl-pipe nvm, or npm -g anything.
    # requirements.txt inside venv/ is the whole footprint. Set it to 0 to opt
    # back into the old container behaviour.
    export QUIRQ_SKIP_BOOT_INSTALL="${QUIRQ_SKIP_BOOT_INSTALL:-1}"
    export UVICORN_RELOAD="${UVICORN_RELOAD:-false}"
    export STARTUP_WARMUP_URL="${STARTUP_WARMUP_URL:-http://127.0.0.1:${PORT}}"
    export XO_PROJECTS_ROOT="$projects_root"
    export AI_WORKSPACE_ROOT="${AI_WORKSPACE_ROOT:-$projects_root}"
    export QUIRQ_STATE_ROOT="$state_root"
    export QUIRQ_RUNTIME_FILE="${QUIRQ_RUNTIME_FILE:-${state_root}/runtime.env}"
    export QUIRQ_SECRETS_FILE="${QUIRQ_SECRETS_FILE:-${state_root}/secrets.env}"
    export QUIRQ_WATCHER_SOURCE_MODE="${QUIRQ_WATCHER_SOURCE_MODE:-all}"
    export QUIRQ_PUBLIC_URL="${QUIRQ_PUBLIC_URL:-http://localhost:${PORT}}"

    # Written here, after every value is resolved, so the file records the
    # configuration this install actually ran with.
    write_env_file

    # One mode: the server owns the terminal — Ctrl-C stops it, closing the
    # terminal takes it down — while its output appends to the log file so
    # the terminal stays quiet. Agents that want a session-scoped background
    # server run this same command as a background task of their harness;
    # process lifecycle is the harness's job, not this script's, so there is
    # no daemonizing, no PID file, and nothing to leak.
    local log_file="${state_root}/quirq.log"

    printf '\n▶️  Starting Quirq: http://localhost:%s/space/\n\n' "$PORT"
    printf '    Logs:  %s\n' "$log_file"
    printf '    Press Ctrl-C to stop.\n\n'
    print_restart_hint

    # exec replaces this shell so Ctrl-C reaches Uvicorn directly and no
    # wrapper lingers. If the server dies at boot, the prompt returns
    # silently — the log above has the reason.
    exec "$VENV_PYTHON" server.py >> "$log_file" 2>&1
}

# ==============================================================
# How to come back. Once Ctrl-C returns the prompt there is nothing on
# screen that says how to start again, and "run the installer command"
# is only obvious to whoever ran it first. Print-only: start_server owns
# the exec, and this must run before it, since nothing of this script
# survives the exec.
# ==============================================================
print_restart_hint() {
    # The ref goes on `sh`, not on `curl`: it is the bootstrap that reads it,
    # and with a pipe an assignment binds to the command it precedes.
    local ref_prefix=""
    [ "$SOURCE_REF" = "main" ] || ref_prefix="QUIRQ_SOURCE_REF=${SOURCE_REF} "

    if [ "$MANAGED_CHECKOUT" -eq 1 ]; then
        printf '    Start again later:  cd %s && %s/install.sh\n' "$LAUNCH_DIR" "$REPO_DIR"
        printf '    Update and start:   cd %s && curl -fsSL %s | %ssh\n\n' \
            "$LAUNCH_DIR" "$INSTALL_URL" "$ref_prefix"
    else
        printf '    Start again later:  cd %s && ./install.sh\n' "$REPO_DIR"
        printf '    Update:             Setup tab → Update, or git pull --ff-only, then start again\n\n'
    fi
}

main() {
    local user_home="${HOME:-}"
    local anchor_dir
    local saved_projects_root
    local saved_state_root
    local projects_root
    local state_root
    local source_label="in place"

    [ -n "$user_home" ] || fail "HOME must be set."

    resolve_repo_dir

    require_command git \
        "git is not installed. Quirq needs it to download itself, and uses it at runtime for project sync and history. Install git and run this script again."

    if [ "$MANAGED_CHECKOUT" -eq 1 ]; then
        source_label="managed"
        fetch_repo
    fi

    cd "$REPO_DIR"
    VENV_DIR="${REPO_DIR}/venv"
    VENV_PYTHON="${VENV_DIR}/bin/python"

    # Before any default is applied, so .env drives the resolution below
    # rather than being shadowed by it.
    load_env_file

    HOST="${HOST:-127.0.0.1}"
    PORT="${PORT:-5002}"

    anchor_dir="${QUIRQ_STATE_ROOT:-${LAUNCH_DIR}/.quirq}"
    saved_projects_root="$(saved_root_from_file "$anchor_dir" "XO_PROJECTS_ROOT")"
    saved_state_root="$(saved_root_from_file "$anchor_dir" "QUIRQ_STATE_ROOT")"
    projects_root="${SHELL_XO_PROJECTS_ROOT:-${saved_projects_root:-${XO_PROJECTS_ROOT:-${LAUNCH_DIR}}}}"
    state_root="${SHELL_QUIRQ_STATE_ROOT:-${saved_state_root:-${QUIRQ_STATE_ROOT:-${LAUNCH_DIR}/.quirq}}}"
    validate_host_root "XO root" "$projects_root"
    validate_host_root "Quirq root" "$state_root"
    validate_separate_roots "$projects_root" "$state_root"

    # The checkout must not sit inside the Quirq root. prepare_state_root
    # copies that directory wholesale when it relocates, and dragging a
    # git checkout along with it would be both slow and wrong.
    case "${REPO_DIR}/" in
        "${state_root}/"*) fail \
            "The Quirq checkout cannot live inside the Quirq root (${state_root}). Set QUIRQ_APP_DIR to a path outside it." ;;
    esac

    ensure_uv
    sync_dependencies

    mkdir -p "$projects_root"
    prepare_state_root "$anchor_dir" "$state_root"

    printf '\nQuirq source: %s (%s)\nXO projects: %s\nQuirq state: %s\n' \
        "$REPO_DIR" "$source_label" "$projects_root" "$state_root"
    check_optional_tools
    print_reporting_notice "${state_root}/quirq.log"

    ensure_port_available "$HOST" "$PORT"
    start_server "$projects_root" "$state_root"
}

main "$@"
