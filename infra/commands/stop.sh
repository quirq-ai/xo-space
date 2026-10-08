#!/bin/sh
: Stop the server of this checkout, nothing else
url=$(. infra/qq-lib.sh; qq_url)
curl -fs -X POST "$url/space/server/stop" >/dev/null || { echo "qq stop: nothing answering at $url" >&2; exit 1; }
echo "qq stop: stopping the server at $url"
