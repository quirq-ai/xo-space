#!/usr/bin/env bash
# Codex plugin entry point. The plugin cache holds only this launcher;
# the checkout, virtualenv and state belong to the chosen workspace.
set -Eeuo pipefail

fail() { printf 'XO Space: %s\n' "$*" >&2; exit 1; }

usage() {
    printf 'Usage: bash space.sh install <absolute-workspace>\n       bash space.sh start <absolute-checkout>\n' >&2
    exit 2
}

absolute_directory() {
    case "$1" in
        /*) ;;
        *) fail "Use an absolute directory path (expand ~ before calling this script)." ;;
    esac
    case "$1" in
        /|*$'\n'*|*$'\r'*) fail "Use a workspace directory other than /, without line breaks." ;;
    esac
    case "/${1#/}/" in
        */../*|*/./*) fail "Use an absolute path without . or .. path segments." ;;
    esac
}

check_codex() {
    local cli="${CODEX_CLI_PATH:-codex}"
    local resolved
    resolved="$(command -v "$cli" 2>/dev/null)" ||
        fail "Codex CLI was not found. Install the Codex CLI or set CODEX_CLI_PATH to its executable, then retry."
    [ -x "$resolved" ] && "$resolved" --version >/dev/null 2>&1 ||
        fail "Codex CLI at ${resolved} could not run. Check CODEX_CLI_PATH or repair the CLI installation."
    # The server's adapter honors this, including an app-bundled executable
    # which is available to Codex but absent from the server's PATH.
    export CODEX_CLI_PATH="$resolved"
}

install_space() {
    local workspace="$1"
    local repo
    local bundle_root
    local existing
    local suffix=""
    absolute_directory "$workspace"
    command -v git >/dev/null 2>&1 || fail "git is required to install XO Space."
    check_codex

    bundle_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
    # Resolve the nearest existing ancestor before creating any directory.
    # A symlink into the cache must not make even an empty workspace there.
    existing="${workspace%/}"
    while [ ! -d "$existing" ]; do
        [ ! -e "$existing" ] && [ ! -L "$existing" ] || fail "Not a directory: ${existing}"
        suffix="/$(basename "$existing")${suffix}"
        existing="$(dirname "$existing")"
    done
    workspace="$(cd "$existing" && pwd -P)${suffix}"
    case "${workspace}/" in
        "${bundle_root}/"*) fail "Choose a durable workspace outside the plugin cache: ${workspace}" ;;
    esac
    [ "$workspace" != / ] || fail "The filesystem root cannot be the workspace."
    repo="${workspace}/xo-space"
    [ ! -e "$repo" ] && [ ! -L "$repo" ] ||
        fail "${repo} already exists. Use start for an installed checkout; installation never replaces or updates it."
    if [ -f "${workspace}/server.py" ] && [ -f "${workspace}/requirements.txt" ]; then
        fail "${workspace} is already an XO Space checkout. Use start with that directory."
    fi

    mkdir -p "$workspace"
    printf 'Installing XO Space into %s\n' "$repo"
    git clone --quiet --depth 1 --branch "${QUIRQ_SOURCE_REF:-main}" -- \
        "${QUIRQ_SOURCE_REPO:-https://github.com/quirq-ai/xo-space.git}" "$repo" ||
        fail "Could not clone XO Space. Check the network and source ref; inspect ${repo} before retrying."
    [ -f "${repo}/install.sh" ] && [ -f "${repo}/server.py" ] && [ -f "${repo}/requirements.txt" ] ||
        fail "The downloaded source is missing install.sh, server.py or requirements.txt: ${repo}"

    cd "$workspace"
    export AGENT_NAME=codex
    export AI_PROVIDER=codex
    export HOST="${HOST:-127.0.0.1}"
    export XO_PROJECTS_ROOT="$workspace"
    export AI_WORKSPACE_ROOT="$workspace"
    export QUIRQ_STATE_ROOT="${workspace}/.quirq"
    export QUIRQ_SKIP_BOOT_INSTALL=1
    printf 'Using your existing Codex CLI login. If chat needs authentication, run codex login.\n'
    # A checked-out installer never fetches or resets the repository. Bash
    # is required: install.sh uses BASH_SOURCE and pipefail.
    exec bash "${repo}/install.sh"
}

start_space() {
    local repo="$1"
    absolute_directory "$repo"
    [ -f "${repo}/server.py" ] && [ -f "${repo}/requirements.txt" ] ||
        fail "No XO Space checkout found at ${repo}."
    [ -x "${repo}/venv/bin/python" ] ||
        fail "The Python environment is missing at ${repo}/venv. Run this checkout's install.sh with Bash to prepare it, then retry."
    repo="$(cd "$repo" && pwd -P)"
    cd "$repo"
    # Read dotenv as data, never as shell code. The existing venv provides
    # its parser; no global Python, dependency sync or network is needed.
    exec "${repo}/venv/bin/python" - "$repo" <<'PY'
import os
from pathlib import Path
import shutil
import sys

try:
    from dotenv import dotenv_values
except ImportError:
    sys.exit("XO Space: Python dependencies are incomplete. Run bash install.sh in this checkout to repair them.")

repo = Path(sys.argv[1])
saved = dotenv_values(repo / ".env")

def setting(name, default=""):
    return os.environ.get(name) or saved.get(name) or default

state = Path(setting("QUIRQ_STATE_ROOT", str(Path.home() / ".quirq"))).expanduser()
roots = state / "settings" / "roots.env"
if not roots.is_file():
    roots = state / "roots.env"
if not os.environ.get("QUIRQ_STATE_ROOT"):
    relocated = dotenv_values(roots).get("QUIRQ_STATE_ROOT")
    if relocated:
        state = Path(relocated).expanduser()
if not state.is_absolute():
    sys.exit("XO Space: QUIRQ_STATE_ROOT must be an absolute path.")

runtime = Path(setting("QUIRQ_RUNTIME_FILE", str(state / "settings" / "runtime.env"))).expanduser()
if not runtime.is_file() and runtime == state / "settings" / "runtime.env":
    runtime = state / "runtime.env"
active_agent = dotenv_values(runtime).get("AGENT_NAME") or setting("AGENT_NAME")
if active_agent == "codex":
    from utils.commands import run_sync

    cli = setting("CODEX_CLI_PATH", "codex")
    executable = shutil.which(cli)
    if not executable:
        sys.exit("XO Space: Codex CLI was not found. Install it or set CODEX_CLI_PATH to its executable.")
    # The check is logged in this state root, which this process knows only
    # from .env and roots.env. Name it for the check alone, so the runner
    # picks the log path by its own rules; the server finds its log itself,
    # and a path passed on to it would read as the user's choice and pin it.
    scoped = {"QUIRQ_STATE_ROOT": str(state)}
    for name in ("QUIRQ_COMMAND_LOG", "QUIRQ_COMMAND_LOG_PATH"):
        if setting(name):
            scoped[name] = setting(name)
    before = {name: os.environ.get(name) for name in scoped}
    os.environ.update(scoped)
    try:
        result = run_sync([executable, "--version"], timeout=10)
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if not result.ok:
        sys.exit("XO Space: Codex CLI could not run. Check CODEX_CLI_PATH or repair the CLI installation.")
    os.environ["CODEX_CLI_PATH"] = executable
    os.environ["AI_PROVIDER"] = setting("AI_PROVIDER", "codex")

# Starting must not provision software. The server still loads the saved
# .env, roots and runtime configuration itself, including its chosen agent.
os.environ["QUIRQ_SKIP_BOOT_INSTALL"] = "1"
os.environ["HOST"] = setting("HOST", "127.0.0.1")
os.environ["QUIRQ_SECRETS_FILE"] = setting("QUIRQ_SECRETS_FILE", str(state / "secrets" / "secrets.env"))
log = state / "logs" / "quirq.log"
log.parent.mkdir(parents=True, exist_ok=True)
print(f"Starting XO Space from {repo}\nLogs: {log}\nThe server stays attached to this Codex task. Stop its terminal to stop Space.", flush=True)
fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.dup2(fd, 1)
os.dup2(fd, 2)
os.close(fd)
os.execv(sys.executable, [sys.executable, str(repo / "server.py")])
PY
}

[ "$#" -eq 2 ] || usage
case "$1" in
    install) install_space "$2" ;;
    start) start_space "$2" ;;
    *) usage ;;
esac
