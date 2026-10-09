#!/bin/sh
: Restart the server of this checkout in the background, also what the Setup tab Restart button runs
. infra/qq-lib.sh
url=$(qq_url)
server_pid() { python3 -c 'import json, sys; print(json.load(sys.stdin).get("pid") or "")'; }
pid=$(curl -fs "$url/space/server/status" 2>/dev/null | server_pid 2>/dev/null)
if [ -n "$pid" ]; then
    # Only ever a server running from this checkout, never whatever else answers on the port.
    if [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" != "$(pwd -P)" ]; then
        echo "qq restart: the server at $url (pid $pid) is not this checkout's, leaving it alone" >&2
        exit 1
    fi
    sleep 1   # a restart from the Setup tab gets its answer before the server goes
    kill -TERM "$pid" 2>/dev/null
    i=0
    while kill -0 "$pid" 2>/dev/null && [ $i -lt 30 ]; do sleep 0.5; i=$((i + 1)); done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
    echo "qq restart: stopped the server (pid $pid)"
else
    echo "qq restart: no server answering at $url, starting one"
fi
exec qq start --background "$@"
