# Shared by infra/commands/*.sh (sourced, not a command): find this checkout's server.
# The server writes ~/.config/quirq/install.json on every boot. Trust it only when its repo_dir is
# this checkout, since another install on the same machine rewrites the same file.

_qq_pointer() {
    python3 - "${XDG_CONFIG_HOME:-$HOME/.config}/quirq/install.json" "$(pwd -P)" "$1" <<'PY'
import json, os, sys
try:
    d = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    sys.exit(1)
if os.path.realpath(d.get("repo_dir", "")) != sys.argv[2] or not d.get(sys.argv[3]):
    sys.exit(1)
print(d[sys.argv[3]])
PY
}

qq_port()  { [ -n "${PORT:-}" ] && echo "$PORT" && return; _qq_pointer port || echo 5002; }
qq_state() { [ -n "${QUIRQ_STATE_ROOT:-}" ] && echo "$QUIRQ_STATE_ROOT" && return; _qq_pointer state_root || echo "$(pwd)/.quirq"; }
qq_url()   { echo "http://127.0.0.1:$(qq_port)"; }

# qq_call METHOD PATH [JSON]: print the reply body on 2xx; otherwise print the server's own error
# message ({"detail": {"code", "message"}}) to stderr and return 1.
qq_call() {
    _url=$(qq_url)
    if [ -n "${3:-}" ]; then
        _out=$(curl -s -X "$1" -H 'Content-Type: application/json' --data "$3" -w '\n%{http_code}' "$_url$2")
    else
        _out=$(curl -s -X "$1" -w '\n%{http_code}' "$_url$2")
    fi || { echo "no answer from $_url, is the server running" >&2; return 1; }
    _code=$(printf '%s\n' "$_out" | tail -n 1)
    _body=$(printf '%s\n' "$_out" | sed '$d')
    case "$_code" in 2*) printf '%s\n' "$_body"; return 0 ;; esac
    python3 - "$_body" "$_code" <<'PY' >&2
import json, sys
try:
    d = json.loads(sys.argv[1]).get("detail")
except Exception:
    d = None
msg = d.get("message") if isinstance(d, dict) else d
print(f"HTTP {sys.argv[2]}: {msg or sys.argv[1][:300]}")
PY
    return 1
}

# qq_path_part TEXT: TEXT encoded for one URL path segment.
qq_path_part() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
