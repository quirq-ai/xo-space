#!/bin/sh
# Port, PID, instance and restart mode of the running server.
. infra/qq-lib.sh
url=$(qq_url)
curl -fsS "$url/space/server/status" | python3 -m json.tool || { echo "qq info: nothing answering at $url" >&2; exit 1; }
