#!/usr/bin/env bash
# Reset an OLD Space's local Composio state so it starts over as a brand-new user.
#
# Only acts on an old-format store (sessions.json whose account_id is not an XO
# account id "user_...", or that has no space_id stamp). On a current store it
# prints "nothing to do" and exits 0, so it is safe to run before every
# ./cowork-api.sh start.
#
# Usage: ./composio-reset.sh [--dry-run] [--force] [--yes] [--revoke] [--include-key] [--no-restart]
#
#   --dry-run      show what would be removed, change nothing
#   --force        reset even a current store (asks first unless --yes)
#   --yes          don't ask for confirmation with --force
#   --revoke       also DELETE the old account's connected accounts at Composio
#                  (gmail, calendar, ...). Connections are account-wide, so this
#                  disconnects them in EVERY Space that uses the same account.
#   --include-key  also remove api_key.json (you will have to paste the key again)
#   --no-restart   don't stop/start the API (stop it yourself first, or the
#                  running server will write the old account id back)
#
# What it clears (after backing it up to ~/.config/composio.bak-<timestamp>):
#   identity.json      cached XO account id  -> re-resolved from xo-swarm-api
#   sessions.json      account/space stamps, session, proxy tokens
#   space_scope.json   which toolkits/connections this Space has enabled
#   action_prefs.json  per-action preferences
#   workspace_scope.json and xo-space/data/composio_*.json legacy copies, which
#   the server would otherwise move back into place on the next start.
#
# Standalone: it only touches those files and calls ./cowork-api.sh stop/start.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
API_SH="$SCRIPT_DIR/cowork-api.sh"
COMPOSIO_API="${COMPOSIO_BASE_URL:-https://backend.composio.dev}"

ASSUME_YES=0
REVOKE=0
INCLUDE_KEY=0
RESTART=1
FORCE=0
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes)      ASSUME_YES=1 ;;
        --force)       FORCE=1 ;;
        --dry-run)     DRY_RUN=1 ;;
        --revoke)      REVOKE=1 ;;
        --include-key) INCLUDE_KEY=1 ;;
        --no-restart)  RESTART=0 ;;
        -h|--help)     sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)"; exit 1 ;;
    esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()         { echo -e "[$(date '+%H:%M:%S')] $*"; }
log_success() { log "${GREEN}✓ $*${NC}"; }
log_warn()    { log "${YELLOW}⚠ $*${NC}"; }
log_error()   { log "${RED}✗ $*${NC}"; }

# Same approach as cowork-api.sh: read one scalar from .env without sourcing it.
read_dotenv_value() {
    local key="$1" line value
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
                return 0 ;;
        esac
    done < "$ENV_FILE"
}

PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { log_error "python3 is required"; exit 1; }

# Same resolution as services/cowork_agent/connectors/composio/paths.py.
STORE_DIR="${COMPOSIO_STORE_DIR:-$(read_dotenv_value COMPOSIO_STORE_DIR)}"
STORE_DIR="${STORE_DIR:-$HOME/.config/composio}"
STORE_DIR="${STORE_DIR/#\~/$HOME}"

STORE_FILES=(identity.json sessions.json space_scope.json action_prefs.json workspace_scope.json)
[ "$INCLUDE_KEY" -eq 1 ] && STORE_FILES+=(api_key.json)
LEGACY_FILES=(
    "$SCRIPT_DIR/data/composio_sessions.json"
    "$SCRIPT_DIR/data/composio_action_prefs.json"
    "$SCRIPT_DIR/data/composio_space_scope.json"
)

json_get() {  # json_get <file> <key>
    [ -f "$1" ] || return 0
    "$PY" - "$1" "$2" <<'EOF' 2>/dev/null
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")
except Exception:
    pass
EOF
}

OLD_ACCOUNT="$(json_get "$STORE_DIR/sessions.json" account_id)"
[ -n "$OLD_ACCOUNT" ] || OLD_ACCOUNT="$(json_get "$STORE_DIR/identity.json" account_id)"
OLD_SPACE="$(json_get "$STORE_DIR/sessions.json" space_id)"
NEW_SPACE="${XO_SPACE_ID:-$(read_dotenv_value XO_SPACE_ID)}"
PINNED_ACCOUNT="${XO_ACCOUNT_ID:-$(read_dotenv_value XO_ACCOUNT_ID)}"

SESSIONS_HAS_SPACE="$(json_get "$STORE_DIR/sessions.json" space_id)"

# Old-format store: written before connections were keyed by the XO account.
# Its account_id is a bare UUID (not "user_..."), and sessions.json carries no
# space_id stamp. A current store is left alone unless --force is given.
IS_OLD=0
REASON=""
if [ -f "$STORE_DIR/sessions.json" ]; then
    case "$OLD_ACCOUNT" in
        user_*) ;;
        "")     IS_OLD=1; REASON="sessions.json has no account_id" ;;
        *)      IS_OLD=1; REASON="account_id '$OLD_ACCOUNT' is not an XO account id (user_...)" ;;
    esac
    if [ "$IS_OLD" -eq 0 ] && [ -z "$SESSIONS_HAS_SPACE" ]; then
        IS_OLD=1; REASON="sessions.json has no space_id stamp"
    fi
fi

if [ "$IS_OLD" -eq 0 ] && [ "$FORCE" -eq 0 ]; then
    log_success "Composio store is already current (account ${OLD_ACCOUNT:-<none>}); nothing to do."
    exit 0
fi
[ "$IS_OLD" -eq 1 ] && log_warn "Old-format Composio store detected: $REASON"
[ "$IS_OLD" -eq 0 ] && log_warn "Store is current, but --force was given"

echo -e "${CYAN}Composio store:${NC}   $STORE_DIR"
echo -e "${CYAN}Old account id:${NC}   ${OLD_ACCOUNT:-<none>}"
echo -e "${CYAN}Old space id:${NC}     ${OLD_SPACE:-<none>}"
echo -e "${CYAN}Current space id:${NC} ${NEW_SPACE:-<unset>}"
echo
echo "Will remove:"
for f in "${STORE_FILES[@]}"; do [ -e "$STORE_DIR/$f" ] && echo "  $STORE_DIR/$f"; done
for f in "${LEGACY_FILES[@]}"; do [ -e "$f" ] && echo "  $f"; done
[ "$INCLUDE_KEY" -eq 0 ] && echo "Keeping api_key.json (use --include-key to remove it too)"
[ "$REVOKE" -eq 1 ] && echo -e "${YELLOW}Will also DELETE the connected accounts of ${OLD_ACCOUNT:-?} at Composio (all Spaces).${NC}"
echo

if [ "$DRY_RUN" -eq 1 ]; then
    log "Dry run: nothing changed."
    exit 0
fi

# Only a --force on a current store asks; an old store is reset without a prompt
# so this can run unattended before ./cowork-api.sh start.
if [ "$IS_OLD" -eq 0 ] && [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "Continue? [y/N] " answer
    case "$answer" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

# --- 1. Stop the API if it is running: it keeps the account id in memory and
#        would write the old one back.
WAS_RUNNING=0
if [ "$RESTART" -eq 1 ] && [ -x "$API_SH" ]; then
    if "$API_SH" status 2>/dev/null | grep -q "is running"; then
        WAS_RUNNING=1
        log "Stopping XO Space API..."
        "$API_SH" stop || log_warn "cowork-api.sh stop reported a problem; continuing"
    fi
fi

# --- 2. Optionally revoke the connections at Composio (before the key is removed).
if [ "$REVOKE" -eq 1 ]; then
    API_KEY="$(json_get "$STORE_DIR/api_key.json" api_key)"
    [ -n "$API_KEY" ] || API_KEY="${COMPOSIO_API_KEY:-$(read_dotenv_value COMPOSIO_API_KEY)}"
    if [ -z "$API_KEY" ]; then
        log_warn "No Composio API key found; skipping --revoke"
    else
        log "Revoking connected accounts at Composio..."
        COMPOSIO_API_KEY="$API_KEY" "$PY" - "$COMPOSIO_API" "$OLD_ACCOUNT" "$STORE_DIR/space_scope.json" <<'EOF'
import json, os, sys, urllib.parse, urllib.request

base, account, scope_path = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
headers = {"x-api-key": os.environ["COMPOSIO_API_KEY"], "Accept": "application/json"}

def call(method, url):
    req = urllib.request.Request(url, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else {}

ids = set()
# Everything the old account holds in this Composio project.
if account:
    cursor = None
    while True:
        q = {"user_ids": account, "limit": "100"}
        if cursor:
            q["cursor"] = cursor
        try:
            page = call("GET", f"{base}/api/v3/connected_accounts?{urllib.parse.urlencode(q)}")
        except Exception as e:
            print(f"  could not list connections: {e}")
            break
        ids.update(i["id"] for i in page.get("items", []) if i.get("id"))
        cursor = page.get("next_cursor")
        if not cursor:
            break
# Plus whatever this Space had pinned locally.
try:
    for tk in (json.load(open(scope_path)).get("toolkits") or {}).values():
        ids.update(tk.get("connected_account_ids") or [])
except Exception:
    pass

if not ids:
    print("  no connected accounts found")
failed = 0
for cid in sorted(ids):
    try:
        call("DELETE", f"{base}/api/v3/connected_accounts/{urllib.parse.quote(cid)}")
        print(f"  deleted {cid}")
    except Exception as e:
        failed += 1
        print(f"  FAILED  {cid}: {e}")
sys.exit(1 if failed else 0)
EOF
        [ $? -eq 0 ] && log_success "Remote connections revoked" || log_warn "Some connections could not be revoked"
    fi
fi

# --- 3. Back up, then remove the local stores.
BACKUP_DIR="${STORE_DIR%/}.bak-$(date +%Y%m%d-%H%M%S)"
removed=0
for f in "${STORE_FILES[@]}"; do
    src="$STORE_DIR/$f"
    [ -e "$src" ] || continue
    mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"
    cp -p "$src" "$BACKUP_DIR/" && rm -f "$src" && removed=$((removed + 1))
done
for src in "${LEGACY_FILES[@]}"; do
    [ -e "$src" ] || continue
    mkdir -p "$BACKUP_DIR/legacy-data" && chmod 700 "$BACKUP_DIR"
    cp -p "$src" "$BACKUP_DIR/legacy-data/" && rm -f "$src" && removed=$((removed + 1))
done
if [ "$removed" -gt 0 ]; then
    log_success "Removed $removed file(s); backup in $BACKUP_DIR"
else
    log_warn "Nothing to remove; the store was already clean"
fi

if [ -n "$PINNED_ACCOUNT" ]; then
    log_warn "XO_ACCOUNT_ID=$PINNED_ACCOUNT is set in the environment/.env; it pins the account id. Remove it if that is the old account."
fi

# --- 4. Start the API again if we stopped it, so it resolves the new identity.
if [ "$WAS_RUNNING" -eq 1 ]; then
    log "Starting XO Space API..."
    "$API_SH" start || { log_error "Start failed; run ./cowork-api.sh start and check the log"; exit 1; }
elif [ "$RESTART" -eq 0 ]; then
    log "Skipped stop/start. Run ./cowork-api.sh restart now."
fi

echo
log_success "Done. This Space will be treated as a new user: sign in, then reconnect every tool."
