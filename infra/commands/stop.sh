#!/bin/sh
# Stop this checkout's server only, through its localhost-only HTTP route (kills nothing else).
. infra/qq-lib.sh
url=$(qq_url)
curl -fsS -X POST "$url/space/server/stop" && echo || { echo "qq stop: no xo-space server answered at $url" >&2; exit 1; }
