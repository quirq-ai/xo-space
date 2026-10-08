#!/bin/sh
# Follow the server log (Ctrl-C to stop following).
. infra/qq-lib.sh
log="$(qq_state)/logs/quirq.log"
[ -f "$log" ] || { echo "qq logs: no log at $log yet; start the server with qq start" >&2; exit 1; }
exec tail -n 50 -f "$log"
