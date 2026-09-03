#!/usr/bin/env bash
# ==============================================================
# OpenClaw OneClick Setup & Gateway Manager
# Combined setup + gateway management in a single script.
#
# Usage: ./agent.sh {setup|start|stop|restart|status|logs}
#
# Required env vars (set in .env):
#     TELEGRAM_BOT_TOKEN      - Telegram bot token from BotFather
#     OPENCLAW_GATEWAY_TOKEN  - Gateway auth token
#
# Model provider (at least one required):
#     ANTHROPIC_API_KEY       - Anthropic API key for Claude model
#     OPENAI_API_KEY          - OpenAI API key for Codex/GPT model
#
# Slack (optional — enables Slack channel):
#     SLACK_BOT_TOKEN         - Slack Bot User OAuth Token (xoxb-...)
#     SLACK_APP_TOKEN         - Slack App-Level Token (xapp-...)
#
# Optional env vars:
#     TELEGRAM_ENABLED        - enable/disable Telegram channel (default: true)
#     OPENCLAW_MAX_RESTARTS   - max consecutive restarts (default: 10)
#     OPENCLAW_RESTART_DELAY  - seconds between restarts (default: 5)
#     OPENCLAW_RESTART_WINDOW - seconds of uptime to reset counter (default: 300)
#     OPENCLAW_LOG_MAX_SIZE   - max log size in bytes before rotation (default: 10485760 / 10MB)
# ==============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# This script lives at config/agents/openclaw/agent.sh; the repo root (where
# the shared .env is materialised by setup.sh) is three levels up.
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
OPENCLAW_DIR="${HOME}/.openclaw"
CONFIG_FILE="${OPENCLAW_DIR}/openclaw.json"
PID_FILE="/tmp/openclaw-gateway.pid"
LOG_FILE="/tmp/openclaw-gateway.log"
LOCK_FILE="/tmp/openclaw-gateway.lock"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

# --- Load .env safely (don't fail on unset vars) ---
load_env() {
    local env_file="$1"
    if [ -f "$env_file" ]; then
        set +u
        set -a
        source "$env_file"
        set +a
        set -u
    fi
}

load_env "$REPO_ROOT/.env"
load_env "$OPENCLAW_DIR/.env"

# --- Settings ---
RESTART_DELAY="${OPENCLAW_RESTART_DELAY:-5}"
MAX_RESTARTS="${OPENCLAW_MAX_RESTARTS:-10}"
RESTART_WINDOW="${OPENCLAW_RESTART_WINDOW:-300}"
LOG_MAX_SIZE="${OPENCLAW_LOG_MAX_SIZE:-10485760}"

# ==============================================================
# Helpers
# ==============================================================

# Rotate log if it exceeds max size
rotate_log() {
    if [ -f "$LOG_FILE" ]; then
        local size
        size=$(stat -c%s "$LOG_FILE" 2>/dev/null || stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)
        if [ "$size" -ge "$LOG_MAX_SIZE" ]; then
            mv "$LOG_FILE" "${LOG_FILE}.1"
            log "Log rotated (was ${size} bytes)"
        fi
    fi
}

# Acquire a lock to prevent concurrent start/stop races
LOCK_ACQUIRED=0
acquire_lock() {
    if [ "$LOCK_ACQUIRED" -eq 1 ]; then
        return 0
    fi
    exec 9>"$LOCK_FILE"
    if ! flock -w 5 9; then
        log_warn "Lock held, cleaning stale lock and retrying..."
        rm -f "$LOCK_FILE"
        exec 9>"$LOCK_FILE"
        if ! flock -n 9; then
            log_error "Another agent.sh operation is in progress"
            exit 1
        fi
    fi
    LOCK_ACQUIRED=1
}

# Clean stale PID file if process is dead
clean_stale_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if ! kill -0 "$pid" 2>/dev/null; then
            log_warn "Removing stale PID file (process $pid is dead)"
            rm -f "$PID_FILE"
        fi
    fi
}

# Find orphan gateway processes not managed by our wrapper
find_orphan_gateways() {
    local managed_wrapper_pid=""
    if [ -f "$PID_FILE" ]; then
        managed_wrapper_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    fi

    # Search for both the command form and the binary name
    local gw_pids
    gw_pids=$(( pgrep -f "openclaw gateway run" 2>/dev/null; pgrep -x "openclaw-gateway" 2>/dev/null ) | sort -u)
    [ -z "$gw_pids" ] && return

    for gw_pid in $gw_pids; do
        # Skip the managed wrapper itself. Its `bash -c` command line embeds the
        # literal "openclaw gateway run" string, so `pgrep -f` matches the loop
        # we manage (PID in $PID_FILE) — it is not an orphan.
        [ -n "$managed_wrapper_pid" ] && [ "$gw_pid" = "$managed_wrapper_pid" ] && continue
        # Skip the real gateway process if it's a child of our managed wrapper.
        if [ -n "$managed_wrapper_pid" ] && kill -0 "$managed_wrapper_pid" 2>/dev/null; then
            local parent
            parent=$(ps -o ppid= -p "$gw_pid" 2>/dev/null | tr -d ' ')
            [ "$parent" = "$managed_wrapper_pid" ] && continue
        fi
        echo "$gw_pid"
    done
}

# Kill orphan gateway processes and warn
kill_orphan_gateways() {
    local orphans
    orphans=$(find_orphan_gateways)
    [ -z "$orphans" ] && return 0

    for opid in $orphans; do
        log_warn "Found orphan gateway process (PID: $opid) — killing it"
        kill "$opid" 2>/dev/null || true
    done
    sleep 1
    # Force kill any survivors
    for opid in $orphans; do
        if kill -0 "$opid" 2>/dev/null; then
            kill -9 "$opid" 2>/dev/null || true
        fi
    done
    return 0
}

# ==============================================================
# Setup: Validate required env vars
# ==============================================================
validate_env() {
    local missing=0
    local warnings=0

    # --- Soft checks (warn only, gateway may have limited functionality) ---
    if [ "${TELEGRAM_ENABLED:-true}" = "true" ] && [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
        log_warn "TELEGRAM_BOT_TOKEN is not set — Telegram channel will be disabled"
        export TELEGRAM_ENABLED=false
        warnings=1
    fi
    if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${OPENROUTER_API_KEY:-}" ]; then
        log_warn "No model provider key set (ANTHROPIC_API_KEY, OPENAI_API_KEY, or OPENROUTER_API_KEY). Gateway will start but agents won't work without a model."
        warnings=1
    fi

    # --- Hard check (gateway cannot function without this) ---
    if [ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
        log_error "OPENCLAW_GATEWAY_TOKEN is not set"
        missing=1
    fi

    if [ "$missing" -eq 1 ]; then
        log_error "Set required vars in .env. Exiting."
        exit 1
    fi
    if [ "$warnings" -eq 1 ]; then
        log_warn "Some optional keys are missing — gateway will start with reduced functionality"
    fi
    log_success "Environment variables validated"
}

# ==============================================================
# Setup: Install .env to ~/.openclaw/
# ==============================================================
install_env() {
    log "Installing .env to ${OPENCLAW_DIR}/.env..."
    mkdir -p "$OPENCLAW_DIR"
    # First-time-only copy: server.py runs setup on every boot, so an
    # unconditional cp would clobber user-added keys in the agent .env each
    # restart. Guard on the destination (same pattern as auth.json below).
    if [ -f "$OPENCLAW_DIR/.env" ]; then
        log ".env already exists at ${OPENCLAW_DIR}/.env — preserving manual edits"
    elif [ -f "$REPO_ROOT/.env" ]; then
        cp "$REPO_ROOT/.env" "$OPENCLAW_DIR/.env"
        chmod 600 "$OPENCLAW_DIR/.env"
        log_success ".env copied to ${OPENCLAW_DIR}/.env (mode 600)"
    else
        log_error "No .env found in $REPO_ROOT"
        exit 1
    fi
}

# ==============================================================
# Setup: Enable channels in openclaw.json (using jq if available)
# ==============================================================
enable_channels() {
    log "Configuring channels..."
    mkdir -p "$OPENCLAW_DIR"

    if [ -f "$CONFIG_FILE" ]; then
        log_warn "openclaw.json already exists, skipping channel config"
        return 0
    fi

    local telegram_enabled="${TELEGRAM_ENABLED:-true}"
    local whatsapp_enabled="${WHATSAPP_ENABLED:-false}"
    local slack_enabled="${SLACK_ENABLED:-false}"
    local control_ui_origin="${OPENCLAW_CONTROL_UI_ORIGIN:-}"

    # Parse ENABLED_CHANNELS JSON array if set (from Coder multi-select)
    if [ -n "${ENABLED_CHANNELS:-}" ]; then
        echo "$ENABLED_CHANNELS" | grep -q '"telegram"' && telegram_enabled=true || telegram_enabled=false
        echo "$ENABLED_CHANNELS" | grep -q '"whatsapp"' && whatsapp_enabled=true || whatsapp_enabled=false
        echo "$ENABLED_CHANNELS" | grep -q '"slack"' && slack_enabled=true || slack_enabled=false
    fi

    # Auto-enable Slack if tokens are provided
    if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_APP_TOKEN:-}" ]; then
        slack_enabled=true
    fi

    # Ensure WhatsApp credentials directory exists
    mkdir -p "$OPENCLAW_DIR/credentials/whatsapp/default"

    # Determine primary model by which API keys are present (precedence:
    # Anthropic > OpenAI > OpenRouter):
    #   only OpenAI            → openai
    #   only OpenRouter        → openrouter/auto
    #   only Anthropic / both / neither → anthropic (default)
    # configure_openrouter() re-asserts this via the CLI (and honors
    # OPENCLAW_PRIMARY_MODEL); setting it here too keeps the file sane even if
    # that later CLI step fails.
    local primary_model="anthropic/claude-opus-4-8"
    if [ -n "${OPENAI_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        primary_model="openai/gpt-5.5"
    elif [ -n "${OPENROUTER_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        primary_model="openrouter/auto"
    fi
    log "Primary model provider: $primary_model"

    if command -v jq &>/dev/null; then
        # Build config safely with jq
        local config
        config=$(jq -n \
            --argjson tg_enabled "$telegram_enabled" \
            --argjson wa_enabled "$whatsapp_enabled" \
            --argjson slack_enabled "$slack_enabled" \
            --arg ui_origin "$control_ui_origin" \
            --arg primary_model "$primary_model" \
            --arg has_openai "$([ -n "${OPENAI_API_KEY:-}" ] && echo true || echo false)" \
            --arg has_anthropic "$([ -n "${ANTHROPIC_API_KEY:-}" ] && echo true || echo false)" \
            --arg slack_bot_token "${SLACK_BOT_TOKEN:-}" \
            --arg slack_app_token "${SLACK_APP_TOKEN:-}" \
            '{
                gateway: {
                    mode: "local",
                    trustedProxies: ["127.0.0.1"],
                    controlUi: {
                        dangerouslyDisableDeviceAuth: true
                    },
                    http: {
                        endpoints: {
                            chatCompletions: { enabled: true },
                            responses: { enabled: true }
                        }
                    }
                },
                commands: { native: "auto", nativeSkills: "auto" },
                channels: {
                    telegram: {
                        enabled: $tg_enabled,
                        dmPolicy: "open",
                        allowFrom: ["*"],
                        groupPolicy: "allowlist",
                        streaming: { mode: "partial" }
                    },
                    whatsapp: {
                        enabled: $wa_enabled,
                        dmPolicy: "open",
                        selfChatMode: false,
                        allowFrom: ["*"],
                        groupPolicy: "allowlist",
                        debounceMs: 0,
                        mediaMaxMb: 50
                    }
                },
                plugins: { entries: { telegram: { enabled: $tg_enabled }, whatsapp: { enabled: $wa_enabled } } },
                agents: {
                    defaults: {
                        maxConcurrent: 4,
                        subagents: { maxConcurrent: 8 },
                        model: { primary: $primary_model }
                    }
                },
                messages: { ackReactionScope: "group-mentions" }
            }
            | if $ui_origin != "" then
                .gateway.controlUi.allowedOrigins = [$ui_origin]
              else . end
            | if $has_anthropic == "true" then
                .plugins.entries.anthropic = { enabled: true }
              else . end
            | if $has_openai == "true" then
                .plugins.entries.openai = { config: { personality: "off" } }
              else . end
            | if $slack_enabled == true then
                .channels.slack = {
                    enabled: true,
                    mode: "socket",
                    appToken: $slack_app_token,
                    botToken: $slack_bot_token,
                    dmPolicy: "open",
                    allowFrom: ["*"],
                    groupPolicy: "allowlist"
                }
                | .plugins.entries.slack = { enabled: true }
              else . end')
        echo "$config" > "$CONFIG_FILE"
    else
        # Fallback: heredoc (no string concatenation)
        local model_line="\"primary\": \"${primary_model}\""
        local openai_plugin=""
        if [ -n "${OPENAI_API_KEY:-}" ]; then
            openai_plugin=", \"openai\": { \"config\": { \"personality\": \"off\" } }"
        fi
        local anthropic_plugin=""
        if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
            anthropic_plugin=", \"anthropic\": { \"enabled\": true }"
        fi
        local slack_plugin=""
        local slack_channel=""
        if [ "$slack_enabled" = "true" ] && [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_APP_TOKEN:-}" ]; then
            slack_plugin=", \"slack\": { \"enabled\": true }"
            slack_channel=",
    \"slack\": {
      \"enabled\": true,
      \"mode\": \"socket\",
      \"appToken\": \"${SLACK_APP_TOKEN}\",
      \"botToken\": \"${SLACK_BOT_TOKEN}\",
      \"dmPolicy\": \"open\",
      \"allowFrom\": [\"*\"],
      \"groupPolicy\": \"allowlist\"
    }"
        fi

        if [ -n "$control_ui_origin" ]; then
            cat > "$CONFIG_FILE" <<EOJSON
{
  "gateway": {
    "mode": "local",
    "trustedProxies": ["127.0.0.1"],
    "controlUi": {
      "allowedOrigins": ["${control_ui_origin}"],
      "dangerouslyDisableDeviceAuth": true
    },
    "http": {
      "endpoints": {
        "chatCompletions": { "enabled": true },
        "responses": { "enabled": true }
      }
    }
  },
  "commands": { "native": "auto", "nativeSkills": "auto" },
  "channels": {
    "telegram": {
      "enabled": true,
      "dmPolicy": "open",
      "allowFrom": ["*"],
      "groupPolicy": "allowlist",
      "streaming": { "mode": "partial" }
    },
    "whatsapp": {
      "enabled": false,
      "dmPolicy": "open",
      "selfChatMode": false,
      "allowFrom": ["*"],
      "groupPolicy": "allowlist",
      "debounceMs": 0,
      "mediaMaxMb": 50
    }${slack_channel}
  },
  "plugins": { "entries": { "telegram": { "enabled": true }, "whatsapp": { "enabled": false }${slack_plugin}${anthropic_plugin}${openai_plugin} } },
  "agents": { "defaults": { "maxConcurrent": 4, "subagents": { "maxConcurrent": 8 }, "model": { ${model_line} } } },
  "messages": { "ackReactionScope": "group-mentions" }
}
EOJSON
        else
            cat > "$CONFIG_FILE" <<EOJSON
{
  "gateway": {
    "mode": "local",
    "trustedProxies": ["127.0.0.1"],
    "controlUi": {
      "dangerouslyDisableDeviceAuth": true
    },
    "http": {
      "endpoints": {
        "chatCompletions": { "enabled": true },
        "responses": { "enabled": true }
      }
    }
  },
  "commands": { "native": "auto", "nativeSkills": "auto" },
  "channels": {
    "telegram": {
      "enabled": true,
      "dmPolicy": "open",
      "allowFrom": ["*"],
      "groupPolicy": "allowlist",
      "streaming": { "mode": "partial" }
    },
    "whatsapp": {
      "enabled": false,
      "dmPolicy": "open",
      "selfChatMode": false,
      "allowFrom": ["*"],
      "groupPolicy": "allowlist",
      "debounceMs": 0,
      "mediaMaxMb": 50
    }${slack_channel}
  },
  "plugins": { "entries": { "telegram": { "enabled": true }, "whatsapp": { "enabled": false }${slack_plugin}${anthropic_plugin}${openai_plugin} } },
  "agents": { "defaults": { "maxConcurrent": 4, "subagents": { "maxConcurrent": 8 }, "model": { ${model_line} } } },
  "messages": { "ackReactionScope": "group-mentions" }
}
EOJSON
        fi
        # Patch in allow_from and enabled state if needed
        if [ "$telegram_enabled" = "false" ]; then
            log_warn "jq not available — Telegram enabled defaults to true in fallback config. Install jq for full config support."
        fi
    fi

    log_success "Channels configured (telegram: ${telegram_enabled}, whatsapp: ${whatsapp_enabled}, slack: ${slack_enabled})"
}

# ==============================================================
# Setup: Ensure gateway.mode is set
# ==============================================================
ensure_gateway_mode() {
    local control_ui_origin="${OPENCLAW_CONTROL_UI_ORIGIN:-}"
    if [ -f "$CONFIG_FILE" ] && ! grep -q '"gateway"' "$CONFIG_FILE"; then
        if command -v jq &>/dev/null; then
            local tmp
            if [ -n "$control_ui_origin" ]; then
                tmp=$(jq --arg origin "$control_ui_origin" '. + {gateway: {mode: "local", controlUi: {allowedOrigins: [$origin], dangerouslyDisableDeviceAuth: true}}}' "$CONFIG_FILE")
            else
                tmp=$(jq '. + {gateway: {mode: "local", controlUi: {dangerouslyDisableDeviceAuth: true}}}' "$CONFIG_FILE")
            fi
            echo "$tmp" > "$CONFIG_FILE"
        else
            local tmpfile
            tmpfile=$(mktemp)
            sed '1s/{/{\n  "gateway": { "mode": "local" },/' "$CONFIG_FILE" > "$tmpfile" && mv "$tmpfile" "$CONFIG_FILE"
        fi
        log_success "Added gateway.mode=local to config"
    fi
}

# ==============================================================
# Setup: Ensure gateway.http.endpoints is present (idempotent patch)
#
# Applied on every `setup` run so existing workspaces get the field
# added without needing to delete openclaw.json. Safe on fresh configs
# too — values are only written if missing.
# ==============================================================
ensure_http_endpoints() {
    [ -f "$CONFIG_FILE" ] || return 0
    if ! command -v jq &>/dev/null; then
        log_warn "jq not available — skipping gateway.http.endpoints patch"
        return 0
    fi
    local tmp
    tmp=$(jq '
        .gateway //= {}
        | .gateway.http //= {}
        | .gateway.http.endpoints //= {}
        | .gateway.http.endpoints.chatCompletions //= {}
        | .gateway.http.endpoints.chatCompletions.enabled //= true
        | .gateway.http.endpoints.responses //= {}
        | .gateway.http.endpoints.responses.enabled //= true
    ' "$CONFIG_FILE") && echo "$tmp" > "$CONFIG_FILE"
    log_success "Ensured gateway.http.endpoints.{chatCompletions,responses}.enabled"
}

# ==============================================================
# Setup: Install OpenClaw CLI
# ==============================================================
install_cli() {
    log "Installing OpenClaw CLI (non-interactive)..."
    export PATH="$HOME/.local/bin:$HOME/.openclaw/bin:$PATH"

    if command -v openclaw &>/dev/null; then
        log_success "OpenClaw CLI already installed: $(which openclaw)"
        return 0
    fi

    export OPENCLAW_NO_ONBOARD=1
    export OPENCLAW_NO_PROMPT=1
    export OPENCLAW_DISABLE_BONJOUR=1

    local cli_version="${OPENCLAW_VERSION:-latest}"
    log "Installing OpenClaw CLI version: $cli_version"
    local install_ok=1
    if [ "$cli_version" = "latest" ] || [ -z "$cli_version" ]; then
        curl -fsSL https://openclaw.ai/install.sh | bash -s -- --install-method npm && install_ok=0
    else
        curl -fsSL https://openclaw.ai/install.sh | bash -s -- --install-method npm --version "$cli_version" && install_ok=0
    fi
    if [ "$install_ok" -eq 0 ]; then
        log_success "OpenClaw CLI installed"
    else
        log_error "Failed to install OpenClaw CLI"
        exit 1
    fi

    # Refresh PATH
    export PATH="$HOME/.local/bin:$HOME/.openclaw/bin:$PATH"

    if command -v openclaw &>/dev/null; then
        log_success "OpenClaw CLI available: $(which openclaw)"
    else
        log_error "OpenClaw CLI not found in PATH after install"
        exit 1
    fi
}

# ==============================================================
# Setup: Install the @openclaw/codex plugin
#
# Runs right after install_cli on every `setup`. `openclaw plugins install`
# refuses a plugin id that is already installed (it points at
# `plugins update` instead), so probe `openclaw plugins list --json` first
# and skip when the plugin is present. A failed install is a warning, not
# fatal — the gateway must still start without the plugin.
# ==============================================================
OPENCLAW_CODEX_PLUGIN="@openclaw/codex"

install_codex_plugin() {
    if ! command -v openclaw &>/dev/null; then
        log_warn "openclaw CLI not found — skipping ${OPENCLAW_CODEX_PLUGIN} plugin install"
        return 0
    fi

    local listing
    listing="$(openclaw plugins list --json 2>/dev/null || true)"
    if printf '%s' "$listing" | grep -Eq '"(@openclaw/codex|codex)"'; then
        log "${OPENCLAW_CODEX_PLUGIN} plugin already installed — skipping"
        return 0
    fi

    log "Installing ${OPENCLAW_CODEX_PLUGIN} plugin..."
    if openclaw plugins install "$OPENCLAW_CODEX_PLUGIN" --accept-capabilities; then
        log_success "${OPENCLAW_CODEX_PLUGIN} plugin installed"
    else
        log_warn "Failed to install ${OPENCLAW_CODEX_PLUGIN} plugin — continuing without it"
    fi
}

# ==============================================================
# Setup: Install missing peer deps for OpenClaw extensions
#
# Some OpenClaw extensions import npm packages they don't bundle
# (e.g. "Cannot find module: @slack/web-api"). Each entry below maps a
# CHANNEL to the package(s) its extension needs. Telegram is bundled
# (grammy) and needs none.
#
# Only deps for channels actually enabled (ENABLED_CHANNELS) are installed,
# and any already present in the global node_modules are skipped — so a
# telegram-only workspace (the default) pulls nothing, and re-runs don't
# hit the registry. Without this, baileys (large) + slack were reinstalled
# on every boot regardless of enabled channels.
# ==============================================================

declare -A OPENCLAW_CHANNEL_PEER_DEPS=(
    [whatsapp]="@whiskeysockets/baileys"
    [slack]="@slack/web-api"
)

install_openclaw_peer_deps() {
    local raw="${ENABLED_CHANNELS:-}"
    local global_modules missing=()
    global_modules="$(npm root -g 2>/dev/null)"

    local ch pkg
    for ch in "${!OPENCLAW_CHANNEL_PEER_DEPS[@]}"; do
        # Channel not enabled → skip its deps entirely.
        echo "$raw" | grep -q "\"$ch\"" || continue
        for pkg in ${OPENCLAW_CHANNEL_PEER_DEPS[$ch]}; do
            if [ -n "$global_modules" ] && [ -d "$global_modules/$pkg" ]; then
                continue
            fi
            missing+=("$pkg")
        done
    done

    if [ "${#missing[@]}" -eq 0 ]; then
        log "No channel peer deps needed (enabled channels need none or already present)"
        return 0
    fi

    log "Installing OpenClaw peer deps: ${missing[*]}"
    if npm install -g "${missing[@]}"; then
        log_success "Peer deps installed"
    else
        log_warn "Peer deps install failed — extensions may fail at runtime"
    fi
}

# ==============================================================
# Gateway: Check if running
# ==============================================================
is_running() {
    clean_stale_pid
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
    else
        return 1
    fi
}

# ==============================================================
# Gateway: Launch background auto-restart loop (shared by start/restart)
# ==============================================================
_launch_gateway_loop() {
    rotate_log
    echo -e "${GREEN}Starting OpenClaw Gateway...${NC}"

    nohup bash -c '
        # Close inherited lock FD so flock is released when parent exits
        exec 9>&-

        pid_file="'"$PID_FILE"'"
        log_file="'"$LOG_FILE"'"
        log_max_size='"$LOG_MAX_SIZE"'

        # Cleanup on exit — remove PID file and kill child
        gateway_pid=""
        cleanup() {
            echo "[$(date "+%Y-%m-%d %H:%M:%S")] Shutting down (signal received)..."
            if [ -n "$gateway_pid" ] && kill -0 "$gateway_pid" 2>/dev/null; then
                # Kill the command and all its children (including openclaw-gateway binary)
                pkill -P "$gateway_pid" 2>/dev/null || true
                kill "$gateway_pid" 2>/dev/null
                wait "$gateway_pid" 2>/dev/null
            fi
            # Also kill any remaining openclaw-gateway binary processes
            pkill -x "openclaw-gateway" 2>/dev/null || true
            rm -f "$pid_file"
            exit 0
        }
        trap cleanup SIGTERM SIGINT SIGHUP EXIT

        restart_count=0
        max_restarts='"$MAX_RESTARTS"'
        restart_delay='"$RESTART_DELAY"'
        restart_window='"$RESTART_WINDOW"'

        while true; do
            # Rotate log inline
            if [ -f "$log_file" ]; then
                size=$(stat -c%s "$log_file" 2>/dev/null || stat -f%z "$log_file" 2>/dev/null || echo 0)
                if [ "$size" -ge "$log_max_size" ]; then
                    mv "$log_file" "${log_file}.1"
                    echo "[$(date "+%Y-%m-%d %H:%M:%S")] Log rotated" >> "$log_file"
                fi
            fi

            start_time=$(date +%s)
            echo "[$(date "+%Y-%m-%d %H:%M:%S")] Starting gateway (attempt $((restart_count + 1)))..."

            openclaw gateway run 2>&1 &
            gateway_pid=$!
            wait "$gateway_pid"
            exit_code=$?
            gateway_pid=""

            end_time=$(date +%s)
            uptime=$((end_time - start_time))
            echo "[$(date "+%Y-%m-%d %H:%M:%S")] Gateway exited with code $exit_code after ${uptime}s"

            if [ "$uptime" -ge "$restart_window" ]; then
                restart_count=0
            fi

            restart_count=$((restart_count + 1))
            if [ "$restart_count" -ge "$max_restarts" ]; then
                echo "[$(date "+%Y-%m-%d %H:%M:%S")] ERROR: Max restarts ($max_restarts) reached. Giving up."
                rm -f "$pid_file"
                exit 1
            fi

            echo "[$(date "+%Y-%m-%d %H:%M:%S")] Restarting in ${restart_delay}s... ($restart_count/$max_restarts)"
            sleep "$restart_delay"
        done
    ' >> "$LOG_FILE" 2>&1 &

    local pid=$!
    echo "$pid" > "$PID_FILE"
    echo -e "${GREEN}Gateway running (PID: $pid)${NC}"
    echo -e "Logs: ${YELLOW}$LOG_FILE${NC}"
}

# ==============================================================
# Gateway: Start
# ==============================================================
start_gateway() {
    acquire_lock

    if is_running; then
        echo -e "${YELLOW}Gateway is already running (PID: $(cat "$PID_FILE"))${NC}"
        return 0
    fi

    # Kill any orphan gateway processes (e.g. user ran "openclaw gateway run" directly)
    kill_orphan_gateways

    _launch_gateway_loop
}

# ==============================================================
# Gateway: Teardown — shared stop sequence for stop/restart.
#
# With a wrapper PID: SIGTERM → wait 10s → force-kill (+ children).
# Always: clean orphans, kill any bare openclaw-gateway binary, then wait
# for the gateway port to be released. Call with "" to run only the
# orphan/binary/port cleanup (restart's not-running path).
# ==============================================================
_teardown_gateway() {
    local pid="$1"

    if [ -n "$pid" ]; then
        # Send SIGTERM to the wrapper — trap will clean up the child
        kill "$pid" 2>/dev/null

        # Wait up to 10s for graceful shutdown
        for i in $(seq 1 10); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done

        # Force kill if still alive
        if kill -0 "$pid" 2>/dev/null; then
            log_warn "Graceful shutdown failed, force killing..."
            kill -9 "$pid" 2>/dev/null
            # Also kill any orphaned child
            pkill -9 -P "$pid" 2>/dev/null || true
        fi

        rm -f "$PID_FILE"
    fi

    # Also kill any orphan gateway processes
    kill_orphan_gateways

    # Explicitly kill the openclaw-gateway binary in case it was orphaned
    pkill -x "openclaw-gateway" 2>/dev/null || true
    sleep 1
    if pgrep -x "openclaw-gateway" >/dev/null 2>&1; then
        log_warn "openclaw-gateway binary still alive, force killing..."
        pkill -9 -x "openclaw-gateway" 2>/dev/null || true
    fi

    # Wait up to 5s for port 18789 to be released
    for i in $(seq 1 5); do
        if ! ss -tlnp 2>/dev/null | grep -q ':18789 ' && \
           ! netstat -tlnp 2>/dev/null | grep -q ':18789 '; then
            break
        fi
        sleep 1
    done
}

# ==============================================================
# Gateway: Stop (targeted, no greedy pkill)
# ==============================================================
stop_gateway() {
    acquire_lock

    if ! is_running; then
        # Even if our managed wrapper isn't running, kill orphan processes
        local orphans
        orphans=$(find_orphan_gateways)
        if [ -n "$orphans" ]; then
            log_warn "No managed gateway, but found orphan process(es) — cleaning up"
            kill_orphan_gateways
            echo -e "${GREEN}Stopped orphan gateway process(es).${NC}"
        else
            echo -e "${YELLOW}Gateway is not running.${NC}"
        fi
        return 0
    fi

    local pid
    pid=$(cat "$PID_FILE")
    echo -e "${RED}Stopping gateway (PID: $pid)...${NC}"

    _teardown_gateway "$pid"

    echo -e "${GREEN}Stopped.${NC}"
}

# ==============================================================
# Gateway: Status
# ==============================================================
status_gateway() {
    if is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        echo -e "${GREEN}Gateway auto-runner is running (PID: $pid)${NC}"
        openclaw gateway status 2>/dev/null || openclaw status 2>/dev/null || true
    else
        echo -e "${RED}Gateway auto-runner is not running.${NC}"
    fi

    # Warn about orphan processes
    local orphans
    orphans=$(find_orphan_gateways)
    if [ -n "$orphans" ]; then
        echo -e "${YELLOW}⚠ WARNING: Found unmanaged gateway process(es): ${orphans}${NC}"
        echo -e "${YELLOW}  These were likely started with 'openclaw gateway run' directly.${NC}"
        echo -e "${YELLOW}  Run '$0 stop' to clean them up, or '$0 restart' to take over.${NC}"
    fi
}

# ==============================================================
# Gateway: Restart (single operation — stop + wait + start)
# ==============================================================
restart_gateway() {
    acquire_lock

    local pid=""
    if is_running; then
        pid=$(cat "$PID_FILE")
        echo -e "${RED}Stopping gateway (PID: $pid) for restart...${NC}"
    fi

    # Tear down the wrapper (if any) plus orphans/binary/port. Passing an
    # empty pid runs only the cleanup, matching the previous always-clean
    # behaviour when no managed gateway was running.
    _teardown_gateway "$pid"

    # Start fresh
    _launch_gateway_loop
}

# ==============================================================
# Gateway: Logs
# ==============================================================
show_logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        echo "No log file found at $LOG_FILE"
    fi
}

# ==============================================================
# Setup: Install gateway guard (prevent direct "openclaw gateway run")
# ==============================================================
install_gateway_guard() {
    local guard_file="/etc/profile.d/openclaw-guard.sh"
    local setup_dir="$SCRIPT_DIR"

    log "Installing gateway guard..."
    sudo tee "$guard_file" > /dev/null <<GUARDEOF
# Intercept "openclaw gateway run" to prevent unmanaged gateway processes.
# All gateway lifecycle should go through agent.sh (start/stop/restart).
openclaw() {
    if [ "\$1" = "gateway" ] && [ "\${2:-}" = "run" ]; then
        echo "⚠  Do not run 'openclaw gateway run' directly."
        echo "   Use the managed gateway instead:"
        echo ""
        echo "     ${setup_dir}/agent.sh start    # start gateway"
        echo "     ${setup_dir}/agent.sh stop     # stop gateway"
        echo "     ${setup_dir}/agent.sh restart  # restart gateway"
        echo "     ${setup_dir}/agent.sh status   # check status"
        echo "     ${setup_dir}/agent.sh logs     # tail logs"
        echo ""
        echo "   This ensures PID tracking, auto-restart, and log rotation."
        return 1
    fi
    command openclaw "\$@"
}
GUARDEOF
    sudo chmod +x "$guard_file"
    log_success "Gateway guard installed (${guard_file})"
}

# ==============================================================
# Setup: Wire OpenRouter as a model via the openclaw CLI
# ==============================================================
# Uses the CLI config commands (mirrors manifest.json's plugin_enable /
# models_set templates) rather than hand-writing openclaw.json. No auth
# profile is created — the openrouter plugin reads OPENROUTER_API_KEY from the
# environment. Runs after install_cli so the `openclaw` binary is available.
configure_openrouter() {
    [ -z "${OPENROUTER_API_KEY:-}" ] && return 0
    if ! command -v openclaw &>/dev/null; then
        log_warn "openclaw CLI not found — skipping OpenRouter model setup"
        return 0
    fi

    log "Enabling OpenRouter plugin via CLI..."
    openclaw config set plugins.entries.openrouter.enabled true \
        || log_warn "Failed to enable openrouter plugin"

    # Make OpenRouter the primary model only when it's the selected provider:
    #   - no Anthropic/OpenAI key present (OpenRouter is the only provider), or
    #   - explicitly forced via OPENCLAW_PRIMARY_MODEL=openrouter/<model>.
    local set_primary=0
    if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
        set_primary=1
    fi
    case "${OPENCLAW_PRIMARY_MODEL:-}" in openrouter/*) set_primary=1 ;; esac

    if [ "$set_primary" = "1" ]; then
        local or_model="${OPENCLAW_PRIMARY_MODEL:-openrouter/auto}"
        log "Setting primary model to ${or_model} via CLI..."
        openclaw models set "$or_model" \
            || log_warn "Failed to set OpenRouter primary model"
    fi
}

# ==============================================================
# Setup: Full setup + start
# ==============================================================
run_setup() {
    log "OpenClaw OneClick Setup"
    log "======================"
    validate_env
    install_env
    enable_channels
    install_cli
    install_codex_plugin
    configure_openrouter
    install_openclaw_peer_deps
    log "Running config doctor..."
    if openclaw doctor --fix --yes 2>/dev/null; then
        log_success "Config validated"
    else
        log_warn "openclaw doctor not available or config needs manual review"
    fi
    ensure_gateway_mode
    ensure_http_endpoints
    install_gateway_guard
    start_gateway
}

# ==============================================================
# Main
# ==============================================================
case "${1:-setup}" in
    setup)   run_setup ;;
    start)   start_gateway ;;
    stop)    stop_gateway ;;
    restart) restart_gateway ;;
    status)  status_gateway ;;
    logs)    show_logs ;;
    *)       echo "Usage: $0 {setup|start|stop|restart|status|logs}"; exit 1 ;;
esac