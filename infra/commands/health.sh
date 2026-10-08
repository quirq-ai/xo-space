#!/bin/sh
: Check the server is up and print its health report
url=$(. infra/qq-lib.sh; qq_url)
body=$(curl -fs "$url/health") || { echo "qq health: nothing answering at $url" >&2; exit 1; }
echo "$body" | python3 -m json.tool
