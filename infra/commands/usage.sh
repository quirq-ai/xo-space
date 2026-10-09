#!/bin/sh
: Show token use and cost per day, last 7 days by default, or qq usage 30, or qq usage sync to upload now
if [ "${1:-}" = "sync" ]; then
    . infra/qq-lib.sh
    shift
    qq_ops usage-sync "$@"
fi
url=$(. infra/qq-lib.sh; qq_url)
body=$(curl -fs "$url/api/usage/summary/card?days=${1:-7}") || { echo "qq usage: no usage data from $url, is the server running" >&2; exit 1; }
python3 - "$body" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
print(f"last {d.get('days')} day(s): ${d.get('totalCost', 0):.2f}, {d.get('totalTokens', 0):,} tokens, {d.get('totalMessages', 0)} messages")
for day in d.get("dailyCost", []):
    print(f"  {day.get('date', ''):<10}  ${day.get('cost', 0):>8.2f}  {day.get('tokens', 0):>12,} tokens  {day.get('messages', 0):>5} messages")
PY
