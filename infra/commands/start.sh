#!/bin/sh
: Start xo-space on .qq/venv, the repo pinned Python, in the foreground, or with --background
qq build server || exit 1
venv="$(pwd -P)/.qq/venv"
if [ "${1:-}" != "--background" ]; then
    QUIRQ_VENV_DIR="$venv" exec bash ./install.sh
fi
# In the background: its own session, so closing this terminal leaves it running. The server
# writes its log itself (qq logs); this file only keeps what install.sh says before that.
. infra/qq-lib.sh
QUIRQ_VENV_DIR="$venv" nohup setsid bash ./install.sh </dev/null >.qq/start.log 2>&1 &
i=0
while [ $i -lt 90 ]; do
    if curl -fs "$(qq_url)/health" >/dev/null 2>&1; then
        echo "qq start: running in the background at $(qq_url)/space/ - qq logs to follow it, qq stop to stop it"
        exit 0
    fi
    sleep 1
    i=$((i + 1))
done
echo "qq start: no answer after 90 s, see .qq/start.log and qq logs" >&2
exit 1
