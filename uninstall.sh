#!/usr/bin/env bash
# ==============================================================
# uninstall.sh — remove everything install.sh created, keeping
# your projects.
#
# What goes: the running server (stopped first), the managed
# checkout with its venv, the .env and connector credential files
# (rclone.conf, mcp-tokens.json), the Quirq state root (roots.env
# is read first, so a root moved from Setup is still found), the
# workspace-tier .xo/ the watcher wrote, the derived telemetry DB
# in ~/.argus, a legacy ~/.xo-cowork migration source, the
# cowork-api.sh daemon files in /tmp, and the local Docker compose
# project and image when the compose launcher was used.
#
# What stays, always: your project folders under the XO root —
# that is your actual work. --purge-projects (or --all) removes
# those too, behind a typed confirmation; there is deliberately no
# way to purge projects without a terminal.
#
# The installer adds no PATH entries, shell aliases, cron jobs, or
# launchd/systemd units, so there are none to remove. uv
# (~/.local/bin/uv) is a general-purpose tool the installer may
# have fetched; it is left in place and named in the summary.
#
# Usage, mirroring the two install modes:
#
#   From the workspace:   ./xo-space/uninstall.sh
#   From the checkout:    ./uninstall.sh
#
# Flags:
#   --yes             skip the main confirmation (scripts/CI; purging
#                     projects still requires a terminal)
#   --dry-run         print what would be removed; remove nothing
#   --purge-projects  ALSO delete the project folders under the XO root
#   --all             same as --purge-projects
#   --force           remove a managed checkout even with local changes
#   -h, --help        this text
#
# Idempotent: anything already gone is reported and skipped, and a
# machine with nothing installed exits cleanly.
# ==============================================================

set -Eeuo pipefail

LAUNCH_DIR="$PWD"
SOURCE_REPO="${QUIRQ_SOURCE_REPO:-https://github.com/quirq-ai/xo-space.git}"
REPO_NAME="${SOURCE_REPO##*/}"
REPO_NAME="${REPO_NAME%.git}"
# cowork-api.sh writes its pid and log to literal /tmp (not $TMPDIR); keep
# the same address here. Overridable so the test harness never touches the
# real files.
DAEMON_TMP="${QUIRQ_DAEMON_TMPDIR:-/tmp}"

REPO_DIR=""
WORKSPACE_DIR=""
ASSUME_YES=0
DRY_RUN=0
PURGE_PROJECTS=0
FORCE_CHECKOUT=0

REMOVED=()
KEPT=()
SKIPPED=()

fail() {
    printf '\nQuirq: %s\n' "$*" >&2
    exit 1
}

usage() {
    sed -n '2,42p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes) ASSUME_YES=1 ;;
            --dry-run) DRY_RUN=1 ;;
            --purge-projects|--all) PURGE_PROJECTS=1 ;;
            --force) FORCE_CHECKOUT=1 ;;
            -h|--help) usage; exit 0 ;;
            *) fail "Unknown flag: $1 (see --help)" ;;
        esac
        shift
    done
}

# ==============================================================
# Locating the install — the same probes install.sh uses, in the
# same order, so the two scripts agree on what "here" means.
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
        return
    fi
    if [ -f "${LAUNCH_DIR}/server.py" ] && [ -f "${LAUNCH_DIR}/requirements.txt" ]; then
        REPO_DIR="$LAUNCH_DIR"
        return
    fi
    if [ -f "${LAUNCH_DIR}/${REPO_NAME}/server.py" ] &&
        [ -f "${LAUNCH_DIR}/${REPO_NAME}/requirements.txt" ]; then
        REPO_DIR="${LAUNCH_DIR}/${REPO_NAME}"
        return
    fi
    REPO_DIR=""
}

resolve_workspace_dir() {
    local parent=""
    if [ -z "$REPO_DIR" ]; then
        WORKSPACE_DIR="$LAUNCH_DIR"
        return
    fi
    parent="$(cd "${REPO_DIR}/.." && pwd)"
    # The managed layout: the checkout sits inside the workspace, whose
    # state anchor is ./.quirq beside it. Anything else is in-place.
    if [ "$parent" != "$REPO_DIR" ] &&
        { [ -d "${parent}/.quirq" ] || [ "$parent" = "$LAUNCH_DIR" ]; }; then
        WORKSPACE_DIR="$parent"
    else
        WORKSPACE_DIR="$REPO_DIR"
    fi
}

# ==============================================================
# Roots — resolved exactly the way install.sh resolves them:
# exported shell values → roots.env saved by the Setup tab → the
# checkout's .env → the workspace defaults.
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
            "${key}="*) found="${line#*=}" ;;
        esac
    done < "$config_file"
    printf '%s' "$found"
}

read_env_value() {
    local key="$1"
    local file="${REPO_DIR:+${REPO_DIR}/.env}"
    local line
    local found=""

    [ -n "$file" ] && [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "${key}="*)
                found="${line#*=}"
                found="${found%%[[:space:]]#*}"
                found="${found#"${found%%[![:space:]]*}"}"
                found="${found%"${found##*[![:space:]]}"}"
                case "$found" in
                    \"*\") found="${found#\"}"; found="${found%\"}" ;;
                    \'*\') found="${found#\'}"; found="${found%\'}" ;;
                esac
                ;;
        esac
    done < "$file"
    printf '%s' "$found"
}

resolve_roots() {
    local anchor_dir saved_projects_root saved_state_root

    anchor_dir="${QUIRQ_STATE_ROOT:-${WORKSPACE_DIR}/.quirq}"
    saved_projects_root="$(saved_root_from_file "$anchor_dir" "XO_PROJECTS_ROOT")"
    saved_state_root="$(saved_root_from_file "$anchor_dir" "QUIRQ_STATE_ROOT")"
    PROJECTS_ROOT="${XO_PROJECTS_ROOT:-${saved_projects_root:-$(read_env_value XO_PROJECTS_ROOT)}}"
    PROJECTS_ROOT="${PROJECTS_ROOT:-${WORKSPACE_DIR}}"
    STATE_ROOT="${QUIRQ_STATE_ROOT:-${saved_state_root:-$(read_env_value QUIRQ_STATE_ROOT)}}"
    STATE_ROOT="${STATE_ROOT:-${WORKSPACE_DIR}/.quirq}"
}

# ==============================================================
# Removal plumbing. Every deletion funnels through remove_path,
# which refuses the paths no uninstaller should ever touch and
# honours --dry-run, so the guarantees hold in one place.
# ==============================================================
guard_path() {
    case "$1" in
        /*) ;;
        *) fail "refusing to remove a relative path: $1" ;;
    esac
    [ "$1" != "/" ] || fail "refusing to remove /"
    [ "$1" != "${HOME%/}" ] || fail "refusing to remove your home directory"
}

remove_path() {
    local label="$1"
    local path="$2"

    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        SKIPPED+=("${label}: not present (${path})")
        return
    fi
    guard_path "$path"
    if [ "$DRY_RUN" -eq 1 ]; then
        REMOVED+=("${label} (${path}) [dry run]")
        return
    fi
    if rm -rf -- "$path" 2>/dev/null; then
        REMOVED+=("${label} (${path})")
    else
        KEPT+=("${label}: could not remove (${path})")
    fi
}

# ==============================================================
# The running server. Prefer the process manager's own stop; fall
# back to the port, killing only a process we can attribute to
# this install — an arbitrary listener on 5002 is not ours to kill.
# ==============================================================
stop_server() {
    local port pid cmd

    if [ -n "$REPO_DIR" ] && [ -x "${REPO_DIR}/cowork-api.sh" ] && [ "$DRY_RUN" -eq 0 ]; then
        (cd "$REPO_DIR" && ./cowork-api.sh stop >/dev/null 2>&1) || true
    fi

    command -v lsof >/dev/null 2>&1 || return 0
    port="${PORT:-$(read_env_value PORT)}"
    for port in ${port:-5002 5003}; do
        for pid in $(lsof -ti ":${port}" 2>/dev/null || true); do
            cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
            case "$cmd" in
                *server.py*|*xo-space*)
                    if [ "$DRY_RUN" -eq 1 ]; then
                        REMOVED+=("server process ${pid} on port ${port} [dry run]")
                    else
                        kill "$pid" 2>/dev/null || true
                        REMOVED+=("server process ${pid} on port ${port}")
                    fi
                    ;;
                "") ;;
                *)
                    KEPT+=("process ${pid} on port ${port}: not a Quirq server (${cmd%% *})")
                    ;;
            esac
        done
    done
}

# ==============================================================
# Docker — only when the compose launcher could have been used.
# A stopped daemon or absent Docker is a skip, not an error.
# ==============================================================
docker_down() {
    local compose_file="${REPO_DIR:+${REPO_DIR}/compose.local.yml}"

    if [ -z "$compose_file" ] || [ ! -f "$compose_file" ]; then
        SKIPPED+=("Docker compose project: no compose.local.yml here")
        return
    fi
    if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
        SKIPPED+=("Docker compose project: Docker not available")
        return
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        REMOVED+=("Docker compose project quirq-local + built image [dry run]")
        return
    fi
    if docker compose -f "$compose_file" down --rmi local --volumes --remove-orphans >/dev/null 2>&1; then
        REMOVED+=("Docker compose project quirq-local + built image")
    else
        SKIPPED+=("Docker compose project: nothing to bring down")
    fi
}

# ==============================================================
# The checkout. Removed wholesale only when it is the managed
# layout (projects live beside it, not inside it) and has no local
# changes (--force overrides). In-place installs strip what the
# installer created and leave the clone — it holds the projects.
# ==============================================================
remove_checkout() {
    if [ -z "$REPO_DIR" ]; then
        SKIPPED+=("checkout: none found from ${LAUNCH_DIR}")
        return
    fi

    if [ "$PROJECTS_ROOT" = "$REPO_DIR" ] || [ "$WORKSPACE_DIR" = "$REPO_DIR" ]; then
        remove_path "checkout venv" "${REPO_DIR}/venv"
        remove_path "checkout .env" "${REPO_DIR}/.env"
        remove_path "connector credentials (rclone.conf)" "${REPO_DIR}/rclone.conf"
        remove_path "connector credentials (mcp-tokens.json)" "${REPO_DIR}/mcp-tokens.json"
        remove_path "usage-sync state (data/)" "${REPO_DIR}/data"
        KEPT+=("checkout itself: in-place install — the clone is yours, and the XO root; delete it manually if you want it gone (${REPO_DIR})")
        return
    fi

    if [ -d "${REPO_DIR}/.git" ] && [ "$FORCE_CHECKOUT" -eq 0 ] &&
        command -v git >/dev/null 2>&1 &&
        [ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]; then
        remove_path "checkout venv" "${REPO_DIR}/venv"
        KEPT+=("checkout: it has local changes — keeping it (rerun with --force to remove) (${REPO_DIR})")
        return
    fi

    remove_path "checkout (venv, .env, credentials included)" "$REPO_DIR"
}

purge_projects() {
    local entry typed count=0
    local names=()

    [ "$PROJECTS_ROOT" != "${HOME%/}" ] ||
        fail "the XO root is your home directory — refusing to purge projects there. Delete them yourself."

    for entry in "$PROJECTS_ROOT"/*; do
        [ -e "$entry" ] || continue
        [ "$entry" != "$REPO_DIR" ] || continue
        names+=("$entry")
        count=$((count + 1))
    done
    if [ "$count" -eq 0 ]; then
        SKIPPED+=("projects: none under ${PROJECTS_ROOT}")
        return
    fi

    printf '\n--purge-projects will PERMANENTLY delete these %d entries under the XO root:\n' "$count"
    printf '    %s\n' "${names[@]}"
    if [ "$DRY_RUN" -eq 1 ]; then
        REMOVED+=("${count} project entries under ${PROJECTS_ROOT} [dry run]")
        return
    fi
    [ -t 0 ] || fail "--purge-projects needs an interactive terminal for its confirmation."
    printf 'Type the XO root path (%s) to confirm: ' "$PROJECTS_ROOT"
    IFS= read -r typed
    [ "$typed" = "$PROJECTS_ROOT" ] || fail "confirmation did not match — no projects were removed."
    for entry in "${names[@]}"; do
        remove_path "project entry" "$entry"
    done
}

confirm_or_exit() {
    local answer

    printf 'This removes Quirq from this machine:\n'
    printf '    checkout:     %s\n' "${REPO_DIR:-none found}"
    printf '    Quirq state:  %s\n' "$STATE_ROOT"
    printf '    XO root:      %s  (projects are %s)\n' "$PROJECTS_ROOT" \
        "$([ "$PURGE_PROJECTS" -eq 1 ] && echo 'PURGED — you asked for --purge-projects' || echo 'kept')"
    [ "$DRY_RUN" -eq 0 ] || { printf 'Dry run: nothing will actually be removed.\n\n'; return; }
    [ "$ASSUME_YES" -eq 0 ] || return 0
    [ -t 0 ] || fail "not an interactive terminal — pass --yes to proceed without a prompt."
    printf 'Continue? [y/N] '
    IFS= read -r answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) fail "aborted — nothing was removed." ;;
    esac
    printf '\n'
}

print_summary() {
    local line

    printf '\nRemoved:\n'
    if [ "${#REMOVED[@]}" -gt 0 ]; then
        for line in "${REMOVED[@]}"; do printf '    %s\n' "$line"; done
    else
        printf '    nothing — Quirq does not appear to be installed here.\n'
    fi
    printf '\nKept:\n'
    printf '    your project folders under %s%s\n' "$PROJECTS_ROOT" \
        "$([ "$PURGE_PROJECTS" -eq 1 ] && echo ' (purged on request)' || echo '')"
    printf '    uv (~/.local/bin/uv), a general-purpose tool — remove it yourself if unwanted\n'
    for line in "${KEPT[@]:-}"; do [ -n "$line" ] && printf '    %s\n' "$line"; done
    if [ "${#SKIPPED[@]}" -gt 0 ]; then
        printf '\nAlready absent:\n'
        for line in "${SKIPPED[@]}"; do printf '    %s\n' "$line"; done
    fi
    printf '\nThe installer adds no PATH entries, cron jobs, or launchd/systemd units, so none were removed.\n'
}

main() {
    parse_args "$@"
    [ -n "${HOME:-}" ] || fail "HOME must be set."

    resolve_repo_dir
    resolve_workspace_dir
    resolve_roots
    guard_path "$STATE_ROOT"

    confirm_or_exit
    stop_server
    docker_down

    remove_path "workspace .xo (watcher output)" "${PROJECTS_ROOT}/.xo"
    remove_checkout
    remove_path "Quirq state root" "$STATE_ROOT"
    remove_path "daemon pid file" "${DAEMON_TMP}/xo-space.pid"
    remove_path "daemon log" "${DAEMON_TMP}/xo-space.log"
    remove_path "derived telemetry DB (~/.argus)" "${HOME}/.argus"
    remove_path "legacy migration state (~/.xo-cowork)" "${HOME}/.xo-cowork"

    [ "$PURGE_PROJECTS" -eq 0 ] || purge_projects

    print_summary
}

main "$@"
