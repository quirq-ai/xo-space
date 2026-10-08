#!/bin/sh
: Show port, PID and restart mode of the running server
curl -fsS "$(. infra/qq-lib.sh; qq_url)/space/server/status" | python3 -m json.tool || { echo "qq info: nothing answering at $(. infra/qq-lib.sh; qq_url)" >&2; exit 1; }
