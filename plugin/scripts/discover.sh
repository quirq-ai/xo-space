#!/usr/bin/env bash
# discover.sh — read-only discovery of the local Quirq install.
#
# Performs NO actions: a few file checks and short GET requests, then one
# JSON object on stdout. Starting, installing, or updating Quirq is a human
# decision made after reading this output — never this script's job.
#
# States:
#   running        a Quirq server answered /health
#   installed      a checkout exists on disk but no server is running
#   not_installed  neither a live server nor a checkout was found
#
# Sources, in order:
#   1. ~/.config/quirq/install.json — written by server.py on every boot
#      (a last-known-location hint; paths are verified before being trusted)
#   2. health probes on 127.0.0.1: pointer port first, then 5002, 5003
#   3. the current directory (./xo-space/server.py, or being inside a checkout)
set -u

POINTER_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/quirq/install.json"
CURL_TIMEOUT=2

# Extract a string field from one-level JSON without jq/python. Good enough
# for a file we write ourselves; paths with spaces survive (split on quotes).
json_str() { # $1=file $2=key
    grep -o "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$1" 2>/dev/null |
        head -1 | sed 's/.*:[[:space:]]*"//; s/"$//'
}

json_num() { # $1=file $2=key
    grep -o "\"$2\"[[:space:]]*:[[:space:]]*[0-9]*" "$1" 2>/dev/null |
        head -1 | grep -o '[0-9]*$'
}

emit() { # $1=state $2=base_url $3=repo_dir $4=projects_root $5=state_root
    printf '{"state":"%s","base_url":"%s","repo_dir":"%s","projects_root":"%s","state_root":"%s","pointer_file":"%s"}\n' \
        "$1" "$2" "$3" "$4" "$5" "$POINTER_FILE"
}

probe_health() { # $1=port → 0 if a healthy Quirq answered
    local body
    body="$(curl -fsS -m "$CURL_TIMEOUT" "http://127.0.0.1:$1/health" 2>/dev/null)" || return 1
    # Not just "any 200": make sure it is Quirq, not another local service.
    printf '%s' "$body" | grep -q '"status"[[:space:]]*:[[:space:]]*"healthy"'
}

pointer_repo=""
pointer_projects=""
pointer_state=""
pointer_port=""
if [ -f "$POINTER_FILE" ]; then
    pointer_repo="$(json_str "$POINTER_FILE" repo_dir)"
    pointer_projects="$(json_str "$POINTER_FILE" projects_root)"
    pointer_state="$(json_str "$POINTER_FILE" state_root)"
    pointer_port="$(json_num "$POINTER_FILE" port)"
fi

# --- 1. Is a server running? Pointer port first, then the defaults. ------
# QUIRQ_DISCOVER_PORTS overrides the defaults for non-standard setups
# (and lets tests point the probe at known-dead ports).
default_ports="${QUIRQ_DISCOVER_PORTS:-5002 5003}"
candidate_ports=""
[ -n "$pointer_port" ] && candidate_ports="$pointer_port"
for p in $default_ports; do
    [ "$p" = "${pointer_port:-}" ] || candidate_ports="$candidate_ports $p"
done

for port in $candidate_ports; do
    if probe_health "$port"; then
        emit running "http://127.0.0.1:$port" "$pointer_repo" "$pointer_projects" "$pointer_state"
        exit 0
    fi
done

# --- 2. Not running. Does a checkout exist where the pointer says? -------
if [ -n "$pointer_repo" ] && [ -f "$pointer_repo/server.py" ] && [ -f "$pointer_repo/requirements.txt" ]; then
    emit installed "" "$pointer_repo" "$pointer_projects" "$pointer_state"
    exit 0
fi

# --- 3. No (valid) pointer. Last resort: look around the cwd. ------------
# Covers installs that predate the pointer write, when the user happens to
# be in the workspace. A filesystem-wide scan is deliberately NOT done.
for dir in "$PWD/xo-space" "$PWD"; do
    if [ -f "$dir/server.py" ] && [ -f "$dir/requirements.txt" ] && [ -f "$dir/cowork-api.sh" ]; then
        emit installed "" "$dir" "" ""
        exit 0
    fi
done

emit not_installed "" "" "" ""
