#!/usr/bin/env bash
# ==============================================================
# config/agents/claude_code/setup.sh — install + configure the
# claude_code agent.
#
# Invoked by xo-space's FastAPI lifespan (server.py's
# _run_agent_setup) when AGENT_NAME=claude_code. Runs every time
# the xo-space server starts; each step is idempotent so
# repeat invocations are cheap (≈ seconds) once the first run is
# done.
#
# This script absorbs the claude_code bootstrap work that used to
# live in the Coder template's startup_script (main-claude.tf), so
# the template can become a thin shell. Reads its inputs from
# environment vars that the Coder k8s pod injects.
#
# Unlike hermes / openclaw, claude_code has no separate gateway
# lifecycle script (no `claude.sh`), no channels, and no shims —
# the Claude CLI is invoked directly by xo-space's
# ClaudeCodeClient via subprocess. So this setup is intentionally
# shorter than its siblings.
#
# Required env (server will misbehave without one of these):
#     ANTHROPIC_API_KEY  or  CLAUDE_CODE_OAUTH_TOKEN
#
# Optional env:
#     XO_AUTH_SESSION_ID / XO_POLL_TOKEN / XO_API_KEY
#     CHAT_API_BASE_URL   default: https://api-swarm-dev.xo.builders
#     NODE_VERSION        default: 22
# ==============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] claude-setup: $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

# Decide whether sudo is needed and available. Many container images run
# the app as the same user that owns / so sudo isn't always required.
if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    SUDO=""
fi

# ==============================================================
# Step 1 — apt prereqs (jq, unzip).
# `jq` and `unzip` are not strictly required by the Claude CLI itself,
# but several xo-space routes (and the Claude installer for some
# distros) lean on them. Keep this list lean: anything that's needed
# *before* cowork-api can boot already lives in the template.
# ==============================================================
install_apt_prereqs() {
    local missing=()
    command -v jq    >/dev/null 2>&1 || missing+=("jq")
    command -v unzip >/dev/null 2>&1 || missing+=("unzip")

    if [ "${#missing[@]}" -eq 0 ]; then
        log "apt prereqs already present (jq, unzip)"
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
        log_warn "apt-get install failed for: ${missing[*]} — claude_code may degrade"
    fi
}

# ==============================================================
# Step 2 — NVM + Node. The Claude CLI binary itself is self-
# contained, but main-claude.tf has always installed Node so that
# users have a working Node toolchain inside the workspace (common
# for the kinds of repos this agent edits). Keep parity.
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
            log_error "NVM install failed — Node toolchain will be unavailable"
            return 1
        fi
    fi

    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh"

    local node_version="${NODE_VERSION:-22}"
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
# Step 3 — Materialise $REPO_ROOT/.env from environment vars.
# xo-space's load_dotenv() reads this on import; the Claude CLI is
# then invoked with the resulting process env. Hosted workspace edits are
# preserved. Managed local Quirq refreshes the file on every boot so values
# saved by the Setup tab in ~/.quirq/secrets.env take effect.
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
# Agent dispatch
AGENT_NAME=claude_code

# Claude / Anthropic auth — claude_code prefers CLAUDE_CODE_OAUTH_TOKEN,
# but falls back to ANTHROPIC_API_KEY if that's all the user supplied.
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-${ANTHROPIC_API_KEY:-}}

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
# Step 4 — Ensure the Claude CLI exists. Hosted workspaces normally bake it
# into the base image; the standalone Docker installer does not. Installing
# on demand keeps both environments idempotent and makes the local setup
# self-contained after the user selects this backend.
# ==============================================================
ensure_claude_cli() {
    export PATH="$HOME/.claude/bin:$HOME/.local/bin:$PATH"
    if command -v claude >/dev/null 2>&1; then
        log_success "claude CLI on PATH at $(command -v claude)"
        return 0
    fi
    if ! command -v npm >/dev/null 2>&1; then
        log_warn "npm is unavailable — cannot install the Claude CLI"
        return 1
    fi

    log "Claude CLI not found — installing @anthropic-ai/claude-code"
    if npm install -g @anthropic-ai/claude-code; then
        hash -r
        if command -v claude >/dev/null 2>&1; then
            log_success "claude CLI installed at $(command -v claude)"
            return 0
        fi
    fi
    log_warn "Claude CLI installation did not produce a usable 'claude' command"
    return 1
}

# ==============================================================
# Step 5 — Interactive-shell login guard (~/.bashrc on the PVC).
# The workspace pod env injects ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN
# into every shell (ttyd, code-server, exec), and the Claude CLI weighs env
# tokens ABOVE the native login in ~/.claude/.credentials.json. After
# "Connect Claude" succeeds, a plain `claude` in the terminal would still
# use the env token and look logged-out. The API's chat path already strips
# these vars (adapter._subprocess_env); this guard gives interactive shells
# the same precedence.
#
# ~/.bashrc lives on the PVC so the guard survives workspace restarts even
# though /opt/xo-space is reset from the image; this script re-runs on
# every boot and rewrites the managed block, so image updates ship guard
# updates too. Details: docs/connect-claude-login-issues-investigation.md (S1).
# ==============================================================
install_login_guard() {
    local rc="$HOME/.bashrc"
    local begin='# >>> xo-cowork claude_code login guard >>>'
    local end='# <<< xo-cowork claude_code login guard <<<'

    if ! touch "$rc" 2>/dev/null; then
        log_warn "cannot write $rc — interactive login guard not installed"
        return 0
    fi

    if grep -qF "$begin" "$rc"; then
        if grep -qF "$end" "$rc"; then
            # Refresh: drop the old managed block so this image's version lands.
            sed -i '/^# >>> xo-cowork claude_code login guard >>>$/,/^# <<< xo-cowork claude_code login guard <<<$/d' "$rc"
        else
            log_warn "login guard block in $rc is missing its end marker — appending a fresh block (bash uses the last definition)"
        fi
    fi

    cat >> "$rc" <<'GUARD'
# >>> xo-cowork claude_code login guard >>>
# Managed by config/agents/claude_code/setup.sh — rewritten on every
# xo-space boot; edits inside this block will be lost.
# When a native Claude login exists (~/.claude/.credentials.json, written by
# "Connect Claude"), strip the pod-injected token env vars for `claude`
# invocations — the CLI would otherwise prefer them over the fresh login.
# Without a native login, pass the env through so explicit API-key use works.
claude() {
    if [ -s "$HOME/.claude/.credentials.json" ]; then
        env -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY -u ANTHROPIC_OAUTH_API_KEY claude "$@"
    else
        command claude "$@"
    fi
}
# <<< xo-cowork claude_code login guard <<<
GUARD
    log_success "interactive-shell login guard installed in ~/.bashrc"

    # Login shells (bash -l) read ~/.bash_profile / ~/.profile, NOT ~/.bashrc.
    # A fresh PVC has no rc files at all, so without this chain a login shell
    # would never load the guard. Mirror the Ubuntu skeleton: make the login rc
    # source ~/.bashrc. Managed with the same marker pattern; skipped when the
    # user's login rc already references .bashrc.
    local login_rc="$HOME/.profile"
    [ -f "$HOME/.bash_profile" ] && login_rc="$HOME/.bash_profile"
    if [ -f "$login_rc" ] && grep -q '\.bashrc' "$login_rc"; then
        log "login shell rc ($login_rc) already sources ~/.bashrc"
        return 0
    fi
    if ! touch "$login_rc" 2>/dev/null; then
        log_warn "cannot write $login_rc — login shells will not load the guard"
        return 0
    fi
    if ! grep -qF '# >>> xo-cowork bashrc chain >>>' "$login_rc"; then
        cat >> "$login_rc" <<'CHAIN'
# >>> xo-cowork bashrc chain >>>
# Managed by config/agents/claude_code/setup.sh: login shells source ~/.bashrc
# (standard Ubuntu skeleton behavior) so PVC-managed shell config loads there too.
if [ -n "$BASH_VERSION" ] && [ -f "$HOME/.bashrc" ]; then
    . "$HOME/.bashrc"
fi
# <<< xo-cowork bashrc chain <<<
CHAIN
        log_success "login-shell → ~/.bashrc chain installed in $login_rc"
    fi
}

# ==============================================================
# Step 6 — Remote Control gate seeding (~/.claude.json).
# `claude remote-control` shows two interactive gates on first use: the
# workspace-trust dialog and an "Enable Remote Control?" prompt. Pre-clear both
# so the API can launch it headless: home-dir trust (inherited by every dir
# under it, xo-projects included) and the enable dialog. Idempotent: skips the
# write when both are already set, so it never races the CLI (which rewrites
# ~/.claude.json on exit). Also re-checked by remote_control.ensure_gates_seeded().
# ==============================================================
seed_remote_control_config() {
    local cfg="$HOME/.claude.json"
    local root="${XO_RC_DIR:-$HOME}"
    root="${root/#\~/$HOME}"
    root="$(cd "$root" 2>/dev/null && pwd -P || echo "$root")"

    if ! command -v jq >/dev/null 2>&1; then
        log_warn "jq not found — skipping Remote Control gate seed"
        return 0
    fi

    local current='{}'
    [ -s "$cfg" ] && current="$(cat "$cfg")"

    if printf '%s' "$current" | jq -e --arg r "$root" \
        '(.remoteDialogSeen == true) and (.projects[$r].hasTrustDialogAccepted == true)' \
        >/dev/null 2>&1; then
        log "Remote Control gates already seeded (trust: $root)"
        return 0
    fi

    local tmp; tmp="$(mktemp "${cfg}.xo-rc.XXXXXX")"
    if printf '%s' "$current" | jq --arg r "$root" \
        '.remoteDialogSeen = true | .projects[$r].hasTrustDialogAccepted = true' \
        > "$tmp" 2>/dev/null; then
        mv "$tmp" "$cfg"
        log_success "seeded Remote Control gates (trust: $root)"
    else
        rm -f "$tmp"
        log_warn "could not seed Remote Control config (jq failed) — leaving ~/.claude.json untouched"
    fi
}

# ==============================================================
# Main
# ==============================================================
log "Starting claude_code agent bootstrap"
install_apt_prereqs
install_node
write_env_file
ensure_claude_cli
install_login_guard
seed_remote_control_config
log_success "claude_code agent bootstrap complete"
exit 0
