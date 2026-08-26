#!/usr/bin/env bash
# ==============================================================
# config/agents/hermes/setup.sh — install + configure Hermes.
#
# Invoked by xo-space's FastAPI lifespan (server.py's
# _run_agent_setup) when AGENT_NAME=hermes. Runs every time the
# xo-space server starts; each step is idempotent so repeat
# invocations are cheap (≈ seconds) once the first run is done.
#
# This script absorbs the Hermes bootstrap work that used to live
# in the Coder template's startup_script (main-hermes.tf), so the
# template can become a thin shell. Reads its inputs from environment
# vars that the Coder k8s pod injects.
#
# Required env (hard-fail downstream in hermes.sh):
#     (Hermes is more lenient than OpenClaw — at least one of
#      ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY)
#
# Optional env:
#     ENABLED_CHANNELS    JSON array — selects telegram/whatsapp/slack
#     TELEGRAM_BOT_TOKEN
#     SLACK_BOT_TOKEN / SLACK_APP_TOKEN
#     WHATSAPP_CREDS      raw JSON; exported for hermes.sh setup to consume
#     XO_AUTH_SESSION_ID / XO_POLL_TOKEN / XO_API_KEY
#     CHAT_API_BASE_URL   default: https://api-swarm-dev.xo.builders
#     CLAUDE_CODE_OAUTH_TOKEN  default: ANTHROPIC_API_KEY
#     NODE_VERSION        default: 20
#
# Hermes-app defaults (overridable via pod env):
#     WHATSAPP_ALLOWED_USERS, TERMINAL_*, BROWSERBASE_*, BROWSER_*,
#     HERMES_MAX_ITERATIONS, *_TOOLS_DEBUG — see write_env_file().
# ==============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
HERMES_SH="$SCRIPT_DIR/agent.sh"
ENV_FILE="$REPO_ROOT/.env"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] hermes-setup: $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

if [ ! -f "$HERMES_SH" ]; then
    log_error "$HERMES_SH not found — cannot proceed"
    exit 1
fi

# Decide whether sudo is needed and available. Many container images run
# the app as the same user that owns / so sudo isn't always required.
if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    SUDO=""
fi

# ==============================================================
# Step 1 — apt prereqs (jq, curl, unzip).
# Same as the OpenClaw setup: cheap idempotent skip when present.
# ==============================================================
install_apt_prereqs() {
    local missing=()
    command -v curl >/dev/null 2>&1 || missing+=("curl")
    command -v jq   >/dev/null 2>&1 || missing+=("jq")
    command -v unzip >/dev/null 2>&1 || missing+=("unzip")

    if [ "${#missing[@]}" -eq 0 ]; then
        log "apt prereqs already present (curl, jq, unzip)"
        return 0
    fi

    if ! command -v apt-get >/dev/null 2>&1; then
        log_warn "apt-get not available — cannot install: ${missing[*]} (continuing anyway)"
        return 0
    fi

    log "Installing apt prereqs: ${missing[*]}"
    $SUDO apt-get update -y || log_warn "apt-get update failed (continuing)"
    if $SUDO apt-get install -y "${missing[@]}"; then
        log_success "apt prereqs installed"
    else
        log_warn "apt-get install failed for: ${missing[*]} — hermes.sh may degrade"
    fi
}

# ==============================================================
# Step 2 — uv (Python package manager that Hermes' tooling uses).
# Try sudo pip first (matching main-hermes.tf), then user pip, then
# give up silently — hermes.sh will surface a clearer error if uv is
# actually required.
# ==============================================================
install_uv() {
    if command -v uv >/dev/null 2>&1; then
        log "uv already installed at $(command -v uv)"
        return 0
    fi

    if ! command -v pip3 >/dev/null 2>&1; then
        log_warn "pip3 not available — cannot install uv"
        return 0
    fi

    log "Installing uv..."
    if $SUDO pip3 install uv 2>/dev/null \
        || pip3 install --user uv 2>/dev/null \
        || pip3 install --break-system-packages uv 2>/dev/null; then
        log_success "uv installed"
        # Ensure ~/.local/bin is on PATH so the user-pip install resolves
        export PATH="$HOME/.local/bin:$PATH"
    else
        log_warn "uv install failed (non-fatal) — hermes.sh may flag this later"
    fi
}

# ==============================================================
# Step 3 — NVM + Node. Hermes CLI / dashboard install via npm.
# Skip if `node` and `npm` are already on PATH.
# ==============================================================
install_node() {
    # Try to load existing NVM into this shell first
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    # shellcheck disable=SC1091
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" >/dev/null 2>&1 || true

    if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
        log "node $(node -v) and npm $(npm -v) already available"
        return 0
    fi

    if [ ! -d "$NVM_DIR" ]; then
        log "Installing NVM..."
        if ! curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash; then
            log_error "NVM install failed — hermes CLI install will likely fail"
            return 1
        fi
    fi

    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh"

    local node_version="${NODE_VERSION:-20}"
    log "Installing Node.js $node_version via NVM..."
    nvm install "$node_version" || { log_error "nvm install $node_version failed"; return 1; }
    nvm alias default "$node_version" >/dev/null
    nvm use default >/dev/null

    # Make NVM auto-load for future shells (idempotent)
    $SUDO tee /etc/profile.d/nvm.sh >/dev/null <<'EOF' || true
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
EOF
    $SUDO chmod +x /etc/profile.d/nvm.sh 2>/dev/null || true

    log_success "node $(node -v), npm $(npm -v) ready"
}

# ==============================================================
# Step 4 — Materialise $REPO_ROOT/.env from environment vars.
# Hermes-specific layout: user-tunable values come from pod env; Hermes
# app defaults are baked in with ${VAR:-default}. Hosted workspace edits
# are preserved. Managed local Quirq refreshes the file on every boot so
# values saved by the Setup tab in ~/.quirq/secrets.env take effect.
# ==============================================================
write_env_file() {
    if [ -f "$ENV_FILE" ] && [ "${QUIRQ_MANAGED_CONTAINER:-false}" != "true" ]; then
        log ".env already exists at $ENV_FILE — leaving it untouched"
        return 0
    fi

    if [ "${QUIRQ_MANAGED_CONTAINER:-false}" = "true" ]; then
        log "Refreshing .env from managed Quirq configuration"
    else
        log "Writing .env from environment (first time)"
    fi
    umask 077
    cat > "$ENV_FILE" <<ENVEOF
# Channel toggles
ENABLED_CHANNELS=${ENABLED_CHANNELS:-}

# Platform tokens
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN:-}
SLACK_APP_TOKEN=${SLACK_APP_TOKEN:-}
WHATSAPP_ALLOWED_USERS=${WHATSAPP_ALLOWED_USERS:-*}

# API keys
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
OPENAI_API_KEY=${OPENAI_API_KEY:-}
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-${ANTHROPIC_API_KEY:-}}

# Terminal backends
TERMINAL_MODAL_IMAGE=${TERMINAL_MODAL_IMAGE:-nikolaik/python-nodejs:python3.11-nodejs20}
TERMINAL_TIMEOUT=${TERMINAL_TIMEOUT:-60}
TERMINAL_LIFETIME_SECONDS=${TERMINAL_LIFETIME_SECONDS:-300}

# Browser
BROWSERBASE_PROXIES=${BROWSERBASE_PROXIES:-true}
BROWSERBASE_ADVANCED_STEALTH=${BROWSERBASE_ADVANCED_STEALTH:-false}
BROWSER_SESSION_TIMEOUT=${BROWSER_SESSION_TIMEOUT:-300}
BROWSER_INACTIVITY_TIMEOUT=${BROWSER_INACTIVITY_TIMEOUT:-120}

# Agent limits
HERMES_MAX_ITERATIONS=${HERMES_MAX_ITERATIONS:-90}

# Debug flags
WEB_TOOLS_DEBUG=${WEB_TOOLS_DEBUG:-false}
VISION_TOOLS_DEBUG=${VISION_TOOLS_DEBUG:-false}
MOA_TOOLS_DEBUG=${MOA_TOOLS_DEBUG:-false}
IMAGE_TOOLS_DEBUG=${IMAGE_TOOLS_DEBUG:-false}

# XO Integration
XO_AUTH_SESSION_ID=${XO_AUTH_SESSION_ID:-}
XO_POLL_TOKEN=${XO_POLL_TOKEN:-}
XO_API_KEY=${XO_API_KEY:-}
CHAT_API_BASE_URL=${CHAT_API_BASE_URL:-https://api-swarm-dev.xo.builders}
ENVEOF
    chmod 600 "$ENV_FILE"
    log_success ".env written (mode 600)"
}

# ==============================================================
# Step 5 — Surface enabled channels as discrete env vars. hermes.sh
# may re-derive them from ENABLED_CHANNELS itself, but exporting these
# keeps parity with the template flow for anything else that reads them.
# ==============================================================
export_channel_flags() {
    export TELEGRAM_ENABLED=false
    export WHATSAPP_ENABLED=false
    export SLACK_ENABLED=false
    local raw="${ENABLED_CHANNELS:-}"
    [ -z "$raw" ] && return 0
    echo "$raw" | grep -q '"telegram"' && export TELEGRAM_ENABLED=true || true
    echo "$raw" | grep -q '"whatsapp"' && export WHATSAPP_ENABLED=true || true
    echo "$raw" | grep -q '"slack"'    && export SLACK_ENABLED=true    || true
    log "Channels: telegram=$TELEGRAM_ENABLED whatsapp=$WHATSAPP_ENABLED slack=$SLACK_ENABLED"
}

# ==============================================================
# Step 6 — Forward WhatsApp credentials to hermes.sh.
# Matches main-hermes.tf behaviour: this layer only re-exports
# $WHATSAPP_CREDS and lets hermes.sh's own setup write the file
# in whatever location it expects. Different from the OpenClaw flow
# (which writes ~/.openclaw/credentials/... directly) because Hermes
# already owns this responsibility.
# ==============================================================
export_whatsapp_creds() {
    local creds="${WHATSAPP_CREDS:-}"
    if [ -z "$creds" ]; then
        log "No WHATSAPP_CREDS provided — skipping"
        return 0
    fi
    export WHATSAPP_CREDS
    log "WHATSAPP_CREDS exported for hermes.sh setup to consume"
}

# ==============================================================
# NOTE: /usr/local/bin/{gateway,systemctl} shims are NOT installed here.
# They are baked into the workspace image (Dockerfile) as the single source
# of truth, pointing at config/agents/hermes/agent.sh. A base image without
# those baked shims won't have the `gateway`/`systemctl` convenience commands —
# invoke agent.sh by path there.
# ==============================================================

# ==============================================================
# Step 7 — Delegate to hermes.sh, the canonical lifecycle manager.
# hermes.sh is already idempotent (skips CLI reinstall if present,
# skips re-config when files exist, skips gateway/dashboard start
# when already running), so this is safe on every cowork-api boot.
# ==============================================================
run_hermes_setup() {
    chmod +x "$HERMES_SH" 2>/dev/null || true

    if command -v hermes >/dev/null 2>&1; then
        log "Hermes CLI already installed at $(command -v hermes) — re-running setup (idempotent)"
    else
        log "Hermes CLI not found — running full install + setup"
    fi

    "$HERMES_SH" setup
}

# ==============================================================
# Main
# ==============================================================
log "Starting Hermes agent bootstrap"
install_apt_prereqs
install_uv
install_node
write_env_file
export_channel_flags
export_whatsapp_creds
run_hermes_setup
status=$?
if [ "$status" -eq 0 ]; then
    log_success "Hermes agent bootstrap complete"
else
    log_error "hermes.sh setup exited with status $status"
fi
exit "$status"
