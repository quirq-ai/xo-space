#!/bin/sh
: List recent agent sessions, newest 20 by default, or qq sessions 50
url=$(. infra/qq-lib.sh; qq_url)
body=$(curl -fs "$url/api/sessions?limit=${1:-20}") || { echo "qq sessions: no answer from $url, is the server running" >&2; exit 1; }
python3 - "$body" <<'PY'
import datetime, json, sys
def when(t):
    if isinstance(t, (int, float)):
        t = t / 1000 if t > 1e12 else t
        return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
    return str(t or "")[:16]
rows = json.loads(sys.argv[1])
print(f"{len(rows)} session(s)")
for s in rows:
    title = (s.get("title") or "").replace("\n", " ")
    title = title if len(title) <= 70 else title[:69] + "…"
    print(f"  {when(s.get('time_updated')):<16}  {s.get('agent') or '':<12} {title}")
PY
