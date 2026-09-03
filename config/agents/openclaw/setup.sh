#!/usr/bin/env bash
# ==============================================================
# config/agents/openclaw/setup.sh — install + configure OpenClaw.
#
# Invoked by xo-space's FastAPI lifespan (server.py's
# _run_agent_setup) when AGENT_NAME=openclaw. Runs every time the
# xo-space server starts; each step is idempotent so repeat
# invocations are cheap (≈ seconds) once the first run is done.
#
# This script absorbs the OpenClaw bootstrap work that used to live
# in the Coder template's startup_script (main.tf), so the template
# can become a thin shell. Reads its inputs from environment vars
# that the Coder k8s pod injects.
#
# Required env (hard-fail downstream):
#     OPENCLAW_GATEWAY_TOKEN
#
# Optional env:
#     ENABLED_CHANNELS    JSON array — selects telegram/whatsapp/slack
#     TELEGRAM_BOT_TOKEN
#     ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY
#     SLACK_BOT_TOKEN / SLACK_APP_TOKEN
#     WHATSAPP_CREDS      raw JSON; written to creds.json if non-empty
#     OPENCLAW_CONTROL_UI_ORIGIN
#     XO_AUTH_SESSION_ID / XO_POLL_TOKEN / XO_API_KEY
#     CHAT_API_BASE_URL   default: https://api-swarm-dev.xo.builders
#     CLAUDE_CODE_OAUTH_TOKEN  default: ANTHROPIC_API_KEY
#     NODE_VERSION        default: 20
# ==============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
OPENCLAW_SH="$SCRIPT_DIR/agent.sh"
ENV_FILE="$REPO_ROOT/.env"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] openclaw-setup: $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

if [ ! -f "$OPENCLAW_SH" ]; then
    log_error "$OPENCLAW_SH not found — cannot proceed"
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
#   curl is needed by the OpenClaw installer.
#   jq is used by openclaw.sh's enable_channels.
# Skip the whole block if every binary is already on PATH.
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
        log_warn "apt-get install failed for: ${missing[*]} — openclaw.sh may degrade"
    fi
}

# ==============================================================
# Step 2 — NVM + Node. OpenClaw CLI installs via npm.
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
            log_error "NVM install failed — openclaw CLI install will likely fail"
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
    if [ -n "$SUDO" ] && [ -w /etc/profile.d ] || [ -n "$SUDO" ]; then
        $SUDO tee /etc/profile.d/nvm.sh >/dev/null <<'EOF' || true
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
EOF
        $SUDO chmod +x /etc/profile.d/nvm.sh 2>/dev/null || true
    fi

    log_success "node $(node -v), npm $(npm -v) ready"
}

# ==============================================================
# Step 3 — Materialise $REPO_ROOT/.env from environment vars.
# openclaw.sh's install_env() and load_env() both rely on this file.
# Hosted workspace edits are preserved. A managed local Quirq container
# refreshes the file on every boot because ~/.quirq/secrets.env is the
# authoritative, write-only store controlled by the Setup tab.
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
    # Values are single-quoted so openclaw.sh's `source .env` preserves
    # embedded characters like the JSON double-quotes in ENABLED_CHANNELS
    # (e.g. ["telegram"]). Without quoting, bash quote-removal would
    # collapse that to [telegram] and openclaw.sh's grep for '"telegram"'
    # would fail, silently disabling the plugin.
    cat > "$ENV_FILE" <<ENVEOF
ENABLED_CHANNELS='${ENABLED_CHANNELS:-}'
TELEGRAM_BOT_TOKEN='${TELEGRAM_BOT_TOKEN:-}'
ANTHROPIC_API_KEY='${ANTHROPIC_API_KEY:-}'
OPENCLAW_GATEWAY_TOKEN='${OPENCLAW_GATEWAY_TOKEN:-}'
XO_AUTH_SESSION_ID='${XO_AUTH_SESSION_ID:-}'
XO_POLL_TOKEN='${XO_POLL_TOKEN:-}'
XO_API_KEY='${XO_API_KEY:-}'
OPENCLAW_CONTROL_UI_ORIGIN='${OPENCLAW_CONTROL_UI_ORIGIN:-}'
CHAT_API_BASE_URL='${CHAT_API_BASE_URL:-https://api-swarm-dev.xo.builders}'
CLAUDE_CODE_OAUTH_TOKEN='${CLAUDE_CODE_OAUTH_TOKEN:-${ANTHROPIC_API_KEY:-}}'
OPENAI_API_KEY='${OPENAI_API_KEY:-}'
OPENROUTER_API_KEY='${OPENROUTER_API_KEY:-}'
SLACK_BOT_TOKEN='${SLACK_BOT_TOKEN:-}'
SLACK_APP_TOKEN='${SLACK_APP_TOKEN:-}'
ENVEOF
    chmod 600 "$ENV_FILE"
    log_success ".env written (mode 600)"
}

# ==============================================================
# Step 4 — Surface enabled channels as discrete env vars. openclaw.sh
# re-derives them from ENABLED_CHANNELS itself, but exporting these
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
# Step 5 — Persist WhatsApp creds JSON if provided.
# ==============================================================
write_whatsapp_creds() {
    local creds="${WHATSAPP_CREDS:-}"
    if [ -z "$creds" ]; then
        log "No WHATSAPP_CREDS provided — skipping"
        return 0
    fi
    local dir="$HOME/.openclaw/credentials/whatsapp/default"
    local file="$dir/creds.json"
    mkdir -p "$dir"
    umask 077
    printf '%s' "$creds" > "$file"
    chmod 600 "$file"
    log_success "WhatsApp credentials written to $file"
}

# ==============================================================
# NOTE: /usr/local/bin/{gateway,systemctl} shims are NOT installed here.
# They are baked into the workspace image (Dockerfile) as the single source
# of truth, pointing at config/agents/openclaw/agent.sh. A base image without
# those baked shims won't have the `gateway`/`systemctl` convenience commands —
# invoke agent.sh by path there.
# ==============================================================

# ==============================================================
# Step 6 — Delegate to openclaw.sh, the canonical lifecycle manager.
# openclaw.sh is already idempotent (skips CLI reinstall if present,
# skips openclaw.json rewrite if present, skips gateway start if
# already running), so this is safe on every cowork-api boot.
# ==============================================================
run_openclaw_setup() {
    chmod +x "$OPENCLAW_SH" 2>/dev/null || true

    if command -v openclaw >/dev/null 2>&1; then
        log "OpenClaw CLI already installed at $(command -v openclaw) — re-running setup (idempotent)"
    else
        log "OpenClaw CLI not found — running full install + setup"
    fi

    "$OPENCLAW_SH" setup
}

# ==============================================================
# Main
# ==============================================================
log "Starting OpenClaw agent bootstrap"
install_apt_prereqs
install_node
write_env_file
export_channel_flags
write_whatsapp_creds
run_openclaw_setup
status=$?
if [ "$status" -eq 0 ]; then
    log_success "OpenClaw agent bootstrap complete"
else
    log_error "openclaw.sh setup exited with status $status"
fi
exit "$status"
