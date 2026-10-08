#!/bin/sh
: Show port, PID and restart mode of the running server
url=$(. infra/qq-lib.sh; qq_url)
body=$(curl -fs "$url/space/server/status") || { echo "qq info: nothing answering at $url" >&2; exit 1; }
echo "$body" | python3 -m json.tool
