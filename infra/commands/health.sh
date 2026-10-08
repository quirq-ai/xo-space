#!/bin/sh
# Is the server up? Prints its /health report.
. infra/qq-lib.sh
url=$(qq_url)
curl -fsS "$url/health" | python3 -m json.tool || { echo "qq health: nothing answering at $url" >&2; exit 1; }
