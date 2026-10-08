#!/bin/sh
: Show the state health report from the running server
url=$(. infra/qq-lib.sh; qq_url)
body=$(curl -fs --max-time 90 "$url/api/doctor") || { echo "qq doctor: no report from $url, is the server running" >&2; exit 1; }
echo "$body" | python3 -m json.tool
