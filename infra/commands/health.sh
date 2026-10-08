#!/bin/sh
# Is the server up? Prints its /health report.
curl -fsS "$(. infra/qq-lib.sh; qq_url)/health" | python3 -m json.tool || { echo "qq health: nothing answering at $(. infra/qq-lib.sh; qq_url)" >&2; exit 1; }
