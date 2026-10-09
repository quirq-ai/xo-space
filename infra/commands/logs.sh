#!/bin/sh
: Follow the server log, or qq logs sharing for the project sharing job
state="$(. infra/qq-lib.sh; qq_state)"
case "${1:-}" in
    "") log="$state/logs/quirq.log" ;;
    sharing) log=$(ls -t "$state"/logs/scheduler/sharing-tick-*.log 2>/dev/null | head -n 1)
             [ -n "$log" ] || { echo "qq logs: the sharing tick job has not run yet (it needs the watcher on)" >&2; exit 1; } ;;
    *) echo "usage: qq logs [sharing]" >&2; exit 2 ;;
esac
[ -f "$log" ] || { echo "qq logs: no log at $log yet; start the server with qq start" >&2; exit 1; }
exec tail -n 50 -f "$log"
