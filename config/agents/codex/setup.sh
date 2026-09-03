#!/usr/bin/env bash
# ==============================================================
# config/agents/codex/setup.sh — install + configure the codex
# (OpenAI Codex CLI) agent.
#
# Invoked by xo-space's FastAPI lifespan (server.py's
# _run_agent_setup) when AGENT_NAME=codex. Runs on every server
# boot; each step is idempotent so repeat invocations are cheap
# once the first run is done.
#
# Unlike hermes / openclaw, codex has no gateway lifecycle script
# and no channels — the `codex` CLI is invoked directly by the
# adapter via subprocess, like claude_code / antigravity.
#
# Auth model: codex authenticates via a user-driven device flow
# (`codex login`, or POST /connect/codex in this API) that writes
# ~/.codex/auth.json — OR an OPENAI_API_KEY. There is NO env-token
# precedence chain to fix up (so, unlike claude_code, NO ~/.bashrc
# login guard). This script only REPORTS login state so a logged-out
# workspace surfaces in the boot log; it never blocks on a browser.
#
# Required env (chat 500s without one of these OR a completed
# /connect/codex device login):
#     OPENAI_API_KEY   (or CODEX_API_KEY)
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
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
AGENT_ENV_FILE="$CODEX_HOME_DIR/.env"
PROVISIONING_LOG="$CODEX_HOME_DIR/provisioning.log"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()         { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] codex-setup: $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

# Append a one-line marker to the manifest's provisioning_log (best-effort).
prov() {
    mkdir -p "$CODEX_HOME_DIR" 2>/dev/null || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] codex-setup: $*" >> "$PROVISIONING_LOG" 2>/dev/null || true
}

# Decide whether sudo is needed and available.
if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else SUDO=""; fi

# ==============================================================
# Step 1 — apt prereqs (jq, unzip). Mirrors claude_code/setup.sh:63-85.
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
        log_warn "apt-get install failed for: ${missing[*]} — codex may degrade"
    fi
}

# ==============================================================
# Step 2 — NVM + Node. Mirrors claude_code/setup.sh:94-130 (parity
# toolchain for the repos this agent edits). Skip if already present.
# ==============================================================
install_node() {
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

    $SUDO tee /etc/profile.d/nvm.sh >/dev/null <<'EOF' || true
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
EOF
    $SUDO chmod +x /etc/profile.d/nvm.sh 2>/dev/null || true
    log_success "node $(node -v), npm $(npm -v) ready"
}

# ==============================================================
# Step 3 — Materialise $REPO_ROOT/.env (the file load_dotenv() reads
# at server.py:29 / settings.py:17). Carries AGENT_NAME=codex for
# dispatch + auth passthrough. Mirrors claude_code/setup.sh:139-164
# but DROPS the OAuth-token precedence line (codex has no such chain).
# Never overwrites an existing .env (manual edits preserved).
# ==============================================================
write_repo_env_file() {
    if [ -f "$ENV_FILE" ]; then
        log ".env already exists at $ENV_FILE — leaving it untouched"
        return 0
    fi
    log "Writing .env from environment (first time)"
    umask 077
    cat > "$ENV_FILE" <<ENVEOF
# Agent dispatch
AGENT_NAME=codex

# OpenAI / Codex auth — codex uses OPENAI_API_KEY (API-key mode) or a
# device-flow login (~/.codex/auth.json, via POST /connect/codex).
# CODEX_API_KEY is accepted as an alias and mapped onto OPENAI_API_KEY.
OPENAI_API_KEY=${OPENAI_API_KEY:-${CODEX_API_KEY:-}}
CODEX_API_KEY=${CODEX_API_KEY:-}

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
# Step 4 — Seed the manifest env_file ~/.codex/.env. This is the file
# agent_env.py:28 reads (get_active_agent().env_file) and the core
# connect flow writes tokens into (codex_setup.py:99). Create it (600)
# with OPENAI_API_KEY when provided; never clobber an existing file so
# a completed /connect/codex token survives re-runs.
# ==============================================================
seed_agent_env() {
    mkdir -p "$CODEX_HOME_DIR"
    if [ -f "$AGENT_ENV_FILE" ]; then
        log "agent env already exists at $AGENT_ENV_FILE — leaving it untouched"
        return 0
    fi
    local key="${OPENAI_API_KEY:-${CODEX_API_KEY:-}}"
    log "Seeding agent env → $AGENT_ENV_FILE"
    umask 077
    cat > "$AGENT_ENV_FILE" <<AGENTENVEOF
# Managed by config/agents/codex/setup.sh (first run only).
# Read by agent_env.py; the /connect/codex device flow upserts a token here.
OPENAI_API_KEY=${key}
AGENTENVEOF
    chmod 600 "$AGENT_ENV_FILE"
    log_success "agent env seeded (mode 600)"
}

# ==============================================================
# Step 5 — codex CLI sanity check. The binary is installed by the
# workspace template; this only logs whether it landed on PATH so a
# missing install surfaces here instead of only at first prompt.
# Mirrors claude_code/setup.sh:174-181 + antigravity/setup.sh:72-80.
# ==============================================================
check_codex_cli() {
    export PATH="$HOME/.local/bin:$HOME/.codex/bin:$PATH"
    if command -v codex >/dev/null 2>&1; then
        local ver; ver="$(codex --version 2>/dev/null | head -1)"
        log_success "codex CLI on PATH at $(command -v codex) (version ${ver:-unknown})"
        prov "codex CLI present: ${ver:-unknown}"
    else
        log_warn "codex CLI not on PATH — template install may have failed; chat will 500"
        prov "codex CLI MISSING on PATH"
    fi
}

# ==============================================================
# Step 6 — Login state report (NON-BLOCKING). There is no headless
# browser login here; first-time login is user-driven via `codex login`
# or POST /connect/codex. We only detect + report so the state is visible
# in the boot log. Mirrors antigravity/setup.sh:88-121 technique, but
# keys off `codex login status` EXIT CODE (rc=1 => logged out; verified
# groundtruth) — `codex login status` has NO --json.
#
# CAVEAT (verified in the capture env): `codex login status` can report
# "Not logged in" with no auth.json even while real sessions ran and a
# live exec reached OpenAI. So this is advisory ONLY — never trust it to
# gate chat, and never hard-fail the boot on it.
# ==============================================================
check_login() {
    if ! command -v codex >/dev/null 2>&1; then
        log_warn "cannot probe codex login — CLI not on PATH"
        return 0
    fi
    # auth.json (device-flow or api-key mode) is the strongest positive signal.
    if [ -f "$CODEX_HOME_DIR/auth.json" ]; then
        log_success "codex auth.json present ($CODEX_HOME_DIR/auth.json) — treating as logged in"
        prov "login: auth.json present"
        return 0
    fi
    # Fall back to the CLI probe (report-only, 15s ceiling, never blocks).
    # TODO(codex): confirm the exact `codex login status` logged-IN string + rc=0
    # (capture env is logged-out). Safe default below: ANY non-zero rc => logged out.
    local out rc
    out="$(timeout 15 codex login status 2>/dev/null)"; rc=$?
    if [ "$rc" -eq 0 ]; then
        log_success "codex login status OK (${out:-logged in})"
        prov "login: status rc=0"
    else
        log_warn "codex appears LOGGED OUT (login status rc=$rc: ${out:-Not logged in}) — chat will error and /models/status will report 'error' until you authenticate via \`codex login\` or POST /connect/codex."
        prov "login: LOGGED OUT (rc=$rc)"
    fi
    return 0
}

# ==============================================================
# Main
# ==============================================================
log "Starting codex agent bootstrap"
install_apt_prereqs
install_node
write_repo_env_file
seed_agent_env
check_codex_cli
check_login
log_success "codex agent bootstrap complete"
exit 0
