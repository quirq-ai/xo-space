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
