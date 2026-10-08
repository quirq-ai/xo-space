#!/bin/sh
: List agent sessions like the Agents, Sessions page, newest 20 by default, or qq sessions 50
url=$(. infra/qq-lib.sh; qq_url)
body=$(curl -fs "$url/xo/sessions.json") || { echo "qq sessions: no answer from $url, is the server running" >&2; exit 1; }
python3 - "$body" "${1:-20}" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
rows = sorted(d.get("sessions") or [], key=lambda s: s.get("started_at") or "", reverse=True)
def tokens(s):
    t = s.get("total_tokens", s.get("tokens"))
    return t if isinstance(t, (int, float)) else 0
def cost(s):
    return "~$%.2f" % s["cost"] if s.get("cost_known", True) and isinstance(s.get("cost"), (int, float)) else "n/a"
print(f"{len(rows)} session(s), all time")
for s in rows[:int(sys.argv[2])]:
    source = s.get("agent") or s.get("source") or "claude_code"
    print(f"  {(s.get('started_at') or '')[:16].replace('T', ' '):<16}  {s.get('project') or '':<18} {source:<12} "
          f"{s.get('model') or '':<10} {tokens(s):>12,} tokens  {cost(s):>8}  {s.get('turns', 0):>4} turns")
PY
