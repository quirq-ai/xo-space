#!/usr/bin/env bash
# ==============================================================
# scripts/install_shared_deps.sh — install shared system deps once at boot.
#
# Invoked by xo-space's FastAPI lifespan (server.py's
# _install_shared_deps) on every server start. Absorbs dep installs
# that used to be repeated in every workspace template, so new templates
# work out of the box.
#
# Deps are core / agent-agnostic (named by no adapter):
#     rclone  — gdrive/onedrive connectors
#     gh      — xo-projects-sync per-project backup repos
#     gnupg   — xo-projects-sync encrypted backup/restore (gpg + gpg-agent)
#
# Also revives the Argus telemetry daemon (installed via requirements.txt;
# feeds ~/.argus/argus.db, which the Space Sessions tab reads). Runs every
# boot so a workspace restart self-heals the daemon.
#
# Idempotent: each dep is skipped if already on PATH, so repeat boots are
# cheap. Non-fatal: every step logs and continues on failure, and the
# script always exits 0 so the API still comes up for debugging.
# ==============================================================

set -uo pipefail

# Pinned gh release. Bump deliberately; an unpinned "latest" makes boots
# non-reproducible.
GH_VERSION="2.92.0"
GH_DEB_URL="https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.deb"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] shared-deps: $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

# Top guard: every installer below is Debian/Ubuntu-specific (apt/dpkg, and
# rclone's install.sh drops a binary into /usr/bin). On a host without apt-get
# (Windows/macOS dev, non-Debian Linux) there's nothing we can safely do, so
# log once and no-op. The deploy target is always Ubuntu, where this passes.
if ! command -v apt-get >/dev/null 2>&1; then
    log_warn "apt-get unavailable — skipping shared dep install (not a Debian/Ubuntu host)"
    exit 0
fi

# Use sudo only if present; many container images run the app as root, where
# sudo is absent but also unnecessary.
if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else SUDO=""; fi

# Refresh the apt index at most once per run (only the apt-based installers
# need it; rclone's install.sh does not).
APT_UPDATED=0
apt_update_once() {
    [ "$APT_UPDATED" -eq 1 ] && return 0
    if $SUDO apt-get update -y; then
        APT_UPDATED=1
    else
        log_warn "apt-get update failed (continuing)"
    fi
}

# rclone — official install script (handles arch detection + /usr/bin install).
install_rclone() {
    if command -v rclone >/dev/null 2>&1; then
        log "rclone already installed — skipping"
        return 0
    fi
    if ! command -v curl >/dev/null 2>&1; then
        log_warn "curl unavailable — cannot install rclone (gdrive/onedrive connectors degraded)"
        return 0
    fi
    log "Installing rclone via official install script..."
    curl -fsSL https://rclone.org/install.sh | $SUDO bash || true
    # install.sh exits non-zero when the latest is already present; trust PATH.
    if command -v rclone >/dev/null 2>&1; then
        log_success "rclone installed"
    else
        log_warn "rclone install failed — gdrive/onedrive connectors degraded"
    fi
}

# gh — pinned .deb. apt-get resolves the local file's deps automatically.
install_gh() {
    if command -v gh >/dev/null 2>&1; then
        log "gh already installed — skipping"
        return 0
    fi
    if ! command -v curl >/dev/null 2>&1; then
        log_warn "curl unavailable — cannot install gh (xo-projects-sync backup repos degraded)"
        return 0
    fi
    local deb="/tmp/gh_${GH_VERSION}.deb"
    log "Installing gh ${GH_VERSION} from pinned .deb..."
    if ! curl -fsSL "$GH_DEB_URL" -o "$deb"; then
        log_warn "gh .deb download failed — gh unavailable (backup repos degraded)"
        return 0
    fi
    if $SUDO apt-get install -y "$deb"; then
        log_success "gh installed"
    else
        log_warn "gh install failed — xo-projects-sync backup repos degraded"
    fi
    rm -f "$deb" 2>/dev/null || true
}

# gnupg — needs both gpg and gpg-agent (crypto.check_gpg_available requires
# both; gpg invokes the agent for passphrase handling even with --passphrase-fd).
install_gnupg() {
    if command -v gpg >/dev/null 2>&1 && command -v gpg-agent >/dev/null 2>&1; then
        log "gnupg already installed (gpg + gpg-agent present) — skipping"
        return 0
    fi
    log "Installing gnupg..."
    apt_update_once
    if $SUDO apt-get install -y gnupg; then
        log_success "gnupg installed"
    else
        log_warn "gnupg install failed — xo-projects-sync encrypted backup/restore unavailable"
    fi
}

# argus — session-telemetry daemon. The package (argus-code, pinned in
# requirements.txt) installs the binary into the API's environment, so it is
# on this process's PATH. `argus daemon start` reports "daemon already
# running, PID …" when up — treated as healthy, keeping this idempotent.
# Never fatal: without the daemon the Sessions tab shows its error card.
start_argus_daemon() {
    if ! command -v argus >/dev/null 2>&1; then
        log_warn "argus binary not on PATH — run the install step (requirements.txt); session telemetry degraded"
        return 0
    fi
    local out
    if out=$(argus daemon start 2>&1); then
        log_success "argus daemon: ${out:-started}"
    elif echo "$out" | grep -qi "already running"; then
        log "argus daemon already running"
    else
        log_warn "argus daemon start failed — session telemetry degraded: ${out}"
    fi
}

log "Ensuring shared system deps (rclone, gh, gnupg) + argus daemon"
install_rclone
install_gh
install_gnupg
start_argus_daemon
log "Shared dep check complete"
exit 0
